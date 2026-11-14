"""Real Mem0 OSS + local Qdrant + loopback model/embedding HTTP tests.

No Mem0 classes or vector stores are mocked. Only the remote model services
are deterministic local HTTP fixtures, and credentials use an in-memory vault.
"""

from dataclasses import replace
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QSettings

from desktop_pet.memory import MemoryService, MemorySettings, MemoryUnavailable
from desktop_pet.memory.service import select_candidates
from desktop_pet.settings_manager import ApiSettings, CredentialStore, CredentialsError, SettingsError, SettingsManager


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class LocalMemoryModels:
    def __init__(self):
        self.requests = []
        self.facts = ["用户长期偏好大字体评论气泡。"]
        self.correction_response = None
        self.dimensions = 8
        self.fail = False
        self.delay = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, "body": body})
                if owner.delay:
                    time.sleep(owner.delay)
                if owner.fail:
                    status, result = 401, {"error": {"message": "fixture provider unavailable", "type": "authentication_error"}}
                elif self.path.endswith("/embeddings"):
                    values = body["input"]
                    if isinstance(values, str):
                        values = [values]
                    vectors = []
                    for index, value in enumerate(values):
                        topic = 0 if any(token in value for token in ("字体", "气泡")) else 1 if "项目" in value else 2
                        vector = [0.0] * owner.dimensions
                        vector[topic] = 1.0
                        vectors.append({"object": "embedding", "index": index, "embedding": vector})
                    status, result = 200, {"object": "list", "data": vectors, "model": "fixture-embedding", "usage": {"prompt_tokens": 1, "total_tokens": 1}}
                else:
                    if owner.correction_response is not None and body.get("messages", [{}])[0].get("content", "").startswith("Identify only an EXPLICIT USER CORRECTION"):
                        response = owner.correction_response(body) if callable(owner.correction_response) else owner.correction_response
                    else:
                        response = {"memory": [{"id": str(i), "text": text, "attributed_to": "user"} for i, text in enumerate(owner.facts)]}
                    content = json.dumps(response, ensure_ascii=False)
                    status, result = 200, {
                        "id": "fixture-memory", "object": "chat.completion", "created": 0, "model": "fixture-chat",
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                    }
                payload = json.dumps(result).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class SimulatedMemoryHelper:
    """A cancellable process boundary fixture; no SDK or HTTP is started."""

    def __init__(self, *, expire=False):
        self.expire = expire
        self.args = ["simulated-memory-helper"]
        self.returncode = None
        self.payload = None
        self.timeout = None
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.payload = json.loads(input.decode("utf-8"))
            self.timeout = timeout
            if self.expire:
                threading.Event().wait(timeout)
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = 0
        return b'{"ok":true,"result":[]}', b""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def terminate(self):
        self.returncode = -15


class MemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.vault = Vault()
        self.settings = SettingsManager(
            QSettings(str(self.path / "settings.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=self.vault),
        )
        self.service = self.make_service()
        self.addCleanup(self.service.shutdown)

    def make_service(self):
        return MemoryService(
            self.settings, self.path / "memory",
            llm_credentials=CredentialStore(account="mem0-llm-key", backend=self.vault),
            embedding_credentials=CredentialStore(account="mem0-embedding-key", backend=self.vault),
        )

    def configure(self, provider, *, automatic=True):
        self.settings.save_api_settings(ApiSettings(provider.base_url, "fixture-chat"))
        self.settings.save_api_key("fixture-chat-key")
        settings = replace(
            self.service.load_settings(), enabled=True, automatic=automatic, embedding_provider="openai",
            embedding_base_url=provider.base_url, embedding_model="fixture-embedding", embedding_dimensions=8,
        )
        self.service.save_settings(settings, embedding_key="fixture-embedding-key")

    @contextmanager
    def simulated_helper(self, helper=None):
        # Patch only our helper launch. Windows credential backend discovery
        # can run a harmless platform-version command through the shared
        # subprocess module; it must keep its actual standard-library behavior.
        original_start = subprocess.Popen
        started = []

        def start(command, *args, **kwargs):
            if isinstance(command, list) and "desktop_pet.memory.worker" in command:
                started.append(True)
                if helper is None:
                    raise AssertionError("An inadmissible operation started a memory helper")
                return helper
            return original_start(command, *args, **kwargs)

        with patch("desktop_pet.memory.service.subprocess.Popen", side_effect=start):
            yield started

    @contextmanager
    def queued_call(self, function):
        """Freeze a real RLock after the worker read its initial preferences."""
        observed = threading.Event()
        outcome = {}
        load_settings = self.service.load_settings

        def run():
            try:
                outcome["result"] = function()
            except Exception as error:
                outcome["error"] = error

        worker = threading.Thread(target=run, daemon=True)

        def watched_load():
            settings = load_settings()
            if threading.current_thread() is worker:
                observed.set()
            return settings

        with patch.object(self.service, "load_settings", side_effect=watched_load):
            try:
                with self.service._lock:
                    worker.start()
                    self.assertTrue(observed.wait(2), "Worker did not read its initial settings")
                    yield worker, outcome
            finally:
                worker.join(3)
                if worker.is_alive():
                    self.service.shutdown()
                    worker.join(2)
                self.assertFalse(worker.is_alive(), "Queued memory worker did not stop")

    def test_queued_disabled_search_and_extract_never_start_helper(self):
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        calls = (
            lambda: self.service.search("字体"),
            lambda: self.service.extract([{"role": "user", "content": "记住我喜欢大字体。"}],
                                         conversation_id=str(uuid4()), trigger="automatic"),
        )
        for operation in calls:
            with self.subTest(operation=operation):
                self.service.save_settings(replace(self.service.load_settings(), enabled=True))
                with self.simulated_helper() as started:
                    with self.queued_call(operation) as (worker, outcome):
                        self.service.save_settings(replace(self.service.load_settings(), enabled=False))
                    self.assertEqual([], outcome.get("result"))
                    self.assertNotIn("error", outcome)
                    self.assertEqual([], started)

    def test_queued_automatic_extraction_honors_new_pause_before_starting_helper(self):
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        with self.simulated_helper() as started:
            with self.queued_call(lambda: self.service.extract(
                [{"role": "user", "content": "记住我喜欢大字体。"}],
                conversation_id=str(uuid4()), trigger="automatic",
            )) as (worker, outcome):
                self.service.save_settings(replace(self.service.load_settings(), automatic=False))
            self.assertEqual([], outcome.get("result"))
            self.assertNotIn("error", outcome)
            self.assertEqual([], started)

    def test_queued_search_uses_latest_result_limit_in_both_directions(self):
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        for before, after in ((8, 1), (1, 4)):
            with self.subTest(before=before, after=after):
                self.service.save_settings(replace(self.service.load_settings(), max_results=before))
                helper = SimulatedMemoryHelper()
                with self.simulated_helper(helper) as started:
                    with self.queued_call(lambda: self.service.search("字体", limit=8)) as (worker, outcome):
                        self.service.save_settings(replace(self.service.load_settings(), max_results=after))
                    self.assertEqual([], outcome.get("result"))
                    self.assertNotIn("error", outcome)
                    self.assertEqual(after, helper.payload["arguments"]["limit"])
                    self.assertEqual(1, len(started))

    def test_one_deadline_covers_queue_wait_and_helper_communication(self):
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        self.service.save_settings(replace(self.service.load_settings(), timeout_seconds=1.0))
        helper = SimulatedMemoryHelper(expire=True)
        with self.simulated_helper(helper):
            started = time.monotonic()
            with self.queued_call(lambda: self.service.search("字体")) as (worker, outcome):
                # More than half the operation's budget is consumed in the
                # actual serial storage lock, leaving only the remainder for
                # the helper rather than granting another full timeout.
                threading.Event().wait(0.55)
            elapsed = time.monotonic() - started
        self.assertIsInstance(outcome.get("error"), MemoryUnavailable)
        self.assertIn("超时", str(outcome["error"]))
        self.assertGreater(helper.timeout, 0)
        self.assertLess(helper.timeout, 0.65)
        self.assertLess(elapsed, 1.4)
        self.assertTrue(helper.killed)
        self.assertFalse(self.service._processes)

    def test_shutdown_cancels_queued_operation_before_storage_lock_is_released(self):
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        with self.simulated_helper() as started:
            with self.queued_call(lambda: self.service.search("字体")) as (worker, outcome):
                self.service.shutdown()
                worker.join(0.6)
                self.assertFalse(worker.is_alive(), "Shutdown waited for the held storage lock")
            self.assertIsInstance(outcome.get("error"), MemoryUnavailable)
            self.assertIn("关闭", str(outcome["error"]))
            self.assertEqual([], started)

    def test_legacy_inventory_shares_the_original_operation_deadline(self):
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        self.service.save_settings(replace(self.service.load_settings(), timeout_seconds=1.0))
        vectors = self.service.data_dir / "qdrant"
        vectors.mkdir()
        (vectors / "synthetic-inventory-trigger").touch()
        helpers = []
        class SlowEmptyInventory(SimulatedMemoryHelper):
            def communicate(self, input=None, timeout=None):
                if input is not None:
                    self.payload = json.loads(input)
                    self.timeout = timeout
                    time.sleep(.30)
                    self.returncode = 0
                return b'{"ok":true,"result":{"empty":true}}', b""
        first, second = SlowEmptyInventory(), SimulatedMemoryHelper(expire=True)
        original_start = subprocess.Popen
        def launch(command, *args, **kwargs):
            if isinstance(command, list) and "desktop_pet.memory.worker" in command:
                helper = first if not helpers else second
                helpers.append(helper)
                return helper
            return original_start(command, *args, **kwargs)
        with patch("desktop_pet.memory.service.subprocess.Popen", side_effect=launch):
            before = time.monotonic()
            with self.queued_call(lambda: self.service.search("query")) as (_worker, outcome):
                time.sleep(.55)
            elapsed = time.monotonic() - before
        self.assertIsInstance(outcome.get("error"), MemoryUnavailable)
        self.assertLess(first.timeout, .65)
        self.assertLess(elapsed, 1.4)
        self.assertEqual(first.payload["operation"], "inspect_empty")
        if second.payload is not None:
            self.assertLess(second.timeout, .3)

    def test_disabled_by_default_and_identity_persists(self):
        first = self.service.load_settings()
        self.assertFalse(first.enabled)
        self.assertFalse(first.automatic)
        self.assertEqual(first.user_id, self.make_service().load_settings().user_id)
        self.assertEqual([], self.service.search("项目"))
        self.assertEqual([], self.service.extract([{"role": "user", "content": "记住我喜欢大字体"}], conversation_id=str(uuid4())))

    def test_nonsecret_config_and_independent_verified_vault_keys(self):
        self.service.save_settings(self.service.load_settings(), llm_key="fixture-llm-key", embedding_key="fixture-embedding-key")
        text = self.service.config_path.read_text(encoding="utf-8")
        self.assertNotIn("fixture-llm-key", text)
        self.assertNotIn("fixture-embedding-key", text)
        self.assertEqual("fixture-llm-key", self.vault.get_password("AURA Desktop Agent", "mem0-llm-key"))
        self.service.clear_llm_key()
        self.service.clear_embedding_key()
        self.assertIsNone(self.vault.get_password("AURA Desktop Agent", "mem0-llm-key"))
        self.assertIsNone(self.vault.get_password("AURA Desktop Agent", "mem0-embedding-key"))

    def test_credential_status_distinguishes_available_vault_from_missing_keys(self):
        self.service.save_settings(replace(self.service.load_settings(), embedding_provider="openai"))
        self.assertTrue(self.service._embedding_credentials.available)
        text = self.service.credential_status()
        self.assertIn("提炼模型复用的聊天密钥：未保存", text)
        self.assertIn("Embedding 密钥：未保存", text)
        self.assertNotIn("已保存", text)

    def test_credential_status_uses_chat_key_when_extractor_reuses_chat(self):
        self.service.save_settings(replace(self.service.load_settings(), embedding_provider="openai"))
        self.settings.save_api_key("fixture-chat-key")
        self.service.set_embedding_key("fixture-embedding-key")
        text = self.service.credential_status()
        self.assertIn("提炼模型复用的聊天密钥：已保存", text)
        self.assertIn("Embedding 密钥：已保存", text)
        self.assertNotIn("独立记忆提炼密钥", text)
        self.assertNotIn("fixture-chat-key", text)
        self.assertNotIn("fixture-embedding-key", text)

    def test_credential_status_checks_independent_extractor_in_selected_mode(self):
        self.settings.save_api_key("fixture-chat-key")
        self.service.save_settings(replace(self.service.load_settings(), use_chat_model=False))
        text = self.service.credential_status()
        self.assertIn("独立记忆提炼密钥：未保存", text)
        self.service.set_llm_key("fixture-llm-key")
        text = self.service.credential_status()
        self.assertIn("独立记忆提炼密钥：已保存", text)
        self.assertNotIn("fixture-llm-key", text)

    def test_credential_status_reports_unreadable_vault_without_secret_details(self):
        self.service.save_settings(replace(self.service.load_settings(), embedding_provider="openai"))
        with patch.object(self.service._embedding_credentials, "get_api_key", side_effect=CredentialsError(
                "DO-NOT-ECHO-THIS-UNTRUSTED-BACKEND-DETAIL")):
            text = self.service.credential_status()
        self.assertIn("Embedding 密钥：无法读取", text)
        self.assertNotIn("Embedding 密钥：未保存", text)
        self.assertNotIn("DO-NOT-ECHO", text)

    def test_key_cannot_be_saved_in_ordinary_fields(self):
        self.service.set_embedding_key("fixture-embedding-key")
        with self.assertRaises(SettingsError):
            self.service.save_settings(replace(self.service.load_settings(), embedding_model="fixture-embedding-key"))
        with self.assertRaises(SettingsError):
            self.service.save_settings(replace(self.service.load_settings(), llm_model="new-secret-value"), llm_key="new-secret-value")

    def test_cannot_change_local_identity(self):
        with self.assertRaises(SettingsError):
            self.service.save_settings(replace(self.service.load_settings(), user_id=str(uuid4())))

    def test_invalid_config_fails_closed_without_overwriting(self):
        self.service.config_path.write_text('{"enabled":"true"}', encoding="utf-8")
        with self.assertRaises(MemoryUnavailable):
            self.service.load_settings()
        self.assertEqual('{"enabled":"true"}', self.service.config_path.read_text(encoding="utf-8"))

    def test_candidate_filter_excludes_screen_assistant_chitchat_questions_secrets(self):
        messages = [
            {"role": "assistant", "content": "用户长期喜欢猫"},
            {"role": "user", "source": "proactive_screen", "content": "喜欢这只猫"},
            {"role": "user", "content": "你好啊"},
            {"role": "user", "content": "我的项目进度怎么样？"},
            {"role": "user", "content": "网页说我喜欢咖啡"},
            {"role": "user", "content": "记住我的密码是123456"},
            {"role": "user", "content": "记住我喜欢fixture-known-value"},
            {"role": "user", "content": "记住，我长期偏好大字体评论气泡。"},
        ]
        result = select_candidates(messages, secrets=("fixture-known-value",))
        self.assertEqual([messages[-1]], result)

    def test_candidate_filter_is_bounded_and_never_splits_long_messages(self):
        self.assertEqual(4, len(select_candidates([{"role": "user", "content": f"我的项目进度{i}"} for i in range(100)])))
        self.assertEqual([], select_candidates([{"role": "user", "content": "记住" + "长" * 1700}]))

    def test_validation_and_shutdown_are_safe(self):
        for field, value in (("enabled", "yes"), ("embedding_dimensions", True), ("relevance_threshold", float("nan")), ("timeout_seconds", 0)):
            with self.subTest(field=field), self.assertRaises(SettingsError):
                replace(self.service.load_settings(), **{field: value}).validate()
        self.service.shutdown()
        with self.assertRaises(MemoryUnavailable):
            self.service.list_memories()

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_real_mem0_extraction_restart_relevance_delete_clear_and_local_management(self):
        with LocalMemoryModels() as provider:
            self.configure(provider)
            conversation_id = str(uuid4())
            result = self.service.extract([
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "我猜你很喜欢小字"},
                {"role": "user", "content": "记住，我长期偏好大字体评论气泡。"},
            ], conversation_id=conversation_id)
            self.assertEqual(1, len(result))
            memory_id = result[0]["id"]
            requests = [row for row in provider.requests if row["path"].endswith("chat/completions")]
            self.assertEqual(1, len(requests))
            prompt = json.dumps(requests[0]["body"]["messages"], ensure_ascii=False)
            self.assertIn("大字体评论气泡", prompt)
            self.assertNotIn("我猜你很喜欢小字", prompt)
            self.assertNotIn('"你好"', prompt)
            self.assertTrue((self.path / "memory" / "qdrant").exists())
            self.assertTrue((self.path / "memory" / "mem0_history.sqlite3").exists())
            restarted = self.make_service()
            self.addCleanup(restarted.shutdown)
            relevant = restarted.search("用户偏好的字体", limit=4)
            self.assertEqual(memory_id, relevant[0]["id"])
            self.assertEqual([], restarted.search("项目开发进度"))
            self.assertEqual(memory_id, restarted.list_memories("字体")[0]["id"])
            self.assertEqual([], restarted.list_memories("无关检索词"))
            with self.assertRaises(SettingsError):
                restarted.save_settings(replace(restarted.load_settings(), embedding_dimensions=16))
            restarted.delete(memory_id)
            self.assertEqual([], restarted.search("大字体气泡"))
            self.assertEqual([], restarted.list_memories())
            provider.facts = ["用户正在开发 AURA 桌宠项目。"]
            self.assertTrue(restarted.extract([{"role": "user", "content": "我正在开发 AURA 桌宠项目。"}], conversation_id=str(uuid4())))
            # Management must work without contacting models or requiring keys.
            restarted.clear_embedding_key()
            self.settings.clear_api_key()
            before = len(provider.requests)
            self.assertEqual(1, len(restarted.list_memories()))
            restarted.clear()
            self.assertEqual([], restarted.list_memories())
            self.assertEqual(before, len(provider.requests))
            self.assertFalse((self.path / "memory" / "embedding_identity.json").exists())
            from qdrant_client import QdrantClient
            vectors = QdrantClient(path=str(self.path / "memory" / "qdrant"))
            try:
                for collection in vectors.get_collections().collections:
                    self.assertEqual(0, vectors.count(collection.name).count)
            finally:
                vectors.close()
            restarted.save_settings(replace(restarted.load_settings(), embedding_dimensions=16))
            restarted.set_embedding_key("fixture-embedding-key")
            self.settings.save_api_key("fixture-chat-key")
            provider.dimensions = 16
            self.assertTrue(restarted.extract(
                [{"role": "user", "content": "我正在开发 AURA 桌宠项目。"}], conversation_id=str(uuid4()),
            ))
            self.assertTrue(restarted.search("AURA 项目进度"))

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_extraction_validates_before_writing_and_uses_ordinary_json_text(self):
        with LocalMemoryModels() as provider:
            self.configure(provider)
            provider.facts = [
                "用户的 token 是 fixture-embedding-key。",
                "用户的密码是 123456。",
                *[f"用户长期偏好第{i}种大字体气泡。" for i in range(8)],
            ]
            result = self.service.extract(
                [{"role": "user", "content": "记住，我长期偏好大字体气泡。"}],
                conversation_id=str(uuid4()),
            )
            self.assertEqual(4, len(result))
            saved = self.service.list_memories()
            self.assertEqual(4, len(saved))
            self.assertTrue(all("fixture-embedding-key" not in row["memory"] and "密码" not in row["memory"] for row in saved))
            generation = next(row["body"] for row in provider.requests if row["path"].endswith("chat/completions"))
            self.assertNotIn("response_format", generation)

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_expanded_sdk_prompt_over_budget_never_calls_extraction_llm_or_saves_memory(self):
        with LocalMemoryModels() as provider:
            self.configure(provider)
            self.service.save_settings(replace(
                self.service.load_settings(), extraction_context_tokens=4096,
                extraction_output_tokens=128,
            ))
            # Individually valid candidates are not shortened just to fit.
            # The check also includes Mem0's native few-shot prompt and its
            # persisted short internal history before allowing an LLM call.
            messages = [{"role": "user", "content": f"记住，我的项目{i}：" + "鲸" * 1400}
                        for i in range(4)]
            self.assertEqual(4, len(select_candidates(messages)))
            with self.assertRaises(MemoryUnavailable) as raised:
                self.service.extract(messages, conversation_id=str(uuid4()))
            self.assertIn("Token 预算", str(raised.exception))
            self.assertIn("未发送提炼模型请求", str(raised.exception))
            self.assertFalse(any(row["path"].endswith("chat/completions") for row in provider.requests))
            # Mem0 performs a bounded retrieval embedding before constructing
            # its native prompt. Every admitted candidate remains complete.
            embeddings = [row["body"] for row in provider.requests if row["path"].endswith("/embeddings")]
            self.assertTrue(embeddings)
            serialized = json.dumps(embeddings[0], ensure_ascii=False)
            for message in messages:
                self.assertIn(message["content"], serialized)
            self.assertEqual([], self.service.list_memories())
            # An admitted mutating operation binds its space before invoking
            # the SDK, even when the helper ultimately reports a budget error.
            self.assertEqual(self.service._read_identity(), self.service._embedding_identity(self.service.load_settings()))

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_output_cap_reaches_real_sdk_http_for_ordinary_and_reasoning_models(self):
        with LocalMemoryModels() as provider:
            self.configure(provider)
            self.service.save_settings(replace(
                self.service.load_settings(), extraction_output_tokens=128,
            ))
            for model, expected in (("fixture-chat", "max_tokens"), ("o3-mini", "max_completion_tokens")):
                with self.subTest(model=model):
                    self.settings.save_api_settings(ApiSettings(provider.base_url, model))
                    self.service.extract(
                        [{"role": "user", "content": "记住，我长期偏好大字体评论气泡。"}],
                        conversation_id=str(uuid4()),
                    )
                    generation = [row["body"] for row in provider.requests
                                  if row["path"].endswith("chat/completions")][-1]
                    self.assertEqual(model, generation["model"])
                    self.assertEqual(128, generation[expected])
                    unexpected = "max_completion_tokens" if expected == "max_tokens" else "max_tokens"
                    self.assertNotIn(unexpected, generation)
                    self.assertNotIn("response_format", generation)

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_search_needs_only_embedding_after_independent_extraction_key_is_cleared(self):
        with LocalMemoryModels() as provider:
            self.configure(provider)
            saved = self.service.extract(
                [{"role": "user", "content": "记住，我长期偏好大字体评论气泡。"}],
                conversation_id=str(uuid4()),
            )
            self.assertEqual(1, len(saved))
            self.service.save_settings(replace(
                self.service.load_settings(), use_chat_model=False,
                llm_base_url=provider.base_url, llm_model="fixture-independent-extractor",
            ), llm_key="fixture-independent-extractor-key")
            self.service.clear_llm_key()
            before = len(provider.requests)
            result = self.service.search("用户偏好的字体")
            self.assertEqual(saved[0]["id"], result[0]["id"])
            self.assertTrue(provider.requests[before:])
            self.assertTrue(all(row["path"].endswith("/embeddings") for row in provider.requests[before:]))
            before = len(provider.requests)
            with self.assertRaises(MemoryUnavailable) as raised:
                self.service.extract(
                    [{"role": "user", "content": "记住，我长期偏好大字体评论气泡。"}],
                    conversation_id=str(uuid4()),
                )
            self.assertIn("缺少记忆提炼 API Key", str(raised.exception))
            self.assertEqual(before, len(provider.requests))

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_pause_automatic_extraction_skips_model_and_explicit_remains_available(self):
        with LocalMemoryModels() as provider:
            self.configure(provider, automatic=False)
            messages = [{"role": "user", "content": "记住我长期偏好大字体气泡。"}]
            self.assertEqual([], self.service.extract(messages, conversation_id=str(uuid4()), trigger="message"))
            self.assertEqual([], provider.requests)
            self.assertTrue(self.service.extract(messages, conversation_id=str(uuid4()), trigger="explicit"))

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_provider_outage_does_not_expose_keys_or_break_local_settings(self):
        with LocalMemoryModels() as provider:
            self.configure(provider)
            provider.fail = True
            with self.assertRaises(MemoryUnavailable) as raised:
                self.service.search("项目")
            self.assertNotIn("fixture-chat-key", str(raised.exception))
            self.assertNotIn("fixture-embedding-key", str(raised.exception))
            self.assertTrue(self.service.load_settings().enabled)

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
    def test_operation_deadline_terminates_sdk_and_releases_qdrant_lock(self):
        with LocalMemoryModels() as provider:
            self.configure(provider)
            self.service.save_settings(replace(self.service.load_settings(), timeout_seconds=2.0))
            provider.delay = 8
            start = time.monotonic()
            with self.assertRaises(MemoryUnavailable):
                self.service.search("字体")
            self.assertLess(time.monotonic() - start, 5)
            provider.delay = 0
            self.service.save_settings(replace(self.service.load_settings(), timeout_seconds=20))
            self.assertEqual([], self.service.list_memories())
            self.assertFalse(self.service._processes)


if __name__ == "__main__":
    unittest.main()
