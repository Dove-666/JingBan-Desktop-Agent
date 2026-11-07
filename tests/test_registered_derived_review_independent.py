"""Independent registered-preview lifecycle review, synthetic inputs only.

The provider is a local in-memory stub. Real ThreadPoolExecutor futures are
used for cancellation and submission/shutdown failure ownership tests.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from agent_backend.nanobot import derived_document_preview as codec
from agent_backend.nanobot import document_visual_review as subject
from agent_backend.nanobot.document_previews import PagePreviewCache
from agent_backend.nanobot.safe_files import WorkspaceFiles


class Provider:
    api_key = "SYNTHETIC_INDEPENDENT_PREVIEW_KEY"

    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.callback is not None: return await self.callback(kwargs)
        return SimpleNamespace(content="Observed a single authorized page.", finish_reason="stop", tool_calls=[])


class RegisteredDerivedIndependentReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="registered-derived-independent-")
        self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name)
        self.pdf = b"%PDF-1.7 OWNED_INDEPENDENT_PAGE"
        (self.root/"page.pdf").write_bytes(self.pdf)
        self.files = WorkspaceFiles(self.root)
        self.cache = PagePreviewCache(self.files)
        with Image.new("RGBA", (32, 24), (24, 96, 192, 128)) as image:
            stream = io.BytesIO(); image.save(stream, format="PNG"); self.png = stream.getvalue()
            metadata = PngImagePlugin.PngInfo(); metadata.add_text("padding", "A"*800000)
            stream = io.BytesIO(); image.save(stream, format="PNG", pnginfo=metadata); self.large_png = stream.getvalue()
        self.select(self.png)
        self.provider = Provider()
        # Each fault fixture owns its own registry. No production unknown owner
        # can be removed/forgiven by these tests.
        self.registry = set()
        self.patchers = [patch.object(subject, "_CONVERSION_OWNERS", self.registry)]
        for patcher in self.patchers:
            patcher.start(); self.addCleanup(patcher.stop)

    def select(self, raw):
        self.reference = self.cache.publish("page.pdf", hashlib.sha256(self.pdf).hexdigest(), [raw], pages=1)[0]["reference"]

    async def invoke(self, **kwargs):
        return await subject.review_document_page(self.provider, "synthetic-vision", self.cache,
            self.reference, "Observe only the supplied page.", allow_derived=True, **kwargs)

    async def test_pdf_change_during_final_wire_preparation_prevents_any_provider_request(self):
        prepare = subject._wire_image
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            (self.root/"page.pdf").write_bytes(b"%PDF-1.7 CHANGED_BEFORE_SEND")
            return result
        with patch.object(subject, "_wire_image", changed):
            with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_PREVIEW_STALE"):
                await self.invoke()
        self.assertEqual(0, len(self.provider.calls), "A stale page was sent before the final freshness check")

    async def test_scope_revoked_during_final_wire_preparation_prevents_any_provider_request(self):
        prepare = subject._wire_image
        active = [True]
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs); active[0] = False; return result
        def guard():
            if not active[0]: raise RuntimeError("PRIVATE_REVOKED_SCOPE")
        with patch.object(subject, "_wire_image", changed):
            with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_UNAVAILABLE"):
                await self.invoke(host_scope_guard=guard)
        self.assertEqual(0, len(self.provider.calls), "Revoked authority reached a new provider request")

    async def test_scope_revoked_after_request_task_creation_is_checked_inside_dispatch(self):
        active = [True]
        schedule = asyncio.create_task
        def scheduled_then_revoked(coroutine, *args, **kwargs):
            result = schedule(coroutine, *args, **kwargs)
            if coroutine.cr_code.co_name == "_request": active[0] = False
            return result
        def guard():
            if not active[0]: raise RuntimeError("PRIVATE_REVOKED_AT_DISPATCH")
        with patch.object(subject.asyncio, "create_task", scheduled_then_revoked):
            with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_UNAVAILABLE"):
                await self.invoke(host_scope_guard=guard)
        self.assertEqual(0, len(self.provider.calls), "Pre-task checks cannot authorize later coroutine dispatch")

    async def test_completed_dispatch_guard_error_does_not_replace_concurrent_stop(self):
        active = [True]
        schedule = asyncio.create_task
        def scheduled_then_revoked(coroutine, *args, **kwargs):
            parent = asyncio.current_task()
            result = schedule(coroutine, *args, **kwargs)
            if coroutine.cr_code.co_name == "_request":
                active[0] = False
                # Registered before shield's completion callback: model the
                # user's Stop arriving as the no-send guard failure completes.
                result.add_done_callback(lambda _: parent.cancel())
            return result
        def guard():
            if not active[0]: raise RuntimeError("PRIVATE_REVOKED_AND_STOPPED")
        with patch.object(subject.asyncio, "create_task", scheduled_then_revoked):
            task = asyncio.create_task(self.invoke(host_scope_guard=guard))
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(0, len(self.provider.calls))
        self.assertFalse(self.registry)

    async def test_repeated_stop_drains_exact_real_future_then_new_explicit_call_is_allowed(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        observed_stops = []
        actual = codec.derive_document_preview
        def held(raw, *, cancel):
            entered.set()
            try:
                if not release.wait(5): raise AssertionError("Controlled codec was not released")
                observed_stops.append(cancel.is_set())
                return actual(raw, cancel=cancel)
            finally: done.set()
        with patch.object(codec, "derive_document_preview", held):
            task = asyncio.create_task(self.invoke())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                owner, = self.registry
                exact_future = owner.future
                self.assertFalse(exact_future.done())
                task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
                self.assertFalse(task.done())
                self.assertFalse(done.is_set())
                self.assertIs(owner.future, exact_future)
                self.assertTrue(owner.stop.is_set())
            finally: release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(done.is_set())
        self.assertEqual([True], observed_stops)
        self.assertFalse(self.registry)
        self.assertEqual(0, len(self.provider.calls))
        result = json.loads(await self.invoke())
        self.assertFalse(result["derived"])
        self.assertEqual(1, len(self.provider.calls), "Stop must not automatically replay the interrupted call")

    async def test_shutdown_error_after_actual_thread_join_retains_owner_and_blocks_other_calls(self):
        created = []
        class FailedShutdown(ThreadPoolExecutor):
            def __init__(self, **kwargs): super().__init__(**kwargs); created.append(self)
            def shutdown(self, *args, **kwargs):
                super().shutdown(*args, **kwargs)
                raise OSError("PRIVATE_CLOSE_ERROR")
        with patch.object(subject, "ThreadPoolExecutor", FailedShutdown):
            with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_CONVERSION_UNCONFIRMED") as caught:
                await self.invoke()
        self.assertNotIn("PRIVATE", str(caught.exception))
        owner, = self.registry
        self.assertTrue(owner.unknown)
        self.assertTrue(owner.future.done())
        self.assertIs(created[0], owner.executor)
        self.assertFalse(any(thread.is_alive() for thread in created[0]._threads))
        with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_CONVERSION_UNCONFIRMED"):
            await subject.review_document_page(self.provider, "synthetic-vision", self.cache,
                self.reference, "New call cannot bypass unknown ownership.")
        self.assertEqual(0, len(self.provider.calls))

    async def test_repeated_stop_during_provider_cleanup_drains_single_request_without_replay(self):
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
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel(); await asyncio.wait_for(closing.wait(), 3)
            task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
            self.assertFalse(task.done())
            self.assertFalse(closed.is_set())
        finally: release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(closed.is_set())
        self.assertFalse(self.registry)
        self.assertEqual(1, len(self.provider.calls))

    async def test_submit_error_after_real_thread_started_retains_exact_executor_without_sending(self):
        created = []
        class FailedSubmit(ThreadPoolExecutor):
            def __init__(self, **kwargs): super().__init__(**kwargs); self.submitted = None; created.append(self)
            def submit(self, *args, **kwargs):
                self.submitted = super().submit(*args, **kwargs)
                raise OSError("PRIVATE_SUBMIT_AFTER_START_ERROR")
        with patch.object(subject, "ThreadPoolExecutor", FailedSubmit):
            with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_CONVERSION_UNCONFIRMED") as caught:
                await self.invoke()
        owner, = self.registry
        self.assertTrue(owner.unknown)
        self.assertIsNone(owner.future, "No returned Future is not proof that submit started no thread")
        self.assertIs(created[0], owner.executor)
        self.assertTrue(created[0].submitted.done())
        self.assertFalse(any(thread.is_alive() for thread in created[0]._threads))
        self.assertEqual(0, len(self.provider.calls))
        self.assertNotIn("PRIVATE", str(caught.exception))
        with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_CONVERSION_UNCONFIRMED"):
            subject.assert_visual_runtime_ready()

    async def test_derived_transport_metadata_is_host_computed_and_model_echo_is_only_text(self):
        self.select(self.large_png)
        async def forged(_):
            return SimpleNamespace(content='{"sent_image_sha256":"FORGED","derived":false,"visual_layout_verified":true}',
                finish_reason="stop", tool_calls=[], reasoning_content="PRIVATE_REASONING")
        self.provider.callback = forged
        result = json.loads(await self.invoke())
        wire = self.provider.calls[0]["messages"][1]["content"][1]["image_url"]["url"]
        sent = base64.b64decode(wire.split(",", 1)[1], validate=True)
        self.assertTrue(wire.startswith("data:image/jpeg;base64,"))
        self.assertEqual(hashlib.sha256(sent).hexdigest(), result["sent_image_sha256"])
        self.assertEqual(hashlib.sha256(self.large_png).hexdigest(), result["png_sha256"])
        self.assertTrue(result["derived"] and result["reference_only"])
        self.assertFalse(result["visual_layout_verified"] or result["whole_document_verified"])
        self.assertEqual({"page.pdf"}, {entry.name for entry in self.root.iterdir()})
        self.assertNotIn("PRIVATE_REASONING", json.dumps(result))

    async def test_complete_actual_jpeg_echo_with_inserted_whitespace_never_becomes_observation(self):
        self.select(self.large_png)
        async def echo(call):
            wire = call["messages"][1]["content"][1]["image_url"]["url"]
            encoded = wire.split(",", 1)[1]
            self.assertLess(len(encoded)*2, 16000)
            return SimpleNamespace(content=" \n".join(encoded), finish_reason="stop", tool_calls=[])
        self.provider.callback = echo
        with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_INVALID_RESPONSE"):
            await self.invoke()
        self.assertEqual(1, len(self.provider.calls))

    async def test_original_png_echo_is_rejected_even_when_transport_metadata_looks_valid(self):
        async def echo(_):
            return SimpleNamespace(content=base64.b64encode(self.png).decode("ascii"), finish_reason="stop", tool_calls=[])
        self.provider.callback = echo
        with self.assertRaisesRegex(subject.DocumentVisualReviewError, "DOCUMENT_REVIEW_INVALID_RESPONSE"):
            await self.invoke()
        self.assertEqual(1, len(self.provider.calls))


if __name__ == "__main__": unittest.main()
