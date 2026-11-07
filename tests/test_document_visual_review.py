"""Passive preview integration and single-call vision boundaries; no real model."""
import asyncio
import base64
import hashlib
import json
import struct
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zlib
from unittest.mock import patch

from agent_backend.nanobot import libreoffice_render as rendering
from agent_backend.nanobot.document_previews import PagePreviewCache, PreviewError
from agent_backend.nanobot.document_visual_review import review_document_page, DocumentVisualReviewError
from agent_backend.nanobot.safe_files import WorkspaceFiles
from tests.test_document_previews import png
from tests.test_libreoffice_render_adapter import FakeRenderer, package


class PreviewAdapterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-preview-render-boundary-"); self.addCleanup(owned.cleanup)
        self.root = Path(owned.name); self.files = WorkspaceFiles(self.root)
        (self.root / "source.docx").write_bytes(package())
        self.cache = PagePreviewCache(self.files)
        self.renderer = FakeRenderer(lambda result, _: (setattr(result, "previews", (png(),)), result)[1])
        self.adapter = rendering.LibreOfficeRenderAdapter(self.renderer)

    async def invoke(self, path="output.pdf", cache=None):
        return json.loads(await rendering.render_document(self.files, "source.docx", path,
            renderer=self.adapter, preview_cache=self.cache if cache is None else cache))

    async def test_success_publishes_pdf_then_only_random_memory_page_references(self):
        result = await self.invoke()
        self.assertEqual("available", result["preview_status"])
        page = result["preview_pages"][0]
        self.assertEqual(hashlib.sha256(png()).hexdigest(), page["png_sha256"])
        self.assertEqual(result["sha256"], self.cache.image(page["reference"]).pdf_sha256)
        self.assertFalse(result["previews_published"])
        self.assertNotIn("data:image", json.dumps(result))
        self.assertEqual({"source.docx", "output.pdf"}, {path.name for path in self.root.iterdir()})

    async def test_capacity_limit_is_preview_unavailable_not_failed_or_repeated_pdf_render(self):
        tiny = PagePreviewCache(self.files, max_bytes=1)
        result = await self.invoke(cache=tiny)
        self.assertEqual("rendered", result["status"])
        self.assertEqual("unavailable", result["preview_status"])
        self.assertEqual([], result["preview_pages"])
        self.assertTrue((self.root / "output.pdf").exists())
        self.assertEqual(1, self.renderer.calls)
        self.assertFalse(self.adapter.host_status()["cleanup_unknown"])

    async def test_cache_failure_or_malformed_metadata_cannot_corrupt_successful_pdf_result(self):
        for index, failure in enumerate((MemoryError("SYNTHETIC_PRIVATE"), PreviewError("DOCUMENT_PREVIEW_STALE"), None)):
            with self.subTest(index=index), patch.object(self.cache, "publish",
                side_effect=failure, return_value=[{"data_url": "PRIVATE_DATA_IMAGE"}]):
                result = await self.invoke("output-%s.pdf" % index)
                self.assertEqual("rendered", result["status"])
                self.assertEqual("unavailable", result["preview_status"])
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertTrue((self.root / ("output-%s.pdf" % index)).exists())
        self.assertEqual(3, self.renderer.calls)
        broken_cache = object.__new__(PagePreviewCache)
        result = await self.invoke("output-broken-cache.pdf", cache=broken_cache)
        self.assertEqual("rendered", result["status"])
        self.assertEqual("unavailable", result["preview_status"])
        self.assertEqual(4, self.renderer.calls)

    async def test_foreign_workspace_cache_is_never_used(self):
        with tempfile.TemporaryDirectory(prefix="aura-foreign-preview-cache-") as other:
            foreign = PagePreviewCache(WorkspaceFiles(Path(other)))
            with patch.object(foreign, "publish", side_effect=AssertionError("Foreign authority")) as publish:
                result = await self.invoke(cache=foreign)
            self.assertEqual("unavailable", result["preview_status"])
            self.assertEqual(0, publish.call_count)

    async def test_failed_source_fence_or_target_cas_never_mints_preview_references(self):
        def changed(result, _):
            result.previews = (png(),)
            (self.root / "source.docx").write_bytes(package({"changed.xml": "changed"}))
            return result
        self.renderer.callback = changed
        with patch.object(self.cache, "publish") as publish:
            with self.assertRaisesRegex(rendering.OfficeRenderError, "OFFICE_SOURCE_CHANGED"): await self.invoke()
        self.assertEqual(0, publish.call_count)
        self.assertEqual(0, self.cache.retained_bytes)
        self.assertFalse((self.root / "output.pdf").exists())


class _Provider:
    api_key = "SYNTHETIC_VISUAL_KEY"
    def __init__(self, callback=None): self.calls = []; self.callback = callback
    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.callback is not None: return await self.callback(kwargs)
        return SimpleNamespace(content="The heading and table are readable. SYNTHETIC_VISUAL_KEY",
            finish_reason="stop", tool_calls=[], reasoning_content="PRIVATE_REASONING_MUST_NOT_RETURN")


class DocumentVisualReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-document-vision-boundary-"); self.addCleanup(owned.cleanup)
        self.root = Path(owned.name); self.files = WorkspaceFiles(self.root)
        self.pdf = b"%PDF-1.7 CONTROLLED_SNAPSHOT"
        (self.root / "page.pdf").write_bytes(self.pdf)
        self.cache = PagePreviewCache(self.files)
        self.reference = self.cache.publish("page.pdf", hashlib.sha256(self.pdf).hexdigest(), [png()], pages=1)[0]["reference"]
        self.provider = _Provider()

    async def invoke(self, **kwargs):
        return await review_document_page(self.provider, "synthetic-vision", self.cache, self.reference,
            "Describe visible layout issues; key SYNTHETIC_VISUAL_KEY", **kwargs)

    async def test_one_exact_authorized_png_no_tools_history_reasoning_or_goal_verdict(self):
        result = json.loads(await self.invoke())
        self.assertEqual(1, len(self.provider.calls))
        call = self.provider.calls[0]
        self.assertEqual([], call["tools"])
        self.assertEqual("none", call["tool_choice"])
        self.assertEqual(2, len(call["messages"]))
        blocks = call["messages"][1]["content"]
        images = [block["image_url"]["url"] for block in blocks if block["type"] == "image_url"]
        self.assertEqual(1, len(images))
        self.assertEqual(png(), base64.b64decode(images[0].split(",", 1)[1], validate=True))
        self.assertNotIn(self.provider.api_key, blocks[0]["text"])
        self.assertEqual("model_visual_observation", result["scope"])
        self.assertTrue(result["reference_only"])
        self.assertFalse(result["visual_layout_verified"])
        self.assertFalse(result["whole_document_verified"])
        self.assertNotIn("PRIVATE_REASONING", json.dumps(result))
        self.assertNotIn(self.provider.api_key, json.dumps(result))
        self.assertNotIn("data:image", json.dumps(result))

    async def test_stale_pdf_before_call_and_foreign_reference_send_nothing(self):
        (self.root / "page.pdf").write_bytes(b"%PDF-1.7 CHANGED")
        with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_PREVIEW_STALE"): await self.invoke()
        (self.root / "page.pdf").write_bytes(self.pdf)
        with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_PREVIEW_UNAVAILABLE"):
            await review_document_page(self.provider, "synthetic-vision", self.cache, "page_" + "0" * 32, "Describe")
        self.assertEqual([], self.provider.calls)

    async def test_large_valid_png_is_not_silently_reencoded_by_the_sdk(self):
        raw = png(); kind, payload = b"tEXt", b"padding\0" + b"x" * 800000
        ancillary = struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
        padded = raw[:-12] + ancillary + raw[-12:]
        self.reference = self.cache.publish("page.pdf", hashlib.sha256(self.pdf).hexdigest(), [padded], pages=1)[0]["reference"]
        with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_REVIEW_INPUT_LIMIT"): await self.invoke()
        self.assertEqual([], self.provider.calls)

    async def test_pdf_changes_during_model_wait_discards_the_observation(self):
        async def changed(_):
            (self.root / "page.pdf").write_bytes(b"%PDF-1.7 LATER")
            return SimpleNamespace(content="MUST_NOT_RETURN", finish_reason="stop", tool_calls=[])
        self.provider.callback = changed
        with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_PREVIEW_STALE") as caught: await self.invoke()
        self.assertNotIn("MUST_NOT_RETURN", str(caught.exception))
        self.assertEqual(1, len(self.provider.calls))

    async def test_eviction_while_waiting_discards_the_observation(self):
        async def evicted(_):
            self.cache.clear()
            return SimpleNamespace(content="MUST_NOT_RETURN", finish_reason="stop", tool_calls=[])
        self.provider.callback = evicted
        with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_PREVIEW_UNAVAILABLE"): await self.invoke()
        self.assertEqual(1, len(self.provider.calls))

    async def test_revoked_external_grant_during_wait_discards_observation(self):
        with tempfile.TemporaryDirectory(prefix="aura-granted-page-") as other:
            path = Path(other) / "external.pdf"; path.write_bytes(self.pdf)
            self.files.add_grant({"path": str(path), "access": "read", "directory": False})
            self.reference = self.cache.publish(str(path), hashlib.sha256(self.pdf).hexdigest(), [png()], pages=1)[0]["reference"]
            async def revoked(_):
                self.files._read_roots.clear()
                return SimpleNamespace(content="MUST_NOT_RETURN", finish_reason="stop", tool_calls=[])
            self.provider.callback = revoked
            with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_PREVIEW_UNAVAILABLE"): await self.invoke()

    async def test_provider_failure_is_fixed_and_never_retried_or_logged_as_raw_error(self):
        async def failed(_): raise OSError("SYNTHETIC_PRIVATE_URL_AND_KEY")
        self.provider.callback = failed
        with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_REVIEW_PROVIDER_FAILED") as caught: await self.invoke()
        self.assertEqual("DOCUMENT_REVIEW_PROVIDER_FAILED", str(caught.exception))
        self.assertEqual(1, len(self.provider.calls))

    async def test_filtered_truncated_tool_or_image_echo_response_is_not_an_observation(self):
        for finish, text, tools in (("length", "partial", []), ("error", "PRIVATE", []),
            ("content_filter", "", []), ("stop", "data:image/png;base64,PRIVATE", []), ("stop", "ok", [object()])):
            async def response(_): return SimpleNamespace(content=text, finish_reason=finish, tool_calls=tools)
            self.provider.callback = response
            with self.subTest(finish=finish), self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_REVIEW_INVALID_RESPONSE"):
                await self.invoke()
        self.assertEqual(5, len(self.provider.calls))

    async def test_timeout_cancels_the_same_request_without_retry(self):
        closed = asyncio.Event()
        async def held(_):
            try: await asyncio.Event().wait()
            finally: closed.set()
        self.provider.callback = held
        with self.assertRaisesRegex(DocumentVisualReviewError, "DOCUMENT_REVIEW_TIMEOUT"): await self.invoke(timeout_seconds=1)
        self.assertTrue(closed.is_set())
        self.assertEqual(1, len(self.provider.calls))

    async def test_repeated_stop_waits_for_same_provider_cleanup_and_returns_no_observation(self):
        entered, closing, release, closed = (asyncio.Event() for _ in range(4))
        async def held(_):
            entered.set()
            try: await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()
                closed.set()
        self.provider.callback = held
        task = asyncio.create_task(self.invoke())
        await entered.wait(); task.cancel(); await closing.wait(); task.cancel(); task.cancel()
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(closed.is_set())
        self.assertEqual(1, len(self.provider.calls))


if __name__ == "__main__": unittest.main()
