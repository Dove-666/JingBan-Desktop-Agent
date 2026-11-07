"""Authenticated, generation-bound archive context exports over real RPC."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
import unittest
from uuid import uuid4

from tests import test_remote_controller as support


class RemoteContextProtocolTests(unittest.TestCase):
    setUpClass = classmethod(support.RemoteControllerTests.setUpClass.__func__)
    setUp = support.RemoteControllerTests.setUp
    tearDown = support.RemoteControllerTests.tearDown
    start = support.RemoteControllerTests.start
    request = support.RemoteControllerTests.request
    pair = support.RemoteControllerTests.pair

    def seed(self):
        cid = str(uuid4())
        self.chat.store.append_message(cid, "user", "My exact archive fact", "user")
        self.chat.store.append_message(cid, "assistant", "Archived answer", "assistant")
        return cid

    def test_export_requires_pairing_and_never_reserves_operations(self):
        cid = self.seed()
        self.start()
        params = {"conversation_id": cid}
        self.assertEqual(self.request("context.get", params)["error"]["code"], "unauthorized")
        self.pair()
        request_id = str(uuid4())
        reply = self.request("context.get", params, request_id=request_id)
        self.assertTrue(reply["ok"], reply)
        envelope = reply["result"]
        self.assertEqual(envelope["conversation_id"], cid)
        self.assertTrue(envelope["reference_data_only"])
        self.assertFalse(envelope["summary"]["verified"])
        self.assertIsNone(self.remote.sync.get_operation(support.PEER, request_id))
        self.assertIn("context.get", self.request("status")["result"]["capabilities"])

    def test_old_generation_unknown_params_and_deleted_conversation_fail(self):
        cid = self.seed()
        self.start()
        self.pair()
        self.assertFalse(self.request("context.get", {"conversation_id": cid, "generation": str(uuid4())})["ok"])
        self.assertFalse(self.request("context.get", {"conversation_id": cid, "grant": "/"})["ok"])
        self.chat.store.delete_conversation(cid)
        self.assertFalse(self.request("context.get", {"conversation_id": cid})["ok"])

    def test_private_redaction_inputs_are_captured_on_qt_thread(self):
        cid = self.seed()
        self.start()
        self.pair()
        ui_thread = threading.get_ident()
        seen = []
        def keys():
            seen.append(threading.get_ident())
            return ("CONTEXT_PRIVATE_KEY",)
        self.settings.get_known_api_keys = keys
        self.chat.store.append_message(cid, "user", "CONTEXT_PRIVATE_KEY " * 10000, "user")
        for _ in range(3):
            self.chat.store.append_message(cid, "assistant", "later text" * 5000, "assistant")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.request, "context.get", {"conversation_id": cid})
            support.spin_until(future.done)
            result = future.result()
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(seen), {ui_thread})
        self.assertNotIn("CONTEXT_PRIVATE_KEY", json.dumps(result))
        self.assertIn("已隐藏密钥", result["result"]["summary"]["text"])

    def test_nonfinite_and_boolean_memory_scores_are_not_exported(self):
        cid = self.seed()
        self.chat._memory = SimpleNamespace(
            load_settings=lambda: SimpleNamespace(enabled=True, relevance_threshold=.6),
            search=lambda *args, **kwargs: [
                {"id": str(uuid4()), "memory": text, "score": score}
                for text, score in (("accepted", .8), ("missing score", None),
                                    ("infinite", float("inf")), ("nan", float("nan")),
                                    ("boolean", True), ("low", .1))])
        envelope = self.chat.context_envelope_for(cid)
        raw = json.dumps(envelope, allow_nan=False)
        self.assertIn("accepted", raw)
        self.assertIn("missing score", raw)
        for text in ("infinite", "nan", "boolean", '"low"'):
            self.assertNotIn(text, raw)
        self.chat._memory = None


if __name__ == "__main__":
    unittest.main()
