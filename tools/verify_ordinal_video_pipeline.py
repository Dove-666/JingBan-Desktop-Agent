"""Isolated ordinal-video acceptance with real NutJS input and GDI capture.

The planner/action model and pixel judge are loopback fixtures, not real LLMs.
All native input is QA-fenced to fixed buttons in our opaque fullscreen window.
Production code and user settings are never modified.
"""
from __future__ import annotations

import base64
import argparse
import ctypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QObject, QPoint, QTimer, Qt, Signal, QSettings
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop_pet.agents import AgentRequest, AgentService, AgentSettings
from desktop_pet.agents import service as host_module
from desktop_pet.agents.checkpoints import CheckpointStore, scope_fingerprint

KEY = "ordinal-loopback-fixture-only"
TASK = "在 AURA 独立视频测试窗口里，从首页第一条切到第二个视频 VIDEO-BETA，给第二个视频点赞，保持第一个视频 VIDEO-ALPHA 未点赞，并确认第二条心形按钮已变红。"
CASES = {"correct_second": ["next", "like"], "wrong_first": ["like", "next"],
         "missed_like": ["next", "noop"], "missing_first_anchor": ["like"]}


def images_from(body):
    return [part["image_url"]["url"] for message in body.get("messages", [])
            if isinstance(message.get("content"), list) for part in message["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"]


def pixel_state(encoded):
    raw = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
    image = QImage.fromData(raw, "PNG")
    if image.isNull():
        raise ValueError("invalid_fixture_png")
    colors = {"anchor": 0, "first": 0, "second": 0, "third": 0,
              "liked": 0, "unliked": 0, "first_liked": 0, "first_unliked": 0}
    for y in range(0, image.height(), 8):
        for x in range(0, image.width(), 8):
            c = image.pixelColor(x, y)
            rgb = (c.red(), c.green(), c.blue())
            for name, target in [("anchor", (211, 17, 219)), ("first", (35, 104, 184)),
                    ("second", (37, 213, 223)), ("third", (235, 177, 37)),
                    ("liked", (237, 21, 44)), ("unliked", (240, 240, 240)),
                    ("first_liked", (249, 103, 16)), ("first_unliked", (66, 78, 96))]:
                colors[name] += rgb == target
    visible = colors["anchor"] > 50
    video = 1 if colors["first"] > 120 else 2 if colors["second"] > 120 else 3 if colors["third"] > 120 else None
    return {"visible": visible, "video": video, "liked": colors["liked"] > 80,
            "first_liked": colors["first_liked"] > 60, "width": image.width(),
            "height": image.height()}, raw if visible else None


class FixtureModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    lock = threading.Lock()
    behavior = "correct_second"
    action_index = 0
    points = {}
    foreground_guard = staticmethod(lambda: False)
    requests, judges, errors, native_inputs = [], [], [], []
    artifact = None
    allow_button_rectangles = False

    def log_message(self, *_):
        pass

    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            if not self.foreground_guard():
                raise ValueError("fixture_foreground_lost")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 40 * 1024 * 1024:
                raise ValueError("fixture_request_size")
            body = json.loads(self.rfile.read(length))
            if self.path == "/fixture/guard":
                point = body.get("point")
                if point is not None:
                    if not isinstance(point, dict):
                        raise ValueError("fixture_bad_point")
                    width, height = body.get("screenWidth"), body.get("screenHeight")
                    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
                        raise ValueError("fixture_input_domain_missing")
                    permitted = []
                    for name, p in self.points.items():
                        logical_x = point.get("x", -10000) / width * p["screen_width"]
                        logical_y = point.get("y", -10000) / height * p["screen_height"]
                        allowed = (p["rect"][0] + 3 <= logical_x <= p["rect"][2] - 3
                                   and p["rect"][1] + 3 <= logical_y <= p["rect"][3] - 3)
                        if (allowed if self.allow_button_rectangles else abs(logical_x - p["x"]) < 4 and abs(logical_y - p["y"]) < 4):
                            permitted.append(name)
                    if len(permitted) != 1:
                        self.errors.append({"code": "own_fixture_point_mismatch", "actual": point, "expected": self.points})
                        raise ValueError("fixture_input_outside_whitelist")
                    self.native_inputs.append({"behavior": self.behavior, "target": permitted[0]})
                return self.send_json({"ok": True})
            if self.path != "/v1/chat/completions":
                raise ValueError("fixture_wrong_path")
            messages = body["messages"]
            judge = any("independent read-only desktop RESULT verifier" in str(m.get("content", "")) for m in messages if m.get("role") == "system")
            call = None
            if judge:
                sequence = [pixel_state(encoded)[0] for encoded in images_from(body)]
                final = sequence[-1] if sequence else {}
                anchor = any(s.get("video") == 1 and not s.get("liked") for s in sequence[:-1])
                target = final.get("visible") and final.get("video") == 2
                state = target and final.get("liked") and not final.get("first_liked")
                outcome = "passed" if state and anchor else "unknown" if state and not anchor else "failed"
                content = json.dumps({"outcome": outcome, "all_requirements_observed": bool(state and anchor),
                    "target_identity_confirmed": bool(target), "temporal_requirements_observed": bool(anchor),
                    "confidence": "high" if final.get("visible") else "low", "checks": [
                        {"requirement": "First feed observed before second feed", "observed": "first ALPHA then BETA visible" if anchor else "first feed anchor unavailable", "passed": bool(anchor)},
                        {"requirement": "Second BETA red heart and first ALPHA unchanged", "observed": "BETA liked and ALPHA unliked" if state else "requested liked state not achieved", "passed": bool(state)}]})
                record = {"behavior": self.behavior, "sequence": sequence, "outcome": outcome,
                          "original_task_present": TASK in str(messages)}
                self.judges.append(record)
                for i, encoded in enumerate(images_from(body)):
                    evidence, png = pixel_state(encoded)
                    if png:
                        (self.artifact / f"judge-{self.behavior}-{i + 1:02}.png").write_bytes(png)
            elif body.get("model") == "ordinal-action-model":
                encoded = images_from(body)[-1]
                image = QImage.fromData(base64.b64decode(encoded.split(",", 1)[1]), "PNG")
                with self.lock:
                    index = self.action_index
                    type(self).action_index += 1
                actions = CASES[self.behavior]
                if index >= len(actions):
                    content = "Thought: scripted action model claims completion\nAction: finished()"
                else:
                    point = self.points[actions[index]]
                    x = round(point["x"] / point["screen_width"] * image.width())
                    y = round(point["y"] / point["screen_height"] * image.height())
                    content = f"Thought: fixed own-fixture action {actions[index]}\nAction: click(start_box='({x},{y})')"
                self.requests.append({"behavior": self.behavior, "kind": "action", "index": index,
                    "image_size": [image.width(), image.height()], "requested_action": actions[index] if index < len(actions) else "finished"})
            else:
                latest = max(i for i, m in enumerate(messages) if m.get("role") == "user")
                observations = [m for m in messages[latest + 1:] if m.get("role") == "tool"]
                if not observations:
                    name, arguments = "computer_use", {"task": TASK}
                elif len(observations) == 1:
                    name, arguments = "finish_task", {"status": "completed", "summary": "scripted planner claims success"}
                else:
                    name, arguments = None, None
                if name:
                    call = {"id": "ordinal-" + str(len(observations)), "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}
                content = None if call else "规划模型声称第二个视频已点赞，全部完成。"
                self.requests.append({"behavior": self.behavior, "kind": "planner", "tool": name})
            message = {"role": "assistant", "content": content}
            if call:
                message["tool_calls"] = [call]
            finish = "tool_calls" if call else "stop"
            if body.get("stream"):
                delta = dict(message)
                if call:
                    delta["tool_calls"] = [{"index": 0, **call}]
                chunks = [{"id": "ordinal-fixture", "object": "chat.completion.chunk", "created": 1,
                    "model": "offline", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "ordinal-fixture", "object": "chat.completion.chunk", "created": 1,
                    "model": "offline", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
                data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json({"id": "ordinal-fixture", "object": "chat.completion", "created": 1,
                    "model": "offline", "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
        except Exception as error:
            code = str(error) if isinstance(error, ValueError) and str(error).startswith("fixture_") else "fixture_request_rejected"
            self.errors.append({"type": type(error).__name__, "code": code})
            try:
                self.send_json({"error": {"message": "fixture_request_rejected"}}, 400)
            except ConnectionError:
                pass


class Events(QObject):
    reset = Signal(str)
    done = Signal(object)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-configured-gui", action="store_true", help="Use configured GUI provider for one strictly input-fenced real-model fixture task")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Windows required")
    artifact = ROOT / ("artifacts/gui_video_followup_20261101/ordinal_configured_gui" if args.run_configured_gui
                       else "artifacts/gui_video_followup_20261101/ordinal_pipeline")
    artifact.mkdir(parents=True, exist_ok=True)
    workspace = ROOT / "agent_workspace" / ("qa-ordinal-video-" + uuid4().hex)
    workspace.mkdir(parents=True)
    database = artifact / ("checkpoint-" + uuid4().hex + ".sqlite3")
    app = QApplication(sys.argv)
    if len(app.screens()) != 1 or app.platformName() != "windows":
        raise SystemExit("Single interactive Windows monitor required")
    configured, gui_key, settings_path, settings_before = None, KEY, None, None
    if args.run_configured_gui:
        from desktop_pet.settings_manager import SettingsManager
        import hashlib
        settings_path = Path(os.environ["LOCALAPPDATA"]) / "AURA Desktop Pet/settings.ini"
        if not settings_path.is_file():
            raise SystemExit("Existing GUI configuration required")
        settings_before = hashlib.sha256(settings_path.read_bytes()).hexdigest()
        manager = SettingsManager(QSettings(str(settings_path), QSettings.IniFormat))
        configured = manager.load_agent_settings()
        gui_key = manager.get_api_key()
        if not configured.enabled or not configured.computer_enabled or not configured.gui_model or not gui_key:
            raise SystemExit("Configured GUI permissions/model/credential not ready")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
    previous = user32.GetForegroundWindow()
    fixture = QWidget()
    fixture.setWindowTitle("AURA isolated ordinal video fixture")
    fixture.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    fixture.setStyleSheet("QWidget {background:#11243a;color:white;font-size:25px;} QPushButton {padding:14px;}")
    layout = QVBoxLayout(fixture)
    layout.setContentsMargins(80, 45, 80, 45)
    marker = QLabel("AURA QA · OWN ISOLATED VIDEO FEED · NOT A REAL ACCOUNT")
    marker.setStyleSheet("background:#d311db;padding:20px;")
    layout.addWidget(marker)
    video = QLabel()
    video.setAlignment(Qt.AlignCenter)
    video.setMinimumHeight(230)
    layout.addWidget(video)
    like = QPushButton()
    like.setAccessibleName("AURA ordinal like current video")
    like.setMinimumHeight(150)
    layout.addWidget(like)
    first_state = QLabel()
    first_state.setMinimumHeight(90)
    first_state.setAlignment(Qt.AlignCenter)
    layout.addWidget(first_state)
    row = QHBoxLayout()
    next_button = QPushButton("NEXT VIDEO / 下一条")
    noop = QPushButton("NO-OP LIKE / 未生效按钮")
    for widget in (next_button, noop):
        widget.setMinimumHeight(80)
        row.addWidget(widget)
    layout.addLayout(row)
    layout.addWidget(QLabel("Local fixture only. Input fenced to these exact buttons. No production settings or accounts."))
    state = {"video": 1, "liked": [False, False, False], "clicks": []}
    def render():
        index = state["video"]
        labels = ["VIDEO-ALPHA · FIRST / 第一条 · 作者 Alice · 夜间海岸", "VIDEO-BETA · SECOND / 第二条 · 作者 Bob · 晨间鲸鱼", "VIDEO-GAMMA · THIRD / 第三条 · 作者 Carol · 阳光港湾"]
        color = ["#2368b8", "#25d5df", "#ebb125"][index - 1]
        video.setText(labels[index - 1])
        video.setStyleSheet("background:" + color + ";color:#081c2b;font-size:34px;padding:22px;")
        liked = state["liked"][index - 1]
        like.setText("♥  LIKED / 已点赞 / RED HEART" if liked else "♡  NOT LIKED / 未点赞 / WHITE HEART")
        like.setStyleSheet("background:" + ("#ed152c" if liked else "#f0f0f0") + ";color:#071c30;font-size:37px;")
        first_state.setText("FIRST VIDEO-ALPHA: LIKED / 第一条已点赞" if state["liked"][0] else "FIRST VIDEO-ALPHA: NOT LIKED / 第一条未点赞")
        first_state.setStyleSheet("background:" + ("#f96710" if state["liked"][0] else "#424e60") + ";color:white;padding:12px;")
        fixture.repaint()
    def clicked(name):
        state["clicks"].append(name)
        if name == "next": state["video"] = min(3, state["video"] + 1)
        if name == "like": state["liked"][state["video"] - 1] = not state["liked"][state["video"] - 1]
        render()
    next_button.clicked.connect(lambda: clicked("next"))
    like.clicked.connect(lambda: clicked("like"))
    noop.clicked.connect(lambda: clicked("noop"))
    events, reset_done = Events(), threading.Event()
    result_code, focus_reports = [1], []
    handle = int(fixture.winId())
    def focus():
        fixture.raise_(); fixture.activateWindow()
        foreground = user32.GetForegroundWindow()
        thread = user32.GetWindowThreadProcessId(foreground, None)
        current = kernel32.GetCurrentThreadId()
        attached = bool(thread and thread != current and user32.AttachThreadInput(current, thread, True))
        try:
            user32.BringWindowToTop(handle); user32.SetForegroundWindow(handle)
        finally:
            if attached: user32.AttachThreadInput(current, thread, False)
        focus_reports.append({"own_fixture_foreground": user32.GetForegroundWindow() == handle})
    def reset(behavior):
        state.update(video=2 if behavior == "missing_first_anchor" else 1, liked=[False, False, False], clicks=[])
        render(); focus()
        FixtureModel.action_index = 0
        FixtureModel.behavior = behavior
        screen = app.primaryScreen().geometry()
        FixtureModel.points = {}
        for name, widget in [("next", next_button), ("like", like), ("noop", noop)]:
            point = widget.mapToGlobal(widget.rect().center())
            corner = widget.mapToGlobal(QPoint(0, 0))
            FixtureModel.points[name] = {"x": point.x(), "y": point.y(),
                "screen_width": screen.width(), "screen_height": screen.height(),
                "rect": [corner.x(), corner.y(), corner.x() + widget.width(), corner.y() + widget.height()]}
        QTimer.singleShot(200, reset_done.set)
    events.reset.connect(reset)
    FixtureModel.foreground_guard = staticmethod(lambda: user32.GetForegroundWindow() == handle and fixture.isVisible())
    FixtureModel.requests, FixtureModel.judges, FixtureModel.errors, FixtureModel.native_inputs = [], [], [], []
    FixtureModel.artifact = artifact
    FixtureModel.allow_button_rectangles = args.run_configured_gui
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
    server.daemon_threads, server.block_on_close = True, False
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    node_root = ROOT / "agent_backend/ui_tars"
    # The wrapper changes only test-owned process entrypoints. Input remains
    # real NutJS, but this subclass rejects every non-whitelisted native input.
    node_script = workspace / "ordinal-worker.mjs"
    production_entry = (node_root / "worker.mjs").read_text(encoding="utf-8")
    prefix = f"""
const {{ loadRuntime, createSdkAgent: make }} = await import({json.dumps((node_root / 'sdk-adapter.mjs').as_uri())});
const {{ parseBoxToScreenCoords }} = await import({json.dumps((node_root / 'node_modules/@ui-tars/sdk/dist/core.js').as_uri())});
const guard = async (point,screenWidth,screenHeight) => {{ const r=await fetch({json.dumps(endpoint + '/fixture/guard')},{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{point,screenWidth,screenHeight}})}});if(!r.ok || !(await r.json()).ok)throw new Error('QA_FIXTURE_GUARD'); }};
const runtime = await loadRuntime();
const Native = runtime.NutJSOperator;
runtime.NutJSOperator = class extends Native {{ async execute(params) {{
 if(params.parsedPrediction.action_type !== 'click') throw new Error('QA_ACTION_REJECTED');
 const point=parseBoxToScreenCoords({{boxStr:params.parsedPrediction.action_inputs.start_box,screenWidth:params.screenWidth,screenHeight:params.screenHeight}});
 await guard(point,params.screenWidth,params.screenHeight); return super.execute(params);
}} }};
const capture = runtime.captureScreen; runtime.captureScreen=async(...args)=>{{await guard();return await capture(...args);}};
const desktop=runtime.checkDesktop; runtime.checkDesktop=async(...args)=>{{await guard();await desktop(...args);await guard();}};
const createSdkAgent = (request) => make(request, runtime);
"""
    # Resolve package export through createRequire anchored at the installed
    # package, avoiding assumptions about its distribution directory layout.
    prefix = prefix.replace(f"const {{ parseBoxToScreenCoords }} = await import({json.dumps((node_root / 'node_modules/@ui-tars/sdk/dist/core.js').as_uri())});",
        f"const {{ createRequire }} = await import('node:module'); const {{ pathToFileURL }} = await import('node:url'); const qaRequire=createRequire({json.dumps((node_root / 'package.json').as_uri())}); const {{ parseBoxToScreenCoords }}=await import(pathToFileURL(qaRequire.resolve('@ui-tars/sdk/core')).href);")
    production_entry = production_entry.replace("const { BridgeController } = await import('./bridge.mjs');", f"const {{ BridgeController }} = await import({json.dumps((node_root / 'bridge.mjs').as_uri())});")
    production_entry = production_entry.replace("const { createSdkAgent } = await import('./sdk-adapter.mjs');", prefix)
    node_script.write_text(production_entry, encoding="utf-8")
    python_script = workspace / "ordinal_worker.py"
    python_script.write_text("import asyncio,sys\nfrom pathlib import Path\n" +
        f"sys.path.insert(0,{str(ROOT / 'agent_backend/nanobot')!r})\nimport worker,gui_bridge\n" +
        "original=asyncio.create_subprocess_exec\nasync def spawn(*args,**kwargs):\n" +
        f"    if len(args)>1 and str(args[1])=={str(node_root / 'worker.mjs')!r}: args=(args[0],{str(node_script)!r},*args[2:])\n" +
        "    return await original(*args,**kwargs)\nasyncio.create_subprocess_exec=spawn\nasyncio.run(worker.main())\n", encoding="utf-8")
    settings = AgentSettings(enabled=True, tools_enabled=True, computer_enabled=True,
        python_executable=str(ROOT / "agent_backend/nanobot/.venv/Scripts/python.exe"), workspace=str(workspace),
        timeout_seconds=180 if args.run_configured_gui else 0, max_steps=12 if args.run_configured_gui else 0,
        gui_provider=configured.gui_provider if configured else "ui-tars-1.5",
        gui_coordinate_profile=configured.gui_coordinate_profile if configured else "pixel",
        gui_base_url=configured.gui_base_url if configured else endpoint + "/v1",
        gui_model=configured.gui_model if configured else "ordinal-action-model")
    service = AgentService(checkpoint_path=database)
    def run():
        report = {"passed": False, "scope": "actual AgentService/nanobot/normal UI-TARS SDK/NutJS inputs/GDI capture/goal judge/SQLite; QA input-fenced",
            "models": ("real configured GUI action model and independent vision judge; loopback planner; own-fixture state oracle"
                       if args.run_configured_gui else "loopback scripted planner/action model and PNG-pixel judge; no real-model capability claim"),
            "configured_model": configured.gui_model if configured else None, "cases": [], "checks": []}
        def check(name, passed):
            report["checks"].append({"name": name, "passed": bool(passed)})
            if not passed: raise AssertionError(name)
        try:
            with patch.object(host_module, "_WORKER_PATH", python_script):
                for behavior in (["correct_second"] if args.run_configured_gui else CASES):
                    reset_done.clear(); events.reset.emit(behavior)
                    if not reset_done.wait(5) or not FixtureModel.foreground_guard():
                        raise RuntimeError("fixture_not_foreground")
                    context = {"api": {"base_url": endpoint + "/v1", "model": "ordinal-planner"},
                        "gui_api_key": gui_key, "conversation_id": str(uuid4()), "messages": []}
                    before_inputs = len(FixtureModel.native_inputs)
                    started = time.monotonic()
                    result = service.execute(settings, KEY, AgentRequest(TASK, "agent"), context)
                    config, _, secrets = service._context(context, KEY, AgentRequest(TASK, "agent"), settings)
                    scope = scope_fingerprint(settings, config, secrets=secrets)
                    cp = CheckpointStore(database).load(context["conversation_id"], scope, secrets=secrets)
                    records = service._sessions.get(context["conversation_id"], [])
                    observation = next(json.loads(m["content"]) for m in records if m.get("role") == "tool" and m.get("name") == "computer_use")
                    row = {"behavior": behavior, "status": result.status, "checkpoint_goal_verified": cp["goal_verified"],
                        "fixture_state": json.loads(json.dumps(state)), "verification": observation.get("verification"),
                        "seconds": round(time.monotonic() - started, 3), "native_input_count": len(FixtureModel.native_inputs)-before_inputs,
                        "original_task_in_native_history": any(m.get("role") == "user" and TASK in m.get("content", "") for m in records)}
                    report["cases"].append(row)
                    if args.run_configured_gui:
                        check("real_model_native_inputs_fenced", row["native_input_count"] > 0 and len(state["clicks"]) == row["native_input_count"])
                        check("real_model_verification_attempted", isinstance(observation.get("verification"), dict))
                    else:
                        check(behavior + "_native_actions_exact", row["native_input_count"] == len(CASES[behavior]) and state["clicks"] == CASES[behavior])
                        check(behavior + "_one_independent_judge", len([j for j in FixtureModel.judges if j["behavior"] == behavior]) == 1)
                    if behavior == "correct_second":
                        check("second_video_confirmed", result.status == "completed" and cp["goal_verified"] is True)
                        check("only_second_liked", state["liked"] == [False, True, False])
                    else:
                        check(behavior + "_cannot_claim_success", result.status == "needs_user" and cp["goal_verified"] is False)
                        check(behavior + "_no_optimistic_prose", "规划模型声称" not in result.reply)
            if args.run_configured_gui:
                check("original_task_in_native_history", all(c["original_task_in_native_history"] for c in report["cases"]))
            else:
                check("goals_bound_to_original", len(FixtureModel.judges) == len(CASES) and all(j["original_task_present"] for j in FixtureModel.judges))
            check("input_foreground_guard_passed", all(f["own_fixture_foreground"] for f in focus_reports))
            check("no_server_errors", not FixtureModel.errors)
            if args.run_configured_gui:
                check("production_settings_unchanged", hashlib.sha256(settings_path.read_bytes()).hexdigest() == settings_before)
            report["passed"] = True
        except Exception as error:
            report.update(error_type=type(error).__name__, error=str(error) if isinstance(error, (AssertionError, RuntimeError)) else "fixture_pipeline_failed")
        finally:
            service.shutdown(); server.shutdown(); server.server_close()
            report.update({"judge_observations": FixtureModel.judges, "scripted_requests": FixtureModel.requests,
                "guarded_native_inputs": FixtureModel.native_inputs, "server_errors": FixtureModel.errors,
                "tested_at": datetime.now(timezone.utc).isoformat(), "database": str(database)})
            events.done.emit(report)
    def finish(report):
        fixture.grab().save(str(artifact / "actual-final.png"))
        (artifact / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]), "cases": report["cases"], "error": report.get("error")}, ensure_ascii=True), flush=True)
        result_code[0] = 0 if report["passed"] else 1
        fixture.close(); app.quit()
    events.done.connect(finish)
    def export_and_start():
        manifest = {"scope": "actual own Qt rendering of controlled reference states; no native input capability claim for reference sequences", "cases": {}}
        for behavior, actions in CASES.items():
            reset(behavior)
            observations = []
            for i, action in enumerate([None, *actions]):
                if action: clicked(action)
                app.processEvents(); fixture.repaint(); app.processEvents()
                filename = f"reference-{behavior}-{i + 1:02}.png"
                fixture.grab().save(str(artifact / filename))
                observations.append({"sequence": i+1, "captured_at": datetime.now(timezone.utc).isoformat(),
                    "after_action": action, "file": str(artifact / filename)})
            manifest["cases"][behavior] = {"task": TASK, "expected": "verified" if behavior == "correct_second" else "unverified" if behavior == "missing_first_anchor" else "failed", "observations": observations}
        (artifact / "reference_sequences.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        threading.Thread(target=run, daemon=True).start()
    fixture.showFullScreen()
    QTimer.singleShot(700, export_and_start)
    QTimer.singleShot(180_000, lambda: (service.shutdown(), fixture.close(), app.quit()))
    try:
        app.exec()
    finally:
        service.shutdown(); server.shutdown(); server.server_close(); fixture.close()
        if previous: user32.SetForegroundWindow(previous)
    return result_code[0]


if __name__ == "__main__":
    raise SystemExit(main())
