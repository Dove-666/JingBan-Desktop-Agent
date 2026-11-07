"""Actual LibreOffice PNG cache→SDK vision wire, localhost synthetic replies only."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
from pathlib import Path
import shutil
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
KEY = "SYNTHETIC_DOCUMENT_VISION_KEY_75319"
REASONING = "SYNTHETIC_REASONING_MUST_NOT_LEAVE_HELPER"


class WireFixture:
    def __init__(self, expected_sha):
        self.expected_sha = expected_sha
        self.mode, self.records, self.errors = "positive", [], []
        self.received, self.release = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                mode, received, release = owner.mode, owner.received, owner.release
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2 * 1024**2: raise ValueError()
                    body = json.loads(self.rfile.read(length))
                    messages = body["messages"]
                    if (self.path != "/v1/chat/completions" or len(messages) != 2
                            or messages[0].get("role") != "system" or messages[1].get("role") != "user"
                            or body.get("tools") or body.get("tool_choice") not in {None, "none"}
                            or any(key in body for key in ("reasoning", "reasoning_effort", "thinking"))):
                        raise ValueError()
                    blocks = messages[1]["content"]
                    images = [block["image_url"]["url"] for block in blocks if block.get("type") == "image_url"]
                    if len(images) != 1 or len(blocks) != 2 or not images[0].startswith("data:image/png;base64,"):
                        raise ValueError()
                    raw = base64.b64decode(images[0].split(",", 1)[1], validate=True)
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest != owner.expected_sha or not raw.startswith(b"\x89PNG\r\n\x1a\n"): raise ValueError()
                    with owner.lock:
                        owner.records.append({"scenario": mode, "single_png": True,
                            "png_sha256": digest, "expected_png_hash_matched": True,
                            "message_count": 2, "tools": False, "reasoning_requested": False})
                    received.set()
                    if mode in {"stale", "cancel"} and not release.wait(8): raise ValueError()
                    status = 503 if mode == "error" else 200
                    result = ({"error": {"message": "SYNTHETIC_PRIVATE_RESPONSE_" + KEY}} if status != 200 else
                        {"id": "owned-document-vision", "object": "chat.completion", "created": 1,
                            "model": "aura-owned-vision", "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "The synthetic page title and table are readable.",
                                    "reasoning_content": REASONING}}]})
                except Exception:
                    with owner.lock: owner.errors.append("FIXED_WIRE_ASSERTION_FAILED")
                    received.set(); status, result = 500, {"error": {"message": "FIXED_WIRE_ASSERTION_FAILED"}}
                payload = json.dumps(result).encode("utf-8")
                try:
                    self.send_response(status); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
                except (ConnectionError, OSError): pass  # Expected after request Stop.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True; self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s/v1" % self.server.server_port

    def scenario(self, name):
        self.mode, self.received, self.release = name, threading.Event(), threading.Event()

    def count(self):
        with self.lock: return len(self.records)

    def close(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)


async def exercise(output):
    from docx import Document
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    from nanobot.providers.model_api import ModelAPICapabilities
    from agent_backend.nanobot.host_office_runtime import HostOfficeRuntime
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    from agent_backend.nanobot.document_visual_review import review_document_page, DocumentVisualReviewError
    source = output / "input.docx"
    document = Document(); document.add_heading("AURA Cached Page QA", 0)
    document.add_paragraph("Actual LibreOffice page; synthetic localhost vision wire only.")
    table = document.add_table(rows=1, cols=2); table.style = "Light Shading Accent 1"
    table.cell(0, 0).text, table.cell(0, 1).text = "Item", "Amount"
    cells = table.add_row().cells; cells[0].text, cells[1].text = "Synthetic budget", "12.50"
    document.save(source)
    original_source = hashlib.sha256(source.read_bytes()).hexdigest()
    poppler = shutil.which("pdftoppm.exe")
    if not poppler: raise RuntimeError("BUNDLED_POPPLER_UNAVAILABLE")
    files = WorkspaceFiles(output)
    runtime = HostOfficeRuntime({"engine": "libreoffice", "runtime": "E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime",
        "scratch_parent": "E:/Tools/AURA/render-scratch", "pdftoppm": poppler}, files)
    rendered = json.loads(await runtime.render_document("input.docx", "output.pdf"))
    if rendered.get("preview_status") != "available" or len(rendered.get("preview_pages", [])) != 1:
        raise RuntimeError("ACTUAL_PAGE_CACHE_UNAVAILABLE")
    metadata = rendered["preview_pages"][0]
    page = runtime.preview(metadata["reference"])
    wire = WireFixture(page.png_sha256)
    provider = OpenAICompatProvider(api_key=KEY, api_base=wire.url, default_model="aura-owned-vision",
        model_api=ModelAPICapabilities(), provider_name="custom")
    original_pdf = (output / "output.pdf").read_bytes()
    checks = {}
    async def review():
        return await review_document_page(provider, "aura-owned-vision", runtime.preview_cache,
            metadata["reference"], "Describe the title/table readability only.", secrets=(KEY,))
    async def received():
        for _ in range(500):
            if wire.received.is_set(): return
            await asyncio.sleep(.01)
        raise RuntimeError("LOCAL_WIRE_NOT_RECEIVED")
    try:
        wire.scenario("positive")
        result = json.loads(await review())
        checks["positive"] = (wire.count() == 1 and result["reference_only"] is True
            and result["visual_layout_verified"] is False and result["whole_document_verified"] is False
            and "data:image" not in json.dumps(result) and REASONING not in json.dumps(result))
        wire.scenario("stale")
        task = asyncio.create_task(review()); await received()
        (output / "output.pdf").write_bytes(original_pdf + b"\n% changed in controlled host fixture")
        wire.release.set()
        try: await task; checks["stale_after_wait"] = False
        except DocumentVisualReviewError as error: checks["stale_after_wait"] = error.code == "DOCUMENT_PREVIEW_STALE"
        (output / "output.pdf").write_bytes(original_pdf)
        wire.scenario("cancel")
        task = asyncio.create_task(review()); await received(); task.cancel(); task.cancel()
        try: await task; checks["cancel"] = False
        except asyncio.CancelledError: checks["cancel"] = True
        finally: wire.release.set()
        wire.scenario("error")
        try: await review(); checks["provider_error"] = False
        except DocumentVisualReviewError as error:
            checks["provider_error"] = error.code == "DOCUMENT_REVIEW_INVALID_RESPONSE" and KEY not in str(error)
        await asyncio.sleep(.05)
        checks["four_explicit_calls_four_requests_no_retry"] = wire.count() == 4
        checks["only_pdf_document_files"] = {path.name for path in output.iterdir()} == {"input.docx", "output.pdf"}
        checks["source_unchanged"] = hashlib.sha256(source.read_bytes()).hexdigest() == original_source
        checks["pdf_restored"] = hashlib.sha256((output / "output.pdf").read_bytes()).hexdigest() == rendered["sha256"]
        return {"schema": "aura.document_visual_review.qa.v1", "passed": all(checks.values()) and not wire.errors,
            "actual_libreoffice": True, "actual_sdk_provider": True, "cloud_model": False,
            "worker_tool_registry_used": False, "visual_quality_verified": False,
            "pdf_pages": rendered["pages"], "png_sha256": page.png_sha256,
            "cache_retained_bytes": runtime.preview_cache.retained_bytes, "wire": wire.records,
            "checks": checks, "wire_errors": wire.errors}
    finally:
        wire.release.set(); await provider.aclose(); wire.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / "artifacts/qa-document-visual-review")
    args = parser.parse_args(); output = args.output.resolve()
    if not output.is_relative_to((PROJECT / "artifacts").resolve()): raise ValueError("QA_OUTPUT_SCOPE_INVALID")
    output.mkdir(parents=True, exist_ok=False)
    logging.disable(logging.CRITICAL)
    from loguru import logger
    logger.remove()  # Owned fixture process only; never change App logging here.
    captured_out, captured_error = io.StringIO(), io.StringIO()
    with redirect_stdout(captured_out), redirect_stderr(captured_error):
        try: result = asyncio.run(exercise(output))
        except BaseException:
            result = {"schema": "aura.document_visual_review.qa.v1", "passed": False,
                "error": "CONTROLLED_DOCUMENT_VISUAL_FIXTURE_FAILED"}
    # Store lengths/booleans only, never captured provider text or image payload.
    result["raw_stdout_bytes"] = len(captured_out.getvalue().encode("utf-8"))
    result["raw_stderr_bytes"] = len(captured_error.getvalue().encode("utf-8"))
    result["passed"] = result["passed"] and result["raw_stdout_bytes"] == 0 and result["raw_stderr_bytes"] == 0
    (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result)); return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
