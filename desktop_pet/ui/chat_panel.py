"""Two synchronized chat views can share one session controller."""

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QInputMethodEvent, QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTextBrowser,
    QVBoxLayout, QWidget,
)


class MessageInput(QPlainTextEdit):
    send_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._composing = False

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._composing:
            super().keyPressEvent(event)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if not event.isAutoRepeat():
                    self.send_requested.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    """The view never starts a network call or changes pet drawing itself."""

    def __init__(self, chat_controller, parent=None) -> None:
        super().__init__(parent)
        self.controller = chat_controller
        self.setObjectName("chatPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        row = QHBoxLayout()
        title = QLabel("和鲸鱼娘聊一会儿")
        title.setObjectName("sectionTitle")
        row.addWidget(title)
        row.addStretch()
        self.clear_button = QPushButton("清空对话")
        self.clear_button.setObjectName("clearChatButton")
        row.addWidget(self.clear_button)
        layout.addLayout(row)
        hint = QLabel("仅保留本次会话的近期上下文，关闭窗口后仍可继续；退出程序后不保留。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.transcript = QTextBrowser()
        self.transcript.setObjectName("chatTranscript")
        self.transcript.setOpenLinks(False)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setMinimumHeight(150)
        self.transcript.setAccessibleName("本次聊天消息")
        layout.addWidget(self.transcript, 1)
        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.input = MessageInput()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("说点什么吧…  Enter 发送，Shift + Enter 换行")
        self.input.setFixedHeight(88)
        layout.addWidget(self.input)
        footer = QHBoxLayout()
        self.status_label = QLabel("准备就绪")
        self.status_label.setObjectName("chatStatus")
        footer.addWidget(self.status_label)
        footer.addStretch()
        self.send_button = QPushButton("发送消息")
        self.send_button.setObjectName("primaryButton")
        self.send_button.setAccessibleName("发送消息")
        footer.addWidget(self.send_button)
        layout.addLayout(footer)
        self.input.textChanged.connect(self._update_buttons)
        self.input.send_requested.connect(self._send)
        self.send_button.clicked.connect(self._send)
        self.clear_button.clicked.connect(self._confirm_clear)
        self.controller.messages_changed.connect(self._render_messages)
        self.controller.busy_changed.connect(self._busy_changed)
        self.controller.error_changed.connect(self._show_error)
        self._render_messages()
        self._busy_changed(self.controller.busy)
        self._show_error(getattr(self.controller, "error", ""))

    def _render_messages(self) -> None:
        cards = []
        for message in self.controller.messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            is_user = role == "user"
            label, color = ("你", "#e3f1f7") if is_user else ("鲸鱼娘", "#f0f4f9")
            # All provider/user text is escaped. It cannot insert HTML, URLs or images.
            content = escape(str(message.get("content", ""))).replace("\n", "<br>")
            cards.append(
                f'<table width="100%" cellpadding="10" cellspacing="0" bgcolor="{color}">'
                f'<tr><td><b>{label}</b><br>{content}</td></tr></table><br>'
            )
        if cards:
            html = "".join(cards)
        else:
            html = '<p style="color:#71879b">还没有消息。先到「API 设置」保存配置，再来打个招呼吧。</p>'
        self.transcript.setHtml(f'<html><body style="line-height:1.5">{html}</body></html>')
        scroll = self.transcript.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        self._update_buttons()

    def _busy_changed(self, busy: bool) -> None:
        self.status_label.setText("正在请求，请稍候… 桌宠仍可拖动和调整" if busy else "准备就绪")
        self.send_button.setText("等待回复…" if busy else "发送消息")
        self._update_buttons()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _update_buttons(self) -> None:
        busy = self.controller.busy
        self.send_button.setEnabled(bool(self.input.toPlainText().strip()) and not busy)
        self.clear_button.setEnabled(bool(self.controller.messages) and not busy)

    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text or self.controller.busy:
            return
        if self.controller.send(text):
            self.input.clear()
            self.input.setFocus()

    def _confirm_clear(self) -> None:
        if self.controller.busy or not self.controller.messages:
            return
        answer = QMessageBox.question(
            self, "清空本次对话", "确定清空当前聊天记录和本次会话上下文吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.clear()
