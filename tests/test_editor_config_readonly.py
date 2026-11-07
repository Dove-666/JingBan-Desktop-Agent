"""Only synthetic settings/vault fixtures. No configured credentials or UI."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from PySide6.QtCore import QSettings
from desktop_pet.settings_manager import SettingsManager,CredentialStore
from tools.qa_editor_config_readonly import GetterSettings,GetterVault,Audit,WriteDenied


class MemoryVault:
    def __init__(self):self.value="SYNTHETIC_CREDENTIAL";self.reads=0;self.writes=0
    def get_password(self,*args):self.reads+=1;return self.value
    def set_password(self,*args):self.writes+=1;raise AssertionError("must_not_reach_backend")
    def delete_password(self,*args):self.writes+=1;raise AssertionError("must_not_reach_backend")


class GetterConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="editor-config-getter-")
        self.path=Path(self.temporary.name)/"synthetic.ini"
        self.store=QSettings(str(self.path),QSettings.Format.IniFormat)
        self.store.setValue("api/model","synthetic-owned-editor");self.store.setValue("api/base_url","http://127.0.0.1:12345/v1")
        self.store.sync();self.before=self.path.read_bytes();self.audit=Audit()
        self.facade=GetterSettings(self.store,self.audit);self.backend=MemoryVault()
        self.credentials=CredentialStore(backend=GetterVault(self.backend,self.audit))
        self.manager=SettingsManager(self.facade,self.credentials)
    def tearDown(self):self.store=None;self.temporary.cleanup()

    def test_same_manager_loader_paths_need_only_getters_and_vault_get(self):
        self.assertEqual("synthetic-owned-editor",self.manager.load_api_settings().model)
        self.assertEqual("SYNTHETIC_CREDENTIAL",self.manager.get_api_key())
        self.manager.load_agent_settings()
        self.assertEqual([],self.audit.denied);self.assertEqual(1,self.audit.vault_reads)
        self.assertEqual(self.before,self.path.read_bytes());self.assertEqual(0,self.backend.writes)

    def test_every_settings_write_is_refused_before_actual_store(self):
        for call in (lambda:self.facade.setValue("api/model","must-not-save"),lambda:self.facade.remove("api/model"),
                     self.facade.sync,self.facade.clear):
            with self.assertRaises(WriteDenied):call()
        self.assertEqual(self.before,self.path.read_bytes())
        self.assertEqual(["SET_VALUE","REMOVE","SYNC","CLEAR"],[row["operation"] for row in self.audit.denied])

    def test_vault_writes_are_refused_and_unknown_keys_are_not_recorded(self):
        for call in (lambda:self.credentials.save_api_key("SYNTHETIC_NEW"),self.credentials.clear_api_key):
            with self.assertRaises(Exception):call()
        self.assertEqual(0,self.backend.writes)
        with self.assertRaises(WriteDenied):self.facade.setValue("private_user_named_key","SYNTHETIC_SECRET_VALUE")
        self.assertEqual("OTHER_KEY",self.audit.denied[-1]["key"])
        self.assertNotIn("private_user_named_key",str(self.audit.denied))
        self.assertNotIn("SYNTHETIC_SECRET_VALUE",str(self.audit.denied))


if __name__=="__main__":unittest.main()
