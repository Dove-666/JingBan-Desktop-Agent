"""Private native Qt editor fixture. Importing does not start UI or perform IO.

Only host setup/control frames use stdin. Document changes must come through
the visible native controls; there is no save/type/open control-frame shortcut.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError("EDITOR_PROTOCOL_INVALID")
        value[key] = item
    return value


def checked_setup(value):
    if (type(value) is not dict or set(value) != {"type", "nonce", "workspace", "fault", "watchdog_ms"}
            or value["type"] != "setup" or type(value["nonce"]) is not str
            or not re.fullmatch("[a-f0-9]{32}", value["nonce"]) or int(value["nonce"], 16) == 0
            or type(value["workspace"]) is not str or not Path(value["workspace"]).is_absolute()
            or type(value["fault"]) is not str or value["fault"] not in {"none", "save_noop", "save_wrong", "reopen_noop"}
            or type(value["watchdog_ms"]) is not int or not 5000 <= value["watchdog_ms"] <= 180000):
        raise ValueError("EDITOR_PROTOCOL_INVALID")
    workspace = Path(value["workspace"]).resolve()
    parent = (ROOT / "agent_workspace").resolve()
    if workspace == parent or not workspace.is_relative_to(parent) or not workspace.is_dir():
        raise ValueError("EDITOR_WORKSPACE_INVALID")
    if (workspace / "report.txt").resolve().parent != workspace:
        raise ValueError("EDITOR_WORKSPACE_INVALID")
    return {**value, "workspace": workspace}


def emit(value):
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main():
    try:
        raw = sys.stdin.buffer.readline(8193)
        if not raw.endswith(b"\n") or len(raw) > 8192: raise ValueError()
        setup = checked_setup(json.loads(raw, object_pairs_hook=_unique))
        if os.name != "nt": raise ValueError()
    except Exception:
        emit({"type": "editor_error", "code": "EDITOR_PROTOCOL_INVALID"}); return 1
    # Native Windows is deliberate; inherited headless Qt cannot count as UIA.
    os.environ["QT_QPA_PLATFORM"] = "windows"
    if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
    # The host launches its real Python executable, bypassing the Windows
    # venv redirector so this exact Popen PID also owns the native HWND.
    # Only this repository's fixed Qt dependency directory is added.
    dependencies = ROOT / ".venv/Lib/site-packages"
    if str(dependencies) not in sys.path: sys.path.insert(0, str(dependencies))
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    from PySide6.QtCore import QObject, Signal, QTimer, Qt, QEvent
    from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QPushButton, QLabel, QVBoxLayout, QHBoxLayout
    files = WorkspaceFiles(setup["workspace"])
    app = QApplication([])
    if app.platformName().casefold() != "windows": return 1

    class Bridge(QObject):
        command = Signal(object)
    bridge = Bridge()

    class Editor(QWidget):
        def __init__(self):
            super().__init__()
            self.closed = False; self.decoy = None
            self.setWindowTitle("AURA Owned Editor " + setup["nonce"])
            self.setObjectName("ownedEditorWindow")
            self.resize(580, 330)
            self.setStyleSheet("QWidget{font:12pt 'Segoe UI';} QLineEdit{padding:8px;} QPushButton{padding:9px;}")
            self.layout = QVBoxLayout(self)
            self.layout.addWidget(QLabel("Owned QA document · Esc stops this test"))
            self.path = QLineEdit("report.txt"); self.path.setReadOnly(True)
            self.path.setAccessibleName("Document path"); self.path.setObjectName("ownedEditorPath")
            self.layout.addWidget(self.path)
            self.field = None; self.add_editor("")
            self.status = QLineEdit("New"); self.status.setReadOnly(True)
            self.status.setAccessibleName("Document status"); self.status.setObjectName("ownedEditorStatus")
            self.layout.addWidget(self.status)
            buttons = QHBoxLayout(); self.layout.addLayout(buttons)
            self.save = QPushButton("Save document"); self.save.setAccessibleName("Save document")
            self.close_doc = QPushButton("Close document"); self.close_doc.setAccessibleName("Close document")
            self.reopen = QPushButton("Reopen document"); self.reopen.setAccessibleName("Reopen document")
            for button in (self.save, self.close_doc, self.reopen): buttons.addWidget(button)
            self.save.clicked.connect(self.save_document)
            self.close_doc.clicked.connect(self.close_document)
            self.reopen.clicked.connect(self.reopen_document)

        def add_editor(self, text):
            old = self.field
            if old is not None:
                self.layout.removeWidget(old); old.hide(); old.deleteLater()
            self.field = QLineEdit(text)
            self.field.setAccessibleName("Document text"); self.field.setObjectName("ownedEditorText")
            self.field.textChanged.connect(lambda _: self.status.setText("Modified") if hasattr(self, "status") else None)
            self.layout.insertWidget(2, self.field)

        def save_document(self):
            if self.field is None: return
            try:
                if setup["fault"] != "save_noop":
                    text = "CONTROLLED_WRONG_BYTES" if setup["fault"] == "save_wrong" else self.field.text()
                    target = setup["workspace"] / "report.txt"
                    if target.exists():
                        _, _, digest = files.read_bytes("report.txt", maximum=65536)
                        files.write("report.txt", text, overwrite=True, expected_hash=digest)
                    else: files.write("report.txt", text)
                # Deliberate lying status in fault modes tests the host oracle.
                self.status.setText("Saved")
            except Exception: self.status.setText("Save failed")

        def close_document(self):
            if self.field is not None:
                old = self.field; self.field = None
                self.layout.removeWidget(old); old.hide(); old.deleteLater()
            self.save.setEnabled(False); self.status.setText("Closed")

        def reopen_document(self):
            try:
                if setup["fault"] != "reopen_noop":
                    _, raw, _ = files.read_bytes("report.txt", maximum=65536)
                    self.add_editor(raw.decode("utf-8"))
                elif self.field is None: self.add_editor("")
                self.save.setEnabled(True); self.status.setText("Reopened")
            except Exception: self.status.setText("Open failed")

        def control(self, value):
            if type(value) is not dict or set(value) != {"type"}: self.close(); return
            if value["type"] == "stop": self.close()
            elif value["type"] == "replace_editor":
                self.add_editor(""); self.save.setEnabled(True); self.status.setText("New")
            elif value["type"] == "show_decoy":
                if self.decoy is None:
                    self.decoy = QWidget()
                    self.decoy.setWindowTitle("AURA Owned Decoy " + setup["nonce"])
                    QVBoxLayout(self.decoy).addWidget(QLabel("Owned foreground-mismatch control"))
                    self.decoy.resize(370, 110)
                self.decoy.show(); self.decoy.raise_(); self.decoy.activateWindow()
                emit({"type": "editor_decoy", "nonce": setup["nonce"], "hwnd": int(self.decoy.winId())})
            else: self.close()

        def closeEvent(self, event):
            if not self.closed:
                self.closed = True
                emit({"type": "editor_closing", "nonce": setup["nonce"]})
            if self.decoy is not None: self.decoy.close()
            event.accept(); app.quit()

    window = Editor()
    bridge.command.connect(window.control)
    class InputGuard(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                window.close(); return True
            # Native UIA Value/Invoke uses no keyboard or mouse event. Human
            # interaction aborts rather than contaminating the agent evidence.
            if event.type() in {QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress} and event.spontaneous():
                emit({"type": "editor_user_input", "nonce": setup["nonce"]})
                window.close(); return True
            return False
    input_guard = InputGuard(); app.installEventFilter(input_guard)
    def reader():
        try:
            while True:
                line = sys.stdin.buffer.readline(8193)
                if not line or not line.endswith(b"\n") or len(line) > 8192: break
                try: value = json.loads(line, object_pairs_hook=_unique)
                except Exception: break
                bridge.command.emit(value)
        finally: bridge.command.emit({"type": "stop"})
    threading.Thread(target=reader, daemon=True, name="owned-editor-controls").start()
    window.show(); window.raise_(); window.activateWindow()
    QTimer.singleShot(setup["watchdog_ms"], window.close)
    QTimer.singleShot(150, lambda: emit({"type": "editor_ready", "nonce": setup["nonce"],
        "pid": os.getpid(), "hwnd": int(window.winId())}))
    return app.exec()


if __name__ == "__main__": raise SystemExit(main())
