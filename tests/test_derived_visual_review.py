"""Host opt-in derivation, exact transport identity and real codec thread drain."""
import asyncio
import base64
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import random
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


def png(image, **kwargs):
    output=io.BytesIO(); image.save(output,format="PNG",**kwargs); return output.getvalue()


class Provider:
    api_key="SYNTHETIC_DERIVED_HELPER_PRIVATE_KEY"
    def __init__(self, callback=None): self.calls=[]; self.callback=callback
    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.callback is not None: return await self.callback(kwargs)
        return SimpleNamespace(content="Observed the derived page only",finish_reason="stop",tool_calls=[])


class DerivedVisualReviewTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        with Image.frombytes("RGB",(800,600),random.Random(73819).randbytes(800*600*3)) as image:
            cls.large_png=png(image)
        with Image.new("RGB",(20,14),"white") as image:
            cls.small_png=png(image)
            info=PngImagePlugin.PngInfo(); info.add_text("padding","X"*800000)
            cls.solid_large_png=png(image,pnginfo=info)

    def setUp(self):
        self.owned=tempfile.TemporaryDirectory(prefix="derived-visual-")
        self.root=Path(self.owned.name); self.pdf=b"%PDF-1.7 OWNED_DERIVED_HELPER"
        (self.root/"source.pdf").write_bytes(self.pdf)
        self.cache=PagePreviewCache(WorkspaceFiles(self.root)); self.provider=Provider()
        self.select(self.large_png)

    def tearDown(self):
        self.assertFalse(subject._CONVERSION_OWNERS,"A codec owner outlived the completed review")
        self.owned.cleanup()

    def select(self, raw):
        self.original=raw
        self.reference=self.cache.publish("source.pdf",hashlib.sha256(self.pdf).hexdigest(),[raw],pages=1)[0]["reference"]

    async def invoke(self, **kwargs):
        return await subject.review_document_page(self.provider,"owned-vision",self.cache,self.reference,
            "Describe this one page",**kwargs)

    async def test_default_stays_strict_optin_sends_host_jpeg_and_original_png_stays_in_cache(self):
        with self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_REVIEW_INPUT_LIMIT"):
            await self.invoke()
        self.assertEqual([],self.provider.calls)
        result=json.loads(await self.invoke(allow_derived=True))
        url=self.provider.calls[0]["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,")); self.assertLessEqual(len(url),1_000_000)
        actual=base64.b64decode(url.split(",",1)[1],validate=True)
        self.assertEqual(hashlib.sha256(actual).hexdigest(),result["sent_image_sha256"])
        self.assertEqual(hashlib.sha256(self.original).hexdigest(),result["png_sha256"])
        self.assertNotEqual(result["png_sha256"],result["sent_image_sha256"])
        self.assertEqual(self.original,self.cache.image(self.reference)._png)
        self.assertTrue(result["derived"] and result["reference_only"])
        self.assertFalse(result["whole_document_verified"] or result["visual_layout_verified"])
        with Image.open(io.BytesIO(actual)) as decoded:
            self.assertEqual("JPEG",decoded.format)
            self.assertEqual(decoded.size,(result["sent_width"],result["sent_height"]))
        self.assertNotIn("data:image",json.dumps(result))
        self.assertEqual({"source.pdf"},{item.name for item in self.root.iterdir()})

    async def test_optin_small_png_remains_exact_and_host_only_flag_is_strict(self):
        self.select(self.small_png)
        for bad in (1,"true",{},None):
            with self.subTest(bad=bad), self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_REVIEW_INVALID_ARGUMENT"):
                await self.invoke(allow_derived=bad)
        result=json.loads(await self.invoke(allow_derived=True))
        actual=base64.b64decode(self.provider.calls[0]["messages"][1]["content"][1]["image_url"]["url"].split(",",1)[1])
        self.assertEqual(self.small_png,actual)
        self.assertFalse(result["derived"])
        self.assertEqual(result["sent_image_sha256"],result["png_sha256"])

    async def test_complete_actual_sent_jpeg_echo_folded_across_whitespace_is_not_planner_text(self):
        self.select(self.solid_large_png)
        async def echo(call):
            encoded=call["messages"][1]["content"][1]["image_url"]["url"].split(",",1)[1]
            self.assertLess(len(encoded),16000,"Owned low-entropy JPEG must fit the response boundary")
            return SimpleNamespace(content="\n ".join(encoded[index:index+23] for index in range(0,len(encoded),23)),
                finish_reason="stop",tool_calls=[])
        self.provider.callback=echo
        with self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_REVIEW_INVALID_RESPONSE"):
            await self.invoke(allow_derived=True)
        self.assertEqual(1,len(self.provider.calls))

    async def test_original_png_bare_encoding_is_also_rejected_with_host_optin(self):
        self.select(self.small_png)
        async def echo(call):
            return SimpleNamespace(content=base64.b64encode(self.small_png).decode("ascii"),finish_reason="stop",tool_calls=[])
        self.provider.callback=echo
        with self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_REVIEW_INVALID_RESPONSE"):
            await self.invoke(allow_derived=True)
        self.assertEqual(1,len(self.provider.calls))

    async def test_normal_short_observation_does_not_base64_encode_the_large_original_again(self):
        self.select(self.solid_large_png)
        encode=base64.b64encode
        def bounded(raw,*args,**kwargs):
            if raw is self.original: raise AssertionError("Unneeded original PNG base64 allocation")
            return encode(raw,*args,**kwargs)
        with patch.object(subject.base64,"b64encode",bounded):
            result=json.loads(await self.invoke(allow_derived=True))
        self.assertTrue(result["derived"])

    async def test_forged_codec_identity_metadata_or_format_fails_before_any_model_request(self):
        real=codec.derive_document_preview(self.large_png)
        for mutation in ({"original_png_sha256":"0"*64},{"sent_image_sha256":"0"*64},
                         {"derived_width":1},{"content_type":"image/png"},{"attempts":999},
                         {"content_type":{}},{"transform":{}},{"transform":"model says verified"}):
            with self.subTest(mutation=mutation), patch.object(codec,"derive_document_preview",return_value=replace(real,**mutation)):
                with self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_REVIEW_CONVERSION_INVALID"):
                    await self.invoke(allow_derived=True)
        self.assertEqual([],self.provider.calls)

    async def test_source_pdf_and_parent_scope_rechecked_after_conversion_before_http(self):
        derive=codec.derive_document_preview
        def changed(raw,**kwargs):
            result=derive(raw,**kwargs)
            (self.root/"source.pdf").write_bytes(b"%PDF-1.7 CHANGED_DURING_CODEC")
            return result
        with patch.object(codec,"derive_document_preview",changed):
            with self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_PREVIEW_STALE"):
                await self.invoke(allow_derived=True)
        self.assertEqual([],self.provider.calls)
        (self.root/"source.pdf").write_bytes(self.pdf)
        active=[True]
        def revoked(raw,**kwargs):
            result=derive(raw,**kwargs); active[0]=False; return result
        def guard():
            if not active[0]: raise RuntimeError("PRIVATE_HOST_REASON")
        with patch.object(codec,"derive_document_preview",revoked):
            with self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_REVIEW_UNAVAILABLE"):
                await self.invoke(allow_derived=True,host_scope_guard=guard)
        self.assertEqual([],self.provider.calls)

    async def test_repeated_stop_waits_for_real_conversion_even_if_its_async_facade_is_cancelled(self):
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        def held(raw,*,cancel):
            entered.set()
            try:
                if not release.wait(8): raise AssertionError("Owned conversion not released")
                return codec.DerivedDocumentPreview("", "",0,0,0,0,"",False,"",0,b"")
            finally: finished.set()
        wrap=asyncio.wrap_future
        facades=[]
        def expose(future,*args,**kwargs):
            facade=wrap(future,*args,**kwargs)
            if any(owner.future is future for owner in subject._CONVERSION_OWNERS): facades.append(facade)
            return facade
        with patch.object(codec,"derive_document_preview",held), patch.object(subject.asyncio,"wrap_future",expose):
            task=asyncio.create_task(self.invoke(allow_derived=True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,4))
                self.assertTrue(facades)
                facades[0].cancel()
                task.cancel(); await asyncio.sleep(.02); task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done()); self.assertFalse(finished.is_set())
                self.assertTrue(subject._CONVERSION_OWNERS)
            finally: release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(finished.is_set())
        self.assertEqual([],self.provider.calls)

    async def test_request_failure_after_conversion_is_one_attempt_and_never_returns_raw_error(self):
        async def error(_): raise OSError("SYNTHETIC_PRIVATE_PROVIDER_ERROR")
        self.provider.callback=error
        with self.assertRaisesRegex(subject.DocumentVisualReviewError,"DOCUMENT_REVIEW_PROVIDER_FAILED") as caught:
            await self.invoke(allow_derived=True)
        self.assertNotIn("PRIVATE",str(caught.exception))
        self.assertEqual(1,len(self.provider.calls))


if __name__=="__main__":unittest.main()
