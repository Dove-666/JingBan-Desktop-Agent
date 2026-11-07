"""Natural-task oracle counterexamples; no credentials, network or model."""
from contextlib import redirect_stdout
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from tools import qa_user_tasks as qa


def workbook(*, formula="SUM(B2:B4)", cache="29.75", string_amount=False, extra=False):
    namespace="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet=ET.Element("worksheet", xmlns=namespace);data=ET.SubElement(sheet,"sheetData")
    rows=[("项目","金额"),("车费","17.30"),("饮料","2.45"),("材料","10.00"),("合计",cache)]
    if extra:rows.append(("不相关项目","1.00"))
    for index,(label,value) in enumerate(rows,1):
        row=ET.SubElement(data,"row",r=str(index))
        cell=ET.SubElement(row,"c",r=f"A{index}",t="inlineStr");ET.SubElement(ET.SubElement(cell,"is"),"t").text=label
        cell=ET.SubElement(row,"c",r=f"B{index}",t="inlineStr" if index==1 or string_amount and index<5 else "n")
        if cell.get("t")=="inlineStr":ET.SubElement(ET.SubElement(cell,"is"),"t").text=value
        else:ET.SubElement(cell,"v").text=value
        if index==5 and formula is not None:ET.SubElement(cell,"f").text=formula
    book=f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="费用" sheetId="1" r:id="rId1"/></sheets></workbook>'
    relations='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml",book);archive.writestr("xl/_rels/workbook.xml.rels",relations)
        archive.writestr("xl/worksheets/sheet1.xml",ET.tostring(sheet))
    return output.getvalue()


class NaturalUserTaskTests(unittest.TestCase):
    def setUp(self):
        self.owner=tempfile.TemporaryDirectory(prefix="natural-user-task-");self.addCleanup(self.owner.cleanup)
        self.root=Path(self.owner.name)

    def case(self,name): return qa.build_case(name,self.root)[1]

    def test_default_cli_is_offline_and_does_not_construct_task_or_read_configuration(self):
        with patch.object(qa,"build_case",side_effect=AssertionError),patch.object(qa,"run_case",side_effect=AssertionError),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0,qa.main([]))
        report=json.loads(output.getvalue());self.assertEqual(0,report["real_api_calls"])
        self.assertTrue(all(row["estimated_calls"]<=8 for row in report["cases"]))

    def test_runner_requires_explicit_loopback_budget_before_creating_any_workspace(self):
        configured=SimpleNamespace(api=SimpleNamespace(base_url="https://provider.invalid/v1"))
        with patch.object(qa.tempfile,"TemporaryDirectory",side_effect=AssertionError):
            with self.assertRaisesRegex(ValueError,"EXPLICIT_BUDGET"):qa.run_case("edit_two_files",configured)
            with self.assertRaisesRegex(ValueError,"LOOPBACK_BUDGET"):qa.run_case("edit_two_files",configured,budgeted=True)

    def test_fixtures_use_natural_prompts_without_tool_names_and_cannot_overwrite_an_existing_workspace(self):
        forbidden=("read_file","write_file","edit_file","create_document","render_document","inspect_document_page","delegate_research","finish_task")
        for case_id in qa.CASES:
            folder=self.root/case_id;folder.mkdir();prompt,expected=qa.build_case(case_id,folder)
            self.assertTrue(all(name not in prompt for name in forbidden))
            self.assertEqual(set(expected["input_hashes"]),{p.name for p in folder.iterdir()})
            if not expected["input_hashes"]:
                (folder/"already-occupied.txt").write_text("EXISTING",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"NOT_EMPTY"):qa.build_case(case_id,folder)

    def test_existing_report_is_rejected_before_workspace_or_model_work(self):
        configured=SimpleNamespace(api=SimpleNamespace(base_url="http://127.0.0.1:12345/v1"))
        existing=self.root/"artifacts/existing-report.json"
        existing.parent.mkdir();existing.write_text("ORIGINAL",encoding="utf-8")
        with patch.object(qa,"ROOT",self.root),patch.object(qa.tempfile,"TemporaryDirectory",side_effect=AssertionError):
            with self.assertRaisesRegex(ValueError,"NEW_ARTIFACT_REPORT_REQUIRED"):
                qa.run_case("long_notes",configured,report_path=existing,budgeted=True)
        self.assertEqual("ORIGINAL",existing.read_text(encoding="utf-8"))

    def test_multi_edit_requires_both_changes_and_preserves_every_other_byte(self):
        expected=self.case("edit_two_files")
        self.assertFalse(qa.evaluate("edit_two_files",self.root,expected)["passed"])
        for name,raw in expected["final_bytes"].items():(self.root/name).write_bytes(raw)
        self.assertTrue(qa.evaluate("edit_two_files",self.root,expected)["passed"])
        path=self.root/"安排.txt";path.write_bytes(path.read_bytes().replace(b"\r\n",b"\n"))
        self.assertFalse(qa.evaluate("edit_two_files",self.root,expected)["passed"])

    def test_long_notes_include_a_late_fact_and_reject_old_ambiguous_or_overlong_answers(self):
        expected=self.case("long_notes");raw=(self.root/"收尾记录.txt").read_bytes()
        self.assertGreater(raw.index("最终确认".encode()),16000)
        answer="周日16:20，在南门图书馆服务台取件。"
        self.assertTrue(qa.evaluate("long_notes",self.root,expected,answer)["passed"])
        for wrong in ("周六14:30在东门咖啡馆取件。",answer+"之前说的是周六。",answer+"补充"*90):
            self.assertFalse(qa.evaluate("long_notes",self.root,expected,wrong)["passed"])
        (self.root/"收尾记录.txt").unlink()
        self.assertFalse(qa.evaluate("long_notes",self.root,expected,answer)["passed"])

    def test_excel_requires_actual_numeric_expenses_and_editable_formula_with_correct_cache(self):
        expected=self.case("editable_budget");path=self.root/"费用汇总.xlsx"
        for formula in ("SUM(B2:B4)","B2+B3+B4"):
            path.write_bytes(workbook(formula=formula));self.assertTrue(qa.evaluate("editable_budget",self.root,expected)["passed"])
        for raw in (workbook(formula=None),workbook(formula="SUM(B2:B3)"),workbook(cache="0"),workbook(string_amount=True),workbook(extra=True),b"not a workbook"):
            path.write_bytes(raw);self.assertFalse(qa.evaluate("editable_budget",self.root,expected)["passed"])

    def test_long_notes_accepts_explicitly_obsolete_parenthetical_but_not_contradictions(self):
        expected=self.case("long_notes")
        correct="周日 16:20，在南门图书馆服务台取件"
        recovered='最后确认的取件安排是：'+correct+'（文件开头的"周六 14:30，东门咖啡馆"已注明作废；文件未做任何修改）。'
        for answer in (recovered, correct+'（周六东门咖啡馆的安排已作废）。'):
            with self.subTest(answer=answer):
                self.assertTrue(qa.evaluate("long_notes",self.root,expected,answer)["passed"])
        for answer in (correct+'，也可能周六东门咖啡馆。',
                correct+'（周六东门咖啡馆的安排未作废）。',
                correct+'（周日16:20的安排已作废，改为周六东门咖啡馆）。',
                correct+'（周六的安排已作废），但不是周日。',
                '（周日16:20南门图书馆服务台已作废）周六东门咖啡馆。',
                correct+'（周六东门咖啡馆没有作废，“已作废”的说法不正确）',
                correct+'（周六方案已作废，真正最终是周一10:00北门）',
                correct+'。但这个安排已经取消。'):
            with self.subTest(answer=answer):
                self.assertFalse(qa.evaluate("long_notes",self.root,expected,answer)["passed"])

    def test_spreadsheet_source_changes_or_unrequested_side_files_do_not_pass(self):
        expected=self.case("editable_budget");(self.root/"费用汇总.xlsx").write_bytes(workbook())
        original=(self.root/"费用.csv").read_bytes();(self.root/"费用.csv").write_bytes(original+"多余,999.00\n".encode())
        self.assertFalse(qa.evaluate("editable_budget",self.root,expected)["passed"])
        (self.root/"费用.csv").write_bytes(original);(self.root/"secret.txt").write_bytes(b"EXTRA")
        self.assertFalse(qa.evaluate("editable_budget",self.root,expected)["passed"])

    def test_document_content_checks_require_a_pdf_reader_and_keep_visual_quality_pending(self):
        expected=self.case("activity_notice")
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w") as archive:
            xml=ET.Element("document",xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main")
            ET.SubElement(ET.SubElement(ET.SubElement(xml,"body"),"p"),"t").text=" ".join(expected["tokens"])
            archive.writestr("word/document.xml",ET.tostring(xml))
        (self.root/"活动安排.docx").write_bytes(output.getvalue());(self.root/"活动安排.pdf").write_bytes(b"FIXED_UNIT_PDF_READER_SUBSTITUTE")
        self.assertFalse(qa.evaluate("activity_notice",self.root,expected)["passed"])
        reader=lambda _: {"pages":1,"text":" ".join(expected["tokens"])}
        result=qa.evaluate("activity_notice",self.root,expected,pdf_reader=reader)
        self.assertTrue(result["passed"]);self.assertTrue(result["manual_visual_review_required"])
        spaced=lambda _: {"pages":1,"text":" \n".join(" ".join(expected["tokens"]))}
        self.assertTrue(qa.evaluate("activity_notice",self.root,expected,pdf_reader=spaced)["passed"])
        missing=lambda _: {"pages":1,"text":" \n".join(" ".join(expected["tokens"][:-1]))}
        self.assertFalse(qa.evaluate("activity_notice",self.root,expected,pdf_reader=missing)["passed"])
        self.assertFalse(qa.evaluate("activity_notice",self.root,expected,pdf_reader=lambda _:{"pages":2,"text":reader(None)["text"]})["passed"])

    def test_official_web_answer_requires_facts_example_and_direct_official_link(self):
        expected=self.case("web_defaults")
        answer=('默认值在函数定义时只计算一次，多次调用会共享同一个列表。\n'
            '```python\ndef f(x, items=[]):\n    items.append(x)\n    return items\n```\n'
            'https://docs.python.org/3/tutorial/controlflow.html#default-argument-values')
        self.assertTrue(qa.evaluate("web_defaults",self.root,expected,answer)["passed"])
        for incorrect in (answer.replace("一次","每次"),answer.replace("共享同一个","创建新"),
                answer.replace("docs.python.org","example.com"),answer.replace("append","remove")):
            self.assertFalse(qa.evaluate("web_defaults",self.root,expected,incorrect)["passed"])

    def test_web_source_recovery_accepts_only_hash_bound_tool_result_in_exact_qa_namespace(self):
        directory=self.root/"agent_workspace";directory.mkdir()
        cp=directory/"owned.checkpoints.sqlite3";archive=directory/"agent_context/observations.sqlite3"
        archive.parent.mkdir();cid="owned-qa-cid";scope="a"*64
        source={"url":"https://docs.python.org/3/tutorial/controlflow.html#default-argument-values",
            "source_sha256":"b"*64,"offset":16000,"text":"At the point of function definition. The default value is evaluated only once.",
            "untrusted_external_data":True}
        raw=json.dumps(source);reference="obs_"+qa.sha(raw.encode())
        history=[{"role":"tool","name":"web_fetch","tool_call_id":"own-call",
            "content":json.dumps({"context_observation":{"reference":reference}})}]
        connection=sqlite3.connect(cp)
        try:
            connection.execute("CREATE TABLE agent_checkpoints(conversation_id TEXT, scope_hash TEXT, history_json TEXT)")
            connection.execute("INSERT INTO agent_checkpoints VALUES(?,?,?)",(cid,scope,json.dumps(history)));connection.commit()
        finally:connection.close()
        connection=sqlite3.connect(archive)
        try:
            connection.execute("CREATE TABLE observations(namespace TEXT, reference TEXT, text TEXT, metadata TEXT)")
            meta=json.dumps({"kind":"tool_result","source":{"tool":"web_fetch","call_id":"own-call"}})
            connection.execute("INSERT INTO observations VALUES(?,?,?,?)",(cid+":"+scope,reference,raw,meta));connection.commit()
        finally:connection.close()
        before={p:qa.sha(p.read_bytes()) for p in (cp,archive)}
        with patch.object(qa,"ROOT",self.root):
            facts=qa._official_web_evidence(cp,cid)
            self.assertEqual(1,len(facts));self.assertTrue(facts[0]["has_once_warning"])
            self.assertTrue(facts[0]["has_definition_scope"])
            self.assertEqual([],qa._official_web_evidence(cp,"foreign-cid"))
        self.assertEqual(before,{p:qa.sha(p.read_bytes()) for p in (cp,archive)})
        for text,metadata in ((raw+"TAMPER",meta),(raw,json.dumps({"kind":"history","source":{"tool":"web_fetch","call_id":"own-call"}})),
                (raw,json.dumps({"kind":"tool_result","source":{"tool":"web_fetch","call_id":"different-call"}}))):
            connection=sqlite3.connect(archive)
            try:
                connection.execute("UPDATE observations SET text=?,metadata=?",(text,metadata));connection.commit()
            finally:connection.close()
            with patch.object(qa,"ROOT",self.root):self.assertEqual([],qa._official_web_evidence(cp,cid))


if __name__=="__main__":unittest.main()
