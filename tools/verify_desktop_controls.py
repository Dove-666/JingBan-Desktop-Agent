"""Exercise native Windows UIA only against an isolated AURA Qt fixture.

No model endpoint, production configuration, personal app, or clipboard is used.
The report distinguishes native property checks from whole-task verification.
"""
from __future__ import annotations

import asyncio
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import traceback

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_backend" / "nanobot"))
from desktop_controls import DesktopControls, DesktopControlError


class Events(QObject):
    replace = Signal()
    finished = Signal(object)


def main():
    if os.name != "nt":
        raise SystemExit("Windows required for native UI Automation verification")
    target = ROOT / "artifacts" / "gui_verified_control_20261101" / "structured_fixture"
    target.mkdir(parents=True, exist_ok=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    previous = user32.GetForegroundWindow()
    app = QApplication(sys.argv)
    window = QWidget()
    window.setWindowTitle("AURA · 电脑操控验证（测试窗口）")
    window.resize(520, 300)
    layout = QVBoxLayout(window)
    layout.addWidget(QLabel("独立测试：输入、点赞状态与未生效操作"))
    field = QLineEdit()
    field.setAccessibleName("测试输入框")
    field.setPlaceholderText("仅写入测试内容")
    layout.addWidget(field)
    checkbox = QCheckBox("测试点赞状态")
    checkbox.setAccessibleName("测试点赞状态")
    layout.addWidget(checkbox)
    noop = QPushButton("测试无效果按钮")
    noop.setAccessibleName("测试无效果按钮")
    layout.addWidget(noop)
    password = QLineEdit("fixture-secret-never-export")
    password.setEchoMode(QLineEdit.EchoMode.Password)
    password.setAccessibleName("测试密码框")
    layout.addWidget(password)
    layout.addWidget(QLabel("该窗口会自行关闭；不访问真实模型与账户。"))
    events = Events()
    replaced = threading.Event()

    def replace_field():
        nonlocal field
        old = field
        field = QLineEdit()
        field.setAccessibleName("测试输入框")
        layout.replaceWidget(old, field)
        old.hide()
        old.deleteLater()
        QTimer.singleShot(100, replaced.set)

    events.replace.connect(replace_field)
    result_code = [1]

    def finished(report):
        report["fixture_final_state"] = {"input": field.text(), "checked": checkbox.isChecked()}
        window.grab().save(str(target / "fixture.png"))
        (target / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "checks": report["checks"], "report": str(target / "verification.json")}, ensure_ascii=True), flush=True)
        result_code[0] = 0 if report["passed"] else 1
        window.close()
        if previous:
            user32.SetForegroundWindow(previous)
        app.quit()

    events.finished.connect(finished)

    async def exercise():
        checks, operations = [], []
        service = DesktopControls()

        def check(name, passed):
            checks.append({"name": name, "passed": bool(passed)})
            if not passed:
                raise AssertionError(name)

        async def observe():
            result = json.loads(await service.observe())
            if result["window"]["process_id"] != os.getpid():
                raise RuntimeError("fixture_not_foreground")
            return result

        def find(snapshot, name):
            matches = [node for node in snapshot["controls"] if node["name"] == name]
            if len(matches) != 1:
                raise RuntimeError("fixture_control_missing_or_ambiguous:" + name)
            return matches[0]

        try:
            snapshot = await observe()
            check("native_snapshot_contains_fixture_controls", len(snapshot["controls"]) >= 5)
            check("password_value_not_exported", "fixture-secret-never-export" not in json.dumps(snapshot))
            field_node = find(snapshot, "测试输入框")
            result = json.loads(await service.act(field_node["ref"], "set_value", "鲸鱼娘 UIA 读回验证 42"))
            operations.append({"action": "set_value", "result": result})
            check("native_setvalue_readback", result["postcondition"]["passed"] is True)
            verified = json.loads(await service.verify(field_node["ref"], "value", "鲸鱼娘 UIA 读回验证 42"))
            check("fresh_value_readback", verified["passed"] is True)
            snapshot = await observe()
            like = find(snapshot, "测试点赞状态")
            result = json.loads(await service.act(like["ref"], "check"))
            operations.append({"action": "check", "result": result})
            check("native_check_readback", result["postcondition"]["passed"] is True)
            snapshot = await observe()
            like = find(snapshot, "测试点赞状态")
            result = json.loads(await service.act(like["ref"], "check"))
            check("second_check_is_idempotent", result["status"] == "already_satisfied" and result["dispatched"] is False)
            check("second_check_still_checked", json.loads(await service.verify(like["ref"], "checked", True))["passed"] is True)
            old_ref = like["ref"]
            snapshot = await observe()
            try:
                await service.act(old_ref, "uncheck")
                check("old_snapshot_rejected", False)
            except DesktopControlError as error:
                check("old_snapshot_rejected", error.code == "DESKTOP_STALE_REF")
            old_field = find(snapshot, "测试输入框")["ref"]
            events.replace.emit()
            for _ in range(100):
                if replaced.is_set():
                    break
                await asyncio.sleep(.05)
            check("fixture_control_replaced", replaced.is_set())
            try:
                await service.act(old_field, "set_value", "must not be delivered")
                check("replaced_native_control_rejected", False)
            except DesktopControlError as error:
                check("replaced_native_control_rejected", error.code == "DESKTOP_STALE_REF")
            snapshot = await observe()
            field_node = find(snapshot, "测试输入框")
            check("replacement_field_not_accidentally_modified", field_node["state"].get("value") == "")
            noop_node = find(snapshot, "测试无效果按钮")
            result = json.loads(await service.act(noop_node["ref"], "invoke"))
            operations.append({"action": "invoke_noop", "result": result})
            check("noop_delivery_not_task_success", result["dispatched"] is True and result["task_verified"] is False
                  and result["postcondition"]["passed"] is None)
            snapshot = await observe()
            like = find(snapshot, "测试点赞状态")
            verification = json.loads(await service.verify(like["ref"], "checked", False))
            check("unmet_expected_state_rejected", verification["passed"] is False)
            password_node = find(snapshot, "测试密码框")
            try:
                await service.act(password_node["ref"], "set_value", "must not modify password")
                check("password_write_rejected", False)
            except DesktopControlError as error:
                check("password_write_rejected", error.code == "DESKTOP_PASSWORD_CONTROL")
            report = {"passed": True, "checks": checks, "operations": operations}
        except Exception as error:
            report = {"passed": False, "checks": checks, "operations": operations,
                      "error_type": type(error).__name__, "error": str(error)}
            traceback.print_exc()
        report.update({"tested_at": datetime.now(timezone.utc).isoformat(),
                       "scope": "isolated Qt native UIA fixture only; no real model or personal app task"})
        events.finished.emit(report)

    def begin():
        window.raise_()
        window.activateWindow()
        user32.SetForegroundWindow(int(window.winId()))
        threading.Thread(target=lambda: asyncio.run(exercise()), daemon=True).start()

    window.show()
    QTimer.singleShot(700, begin)
    # This is a verification fixture watchdog, not an Agent task-duration limit.
    QTimer.singleShot(180_000, app.quit)
    app.exec()
    return result_code[0]


if __name__ == "__main__":
    raise SystemExit(main())
