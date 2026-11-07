"""Local-only host checks. Fake workers never call a model or input APIs."""

from dataclasses import replace
import importlib
import json
from enum import Enum
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from desktop_pet.agents import (
    AgentError, AgentRequest, AgentResult, AgentService, AgentSettings,
    AgentValidationError, load_agent_settings, save_agent_settings,
)
from desktop_pet.agents import service as runtime
from desktop_pet.agents.settings import shared_api_settings


KEY = "local-host-test-secret"
GUI_KEY = "local-gui-test-secret"


class Store:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass

    def status(self):
        return 0


class AgentContractTests(unittest.TestCase):
    def test_null_routes_to_ordinary_chat_and_only_exact_fields_are_accepted(self):
        self.assertIsNone(AgentRequest.from_value(None))
        self.assertEqual(AgentRequest.from_value({"task": "查一下天气", "kind": "tools"}),
                         AgentRequest("查一下天气"))
        for value in ({}, {"task": "x"}, {"task": "x", "kind": "tools", "command": "x"},
                      [], "tools", {"task": "x", "kind": "shell"}, {"task": "x", "kind": "agent"},
                      {"task": "\0", "kind": "tools"}, {"task": " ", "kind": "computer"},
                      {"task": "x" * 12_001, "kind": "tools"}):
            with self.subTest(value=str(value)[:50]):
                with self.assertRaises(AgentValidationError):
                    AgentRequest.from_value(value)
        AgentRequest("A direct host-authorized turn", "agent").validate()

    def test_settings_round_trip_only_contains_public_fields(self):
        store = Store()
        settings = replace(AgentSettings(), enabled=True, python_executable=sys.executable,
                           gui_model="ui-tars-local")
        save_agent_settings(store, settings)
        self.assertEqual(load_agent_settings(store), settings)
        self.assertFalse(any("api_key" in name for name in store.values))
        with self.assertRaises(AgentValidationError):
            save_agent_settings(store, replace(settings, gui_model=KEY), known_secrets=(KEY,))
        store.values["agents/enabled"] = "surprise"
        self.assertEqual(load_agent_settings(store), replace(AgentSettings(), enabled=False))

    def test_settings_support_qsettings_style_enum_status_without_qt_import(self):
        class Status(Enum):
            NoError = 0
        store = Store()
        store.status = lambda: Status.NoError
        save_agent_settings(store, AgentSettings())
        self.assertEqual(load_agent_settings(store), AgentSettings())

    def test_global_api_ignores_retired_gui_preferences_and_does_not_write_a_second_route(self):
        api = {"base_url": "https://main.example/v1", "model": "main-vision-model"}
        store = Store()
        retired = {"agents/gui_base_url": "https://retired.example/v1?bad=1",
                   "agents/gui_model": object(), "agents/gui_provider": "obsolete",
                   "agents/gui_coordinate_profile": "obsolete"}
        store.values.update(retired)
        settings = load_agent_settings(store, api=api)
        self.assertTrue(settings.enabled)
        self.assertEqual((settings.gui_base_url, settings.gui_model), (api["base_url"], api["model"]))
        self.assertEqual((settings.gui_provider, settings.gui_coordinate_profile), ("ui-tars-1.5", "pixel"))
        save_agent_settings(store, replace(settings, computer_enabled=True), api=api)
        self.assertEqual({name: store.values[name] for name in retired}, retired)
        self.assertTrue(load_agent_settings(store, api=api).computer_enabled)
        # Even ignored legacy fields cannot be used to smuggle a key into a save.
        with self.assertRaises(AgentValidationError):
            save_agent_settings(store, replace(settings, gui_model=KEY), api=api, known_secrets=(KEY,))
        before = dict(store.values)
        shared = shared_api_settings(settings, {**api, "model": "updated-model"})
        self.assertEqual(shared.gui_model, "updated-model")
        self.assertEqual(store.values, before)

    def test_zero_is_unlimited_default_and_positive_compatibility_limits_round_trip(self):
        self.assertEqual(AgentSettings().max_steps, 0)
        for limit in (0, 1, 12, 40):
            with self.subTest(limit=limit):
                store = Store()
                settings = replace(AgentSettings(), max_steps=limit)
                save_agent_settings(store, settings)
                self.assertEqual(load_agent_settings(store).max_steps, limit)
        for limit in (-1, 41, 10**30, False, 0.0, "0"):
            with self.subTest(limit=limit), self.assertRaises(AgentValidationError):
                replace(AgentSettings(), max_steps=limit).validate()

    def test_zero_removes_whole_task_deadline_and_explicit_finite_limits_remain_supported(self):
        self.assertEqual(AgentSettings().timeout_seconds, 0.0)
        for limit in (0, 5, 120, 900):
            with self.subTest(limit=limit):
                store = Store()
                settings = replace(AgentSettings(), timeout_seconds=limit)
                save_agent_settings(store, settings)
                self.assertEqual(load_agent_settings(store).timeout_seconds, limit)
        for limit in (-1, 1, 900.1, True, False, float("inf"), float("nan"), "0"):
            with self.subTest(limit=limit), self.assertRaises(AgentValidationError):
                replace(AgentSettings(), timeout_seconds=limit).validate()

    def test_settings_reject_arbitrary_commands_and_invalid_endpoints(self):
        for settings in (
            AgentSettings(python_executable="cmd.exe /c x"),
            AgentSettings(python_executable=str(Path(sys.executable).with_name("cmd.exe"))),
            AgentSettings(workspace="relative"), AgentSettings(gui_provider="openai"),
            AgentSettings(gui_coordinate_profile="invalid"),
            AgentSettings(gui_coordinate_profile="pixel", gui_provider="ui-tars-1.0"),
            AgentSettings(workspace=str(Path(sys.executable).parent)),
            AgentSettings(gui_base_url="https://user:pass@example.test/v1"),
            AgentSettings(gui_base_url="https://example.test/v1?key=secret"),
            AgentSettings(max_steps=True), AgentSettings(timeout_seconds=float("nan")),
            AgentSettings(enabled=1),
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(AgentValidationError):
                    settings.validate()

    def test_import_construct_and_ordinary_chat_do_not_spawn_or_start_threads(self):
        with patch.object(subprocess, "Popen") as spawn, patch.object(threading.Thread, "start") as start:
            importlib.reload(runtime)
            service = runtime.AgentService()
            self.assertIsNone(service.execute(None, None, None, object()))
            service.shutdown()
            spawn.assert_not_called()
            start.assert_not_called()
        self.assertNotIn("nanobot", sys.modules)
        self.assertNotIn("computer_use", sys.modules)


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-agent-host-test-")
        self.root = Path(self.directory.name)
        self.worker = self.root / "worker.py"
        self.capture = self.root / "request.json"
        self.service = AgentService()
        self.settings = AgentSettings(enabled=True, python_executable=sys.executable)
        self.context = {"api": {"base_url": "http://127.0.0.1:1/v1", "model": "fixture"},
                        "messages": [{"role": "user", "content": "参考 " + KEY}]}
        self.request = AgentRequest("测试任务 " + KEY)
        self.spawned = []
        self.original_popen = subprocess.Popen

    def tearDown(self):
        self.service.shutdown()
        self.directory.cleanup()

    def make_worker(self, body):
        prefix = (
            "import sys,json,time,os\n"
            "print(json.dumps({'type':'ready','protocol':1,'sdk':'nanobot-ai','sdk_version':'0.4.0'}),flush=True)\n"
            "request=json.loads(sys.stdin.readline())\n"
            "task_id=request['id']\n"
            f"open({str(self.capture)!r},'w',encoding='utf-8').write(json.dumps(request))\n"
            "def send(frame):\n"
            "    frame.setdefault('id',task_id)\n"
            "    print(json.dumps(frame),flush=True)\n"
        )
        self.worker.write_text(prefix + body, encoding="utf-8")

    def execute(self, **kwargs):
        def spy(*args, **options):
            process = self.original_popen(*args, **options)
            self.spawned.append((process, args, options))
            return process
        with patch.object(runtime, "_WORKER_PATH", self.worker), patch.object(runtime.subprocess, "Popen", spy):
            return self.service.execute(kwargs.pop("settings", self.settings), kwargs.pop("key", KEY),
                kwargs.pop("request", self.request), kwargs.pop("context", self.context), **kwargs)

    def test_success_uses_fixed_worker_stdin_keys_redaction_and_static_progress(self):
        self.make_worker(
            "send({'type':'progress','phase':'tool','message':request['config']['api_key']})\n"
            "print(request['config']['api_key'],file=sys.stderr)\n"
            "send({'type':'final','reply':'完成 '+request['config']['api_key'],'emotion':'happy'})\n"
        )
        progress = []
        with patch.dict(os.environ, {"UNRELATED_API_KEY": "ambient-key", "OPENAI_BASE_URL": "ambient-url"}):
            result = self.execute(progress_callback=progress.append)
        self.assertEqual(result, AgentResult("完成 [已隐藏密钥]", "happy"))
        self.assertEqual(progress, ["starting", "tool", "completed"])
        payload = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(payload["config"]["api_key"], KEY)
        self.assertEqual(payload["options"]["max_steps"], 0)
        self.assertNotIn(KEY, payload["task"])
        self.assertNotIn(KEY, payload["context"])
        process, args, options = self.spawned[0]
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(args[0], [sys.executable, "-I", "-u", str(self.worker)])
        self.assertNotIn(KEY, repr(args))
        self.assertNotIn("UNRELATED_API_KEY", options["env"])
        self.assertNotIn("OPENAI_BASE_URL", options["env"])
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertIsNotNone(process.poll())

    def test_computer_tasks_reuse_global_api_and_ignore_retired_route_and_credential(self):
        self.make_worker("send({'type':'final','reply':'完成 '+request['config']['gui']['api_key']})\n")
        settings = replace(self.settings, computer_enabled=True, gui_model="retired-vision-model",
                           gui_base_url="https://retired.example/v1", gui_provider="doubao-1.5-20b")
        result = self.execute(settings=settings, request=AgentRequest("桌面测试", "computer"),
                              context=dict(self.context, gui_api_key=GUI_KEY))
        self.assertEqual(result.kind, "computer")
        self.assertNotIn(GUI_KEY, result.reply)
        payload = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(payload["config"]["gui"]["api_key"], KEY)
        self.assertEqual(payload["config"]["gui"]["base_url"], self.context["api"]["base_url"])
        self.assertEqual(payload["config"]["gui"]["model"], self.context["api"]["model"])
        self.assertEqual(payload["config"]["gui"]["provider"], "ui-tars-1.5")
        self.assertEqual(payload["config"]["gui"]["coordinate_profile"], "pixel")
        self.assertNotIn(GUI_KEY, repr(payload))
        self.execute(settings=settings, request=AgentRequest("桌面测试", "computer"),
                     context=self.context)
        payload = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(payload["config"]["gui"]["coordinate_profile"], "pixel")
        self.assertEqual(payload["config"]["gui"]["api_key"], KEY)

    def test_only_the_actual_shared_account_changes_the_session_scope(self):
        from uuid import uuid4
        self.make_worker("send({'type':'final','reply':'done'})\n")
        conversation_id = str(uuid4())
        context = dict(self.context, conversation_id=conversation_id, gui_api_key=GUI_KEY)
        self.execute(context=context)
        original = self.service._session_scopes[conversation_id]
        # An obsolete key supplied by an older internal caller is redacted but
        # must not invalidate facts or change the account used for GUI traffic.
        self.execute(context={**context, "gui_api_key": "different-retired-gui-key"})
        self.assertEqual(self.service._session_scopes[conversation_id], original)
        payload = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("session_history"))
        self.execute(context=context, key="different-actual-main-key")
        self.assertNotEqual(self.service._session_scopes[conversation_id], original)
        payload = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertFalse(payload.get("session_history"))

    def test_disabled_and_invalid_context_do_not_spawn(self):
        cases = [dict(settings=replace(self.settings, enabled=False)),
                 dict(request=AgentRequest("x", "computer")),
                 dict(context=dict(self.context, command="arbitrary")),
                 dict(context=dict(self.context, messages=[{"role": "tool", "content": "x"}]))]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises((AgentError, AgentValidationError)):
                    self.execute(**case)
        self.assertEqual(self.spawned, [])

    def test_progress_cancel_terminates_worker_without_retry(self):
        self.make_worker("send({'type':'progress','phase':'planning'})\ntime.sleep(60)\n")
        event = threading.Event()
        started = time.monotonic()
        with self.assertRaises(AgentError) as caught:
            self.execute(cancellation_event=event,
                         progress_callback=lambda phase: event.set() if phase == "planning" else None)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(self.spawned), 1)
        self.assertIsNotNone(self.spawned[0][0].poll())

    def test_timeout_stops_once_without_retry_or_raw_output(self):
        self.make_worker("send({'type':'progress','phase':'planning'})\ntime.sleep(60)\n")
        clock = [0.0]
        def progress(phase):
            if phase == 'planning': clock[0] = 6.0
        with patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(AgentError) as caught:
                self.execute(settings=replace(self.settings, timeout_seconds=5),progress_callback=progress)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(len(self.spawned), 1)
        self.assertIsNotNone(self.spawned[0][0].poll())

    def test_unlimited_task_finishes_after_clock_passes_all_former_task_limits(self):
        self.make_worker("send({'type':'progress','phase':'planning'})\ntime.sleep(.04)\nsend({'type':'final','reply':'长任务完成'})\n")
        clock = [100.0]
        def progress(phase):
            if phase == "planning":
                clock[0] = 1_000_000.0
        with patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.execute(progress_callback=progress)
        self.assertEqual(result.reply, "长任务完成")
        self.assertEqual(json.loads(self.capture.read_text(encoding="utf-8"))["options"]["timeout_seconds"], 0)
        self.assertEqual(len(self.spawned), 1)

    def test_unlimited_task_still_stops_after_clock_passes_former_time_limits(self):
        self.make_worker("send({'type':'progress','phase':'planning'})\ntime.sleep(60)\n")
        clock, cancel = [100.0], threading.Event()
        def progress(phase):
            if phase == "planning":
                clock[0] = 1_000_000.0
                cancel.set()
        with patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(AgentError) as caught:
                self.execute(progress_callback=progress, cancellation_event=cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(len(self.spawned), 1)
        self.assertIsNotNone(self.spawned[0][0].poll())

    def test_error_after_action_never_retries_or_discloses_provider_error(self):
        self.make_worker(
            "send({'type':'progress','phase':'tool'})\n"
            "send({'type':'error','code':'PROVIDER_FAILURE','message':request['config']['api_key']})\n"
        )
        with self.assertRaises(AgentError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "backend_failed")
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(len(self.spawned), 1)

    def test_shutdown_in_progress_terminates_and_rejects_future_tasks(self):
        self.make_worker("send({'type':'progress','phase':'planning'})\ntime.sleep(60)\n")
        with self.assertRaises(AgentError) as caught:
            self.execute(progress_callback=lambda phase: self.service.shutdown() if phase == "planning" else None)
        self.assertEqual(caught.exception.code, "cancelled")
        with self.assertRaises(AgentError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "closed")
        self.assertEqual(len(self.spawned), 1)

    def test_concurrent_task_is_rejected_until_cancelled_worker_cleanup_finishes(self):
        self.make_worker("send({'type':'progress','phase':'planning'})\ntime.sleep(60)\n")
        started, cancel = threading.Event(), threading.Event()
        outcomes = []
        def first_task():
            try:
                self.execute(cancellation_event=cancel,
                             progress_callback=lambda phase: started.set() if phase == "planning" else None)
            except AgentError as exc:
                outcomes.append(exc.code)
        thread = threading.Thread(target=first_task)
        thread.start()
        try:
            self.assertTrue(started.wait(3), "fake worker did not announce progress")
            with self.assertRaises(AgentError) as caught:
                self.execute()
            self.assertEqual(caught.exception.code, "busy")
            self.assertEqual(len(self.spawned), 1)
            self.assertIsNone(self.service.execute(None, None, None, object()))
        finally:
            cancel.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ["cancelled"])
        self.assertIsNotNone(self.spawned[0][0].poll())
        self.make_worker("send({'type':'final','reply':'新的任务可以启动'})\n")
        result = self.execute()
        self.assertEqual(result.reply, "新的任务可以启动")
        self.assertEqual(len(self.spawned), 2)

    def test_specific_gui_errors_are_preserved_without_worker_exception_text_or_retry(self):
        for code in ("GUI_STEP_LIMIT", "GUI_MODEL_FAILED", "GUI_RUNTIME_MISSING", "COMPUTER_NOT_USED", "AGENT_FAILED", "SDK_UNLIMITED_UNSUPPORTED"):
            with self.subTest(code=code):
                self.make_worker(f"send({{'type':'error','code':{code!r},'message':request['config']['api_key']}})\n")
                before = len(self.spawned)
                with self.assertRaises(AgentError) as caught:
                    self.execute()
                self.assertEqual(caught.exception.code, code.casefold())
                self.assertEqual(str(caught.exception), runtime._ERROR_MESSAGES[code.casefold()])
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(len(self.spawned), before + 1)
        self.make_worker("send({'type':'error','code':'HOSTILE_UNKNOWN','message':request['config']['api_key']})\n")
        with self.assertRaises(AgentError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "backend_failed")
        self.assertNotIn(KEY, str(caught.exception))

    def test_public_activity_is_fixed_labels_and_never_model_reasoning_or_arguments(self):
        self.make_worker(
            "send({'type':'activity','kind':'tool','name':'read_file','event_id':'tool-1','status':'running','detail':request['config']['api_key'],'reasoning':'private'})\n"
            "send({'type':'activity','kind':'tool','name':'read_file','event_id':'tool-1','status':'completed'})\n"
            "send({'type':'activity','kind':'computer','name':'click','event_id':'gui-1-acting','status':'completed','step':1,'text':request['config']['api_key']})\n"
            "send({'type':'final','reply':'done'})\n")
        events = []
        result = self.execute(event_callback=events.append)
        # A successful click event proves dispatch, not the requested outcome.
        self.assertEqual(result.status, "needs_user")
        self.assertIn("未完成", result.reply)
        self.assertEqual([item['label'] for item in events], ['读取文件', '读取文件', '点击界面'])
        self.assertEqual(events[0]['id'], events[1]['id'])
        self.assertEqual(events[0]['status'], 'running')
        self.assertEqual(events[1]['status'], 'completed')
        self.assertNotIn(KEY, json.dumps(events))
        self.assertNotIn('reasoning', json.dumps(events))
        payload = json.loads(self.capture.read_text(encoding='utf-8'))
        self.assertTrue(payload['options']['include_activity'])

    def test_local_subjects_are_bounded_and_redacted_while_raw_arguments_stay_hidden(self):
        self.make_worker(
            "send({'type':'activity','kind':'tool','name':'read_file','event_id':'tool-1','status':'completed','subject':{'kind':'file','name':'需求说明.docx'},'detail':'private raw arguments'})\n"
            "send({'type':'activity','kind':'tool','name':'read_file','event_id':'tool-2','status':'completed','subject':{'kind':'file','name':request['config']['api_key']}})\n"
            "send({'type':'activity','kind':'tool','name':'read_file','event_id':'tool-3','status':'completed','subject':{'kind':'file','name':'C:/private/secret.txt'}})\n"
            "send({'type':'final','reply':'done'})\n")
        events = []
        self.execute(event_callback=events.append)
        self.assertEqual(events[0]["subject"], {"kind": "file", "name": "需求说明.docx"})
        self.assertNotIn(KEY, json.dumps(events, ensure_ascii=False))
        self.assertNotIn("subject", events[2])
        self.assertNotIn("private raw arguments", json.dumps(events))

    def test_full_model_context_crosses_old_message_character_and_request_caps(self):
        self.make_worker("send({'type':'final','reply':'done'})\n")
        messages = [{'role':'user','content': str(i) + 'x' * 5000} for i in range(240)]
        self.execute(context=dict(self.context, messages=messages))
        payload = json.loads(self.capture.read_text(encoding='utf-8'))
        self.assertEqual(len(json.loads(payload['context'])), 240)
        self.assertGreater(len(payload['context']), 1_048_576)

    def test_hostile_protocol_is_data_and_rejected_without_retry(self):
        bodies = [
            "print('__import__(\"os\").system(\"ignored\")',flush=True)\n",
            "send({'type':'final','id':'wrong','reply':'x'})\n",
            "send({'type':'command','command':'ignored'})\n",
            "send({'type':'progress','phase':request['config']['api_key']})\n",
            "send({'type':'final','reply':'x','emotion':['happy']})\n",
            "print('{\"type\":\"ready\",\"type\":\"final\"}',flush=True)\n",
            "print('x'*128001,flush=True)\n",
        ]
        for body in bodies:
            with self.subTest(body=body[:70]):
                before = len(self.spawned)
                self.make_worker(body)
                with self.assertRaises(AgentError) as caught:
                    self.execute()
                self.assertEqual(caught.exception.code, "protocol")
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(len(self.spawned), before + 1)


if __name__ == "__main__":
    unittest.main()
