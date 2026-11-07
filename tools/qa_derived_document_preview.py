"""Actual LO large page→explicit JPEG→SDK wire prototype; localhost only."""
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
import random
import shutil
import sys
import threading

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
KEY = "SYNTHETIC_DERIVED_VISION_KEY_75319"


class Wire:
    def __init__(self, expected):
        self.expected, self.records, self.errors = expected, [], []
        self.mode, self.received, self.release = "positive", threading.Event(), threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                mode, received, release = owner.mode, owner.received, owner.release
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2 * 1024**2: raise ValueError()
                    body = json.loads(self.rfile.read(size)); messages = body["messages"]
                    if len(messages) != 2 or messages[0]["role"] != "system" or messages[1]["role"] != "user" or body.get("tools"):
                        raise ValueError()
                    blocks = messages[1]["content"]
                    urls = [block["image_url"]["url"] for block in blocks if block.get("type") == "image_url"]
                    if len(urls) != 1 or len(blocks) != 2 or not urls[0].startswith("data:image/jpeg;base64,"):
                        raise ValueError()
                    raw = base64.b64decode(urls[0].split(",", 1)[1], validate=True)
                    if hashlib.sha256(raw).hexdigest() != owner.expected.sent_image_sha256: raise ValueError()
                    from PIL import Image
                    with Image.open(io.BytesIO(raw)) as decoded:
                        if decoded.format != "JPEG" or decoded.size != (owner.expected.derived_width, owner.expected.derived_height):
                            raise ValueError()
                    owner.records.append({"scenario": mode, "single_jpeg": True, "exact_sent_sha": True,
                        "sdk_reencoded": False, "url_characters": len(urls[0])})
                    received.set()
                    if mode == "cancel" and not release.wait(8): raise ValueError()
                    status = 200
                    result = {"id": "owned-derived-wire", "object": "chat.completion", "created": 1,
                        "model": "aura-derived-loopback", "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "Synthetic page observation only; this is a derived image.",
                                "reasoning_content": "DO_NOT_RETURN_PRIVATE_REASONING"}}]}
                except Exception:
                    owner.errors.append("FIXED_DERIVED_WIRE_MISMATCH"); received.set()
                    status, result = 500, {"error": {"message": "FIXED_DERIVED_WIRE_MISMATCH"}}
                payload = json.dumps(result).encode("utf-8")
                try:
                    self.send_response(status); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
                except (ConnectionError, OSError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True; self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start(); self.url = "http://127.0.0.1:%s/v1" % self.server.server_port

    def close(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)


async def exercise(output):
    from PIL import Image
    from docx import Document
    from docx.shared import Inches
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    from nanobot.providers.model_api import ModelAPICapabilities
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    from agent_backend.nanobot.host_office_runtime import HostOfficeRuntime
    from agent_backend.nanobot.derived_document_preview import derive_document_preview, DerivedPreviewError
    source = output / "noise-page.docx"
    with Image.frombytes("RGB", (1300, 900), random.Random(75319).randbytes(1300 * 900 * 3)) as image:
        raw = io.BytesIO(); image.save(raw, format="PNG")
    document = Document(); section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(.6)
    document.add_heading("AURA Explicit Derived Preview", 0)
    document.add_paragraph("Controlled high-entropy picture; no production data or cloud model.")
    document.add_picture(io.BytesIO(raw.getvalue()), width=Inches(6.5)); document.save(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    poppler = shutil.which("pdftoppm.exe")
    if not poppler: raise RuntimeError("POPPLER_UNAVAILABLE")
    runtime = HostOfficeRuntime({"engine": "libreoffice", "runtime": "E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime",
        "scratch_parent": "E:/Tools/AURA/render-scratch", "pdftoppm": poppler}, WorkspaceFiles(output))
    render = json.loads(await runtime.render_document("noise-page.docx", "noise-page.pdf"))
    page = next((runtime.preview(item["reference"]) for item in render["preview_pages"]
        if len(runtime.preview(item["reference"]).data_url) > 1_000_000), None)
    if page is None: raise RuntimeError("CONTROLLED_LARGE_PAGE_NOT_OBSERVED")
    original = page._png; original_sha = hashlib.sha256(original).hexdigest()
    sent = derive_document_preview(original)
    if not sent.derived or sent.content_type != "image/jpeg" or len(sent.data_url) > 1_000_000:
        raise RuntimeError("DERIVED_PAGE_NOT_BOUNDED_JPEG")
    stop = threading.Event(); stop.set(); pre_cancelled = False
    try: derive_document_preview(original, cancel=stop)
    except DerivedPreviewError as error: pre_cancelled = error.code == "DOCUMENT_DERIVED_PREVIEW_CANCELLED"
    wire = Wire(sent)
    provider = OpenAICompatProvider(api_key=KEY, api_base=wire.url, default_model="aura-derived-loopback",
        model_api=ModelAPICapabilities(), provider_name="custom")
    async def observation():
        before = runtime.preview(page.reference)
        response = await provider.chat(messages=[{"role": "system", "content":
            "Describe only this explicitly derived page image as reference data; no tools, reasoning or task completion verdict."},
            {"role": "user", "content": [{"type": "text", "text": "Check the visible page only. The JPEG is a host-derived preview."},
                {"type": "image_url", "image_url": {"url": sent.data_url}}]}], model="aura-derived-loopback",
            tools=[], tool_choice="none", max_tokens=512, temperature=0)
        after = runtime.preview(page.reference)
        if before.pdf_sha256 != after.pdf_sha256 or before.png_sha256 != after.png_sha256 or response.finish_reason != "stop":
            raise RuntimeError("PAGE_CHANGED_OR_PROVIDER_FAILED")
        # Prototype-only projection, not the frozen production review helper.
        return {"scope": "model_visual_observation", "reference_only": True,
            "visual_layout_verified": False, "sent_image_sha256": sent.sent_image_sha256}
    try:
        positive = await observation()
        wire.mode, wire.received, wire.release = "cancel", threading.Event(), threading.Event()
        task = asyncio.create_task(observation())
        for _ in range(500):
            if wire.received.is_set(): break
            await asyncio.sleep(.01)
        if not wire.received.is_set(): raise RuntimeError("LOCAL_REQUEST_NOT_OBSERVED")
        task.cancel(); task.cancel(); cancelled = False
        try: await task
        except asyncio.CancelledError: cancelled = True
        finally: wire.release.set()
        await asyncio.sleep(.05)
        checks = {"original_png_unchanged": hashlib.sha256(original).hexdigest() == original_sha
            and runtime.preview(page.reference).png_sha256 == original_sha,
            "source_docx_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == source_sha,
            "source_pdf_unchanged": hashlib.sha256((output / "noise-page.pdf").read_bytes()).hexdigest() == render["sha256"],
            "pre_conversion_stop_no_request": pre_cancelled,
            "network_stop_no_observation": cancelled,
            "two_explicit_calls_two_requests": len(wire.records) == 2,
            "only_source_and_pdf_files": {path.name for path in output.iterdir()} == {"noise-page.docx", "noise-page.pdf"},
            "reference_only_visual_false": positive["reference_only"] is True and positive["visual_layout_verified"] is False}
        return {"schema": "aura.derived_document_preview.qa.v1", "passed": all(checks.values()) and not wire.errors,
            "actual_libreoffice": True, "actual_sdk_provider": True, "cloud_model": False,
            "prototype_direct_wire": True, "production_helper_modified": False, "registered_tool_used": False,
            "original_png_bytes": len(original), "metadata": sent.metadata(), "wire": wire.records,
            "checks": checks, "errors": wire.errors, "visual_quality_verified": False}
    finally:
        wire.release.set(); await provider.aclose(); wire.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / "artifacts/qa-derived-document-preview")
    args = parser.parse_args(); output = args.output.resolve()
    if not output.is_relative_to((PROJECT / "artifacts").resolve()): raise ValueError("QA_OUTPUT_SCOPE_INVALID")
    output.mkdir(parents=True, exist_ok=False)
    logging.disable(logging.CRITICAL)
    from loguru import logger
    logger.remove()
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try: result = asyncio.run(exercise(output))
        except BaseException: result = {"schema": "aura.derived_document_preview.qa.v1", "passed": False, "error": "CONTROLLED_DERIVED_PREVIEW_FIXTURE_FAILED"}
    result["raw_stdout_bytes"], result["raw_stderr_bytes"] = len(stdout.getvalue()), len(stderr.getvalue())
    result["passed"] = result["passed"] and not result["raw_stdout_bytes"] and not result["raw_stderr_bytes"]
    (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result)); return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
