"""Small public Agent questions; grants remain authoritative in the controller."""
from __future__ import annotations

from copy import deepcopy
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout


def public_interaction(value, conversation_id):
    if (not isinstance(value, dict) or value.get("conversation_id") != conversation_id
            or not isinstance(value.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["id"])
            or not isinstance(value.get("kind"), str) or value.get("kind") not in {"question", "file_access"}
            or not isinstance(value.get("question"), str) or not 1 <= len(value["question"]) <= 2000):
        return None
    result = {key: value[key] for key in ("id", "kind", "question", "conversation_id")}
    if value["kind"] == "file_access":
        resource = value.get("resource")
        if (not isinstance(resource, dict) or not isinstance(resource.get("path"), str)
                or not 1 <= len(resource["path"]) <= 4096 or any(ord(char) < 32 for char in resource["path"])
                or not isinstance(resource.get("access"), str) or resource.get("access") not in {"read", "write"}
                or type(resource.get("directory")) is not bool):
            return None
        result["resource"] = {key: resource[key] for key in ("path", "access", "directory")}
    elif "options" in value:
        options = value["options"]
        if (not isinstance(options, list) or len(options) > 8
                or any(not isinstance(item, str) or not 1 <= len(item) <= 240 for item in options)):
            return None
        result["options"] = list(options)
    return deepcopy(result)


def valid_answer(interaction, value):
    if not isinstance(value, dict):
        return False
    if interaction["kind"] == "file_access":
        return set(value) == {"allowed"} and type(value["allowed"]) is bool
    return (set(value) == {"text"} and isinstance(value["text"], str)
            and 1 <= len(value["text"].strip()) <= 8000)


class AgentInteractionCard(QFrame):
    def __init__(self, answer, parent=None):
        super().__init__(parent)
        self.setObjectName("agentInteractionCard")
        self.setStyleSheet("QFrame#agentInteractionCard {border:1px solid palette(mid);border-radius:12px;background:palette(base);}")
        self._answer = answer
        self._interaction = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(9)
        self.hide()

    def update_interaction(self, interaction):
        if interaction == self._interaction:
            return
        self._interaction = deepcopy(interaction)
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
        if interaction is None:
            self.hide(); return
        self.setEnabled(True)
        question = QLabel(interaction["question"])
        question.setTextFormat(Qt.TextFormat.PlainText); question.setWordWrap(True)
        self._layout.addWidget(question)
        if interaction["kind"] == "file_access":
            resource = interaction["resource"]
            label = QLabel(("读写" if resource["access"] == "write" else "只读") + " · " +
                           ("文件夹" if resource["directory"] else "文件"))
            self._layout.addWidget(label)
            path = QLabel(resource["path"])
            path.setTextFormat(Qt.TextFormat.PlainText); path.setWordWrap(True)
            path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._layout.addWidget(path)
            buttons = QHBoxLayout()
            deny = QPushButton("拒绝"); approve = QPushButton("允许这次访问")
            deny.clicked.connect(lambda: self.submit({"allowed": False}, interaction["id"]))
            approve.clicked.connect(lambda: self.submit({"allowed": True}, interaction["id"]))
            buttons.addWidget(deny); buttons.addWidget(approve)
            self._layout.addLayout(buttons)
        else:
            for option in interaction.get("options", []):
                button = QPushButton(option)
                button.clicked.connect(lambda _checked=False, text=option: self.submit({"text": text}, interaction["id"]))
                self._layout.addWidget(button)
            editor = QLineEdit(); editor.setMaxLength(8000); editor.setPlaceholderText("补充回答…")
            editor.setAccessibleName("Agent 问题回答")
            self._layout.addWidget(editor)
            submit = QPushButton("继续")
            submit.clicked.connect(lambda: self.submit({"text": editor.text().strip()}, interaction["id"]))
            editor.returnPressed.connect(lambda: self.submit({"text": editor.text().strip()}, interaction["id"]))
            self._layout.addWidget(submit)
        self.error = QLabel(); self.error.setWordWrap(True); self.error.hide()
        self._layout.addWidget(self.error)
        self.show()

    def submit(self, answer, expected_id=None):
        interaction = self._interaction
        if (interaction is None or (expected_id is not None and interaction["id"] != expected_id)
                or not valid_answer(interaction, answer)):
            return
        if self._answer(interaction["id"], answer, interaction["conversation_id"]):
            self.setEnabled(False)
        else:
            self.error.setText("问题已更新或连接暂不可用，请检查后重试。")
            self.error.show()
