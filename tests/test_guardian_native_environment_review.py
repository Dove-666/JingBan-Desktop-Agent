"""Actual owned native process: Unicode and ambient environment isolation.

The child is fixed stdlib code, never a model program. It writes booleans about
synthetic variables, never a dump of the host's environment or credentials.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from desktop_pet.agents.guardian_native import Native, OwnedJob, OwnedProcess

_UNCONFIRMED = []  # Keep exact native owners if a test exposes failed cleanup.


@unittest.skipUnless(os.name == "nt", "Actual Windows native creation")
class NativeEnvironmentReviewTests(unittest.TestCase):
    def test_real_child_receives_explicit_unicode_snapshot_and_default_never_inherits_ambient(self):
        native = Native()
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                owned = tempfile.TemporaryDirectory(prefix="native-env-independent-")
                root = Path(owned.name)
                script, output = root / "owned_child.py", root / "observed.json"
                script.write_text(
                    "import json,os,sys\nfrom pathlib import Path\n"
                    "value={'ambient_absent':'AURA_QA_AMBIENT' not in os.environ,"
                    "'unicode_match':os.environ.get('AURA_QA_UNICODE')=='中🌊=可信值',"
                    "'empty_match':os.environ.get('AURA_QA_EMPTY')==''}\n"
                    "Path(sys.argv[1]).write_text(json.dumps(value),encoding='utf-8')\n", encoding="utf-8")
                null = native.k.CreateFileW("NUL", 0xC0000000, 3, None, 3, 0, None)
                self.assertTrue(null and null != -1)
                job, process, closed = OwnedJob(native), None, False
                try:
                    environment = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
                        "AURA_QA_UNICODE": "中🌊=可信值", "AURA_QA_EMPTY": ""} if explicit else None
                    with patch.dict(os.environ, {"AURA_QA_AMBIENT": "synthetic-do-not-inherit"}):
                        process = OwnedProcess.create(native, [sys.executable, "-I", str(script), str(output)],
                            cwd=root, inherited=[null], stdio=(null, null, null), environment=environment)
                    job.assign(process)
                    process.resume()
                    self.assertEqual(0, native.k.WaitForSingleObject(process.handle, 10000))
                    value = json.loads(output.read_text(encoding="utf-8"))
                    self.assertTrue(value["ambient_absent"])
                    self.assertEqual(explicit, value["unicode_match"])
                    self.assertEqual(explicit, value["empty_match"])
                finally:
                    released = False
                    try:
                        job.terminate()
                        closed = process is not None and native.confirm_closed(job.handle, process.handle, timeout=3)
                        if closed:
                            for owner, name in ((process, "thread"), (process, "handle"), (job, "handle")):
                                handle = getattr(owner, name)
                                if handle:
                                    if not native.k.CloseHandle(handle): raise OSError("Owned handle close failed")
                                    setattr(owner, name, 0)
                            if not native.k.CloseHandle(null): raise OSError("Owned NUL close failed")
                            null = 0
                            owned.cleanup()
                            released = True
                    except Exception:
                        pass
                    if not released:
                        _UNCONFIRMED.append((native, job, process, null, owned))
                    self.assertTrue(closed and released, "Exact native root/Job/resource cleanup was not confirmed")


if __name__ == "__main__": unittest.main()
