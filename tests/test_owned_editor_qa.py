"""Pure owned-editor contract/oracle review. No visible UI or native input."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.qa_owned_editor import roundtrip_oracle, ROOT
from tools.qa_owned_editor_fixture import checked_setup


class OwnedEditorOracleTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="editor-oracle-")
        self.root = Path(self.owned.name); self.marker = "鲸鱼娘 native ✅ 13751"
        (self.root/"report.txt").write_bytes(self.marker.encode("utf-8"))
        self.arguments = {"first_runtime":"42.old", "reopened_runtime":"42.new", "closed_without_editor":True,
            "current_value":self.marker, "status":"Reopened", "saved_sha":hashlib.sha256(self.marker.encode()).hexdigest(),
            "current_identity_matches":True, "cancelled":False}

    def tearDown(self): self.owned.cleanup()

    def result(self, **kwargs):
        return roundtrip_oracle(self.root,self.marker,**{**self.arguments,**kwargs})

    def test_actual_disk_and_fresh_reopened_ui_are_both_required(self):
        self.assertTrue(self.result()["passed"])
        for mutation in ({"current_value":"stale"},{"status":"Saved"},{"closed_without_editor":False},
                         {"reopened_runtime":"42.old"},{"current_identity_matches":False},{"cancelled":True}):
            with self.subTest(mutation=mutation): self.assertFalse(self.result(**mutation)["passed"])

    def test_noop_wrong_save_and_changed_file_version_cannot_be_approved_by_labels(self):
        (self.root/"report.txt").unlink()
        self.assertFalse(self.result()["passed"])
        (self.root/"report.txt").write_bytes(b"WRONG")
        self.assertFalse(self.result()["passed"])
        (self.root/"report.txt").write_bytes(self.marker.encode())
        self.assertFalse(self.result(saved_sha="0"*64)["passed"])

    def test_linked_external_file_is_not_read_as_owned_goal_artifact(self):
        outside = self.root/"other.txt"; outside.write_bytes(b"SYNTHETIC_OUTSIDE_TARGET")
        target = self.root/"report.txt"; target.unlink(); os.link(outside,target)
        value=self.result()
        self.assertFalse(value["passed"])
        self.assertIsNone(value["file_sha256"])
        self.assertEqual(0,value["file_bytes"])

    def test_replaced_workspace_directory_cannot_redirect_the_independent_readback(self):
        alias=self.root/"workspace-alias"
        if os.name=="nt":
            import _winapi
            _winapi.CreateJunction(str(self.root),str(alias))
        else: alias.symlink_to(self.root,target_is_directory=True)
        try:
            value=roundtrip_oracle(alias,self.marker,**self.arguments)
            self.assertFalse(value["passed"])
            self.assertIsNone(value["file_sha256"])
            self.assertEqual(0,value["file_bytes"])
        finally:
            if os.name=="nt": alias.rmdir()
            else: alias.unlink()

    def test_default_cli_does_not_start_ui_model_or_write_artifacts(self):
        output=self.root/"must-not-create.json"
        result=subprocess.run([sys.executable,str(ROOT/"tools/qa_owned_editor.py"),"--output",str(output)],
            capture_output=True,encoding="utf-8",timeout=10,
            **({"creationflags":0x08000000} if os.name=="nt" else {}))
        self.assertEqual(0,result.returncode)
        self.assertEqual({"status":"not_run","native_input":False,"model_called":False},json.loads(result.stdout))
        self.assertFalse(output.exists())

    def test_setup_is_host_only_canonical_workspace_and_closed_shape(self):
        parent=ROOT/"agent_workspace"; parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="editor-setup-",dir=parent) as workspace:
            value={"type":"setup","nonce":"1"*32,"workspace":workspace,"fault":"none","watchdog_ms":90000}
            self.assertEqual(Path(workspace).resolve(),checked_setup(value)["workspace"])
            for mutation in ({"nonce":"0"*32},{"workspace":str(self.root)},{"watchdog_ms":True},
                             {"fault":"user app"},{"path":"anywhere.txt"}):
                with self.subTest(mutation=mutation), self.assertRaises(ValueError): checked_setup({**value,**mutation})


if __name__=="__main__":unittest.main()
