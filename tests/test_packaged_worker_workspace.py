"""Plain backend request scope follows the packaged host's selected profile."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT/"agent_backend/nanobot"


class PackagedWorkerWorkspaceTests(unittest.TestCase):
    def inspect(self, packaged, profile, workspace, local=None):
        environment = os.environ.copy()
        environment["JINGBAN_PACKAGED"] = packaged
        environment["JINGBAN_PROFILE_DIR"] = profile
        if local is not None:
            environment["LOCALAPPDATA"] = local
        script = (
            "import sys,json;sys.path.insert(0,"+repr(str(WORKER))+");import worker;sys.stdout=sys.__stdout__;sys.stderr=sys.__stderr__;"
            "request={'type':'run','id':'scope-test','kind':'tools','task':'Create hello_jingban.txt','context':'',"
            "'config':{'base_url':'http://127.0.0.1:9/v1','model':'offline','api_key':'synthetic'},"
            "'options':{'workspace':"+repr(str(workspace))+",'tools_enabled':True,'computer_enabled':False}};"
            "exec('try:\\n result=worker.checked_request(request);print(json.dumps({\"accepted\":True,\"workspace\":result[\"options\"][\"workspace\"],\"profile\":str(worker._packaged_profile_root())}))"
            "\\nexcept worker.InvalidRequest:\\n print(json.dumps({\"accepted\":False}))')"
        )
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True,
                                encoding="utf-8", env=environment, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_packaged_accepts_profile_workspace_and_rejects_installation_and_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)/"profile"
            allowed = profile/"agent_workspace/task"
            result = self.inspect("1", str(profile), allowed)
            self.assertTrue(result["accepted"])
            self.assertEqual(Path(result["workspace"]), allowed.resolve())
            self.assertFalse(self.inspect("1", str(profile), ROOT/"agent_workspace/test")["accepted"])
            self.assertFalse(self.inspect("1", str(profile), profile/"outside")["accepted"])

    def test_relative_packaged_profile_is_rejected(self):
        self.assertFalse(self.inspect("1", "relative-profile", ROOT/"agent_workspace/test")["accepted"])

    def test_packaged_default_profile_uses_local_appdata(self):
        with tempfile.TemporaryDirectory() as temporary:
            allowed = Path(temporary)/"AURA Desktop Pet/agent_workspace/task"
            result = self.inspect("1", "", allowed, local=temporary)
            self.assertTrue(result["accepted"])
            self.assertEqual(Path(result["profile"]), Path(temporary)/"AURA Desktop Pet")

    def test_development_request_scope_is_unchanged_by_profile_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)/"profile"
            self.assertTrue(self.inspect("", str(profile), ROOT/"agent_workspace/test")["accepted"])
            self.assertFalse(self.inspect("", str(profile), profile/"agent_workspace/test")["accepted"])
