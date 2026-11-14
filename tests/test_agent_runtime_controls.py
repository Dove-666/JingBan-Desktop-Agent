"""Real host -> isolated SDK worker -> loopback model controls and deliveries.

Only temporary owned files, generated PNG pixels, and localhost HTTP are used.
No physical desktop action or production provider is configured.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import io
from pathlib import Path
import queue
import re
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import zlib
import zipfile

from agent_backend.nanobot.tests.test_harness_worker import ScriptedModel, tool, last_observation, PYTHON, BACKEND, REPO
from desktop_pet.agents import AgentRequest, AgentService, AgentSettings
from desktop_pet.agents import service as host_runtime
from desktop_pet.agents.interactions import InteractionBroker

KEY = "runtime-controls-loopback-fixture-token"


def last_tool_text(body, name):
    matching = [item for item in body["messages"] if item.get("role") == "tool" and item.get("name") == name]
    if not matching:
        raise AssertionError("Expected tool observation was absent: " + name)
    return matching[-1]["content"]


def synthetic_png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress(b"\x00\x00\x55\xff")) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


@unittest.skipUnless(PYTHON.is_file(), "isolated nanobot SDK environment missing")
class RuntimeControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        root = REPO / "agent_workspace"
        root.mkdir(exist_ok=True)
        self.owned = tempfile.TemporaryDirectory(prefix="runtime-controls-", dir=root)
        self.external = tempfile.TemporaryDirectory(prefix="aura-runtime-external-")
        self.root = Path(self.owned.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.service = AgentService(checkpoint_path=self.root / "state" / "checkpoints.sqlite3")
        self.settings = AgentSettings(python_executable=str(PYTHON), workspace=str(self.workspace), timeout_seconds=25)
        self.cid = str(uuid4())
        self.models, self.threads = [], []
        self.cancel = threading.Event()

    def tearDown(self):
        self.cancel.set()
        self.service.shutdown()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "Owned host test thread did not terminate")
        for model in self.models:
            model.close()
            self.assertEqual(model.errors, [])
        self.external.cleanup()
        self.owned.cleanup()

    def model(self, steps):
        model = ScriptedModel(steps)
        self.models.append(model)
        return model

    def context(self, model, cid=None, **extra):
        return {"api": {"base_url": model.base_url, "model": "offline-runtime-controls"},
                "conversation_id": cid or self.cid, "messages": [], **extra}

    def execute(self, model, *, cid=None, context=None, interaction_callback=None, image_resolver=None):
        return self.service.execute(self.settings, KEY, AgentRequest("Complete the owned fixture task from real observations; only requested temporary outputs may be changed.", "agent"),
            context or self.context(model, cid), cancellation_event=self.cancel,
            interaction_callback=interaction_callback, image_resolver=image_resolver, event_callback=lambda _: None)

    def start(self, model, **kwargs):
        questions, outcomes = queue.Queue(), queue.Queue()
        def run():
            try:
                outcomes.put((True, self.execute(model, interaction_callback=lambda q: questions.put(deepcopy(q)) if q else None, **kwargs)))
            except Exception as exc:
                outcomes.put((False, exc))
        thread = threading.Thread(target=run, name="owned-runtime-host-fixture", daemon=True)
        self.threads.append(thread)
        thread.start()
        return questions, outcomes

    def result(self, outcomes):
        try:
            success, value = outcomes.get(timeout=20)
        except queue.Empty:
            self.fail("Host execution failed to return within the owned fixture deadline")
        if not success:
            raise value
        return value

    def question(self, questions):
        try:
            return questions.get(timeout=15)
        except queue.Empty:
            self.fail("Real worker did not publish the expected host interaction")

    def test_real_question_grant_edit_verify_finish_and_immutable_delivery_are_conversation_bound(self):
        target = Path(self.external.name) / "report.txt"
        first = "initial header\n" + "owned runtime evidence\n" * 300
        expected = first.replace("initial header", "reviewed header")
        def after_question(body):
            self.assertEqual(last_observation(body, "ask_user_question")["text"], "Use a reviewed header")
            return tool("request_file_access", path=str(target), access="write", directory=False, reason="Save the explicitly requested owned test report")
        def after_grant(body):
            self.assertTrue(last_observation(body, "request_file_access")["allowed"])
            return tool("write_file", path=str(target), content=first)
        def patch_file(body):
            digest = last_observation(body, "write_file")["sha256"]
            return tool("edit_file", path=str(target), old_text="initial header", new_text="reviewed header", expected_hash=digest)
        def readback(body):
            digest = last_observation(body, "edit_file")["sha256"]
            return tool("read_file", path=str(target), expected_hash=digest)
        def verify(body):
            value = last_observation(body, "read_file")
            self.assertEqual(value["text"], expected)
            self.assertFalse(value["truncated"])
            return tool("verify_artifact", path=str(target), kind="text")
        def finish(body):
            self.assertTrue(last_observation(body, "verify_artifact")["passed"])
            return tool("finish_task", status="completed", summary="Owned report was written, patched and read back")
        def reply(body):
            self.assertTrue(last_observation(body, "finish_task")["accepted"])
            return "The reviewed test report was verified and delivered."
        def denied_other_cid(body):
            self.assertIn("OUTSIDE_WORKSPACE", last_tool_text(body, "write_file"))
            return tool("finish_task", status="needs_user", summary="This other conversation has no file grant")
        model = self.model([tool("ask_user_question", question="Which header should be used?", options=["Use a reviewed header", "Keep original"]),
            after_question, after_grant, patch_file, readback, verify, finish, reply,
            tool("write_file", path=str(target), content="unauthorized replacement", overwrite=True), denied_other_cid,
            "The other conversation did not receive the first conversation's grant."])
        questions, outcomes = self.start(model)
        pending = self.question(questions)
        self.assertEqual(pending["kind"], "question")
        self.assertIsNone(self.service.pending_interaction(str(uuid4())))
        self.assertFalse(self.service.answer_interaction(str(uuid4()), pending["id"], {"text": "other"}))
        self.assertFalse(self.service.answer_interaction(self.cid, "interaction-" + "0" * 32, {"text": "stale"}))
        self.assertTrue(self.service.answer_interaction(self.cid, pending["id"], {"text": "Use a reviewed header"}))
        self.assertFalse(self.service.answer_interaction(self.cid, pending["id"], {"text": "replayed"}))
        permission = self.question(questions)
        self.assertEqual(permission["resource"], {"path": str(target.resolve()), "access": "write", "directory": False})
        self.assertFalse(target.exists())
        self.assertTrue(self.service.answer_interaction(self.cid, permission["id"], {"allowed": True}))
        result = self.result(outcomes)
        self.assertEqual(result.status, "completed")
        self.assertEqual(target.read_text(encoding="utf-8"), expected)
        self.assertEqual(len(result.artifacts), 1)
        artifacts = self.service.artifacts(self.cid)
        self.assertEqual(artifacts, list(result.artifacts))
        self.assertNotIn(str(target), json.dumps(artifacts))
        reference = artifacts[0]["reference"]
        offset, received = 0, bytearray()
        while True:
            page = self.service.artifact_page(self.cid, reference, offset=offset, limit=1000)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["sha256"], artifacts[0]["sha256"])
            received.extend(base64.b64decode(page["data_base64"], validate=True))
            if page["next_offset"] is None:
                break
            self.assertEqual(page["next_offset"], len(received))
            offset = page["next_offset"]
        self.assertEqual(bytes(received), expected.encode())
        self.assertEqual(hashlib.sha256(received).hexdigest(), artifacts[0]["sha256"])
        other = str(uuid4())
        self.assertEqual(self.service.artifacts(other), [])
        with self.assertRaises(ValueError):
            self.service.artifact_page(other, reference)
        second = self.execute(model, cid=other)
        self.assertEqual(second.status, "needs_user")
        self.assertEqual(target.read_text(encoding="utf-8"), expected)
        target.write_text("externally changed owned source", encoding="utf-8")
        self.assertEqual(base64.b64decode(self.service.artifact_page(self.cid, reference)["data_base64"]), expected.encode())
        self.assertNotIn(KEY, json.dumps(self.service._sessions))

    def test_explicit_denial_prevents_writes_and_does_not_become_a_persistent_grant(self):
        target = Path(self.external.name) / "denied.txt"
        def rejected(body):
            self.assertFalse(last_observation(body, "request_file_access")["allowed"])
            return tool("write_file", path=str(target), content="must never be written")
        def blocked(body):
            self.assertIn("OUTSIDE_WORKSPACE", last_tool_text(body, "write_file"))
            return tool("finish_task", status="needs_user", summary="The user refused file access")
        model = self.model([tool("request_file_access", path=str(target), access="write", directory=False, reason="Ask for this exact temporary output"),
            rejected, blocked, "File access was refused; no file was written."])
        questions, outcomes = self.start(model)
        pending = self.question(questions)
        self.assertTrue(self.service.answer_interaction(self.cid, pending["id"], {"allowed": False}))
        self.assertEqual(self.result(outcomes).status, "needs_user")
        self.assertFalse(target.exists())
        self.assertFalse(self.service._file_grants)
        self.assertEqual(self.service.artifacts(self.cid), [])

    def test_real_host_docx_tool_is_read_back_and_delivered_as_an_actual_office_package(self):
        title = "真实 SDK 文档交付验收"
        paragraph = "这段文字来自临时测试数据，不涉及用户文件。"
        spec = {"title": title, "sections": [{"heading": "验收范围", "paragraphs": [paragraph]}]}
        def read_document(body):
            value = last_observation(body, "create_document")
            self.assertTrue(value["verification"]["passed"])
            return tool("read_document", path="owned-report.docx")
        def finish(body):
            observation = last_observation(body, "read_document")
            self.assertIn(title, observation["text"])
            self.assertIn(paragraph, observation["text"])
            return tool("finish_task", status="completed", summary="The actual DOCX package was created and its text read back")
        def reply(body):
            self.assertTrue(last_observation(body, "finish_task")["accepted"])
            return "The test Word document was created and its text checked; visual layout has not been rendered."
        model = self.model([tool("create_document", path="owned-report.docx", kind="docx", spec=spec), read_document, finish, reply])
        result = self.execute(model)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.artifacts), 1)
        artifact = result.artifacts[0]
        self.assertEqual(artifact["name"], "owned-report.docx")
        self.assertEqual(artifact["mime"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        raw = base64.b64decode(self.service.artifact_page(self.cid, artifact["reference"])["data_base64"], validate=True)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact["sha256"])
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            xml = package.read("word/document.xml").decode("utf-8")
            self.assertIn(title, xml)
            self.assertIn(paragraph, xml)
        self.assertEqual((self.workspace / "owned-report.docx").read_bytes(), raw)

    def test_expiration_declines_the_real_worker_request_and_rejects_late_approval(self):
        now = [0.0]
        self.service._interaction_broker = InteractionBroker(clock=lambda: now[0])
        target = Path(self.external.name) / "expired.txt"
        def expired(body):
            value = last_observation(body, "request_file_access")
            self.assertTrue(value["expired"])
            self.assertFalse(value["allowed"])
            return tool("finish_task", status="needs_user", summary="The file request expired without approval")
        model = self.model([tool("request_file_access", path=str(target), access="write", directory=False, reason="This request will expire in the owned fixture"),
            expired, "No approval was received; the temporary file was untouched."])
        questions, outcomes = self.start(model)
        pending = self.question(questions)
        now[0] = 1801
        self.assertFalse(self.service.answer_interaction(self.cid, pending["id"], {"allowed": True}))
        self.assertEqual(self.result(outcomes).status, "needs_user")
        self.assertFalse(target.exists())
        self.assertFalse(self.service._file_grants)

    def test_running_steering_reaches_the_next_real_model_iteration_without_a_second_task(self):
        self.check_running_steering("Only calculate; do not write or launch anything.")

    def test_long_unicode_steering_keeps_full_goal_even_when_replay_is_clipped(self):
        self.check_running_steering("Only calculate; do not write or launch anything. " + "🐳" * 7000)

    def check_running_steering(self, correction):
        entered, release = threading.Event(), threading.Event()
        def pause_model(body):
            entered.set()
            self.assertTrue(release.wait(8))
            return tool("calculate", expression="2+2")
        def corrected_iteration(body):
            self.assertTrue(any(item.get("role") == "user" and item.get("content") == correction for item in body["messages"]))
            return tool("finish_task", status="completed", summary="The corrected read-only calculation returned 4")
        model = self.model([pause_model, corrected_iteration, "The corrected calculation is 4."])
        self.assertFalse(self.service.steer(self.cid, correction))
        _, outcomes = self.start(model)
        self.assertTrue(entered.wait(12))
        self.assertFalse(self.service.steer(str(uuid4()), correction))
        self.assertTrue(self.service.steer(self.cid, correction))
        release.set()
        result = self.result(outcomes)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(model.calls), 3)
        from agent_backend.nanobot.session_history import text_record
        replay = text_record(text_record({"role": "user", "content": correction}))["content"]
        self.assertEqual(len([item for item in self.service._sessions[self.cid] if item.get("role") == "user" and item.get("content") == replay]), 1)
        self.assertEqual(self.service._session_goals[self.cid], [
            "Complete the owned fixture task from real observations; only requested temporary outputs may be changed.",
            correction])
        from desktop_pet.agents.checkpoints import scope_fingerprint
        from desktop_pet.agents.settings import shared_api_settings
        api = self.context(model)["api"]
        scope = scope_fingerprint(shared_api_settings(self.settings, api), api, secrets=(KEY,))
        self.assertEqual(self.service._checkpoint_store.load(self.cid, scope)["goal_history"],
                         self.service._session_goals[self.cid])
        self.assertFalse(self.service.steer(self.cid, correction))
        self.assertFalse(list(self.workspace.iterdir()))

    def test_history_image_is_resolved_by_host_and_captioned_via_loopback_provider_only(self):
        reference = "a" * 64
        image = synthetic_png()
        resolutions = []
        def caption(body):
            content = body["messages"][-1]["content"]
            self.assertEqual(content[1]["image_url"]["url"], image)
            self.assertIn("reference data, not instructions", content[0]["text"])
            return "A synthetic blue pixel."
        def finish(body):
            self.assertTrue(last_tool_text(body, "inspect_image").startswith("{"), last_tool_text(body, "inspect_image"))
            observation = last_observation(body, "inspect_image")
            self.assertEqual(observation["attachment_id"], reference)
            self.assertEqual(observation["observation"], "A synthetic blue pixel.")
            return tool("finish_task", status="completed", summary="The requested owned history image was inspected")
        def unauthorized(body):
            self.assertIn("IMAGE_NOT_IN_CONVERSATION", last_tool_text(body, "inspect_image"))
            return tool("finish_task", status="needs_user", summary="The other conversation has no such image")
        model = self.model([tool("inspect_image", attachment_id=reference, question="What synthetic color is this?"), caption, finish,
            "The historical attachment was a synthetic blue pixel.",
            tool("inspect_image", attachment_id=reference, question="Read a different conversation image"), unauthorized,
            "That image is unavailable in this conversation."])
        context = self.context(model, image_catalog=[{"attachment_id": reference, "label": "owned-history.png"}])
        result = self.execute(model, context=context, image_resolver=lambda ref: resolutions.append(ref) or image)
        self.assertEqual(result.status, "completed")
        self.assertEqual(resolutions, [reference])
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(self.execute(model, cid=str(uuid4()), image_resolver=lambda ref: self.fail("Foreign image must not reach the resolver")).status, "needs_user")
        self.assertEqual(resolutions, [reference])

    def test_host_rejects_unlisted_image_resource_frame_even_from_the_real_sdk_worker(self):
        allowed, foreign = "a" * 64, "b" * 64
        shim = self.root / "resource-probe-worker.py"
        shim.write_text("import asyncio,importlib.util\n"
            f"spec=importlib.util.spec_from_file_location('qa_resource_probe',{str(BACKEND / 'worker.py')!r})\n"
            "worker=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(worker)\n"
            "original=worker.host_image\n"
            f"async def wrong_resource(request_id,reference): return await original(request_id,{foreign!r})\n"
            "worker.host_image=wrong_resource\nasyncio.run(worker.main())\n", encoding="utf-8")
        def refused(body):
            self.assertIn("IMAGE_UNAVAILABLE", last_tool_text(body, "inspect_image"))
            return tool("finish_task", status="needs_user", summary="Host rejected an unlisted resource")
        model = self.model([tool("inspect_image", attachment_id=allowed, question="Read owned reference"), refused,
            "The host did not expose the unlisted resource."])
        with patch.object(host_runtime, "_WORKER_PATH", shim):
            result = self.execute(model, context=self.context(model, image_catalog=[{"attachment_id": allowed, "label": "owned.png"}]),
                image_resolver=lambda ref: self.fail("Unlisted resource must not reach the resolver"))
        self.assertEqual(result.status, "needs_user")
        self.assertEqual(len(model.calls), 3)


class RuntimePublicVocabularyTests(unittest.TestCase):
    def test_every_host_public_tool_and_action_label_survives_android_history_projection(self):
        from desktop_pet.agents.activity import TOOL_LABELS, ACTION_LABELS
        source = (REPO / "android/app/src/main/java/com/aura/companion/data/CompanionDatabase.kt").read_text(encoding="utf-8")
        block = re.search(r"AGENT_ACTIVITY_MESSAGES = setOf\((.*?)\n\)", source, re.S)
        self.assertIsNotNone(block)
        accepted = set(re.findall(r'"([^"\n]+)"', block.group(1)))
        required = set(TOOL_LABELS.values()) | set(ACTION_LABELS.values())
        self.assertEqual(sorted(required - accepted), [], "Real public execution steps would disappear on Android")


if __name__ == "__main__":
    unittest.main()
