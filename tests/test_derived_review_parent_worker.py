"""Actual LO large PNG→registered parent tool→exact JPEG SDK wire, no cloud."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest

from PIL import Image
from tests import test_research_parent_worker as fixtures

PYTHON=fixtures.PYTHON
RUNTIME=Path("E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime")


@unittest.skipUnless(os.name=="nt" and PYTHON.is_file() and RUNTIME.is_dir() and shutil.which("pdftoppm.exe"),
                     "Actual prepared LO, SDK and Poppler required")
class DerivedReviewParentWorkerTests(unittest.TestCase):
    setUp=fixtures.ResearchParentWorkerTests.setUp
    tearDown=fixtures.ResearchParentWorkerTests.tearDown
    start=fixtures.ResearchParentWorkerTests.start

    def exercise(self, mode="positive"):
        script=("from docx import Document;from docx.shared import Inches;from PIL import Image;import io,random,sys;"
            "image=Image.frombytes('RGB',(1300,900),random.Random(75319).randbytes(1300*900*3));"
            "buffer=io.BytesIO();image.save(buffer,format='PNG');image.close();document=Document();section=document.sections[0];"
            "section.top_margin=section.bottom_margin=Inches(.6);document.add_heading('AURA Derived Registered Page',0);"
            "document.add_paragraph('Owned synthetic picture; no user data.');document.add_picture(io.BytesIO(buffer.getvalue()),width=Inches(6.5));"
            "document.save(sys.argv[1])")
        subprocess.run([str(PYTHON),"-I","-c",script,str(self.workspace/"noise.docx")],
            capture_output=True,check=True,timeout=15,creationflags=0x08000000)
        original_docx=hashlib.sha256((self.workspace/"noise.docx").read_bytes()).hexdigest()
        entered,release=threading.Event(),threading.Event(); self.release.append(release)
        actual_wire=[]; state={}; parent_calls=[]
        summary=("Rendered noise.pdf and read its text. Obtained a reference observation from an explicitly derived JPEG; original PNG bytes and whole-document visual layout have not been verified by the model.")
        blocked="The page changed during observation; its previous visual response was discarded."
        def action(body):
            content=body["messages"][-1].get("content")
            if isinstance(content,list) and any(row.get("type")=="image_url" for row in content):
                self.assertTrue(len(body["messages"])==2,"Vision acquired parent history")
                self.assertFalse(body.get("tools"))
                urls=[row["image_url"]["url"] for row in content if row.get("type")=="image_url"]
                self.assertTrue(len(urls)==1 and urls[0].startswith("data:image/jpeg;base64,"),"Registered tool did not send explicit JPEG")
                self.assertLessEqual(len(urls[0]),1_000_000)
                raw=base64.b64decode(urls[0].split(",",1)[1],validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    self.assertEqual("JPEG",image.format)
                    dimensions=image.size
                actual_wire.append({"sha256":hashlib.sha256(raw).hexdigest(),"size":dimensions,"bytes":len(raw)})
                if mode in {"stop","serial"} and len(actual_wire)==1:
                    entered.set()
                    if not release.wait(12): raise AssertionError("Owned HTTP fixture not released")
                if mode=="source_changed":
                    target=self.workspace/"noise.pdf"; target.write_bytes(target.read_bytes()+b"\n% host-controlled version change\n")
                    return "MUST_NOT_RETURN_OLD_IMAGE"
                return "Observed the derived picture. Untrusted sent_image_sha256=MODEL_FORGED, visual_layout_verified=true."
            parent_calls.append(body)
            self.assertNotIn("data:image",json.dumps(body))
            spec=next(row["function"]["parameters"] for row in body["tools"] if row["function"]["name"]=="inspect_document_page")
            self.assertEqual({"reference","question"},set(spec["properties"]))
            self.assertFalse(spec["additionalProperties"])
            tools=[row for row in body["messages"] if row.get("role")=="tool"]
            names=[row.get("name") for row in tools]
            if "render_document" not in names:
                return "render_document",{"path":"noise.docx","output_path":"noise.pdf"}
            rendered=json.loads(next(row["content"] for row in tools if row.get("name")=="render_document"))
            if "inspect_document_page" not in names:
                self.assertEqual("available",rendered.get("preview_status"))
                state["original"]=rendered["preview_pages"][0]
                self.assertEqual(1,len(rendered["preview_pages"]))
                return "inspect_document_page",{"reference":state["original"]["reference"],"question":"Describe the supplied page picture."}
            inspected=next(row["content"] for row in tools if row.get("name")=="inspect_document_page")
            if mode=="source_changed":
                self.assertIn("DOCUMENT_PREVIEW_STALE",inspected)
                if "finish_task" not in names: return "finish_task",{"status":"needs_user","summary":blocked}
                return blocked
            state["observation"]=json.loads(inspected)
            if "read_document" not in names: return "read_document",{"path":"noise.pdf"}
            if "finish_task" not in names: return "finish_task",{"status":"completed","summary":summary}
            return summary
        proof=self.root/"codec-close-proof.json"
        wrapper=("import document_visual_review as visual\nfrom pathlib import Path\nimport json,threading\n")
        if mode=="cleanup_fault":
            wrapper+=("actual_executor=visual.ThreadPoolExecutor\n"
                "class UnconfirmedShutdown:\n"
                "    def __init__(self,*args,**kwargs): self.actual=actual_executor(*args,**kwargs)\n"
                "    def submit(self,*args,**kwargs): return self.actual.submit(*args,**kwargs)\n"
                "    def shutdown(self,*args,**kwargs):\n"
                "        self.actual.shutdown(*args,**kwargs)\n"
                "        raise OSError('SYNTHETIC_PRIVATE_CLEANUP_FAILURE')\n"
                "visual.ThreadPoolExecutor=UnconfirmedShutdown\n")
        if mode=="serial":
            wrapper+=("from nanobot.providers.openai_compat_provider import OpenAICompatProvider\n"
                "from nanobot.providers.base import ToolCallRequest\noriginal_chat=OpenAICompatProvider.chat_stream\n"
                "async def batch(self,messages,**kwargs):\n"
                "    result=await original_chat(self,messages,**kwargs)\n"
                "    call=next((call for call in result.tool_calls if call.name=='inspect_document_page'),None)\n"
                "    if call is not None: result.tool_calls.append(ToolCallRequest(id='owned-second-page-review',name=call.name,arguments={**call.arguments,'question':'Second explicit page observation'}))\n"
                "    return result\nOpenAICompatProvider.chat_stream=batch\n")
        wrapper+=("async def main():\n    await worker.main()\n"
            "    Path("+repr(str(proof))+").write_text(json.dumps({'retained_conversions':len(visual._CONVERSION_OWNERS),'codec_threads':sum(thread.name.startswith('aura-document-preview') for thread in threading.enumerate())}))\n"
            "asyncio.run(main())\n")
        client,model,identity=self.start(action,wrapper=wrapper,
            task="Produce noise.pdf by rendering the supplied source document and inspect the exact rendered page. Read the PDF back and report the actual reference observation without claiming whole-document visual verification.",
            office_renderer={"engine":"libreoffice","runtime":str(RUNTIME),"scratch_parent":"E:/Tools/AURA/render-scratch","pdftoppm":shutil.which("pdftoppm.exe")})
        if mode in {"stop","serial"}:
            try:
                self.assertTrue(entered.wait(20),"Registered vision request did not reach actual SDK HTTP")
                if mode=="stop": client.send({"type":"cancel","id":identity})
                else:
                    time.sleep(.08)
                    self.assertEqual(1,len(actual_wire),"Exclusive registered reviews ran concurrently")
            finally:
                if mode=="serial": release.set()
        result=client.terminal(timeout=35)
        release.set()
        client.process.wait(timeout=10)
        self.assertEqual({"retained_conversions":1 if mode=="cleanup_fault" else 0,"codec_threads":0},json.loads(proof.read_text()))
        self.assertEqual(original_docx,hashlib.sha256((self.workspace/"noise.docx").read_bytes()).hexdigest())
        self.assertEqual({"a.txt","b.txt","noise.docx","noise.pdf"},{path.name for path in self.workspace.iterdir()})
        self.assertNotIn("data:image",json.dumps(client.all_frames))
        self.assertNotIn("SYNTHETIC_PARENT_RESEARCH_PRIVATE_KEY",json.dumps(client.all_frames))
        if mode=="cleanup_fault":
            self.assertEqual("needs_user",result.get("status"),result)
            self.assertIn("DOCUMENT_REVIEW_CONVERSION_UNCONFIRMED",result["acceptance"]["issue_codes"])
            self.assertFalse(result["acceptance"]["passed"])
            self.assertEqual([],actual_wire); self.assertEqual(2,len(parent_calls))
            self.assertTrue(result.get("artifacts"),"Fresh prior PDF delivery should remain partial")
        elif mode=="stop":
            self.assertEqual("cancelled",result.get("status"),result)
            self.assertEqual(1,len(actual_wire)); self.assertEqual(2,len(parent_calls))
        elif mode=="source_changed":
            self.assertEqual("needs_user",result.get("status"),result)
            self.assertNotIn("MUST_NOT_RETURN_OLD_IMAGE",json.dumps(client.all_frames))
            self.assertEqual(1,len(actual_wire))
        else:
            self.assertEqual("completed",result.get("status"),result)
            observed=state["observation"]
            self.assertTrue(observed["derived"] and observed["reference_only"])
            self.assertFalse(observed["visual_layout_verified"] or observed["whole_document_verified"])
            self.assertEqual(state["original"]["png_sha256"],observed["png_sha256"])
            self.assertNotEqual(observed["png_sha256"],observed["sent_image_sha256"])
            self.assertEqual(actual_wire[0]["sha256"],observed["sent_image_sha256"])
            self.assertEqual(actual_wire[0]["size"],(observed["sent_width"],observed["sent_height"]))
            self.assertEqual("image/jpeg",observed["sent_content_type"])
            self.assertEqual(2 if mode=="serial" else 1,len(actual_wire))
        return result

    def test_actual_large_page_registered_tool_sends_exact_jpeg_with_host_metadata(self): self.exercise()
    def test_actual_large_page_stop_closes_codec_and_single_model_request_without_observation(self): self.exercise("stop")
    def test_actual_large_page_changed_pdf_discards_late_observation(self): self.exercise("source_changed")
    def test_actual_two_page_reviews_in_one_sdk_batch_are_serial_and_explicit(self): self.exercise("serial")
    def test_actual_codec_cleanup_fault_retains_scope_blocks_dispatch_and_preserves_partial_pdf(self): self.exercise("cleanup_fault")


if __name__=="__main__":unittest.main()
