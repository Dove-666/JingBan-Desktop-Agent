"""Conversation history, screen-event cards and synchronized chat views."""

from html import escape
import base64
from datetime import datetime
import json
import math
from pathlib import Path
import re
import threading
from urllib.parse import quote, unquote

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QTimer, QUrl, Signal, QSignalBlocker
from PySide6.QtGui import QDesktopServices, QFont, QImage, QImageReader, QInputMethodEvent, QKeyEvent, QKeySequence, QPainter, QPixmap, QShortcut, QTextOption
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QSplitter, QTextBrowser, QToolButton,
    QVBoxLayout, QWidget, QWidgetAction,
)

from ..api_client import ApiError
from ..async_tasks import TaskRunner
from ..chat_images import IMAGE_ERRORS, IMAGE_ID, MAX_CHAT_IMAGE_BYTES
from ..image_storage import ImageStorageGate, ImageStorageGateError
from ..agent_presentation import public_agent_metadata
from .agent_interaction import AgentInteractionCard, public_interaction
from .theme import current_theme, style_widget, theme_manager
from .draft_state import shared_draft_state


IMAGE_STAGE_ERROR = "无法准备图片；请选择有效的 PNG、JPEG 或 WebP 图片，最多 4 MiB、1600 万像素。"
IMAGE_STAGE_STORAGE_ERRORS = {
    "image_storage_full": "图片存储空间已满（64 MiB），本次图片未添加，文字已保留。",
    "image_storage_error": "无法读取或保存图片，请检查本地可用空间和读写权限后重试；文字已保留。",
}
MAX_STAGE_IMAGE_BASE64 = ((MAX_CHAT_IMAGE_BYTES + 2) // 3) * 4


def _native_color(light, dark):
    """Choose a trusted template color without rewriting message content."""
    return dark if current_theme() == "dark" else light


def _execution_text(value):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"data:[^\s,<>\"']{0,160},[^\s<>\"']*", "[已隐藏内嵌数据]", value, flags=re.I)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[已隐藏密钥]", value)
    value = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [已隐藏密钥]", value, flags=re.I)
    return re.sub(r"\b(?:api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)", "[已隐藏凭据]", value, flags=re.I)


def _execution_count(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return min(max(0, value), 2 ** 53 - 1)
    return int(value) if isinstance(value, float) and math.isfinite(value) and value >= 0 else 0


def safe_execution(value, redactor=None):
    """Only public execution fields reach either local renderer, never reasoning."""
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1
            or not isinstance(value.get("state"), str) or value.get("state") not in
            {"running", "completed", "failed", "cancelled", "needs_user", "interrupted"}):
        return None
    started = value.get("started_at", "")
    try:
        datetime.fromisoformat(started.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        started = ""
    events = []
    for index, event in enumerate(value.get("events", []) if isinstance(value.get("events"), list) else []):
        if (not isinstance(event, dict) or not isinstance(event.get("kind"), str)
                or event.get("kind") not in {"progress", "tool", "computer", "summary"}
                or not isinstance(event.get("status"), str) or event.get("status") not in {"running", "completed", "failed"}):
            continue
        label, detail = _execution_text(event.get("label")), _execution_text(event.get("detail"))
        if not label or re.match(r"\s*(?:thought|analysis|reasoning(?:_content)?)\s*:", label, re.I):
            continue
        row = {"id": _execution_text(event.get("id")) or f"event-{index}", "elapsed_ms": _execution_count(event.get("elapsed_ms")),
               "kind": event["kind"], "status": event["status"], "label": label}
        if detail and not re.match(r"\s*(?:thought|analysis|reasoning(?:_content)?)\s*:", detail, re.I):
            row["detail"] = detail
        events.append(row)
    result = {"version": 1, "state": value["state"], "started_at": started,
              "duration_ms": _execution_count(value.get("duration_ms")), "events": events}
    context = value.get("context")
    if isinstance(context, dict):
        counts = context.get("source_counts")
        counts = counts if isinstance(counts, dict) else {}
        messages = context.get("messages")
        messages = messages if isinstance(messages, list) else []
        result["context"] = {
            "model": _execution_text(context.get("model")), "policy": "manual" if context.get("policy") == "manual" else "model",
            "agent_model": _execution_text(context.get("agent_model")), "gui_model": _execution_text(context.get("gui_model")),
            "history_messages": _execution_count(context.get("history_messages")),
            "estimated_input_tokens": _execution_count(context.get("estimated_input_tokens")),
            "model_context_tokens": _execution_count(context.get("model_context_tokens")) or None,
            "output_tokens": _execution_count(context.get("output_tokens")) or None,
            "source_counts": {key: _execution_count(count) for key, count in counts.items()
                              if isinstance(key, str) and re.fullmatch(r"[a-z][a-z_]{0,39}", key)},
            "messages": [{"role": message["role"], "content": _execution_text(message["content"])} for message in messages
                         if isinstance(message, dict) and isinstance(message.get("role"), str)
                         and message.get("role") in {"system", "developer", "user", "assistant", "tool"}
                         and isinstance(message.get("content"), str)],
        }
    if callable(redactor):
        try:
            # One pass reads configured secret values once, rather than once
            # per message/event on the GUI thread. The result remains JSON data.
            result = json.loads(redactor(json.dumps(result, ensure_ascii=False)))
        except Exception:
            return None
    return result


def _duration_text(milliseconds):
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds}秒" if seconds < 60 else f"{seconds // 60}分" + (f"{seconds % 60}秒" if seconds % 60 else "")


def _read_image_preview(controller, reference, maximum):
    """Read/decode just the clicked attachment, outside the Qt UI thread."""
    image = controller.get_message_image(*reference)
    return _decode_image_preview(image, maximum)


def _decode_image_preview(image, maximum):
    try:
        encoded = image["data_base64"]
        if not isinstance(encoded, str) or len(encoded) > ((MAX_CHAT_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("invalid preview")
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > MAX_CHAT_IMAGE_BYTES:
            raise ValueError("invalid preview")
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        size = reader.size()
        if (bytes(reader.format()).lower() not in {b"png", b"jpeg", b"webp"}
                or not size.isValid() or size.width() * size.height() > 16_000_000 or reader.imageCount() > 1):
            raise ValueError("invalid preview")
        if size.width() > maximum.width() or size.height() > maximum.height():
            reader.setScaledSize(size.scaled(maximum, Qt.AspectRatioMode.KeepAspectRatio))
        decoded = reader.read()
        if decoded.isNull():
            raise ValueError("invalid preview")
        if decoded.width() > maximum.width() or decoded.height() > maximum.height():
            decoded = decoded.scaled(maximum, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return decoded
    except Exception:
        raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"]) from None


def _image_thumbnail(decoded):
    """Fresh bounded PNG pixels only, with no original file metadata."""
    clean = QImage(decoded.size(), QImage.Format.Format_RGBA8888)
    clean.fill(0)
    painter = QPainter(clean)
    painter.drawImage(0, 0, decoded)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not clean.save(buffer, "PNG") or data.size() > 2 * 1024 * 1024:
        raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
    return {"src": "data:image/png;base64," + base64.b64encode(bytes(data)).decode("ascii"),
            "width": clean.width(), "height": clean.height()}


class _ImagePreviewLabel(QLabel):
    def set_image(self, image):
        self._image = QPixmap.fromImage(image)
        self._fit_image()

    def _fit_image(self):
        if hasattr(self, "_image"):
            self.setPixmap(self._image.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_image()


def validated_external_url(value):
    """The privileged host accepts browser links, never local/custom URLs."""
    if (not isinstance(value, str) or len(value) > 8192 or not value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or "\\" in value or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I)):
        return None
    url = QUrl(value, QUrl.ParsingMode.StrictMode)
    if (not url.isValid() or url.scheme().lower() not in {"http", "https"}
            or not url.host() or url.userInfo() or url.port(-1) > 65535):
        return None
    return url


def _message_html(text):
    """Escape text and expose validated Markdown links through native actions."""
    chunks, cursor = [], 0
    for match in re.finditer(r"\[([^\]\n]+)\]\(([^\s)]+)\)", text):
        chunks.append(escape(text[cursor:match.start()]))
        label, target = match.group(1), match.group(2)
        url = validated_external_url(target)
        if url is None:
            chunks.append(escape(match.group()))
        else:
            chunks.append(f'<a href="{escape(target, quote=True)}" title="{escape(target, quote=True)}">'
                          f'{escape(label)}</a> <a href="aura-copy:{quote(target, safe="")}">复制链接</a>')
        cursor = match.end()
    chunks.append(escape(text[cursor:]))
    return "".join(chunks).replace("\n", "<br>")


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

    _image_stage_submission_finished = Signal(object, str)
    _image_read_finished = Signal(object)

    def __init__(self, chat_controller, parent=None, memory_controller=None, memory_opener=None,
                 settings_opener=None) -> None:
        super().__init__(parent)
        self.controller = chat_controller
        self.memory_controller = memory_controller
        self._memory_opener = memory_opener
        self._settings_opener = settings_opener
        self._export_titles = {}
        self.memory_panel = None
        self._sessions_supported = hasattr(chat_controller, "open_conversation")
        self._event_messages = {}
        self._execution_messages = {}
        self._execution_expanded = {}
        self._execution_context_expanded = set()
        self._image_messages = {}
        self._image_preview_dialog = None
        self._image_preview_reference = None
        self._image_preview_loading = False
        self._image_runner = TaskRunner(self)
        self._pending_focus = None
        self._displayed_conversation_id = None
        self._rendered_conversation_id = None
        self._rendered_inline_error_scope = None
        self._draft_state = shared_draft_state(chat_controller)
        self._draft_owner = self._draft_state.owner
        self._drafts = self._draft_state.texts
        self._draft_restoring = False
        self._draft_editor_epoch = ""
        self._staged_images = {}
        gate = getattr(chat_controller, "image_storage_gate", None)
        self._image_storage_gate = gate if isinstance(gate, ImageStorageGate) else None
        self._image_storage_owner = self._image_storage_gate.create_owner() if self._image_storage_gate is not None else None
        self._image_storage_closed = False
        self._image_content_epoch = self._read_image_storage_epoch()
        self._image_stage_tokens = {}
        self._image_stage_import_token = None
        self._image_stage_active_serial = None
        self._image_preparing = False
        self._image_stage_errors = {}
        self._image_stage_serial = 0
        self._image_stage_conversation_id = None
        self._image_stage_runner = TaskRunner(self)
        self._image_stage_submission_finished.connect(self._failed_image_stage_finished)
        self._image_read_finished.connect(self._finish_image_read)
        if self._image_storage_gate is not None:
            # This closure does not dereference a QWidget during destruction.
            owner_gate, owner = self._image_storage_gate, self._image_storage_owner
            self.destroyed.connect(lambda *_: owner_gate.close_owner(owner))
            chat_controller.destroyed.connect(self._shutdown_image_staging)
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._shutdown_image_staging)
        self._staged_preview_key = None
        self._sidebar_user_choice = None
        self._sidebar_width = 215
        self._chat_font_size = getattr(chat_controller, "reading_font_size", 16)
        self.setObjectName("chatPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(9)
        layout.addWidget(self.splitter, 1)
        self.sidebar = self._make_sidebar()
        self.splitter.addWidget(self.sidebar)
        self.sidebar.setVisible(self._sessions_supported)
        conversation = QWidget()
        self.splitter.addWidget(conversation)
        layout = QVBoxLayout(conversation)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(10)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([210, 480])
        row = QHBoxLayout()
        self._native_header_row = row
        self.history_toggle = QPushButton("历史")
        self.history_toggle.setObjectName("toggleHistoryButton")
        self.history_toggle.setCheckable(True)
        self.history_toggle.setChecked(True)
        self.history_toggle.setVisible(self._sessions_supported)
        self.history_toggle.setToolTip("展开或收起历史会话 · Ctrl+F 搜索")
        self.history_toggle.toggled.connect(self._toggle_history)
        row.addWidget(self.history_toggle)
        self.title_label = QLabel("和鲸鱼娘聊一会儿")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setObjectName("sectionTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setMaximumHeight(54)
        row.addWidget(self.title_label, 1)
        self.new_button.setVisible(self._sessions_supported)
        self.new_button.setToolTip("开始独立的新对话 · Ctrl+N")
        row.addWidget(self.new_button)
        self.clear_button = QPushButton("清空对话")
        self.clear_button.setObjectName("clearChatButton")
        self.tools_button = QToolButton()
        self.tools_button.setObjectName("chatToolsButton")
        self.tools_button.setText("工具")
        self.tools_button.setToolTip("长期记忆、上下文预算和当前会话操作")
        self.tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tools_menu = QMenu(self.tools_button)
        self.tools_menu.setObjectName("chatToolsMenu")
        self.settings_button = QPushButton("聊天服务设置")
        self.settings_button.setObjectName("openChatSettingsButton")
        self.settings_button.setEnabled(callable(settings_opener))
        self.settings_button.clicked.connect(self._open_settings)
        self.export_button = QPushButton("导出当前对话")
        self.export_button.setObjectName("exportConversationButton")
        self.export_button.setToolTip("选择 Markdown 或 JSON 文件；导出完整对话与文字情境，不包含配置或截图。")
        self.export_button.clicked.connect(self._export_conversation)
        for button, supported in ((self.settings_button, callable(settings_opener)),
                                  (self.export_button, hasattr(self.controller, "export_conversation"))):
            action = QWidgetAction(self.tools_menu)
            action.setDefaultWidget(button)
            action.setVisible(supported)
            self.tools_menu.addAction(action)
            button.clicked.connect(self.tools_menu.close)
        for button in (self.memory_button, self.context_budget_button):
            action = QWidgetAction(self.tools_menu)
            action.setDefaultWidget(button)
            action.setVisible(button is self.memory_button or hasattr(self.controller, "update_context_budget"))
            self.tools_menu.addAction(action)
            button.clicked.connect(self.tools_menu.close)
        self._tools_separator = self.tools_menu.addSeparator()
        self.clear_button.setProperty("destructive", True)
        clear_action = QWidgetAction(self.tools_menu)
        clear_action.setDefaultWidget(self.clear_button)
        self.tools_menu.addAction(clear_action)
        self.clear_button.clicked.connect(self.tools_menu.close)
        self.tools_button.setMenu(self.tools_menu)
        row.addWidget(self.tools_button)
        layout.addLayout(row)
        self.context_usage_label = QLabel(str(getattr(self.controller, "context_usage", "")), self)
        self.context_usage_label.setObjectName("contextUsage")
        self.context_usage_label.setTextFormat(Qt.TextFormat.PlainText)
        self.context_usage_label.setWordWrap(True)
        self.context_usage_label.setMaximumWidth(260)
        context_usage_action = QWidgetAction(self.tools_menu)
        context_usage_action.setDefaultWidget(self.context_usage_label)
        context_usage_action.setVisible(hasattr(self.controller, "context_usage_changed"))
        self.tools_menu.insertAction(self._tools_separator, context_usage_action)
        self.transcript = QTextBrowser()
        self.transcript.setObjectName("chatTranscript")
        self.transcript.setOpenLinks(False)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setMinimumHeight(150)
        reading_font = QFont("Microsoft YaHei UI")
        reading_font.setPixelSize(16)
        self.transcript.setFont(reading_font)
        self.transcript.document().setDefaultFont(reading_font)
        self.transcript.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.transcript.setAccessibleName("当前会话聊天消息")
        self.transcript.anchorClicked.connect(self._open_event)
        layout.addWidget(self.transcript, 1)
        self.agent_interaction_card = AgentInteractionCard(self._answer_agent_interaction, self)
        layout.addWidget(self.agent_interaction_card)
        self.event_details = QLabel()
        self.event_details.setObjectName("screenEventDetails")
        self.event_details.setTextFormat(Qt.TextFormat.PlainText)
        self.event_details.setWordWrap(True)
        self.event_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.event_details.hide()
        layout.addWidget(self.event_details)
        self.close_event_button = QPushButton("收起屏幕情境")
        self.close_event_button.clicked.connect(self._hide_event)
        self.close_event_button.hide()
        layout.addWidget(self.close_event_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.export_status_label = QLabel()
        self.export_status_label.setObjectName("exportStatus")
        self.export_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.export_status_label.setWordWrap(True)
        self.export_status_label.hide()
        layout.addWidget(self.export_status_label)
        self.image_stage_row = QWidget()
        self.image_stage_row.setObjectName("chatStagedImage")
        image_stage_layout = QHBoxLayout(self.image_stage_row)
        image_stage_layout.setContentsMargins(0, 0, 0, 0)
        self.image_stage_preview = QLabel()
        self.image_stage_preview.setObjectName("chatStagedImagePreview")
        image_stage_layout.addWidget(self.image_stage_preview)
        self.image_stage_label = QLabel()
        self.image_stage_label.setTextFormat(Qt.TextFormat.PlainText)
        self.image_stage_label.setWordWrap(True)
        image_stage_layout.addWidget(self.image_stage_label, 1)
        self.image_remove_button = QPushButton("移除图片")
        self.image_remove_button.setObjectName("removeChatImageButton")
        self.image_remove_button.clicked.connect(self._remove_staged_image)
        image_stage_layout.addWidget(self.image_remove_button)
        self.image_stage_row.hide()
        layout.addWidget(self.image_stage_row)
        mode_row = QHBoxLayout()
        self.agent_mode_selector = QComboBox()
        self.agent_mode_selector.setObjectName("chatModeSelector")
        self.agent_mode_selector.setAccessibleName("对话模式")
        self.agent_mode_selector.addItem("聊天", False)
        self.agent_mode_selector.addItem("电脑 Agent", True)
        self.agent_mode_selector.setToolTip("为当前对话选择模式；电脑 Agent 可使用已启用的电脑工具。")
        self.agent_mode_selector.currentIndexChanged.connect(self._set_agent_mode)
        mode_row.addWidget(self.agent_mode_selector)
        self.agent_mode_hint = QLabel("当前对话持续使用电脑 Agent")
        self.agent_mode_hint.setObjectName("chatModeHint")
        mode_row.addWidget(self.agent_mode_hint, 1)
        layout.addLayout(mode_row)
        self.input = MessageInput()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("发消息…")
        input_font = QFont(reading_font)
        input_font.setPixelSize(15)
        self.input.setFont(input_font)
        self.input.setFixedHeight(92)
        layout.addWidget(self.input)
        footer = QHBoxLayout()
        self.status_label = QLabel("准备就绪")
        self.status_label.setObjectName("chatStatus")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(self.status_label, 1)
        self.image_select_button = QPushButton("添加图片")
        self.image_select_button.setObjectName("selectChatImageButton")
        self.image_select_button.setEnabled(callable(getattr(self.controller, "import_local_image", None)))
        self.image_select_button.clicked.connect(self._select_image)
        footer.addWidget(self.image_select_button)
        self.retry_button = QPushButton("重试")
        self.retry_button.setObjectName("retryChatButton")
        self.retry_button.clicked.connect(self._retry)
        self.retry_button.hide()
        footer.addWidget(self.retry_button)
        self.stop_button = QPushButton("停止生成")
        self.stop_button.setObjectName("stopChatButton")
        self.stop_button.setToolTip("停止等待本次回复；服务端可能已经收到请求。")
        self.stop_button.clicked.connect(self._stop_request)
        self.stop_button.hide()
        footer.addWidget(self.stop_button)
        self.send_button = QPushButton("发送消息")
        self.send_button.setObjectName("primaryButton")
        self.send_button.setAccessibleName("发送消息")
        footer.addWidget(self.send_button)
        layout.addLayout(footer)
        self.input.textChanged.connect(self._update_buttons)
        self.input.textChanged.connect(self._native_draft_changed)
        self.input.send_requested.connect(self._send)
        self.send_button.clicked.connect(self._send)
        self.clear_button.clicked.connect(self._confirm_clear)
        self.controller.messages_changed.connect(self._render_messages)
        self.controller.busy_changed.connect(self._busy_changed)
        self.controller.error_changed.connect(self._show_error)
        interaction_signal = getattr(self.controller, "agent_interaction_changed", None)
        if interaction_signal is not None:
            interaction_signal.connect(self._agent_interaction_changed)
        if hasattr(self.controller, "retry_availability_changed"):
            self.controller.retry_availability_changed.connect(self._render_messages)
        if hasattr(self.controller, "reading_font_changed"):
            self.controller.reading_font_changed.connect(self._apply_reading_font)
        if hasattr(self.controller, "request_status_changed"):
            self.controller.request_status_changed.connect(self._request_status_changed)
        if hasattr(self.controller, "export_busy_changed"):
            self.controller.export_busy_changed.connect(self._export_busy_changed)
        if hasattr(self.controller, "export_finished"):
            self.controller.export_finished.connect(self._export_finished)
        if hasattr(self.controller, "context_usage_changed"):
            self.controller.context_usage_changed.connect(self._context_usage_changed)
        if hasattr(self.controller, "execution_changed"):
            self.controller.execution_changed.connect(self._render_messages)
        if hasattr(self.controller, "conversation_invalidated"):
            self.controller.conversation_invalidated.connect(self._invalidate_staged_image)
        storage_signal = getattr(self.controller, "image_storage_changed", None)
        if storage_signal is not None:
            storage_signal.connect(self._image_storage_changed)
        content_signal = getattr(self.controller, "image_storage_content_changed", None)
        if content_signal is not None:
            content_signal.connect(self._image_content_changed)
        self._context_usage_changed(str(getattr(self.controller, "context_usage", "")))
        self._install_shortcuts()
        if self._sessions_supported:
            self.controller.conversations_changed.connect(self._render_conversations)
            self.controller.conversation_changed.connect(self._conversation_changed)
            self.controller.history_busy_changed.connect(self._history_busy_changed)
            self.controller.message_focus_requested.connect(self.focus_message)
            self._render_conversations()
            self._conversation_changed()
        self._render_messages()
        self._apply_reading_font(self._chat_font_size)
        self._busy_changed(self.controller.busy)
        self._show_error(getattr(self.controller, "error", ""))
        manager = theme_manager()
        if manager is not None:
            manager.theme_changed.connect(self._theme_changed)
        self._draft_state.changed.connect(self._draft_owner_changed)
        self._draft_state.loaded.connect(self._drafts_loaded)
        if self._draft_owner._loaded.is_set(): QTimer.singleShot(0, self, self._drafts_loaded)

    def _draft_editor_authoritative(self):
        return True

    def _restore_editor_draft(self, cid):
        value = self._draft_owner.snapshot(str(cid))
        if value is None: return
        self._draft_editor_epoch = value.epoch
        if self.input.toPlainText() != value.text:
            self._draft_restoring = True
            try:
                blocker = QSignalBlocker(self.input)
                self.input.setPlainText(value.text)
                del blocker
            finally: self._draft_restoring = False

    def _native_draft_changed(self):
        if self._draft_restoring or not self._draft_editor_authoritative(): return
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        if cid != self._displayed_conversation_id: return
        self._draft_owner.set_text(cid, self.input.toPlainText(), epoch=self._draft_editor_epoch)

    def _draft_owner_changed(self, cid):
        if cid == self._displayed_conversation_id and self._draft_editor_authoritative():
            self._restore_editor_draft(cid)
            self._update_buttons()
        publish = getattr(self, "_queue_web_state", None)
        if callable(publish) and not getattr(self, "_handling_web_draft", False): publish()

    def _drafts_loaded(self):
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        self._draft_owner_changed(cid)
        restore = getattr(self.controller, "restore_local_draft_conversation", None)
        if callable(restore):
            candidate = self._draft_owner.startup_candidate(cid)
            if candidate is not None: restore(candidate)

    def hideEvent(self, event):
        self._draft_state.flush()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._draft_state.flush()
        super().closeEvent(event)

    def _theme_changed(self, _theme):
        # AssistantChatPanel overrides this renderer and keeps its hidden
        # QTextDocument idle; its web state carries the application theme.
        self._render_messages()

    def _apply_reading_font(self, size):
        if not isinstance(size, int) or isinstance(size, bool) or not 12 <= size <= 22:
            size = 16
        self._chat_font_size = size
        for widget in (self.transcript, self.input):
            font = widget.font()
            font.setPixelSize(size)
            widget.setFont(font)
            style_widget(widget, f"font-size: {size}px;")
        self.transcript.document().setDefaultFont(self.transcript.font())
        self._render_messages()

    def _install_shortcuts(self) -> None:
        """Scope shortcuts to this visible panel, including its input children."""
        self._shortcuts = []
        bindings = [(QKeySequence("Escape"), self._dismiss_auxiliary)]
        if self._sessions_supported:
            bindings.extend([
                (QKeySequence.StandardKey.New, self.new_button.click),
                (QKeySequence.StandardKey.Find, self._focus_history_search),
            ])
        for sequence, callback in bindings:
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def _focus_history_search(self) -> None:
        self.history_toggle.setChecked(True)
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_input.selectAll()

    def _dismiss_auxiliary(self) -> None:
        """Escape dismisses auxiliary UI; it never removes messages or drafts."""
        if self.tools_menu.isVisible():
            self.tools_menu.close()
        elif not self.event_details.isHidden():
            self._hide_event()
        else:
            self.input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _toggle_history(self, visible: bool) -> None:
        self._sidebar_user_choice = bool(visible)
        self._set_history_visible(visible)

    def _set_history_visible(self, visible: bool) -> None:
        if not self._sessions_supported:
            return
        if not visible and self.splitter.sizes()[0] > 0:
            self._sidebar_width = self.splitter.sizes()[0]
        self.sidebar.setVisible(visible)
        self.history_toggle.blockSignals(True)
        self.history_toggle.setChecked(visible)
        self.history_toggle.blockSignals(False)
        if visible:
            sidebar_width = min(self._sidebar_width, max(155, self.width() - 380))
            self.splitter.setSizes([sidebar_width, max(300, self.width() - sidebar_width)])

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "history_toggle") and self._sidebar_user_choice is None:
            self._set_history_visible(self.width() >= 780)

    def _context_usage_changed(self, text: str) -> None:
        # Put advanced request details in the tools menu so a narrow chat does
        # not spend another full line on them; the tooltip also exposes them.
        self.context_usage_label.setText(text)
        self.tools_button.setToolTip("当前会话操作")

    def _make_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("conversationSidebar")
        sidebar.setMinimumWidth(155)
        sidebar.setMaximumWidth(330)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 6, 0)
        self.new_button = QPushButton("新建对话")
        self.new_button.setObjectName("newConversationButton")
        self.new_button.clicked.connect(self._new_conversation)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("conversationSearch")
        self.search_input.setPlaceholderText("搜索标题和消息")
        self.search_input.setClearButtonEnabled(True)
        layout.addWidget(self.search_input)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._search)
        self.search_input.textChanged.connect(lambda: self._search_timer.start())
        self.history_status = QLabel("历史会话")
        self.history_status.setObjectName("hint")
        self.history_status.setWordWrap(True)
        layout.addWidget(self.history_status)
        self.conversation_list = QListWidget()
        self.conversation_list.setObjectName("conversationList")
        self.conversation_list.setAccessibleName("历史会话，按最近更新时间排列")
        self.conversation_list.setWordWrap(False)
        self.conversation_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.conversation_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.conversation_list.setUniformItemSizes(True)
        self.conversation_list.itemClicked.connect(self._open_conversation)
        layout.addWidget(self.conversation_list, 1)
        self.history_empty_label = QLabel("还没有历史对话\n发送第一条消息后会自动保存。")
        self.history_empty_label.setObjectName("historyEmptyState")
        self.history_empty_label.setWordWrap(True)
        self.history_empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.history_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.history_empty_label, 1)
        buttons = QHBoxLayout()
        self.rename_button = QPushButton("重命名")
        self.rename_button.setObjectName("renameConversationButton")
        self.rename_button.clicked.connect(self._rename_conversation)
        buttons.addWidget(self.rename_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("deleteConversationButton")
        self.delete_button.clicked.connect(self._delete_conversation)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)
        self.memory_button = QPushButton("长期记忆")
        self.memory_button.setObjectName("openMemoryButton")
        self.memory_button.setEnabled(self.memory_controller is not None or callable(self._memory_opener))
        self.memory_button.setToolTip("查看、搜索和删除 Mem0 长期记忆；独立于聊天记录。")
        self.memory_button.clicked.connect(self._open_memory)
        self.context_budget_button = QPushButton("上下文：跟随模型")
        self.context_budget_button.setObjectName("contextBudgetButton")
        self.context_budget_button.hide()
        self.context_budget_button.clicked.connect(self._show_context_budget)
        return sidebar

    def _show_context_budget(self):
        self.input.setFocus()

    def _execution_html(self, message_id, execution):
        muted = _native_color("#52677a", "#a7b5c8")
        label_color = _native_color("#38566f", "#c0d5ea")
        detail_color = _native_color("#607489", "#a7b5c8")
        expanded = self._execution_expanded.get(message_id, execution["state"] == "running")
        operations = sum(event["kind"] in {"tool", "computer"} for event in execution["events"])
        elapsed = "用时 " + _duration_text(execution["duration_ms"])
        if operations:
            elapsed += f" · {operations}项操作"
        caption = {"failed": "未完成", "cancelled": "已停止", "needs_user": "未完成", "interrupted": "已中断", "running": "进行中"}.get(execution["state"], "")
        href = "aura-execution:" + quote(message_id, safe="")
        html = f'<p><a href="{escape(href, quote=True)}">{("▾" if expanded else "▸")} {escape(elapsed + (" · " + caption if caption else ""))}</a></p>'
        if expanded:
            html += f'<p style="font-size:12px;color:{muted}">执行过程 · 公开的操作摘要</p>'
            for event in execution["events"]:
                if event["kind"] not in {"tool", "computer"}:
                    continue
                label = "操作已暂停" if event["label"] == "需要手动接手" else event["label"]
                html += f'<p style="font-size:12px;color:{label_color}">' + escape(_duration_text(event["elapsed_ms"]) + " · " + label) + '</p>'
                if event.get("detail"):
                    html += f'<p style="font-size:12px;color:{detail_color}">' + escape(event["detail"]).replace("\n", "<br>") + '</p>'
        return html

    def _render_conversations(self) -> None:
        active_id = getattr(self.controller, "current_conversation_id", "")
        self.conversation_list.clear()
        for conversation in getattr(self.controller, "conversations", []):
            title = " ".join(str(conversation.get("title") or "新对话").split())
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, str(conversation["id"]))
            item.setToolTip(f"<qt>{escape(title)}</qt>")
            self.conversation_list.addItem(item)
            if str(conversation["id"]) == active_id:
                self.conversation_list.setCurrentItem(item)
        self._update_history_empty()
        self._update_buttons()

    def _update_history_empty(self) -> None:
        empty = self.conversation_list.count() == 0
        loading = getattr(self.controller, "history_loading", False)
        self.conversation_list.setVisible(not empty)
        self.history_empty_label.setVisible(empty)
        if loading:
            self.history_empty_label.setText("正在读取历史…\n当前草稿会保留。")
        elif self.search_input.text().strip():
            self.history_empty_label.setText("没有找到匹配的对话\n试试其他词，或清除搜索。")
        else:
            self.history_empty_label.setText("还没有历史对话\n发送第一条消息后会自动保存。")

    def _conversation_changed(self) -> None:
        self.title_label.setText(str(getattr(self.controller, "current_title", "新对话")))
        self.title_label.setToolTip("<qt>" + escape(str(getattr(self.controller, "current_summary", ""))) + "</qt>")
        active_id = getattr(self.controller, "current_conversation_id", "")
        self.conversation_list.setCurrentRow(-1)
        for index in range(self.conversation_list.count()):
            item = self.conversation_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == active_id:
                self.conversation_list.setCurrentItem(item)
                break
        if active_id != self._displayed_conversation_id:
            self._displayed_conversation_id = active_id
            self._restore_editor_draft(active_id)
            self._pending_focus = None
            self._hide_event()
            self._render_messages()
        self._update_buttons()

    def _history_busy_changed(self, busy: bool) -> None:
        self.history_status.setText("正在加载历史…" if busy else "历史会话")
        self._update_history_empty()
        self._busy_changed(self.controller.busy)
        if not self.controller.messages:
            self._render_messages()
        self._update_buttons()

    def _selected_conversation_id(self) -> str:
        item = self.conversation_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _new_conversation(self) -> None:
        if self._sessions_supported:
            self.controller.new_conversation()
            self.input.clear()
            self.input.setFocus()

    def _search(self) -> None:
        if self._sessions_supported:
            self.controller.search_conversations(self.search_input.text().strip())

    def _open_conversation(self, item) -> None:
        if self._sessions_supported:
            self.controller.open_conversation(str(item.data(Qt.ItemDataRole.UserRole)))

    def _rename_conversation(self) -> None:
        conversation_id = self._selected_conversation_id()
        if not conversation_id:
            return
        item = self.conversation_list.currentItem()
        original = item.text().split("\n", 1)[0]
        title, accepted = QInputDialog.getText(self, "重命名会话", "会话标题", text=original)
        if accepted and title.strip():
            self.controller.rename_conversation(conversation_id, title.strip())

    def _delete_conversation(self) -> None:
        conversation_id = self._selected_conversation_id()
        if not conversation_id:
            return
        answer = QMessageBox.question(
            self, "删除会话", "永久删除此会话、所有消息和关联屏幕事件？长期记忆不受影响。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.delete_conversation(conversation_id)

    def _open_memory(self) -> None:
        if callable(self._memory_opener):
            self._memory_opener()
            return
        if self.memory_controller is None:
            return
        if self.memory_panel is None:
            from .memory_panel import MemoryPanel
            self.memory_panel = MemoryPanel(self.memory_controller, self)
        self.memory_panel.present()

    def _open_settings(self) -> None:
        if callable(self._settings_opener):
            self.tools_menu.close()
            self._settings_opener()

    def _export_conversation(self) -> None:
        if not self.export_button.isEnabled():
            return
        self.tools_menu.close()
        from ..conversations.export import suggested_export_filename
        # The native file picker can outlive queued UI updates, so capture this
        # session before showing it rather than reading the active ID later.
        conversation_id = str(self.controller.current_conversation_id)
        title = str(getattr(self.controller, "current_title", "对话"))
        suggestion = suggested_export_filename(title, conversation_id)
        destination, selected_filter = QFileDialog.getSaveFileName(
            self, "导出当前对话", suggestion, "Markdown 对话 (*.md);;JSON 对话 (*.json)")
        if not destination:
            return
        suffix = Path(destination).suffix.lower()
        format = ("json" if suffix == ".json" else "markdown" if suffix == ".md"
                  else "json" if selected_filter.startswith("JSON") else "markdown")
        if suffix not in (".md", ".json"):
            destination += ".json" if format == "json" else ".md"
            # The picker confirmed the typed path, not necessarily this newly
            # completed name. Do not overwrite a different existing target.
            if Path(destination).exists():
                answer = QMessageBox.question(
                    self, "覆盖导出文件", "补全扩展名后的目标文件已存在。是否覆盖？选择“否”将保留原文件。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
        self._export_titles[conversation_id] = title
        if not self.controller.export_conversation(conversation_id, destination, format):
            self._export_titles.pop(conversation_id, None)
            self.export_status_label.setText("暂时无法导出，请等当前请求或历史加载完成后重试。")
            self.export_status_label.show()

    def _export_busy_changed(self, busy: bool) -> None:
        if busy:
            self.export_status_label.setText("正在导出完整对话… 可以继续浏览或切换会话。")
            style_widget(self.export_status_label, "color: #315b6d;")
            self.export_status_label.show()
        self._update_buttons()

    def _export_finished(self, conversation_id: str, path: str, error: str) -> None:
        title = self._export_titles.pop(conversation_id, "")
        reference = title or f"会话 {conversation_id[:8]}"
        if error:
            self.export_status_label.setText(f"导出失败（{reference}）：{error}")
            style_widget(self.export_status_label, "color: #9b3d30;")
        else:
            self.export_status_label.setText(f"已导出「{reference}」：{Path(path).name}")
            style_widget(self.export_status_label, "color: #315b6d;")
        self.export_status_label.show()
        self._update_buttons()

    def _retry(self) -> None:
        action = getattr(self.controller, "error_action", "retry")
        if action == "settings":
            self._open_settings()
            return
        if action == "history" and hasattr(self.controller, "reload_history"):
            self.controller.reload_history(getattr(self.controller, "error_conversation_id", ""))
            return
        if hasattr(self.controller, "retry") and not self.controller.busy:
            message_id = getattr(self.controller, "error_message_id", "")
            if message_id:
                self.controller.retry(getattr(self.controller, "error_conversation_id", ""), message_id)
            else:
                self.controller.retry()

    def _stop_request(self):
        if hasattr(self.controller, "stop_request"):
            self.controller.stop_request(getattr(self.controller, "request_conversation_id", ""))

    def _render_messages(self) -> None:
        self._sync_image_storage_epoch()
        text_color = _native_color("#202734", "#e6edf5")
        label_color = _native_color("#416078", "#c0d5ea")
        muted_color = _native_color("#52637a", "#a7b5c8")
        error_color = _native_color("#9b3d30", "#f2a49b")
        scroll = self.transcript.verticalScrollBar()
        previous_scroll = scroll.value()
        near_latest = scroll.maximum() - previous_scroll <= 28
        conversation_id = getattr(self.controller, "current_conversation_id", None)
        error_scope = self._inline_error_scope()
        changed_conversation = conversation_id != self._rendered_conversation_id
        cards = []
        self._event_messages = {}
        self._image_messages = {}
        self._execution_messages = {}
        source_messages = [message for message in self.controller.messages
                           if message.get("conversation_id", conversation_id) == conversation_id]
        execution_views = {}
        factory = getattr(self.controller, "_view_redactor", None)
        redactor = factory() if callable(factory) else getattr(self.controller, "_redact_known_secrets", None)
        for message in source_messages:
            metadata = message.get("metadata")
            if isinstance(metadata, dict) and message.get("conversation_id", conversation_id) == conversation_id:
                execution = safe_execution(metadata.get("execution"), redactor)
                if execution is not None:
                    execution_views[str(message.get("id", ""))] = execution
        users = {message["id"] for message in source_messages
                 if message.get("role") == "user" and message.get("source") in {None, "", "user"}
                 and isinstance(message.get("id"), str) and message["id"]}
        replies = {str(message.get("id", "")): message for message in source_messages
                   if message.get("role") == "assistant" and message.get("source") in {None, "", "assistant"}}
        requests = {message_id: message_id for message_id in users}
        linked_users = set()
        for message_id, message in replies.items():
            metadata = message.get("metadata")
            reply_to = metadata.get("reply_to") if isinstance(metadata, dict) else None
            if isinstance(reply_to, str) and reply_to in users:
                requests[message_id] = reply_to
                linked_users.add(reply_to)
        # Explicit links are authoritative even when replies arrive after
        # another user. Only genuinely absent links use legacy adjacency.
        legacy_request = None
        for message in source_messages:
            message_id = str(message.get("id", ""))
            if message.get("role") == "user":
                legacy_request = message_id if message_id in users and message_id not in linked_users else None
            elif message_id in replies and legacy_request is not None:
                metadata = message.get("metadata")
                if not isinstance(metadata, dict) or "reply_to" not in metadata:
                    requests[message_id] = legacy_request
                    legacy_request = None
        for message_id in replies:
            execution = execution_views.get(requests.get(message_id))
            if execution is not None and execution["state"] in {"completed", "needs_user"}:
                execution_views.setdefault(message_id, execution)
        owners = {}
        execution_keys = {}
        for message in source_messages:
            message_id = str(message.get("id", ""))
            execution = execution_views.get(message_id)
            if execution is not None:
                key = (requests.get(message_id, message_id), execution["started_at"] or message_id)
                execution_keys[message_id] = key
                if key not in owners or message.get("role") == "assistant":
                    owners[key] = message_id
        for message in source_messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            is_user = role == "user"
            label, color = (("你", _native_color("#e9f3f9", "#1e3248")) if is_user
                            else ("鲸鱼娘", _native_color("#f5f7fb", "#192330")))
            message_id = str(message.get("id", ""))
            global_reason = (error_scope[2] if error_scope is not None
                and error_scope[:2] == (str(conversation_id), message_id) else "")
            anchor = f'<a name="message-{escape(message_id, quote=True)}"></a>' if message_id else ""
            badge = ""
            metadata = message.get("metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            status = metadata.get("request_status", "")
            if is_user and status in {"failed", "cancelled", "interrupted", "pending"}:
                agent = public_agent_metadata(message)
                caption = {"failed": "回复失败", "cancelled": "已停止", "interrupted": "上次请求中断",
                           "pending": "等待回复"}[status]
                if status == "failed" and agent["agent_error_code"]:
                    caption = "任务超时" if agent["agent_error_code"] == "timeout" else "任务未完成"
                badge = f' <span style="color:{error_color}">{caption}</span>'
                if agent["agent_error"] and agent["agent_error"] != global_reason:
                    badge += ' <span>' + escape(agent["agent_error"]) + '</span>'
                if status != "pending" and message_id:
                    cid = str(getattr(self.controller, "current_conversation_id", ""))
                    retry_state = (self.controller.message_retry_state(cid, message_id)
                                   if hasattr(self.controller, "message_retry_state") else {"available": True})
                    if retry_state["available"]:
                        badge += f' <a href="aura-retry:{quote(cid, safe="")}:{quote(message_id, safe="")}">重试回复</a>'
                    elif retry_state.get("status") and retry_state["status"] != global_reason:
                        badge += " <span>" + escape(retry_state["status"]) + "</span>"
            if message.get("source") in ("proactive_screen", "proactive_camera"):
                camera = message.get("source") == "proactive_camera"
                label, color = (("鲸鱼娘 · 来自摄像头" if camera else "鲸鱼娘 · 来自屏幕感知"),
                                _native_color("#e8f5ef", "#17332f"))
                if message_id and message.get("screen_event"):
                    self._event_messages[message_id] = message
                    href = "aura-event:" + quote(message_id, safe="")
                    badge = f' <a href="{escape(href, quote=True)}">查看情境</a>'
            # All provider/user text is escaped. It cannot insert HTML, URLs or images.
            execution = execution_views.get(message_id)
            display_text = str(message.get("content", ""))
            if (not is_user and execution is not None and execution["state"] in {"needs_user", "failed"}
                    and display_text.startswith(("任务尚未确认完成。", "任务尚未通过验收：存在未完成步骤、工具失败或产物校验问题。"))):
                display_text = "未完成：执行结果检查未通过。"
            content = _message_html(display_text)
            if execution is not None and owners.get(execution_keys[message_id]) == message_id:
                self._execution_messages[message_id] = execution
                trace_html = self._execution_html(message_id, execution)
                content = content + trace_html if is_user else trace_html + content
            image_id = metadata.get("image_id")
            if is_user and "image_id" in metadata:
                if message_id and isinstance(image_id, str) and IMAGE_ID.fullmatch(image_id):
                    self._image_messages[message_id] = (conversation_id, message_id, image_id)
                    href = "aura-image:" + quote(message_id, safe="")
                    content += f'<p style="color:{muted_color};font-size:13px">图片附件 · <a href="{escape(href, quote=True)}">查看图片</a></p>'
                else:
                    content += f'<p style="color:{error_color};font-size:13px">图片附件不可用，请重新发送图片。</p>'
            image_error = metadata.get("image_error_code")
            if is_user and status == "failed" and isinstance(image_error, str) and image_error in IMAGE_ERRORS:
                content += f'<p style="color:{error_color};font-size:13px">' + escape(IMAGE_ERRORS[image_error]) + '</p>'
            selection = metadata.get("selection_context")
            if is_user and isinstance(selection, dict):
                source = escape(str(selection.get("title") or "网页选区"))
                excerpt = escape(str(selection.get("text") or "")[:800]).replace("\n", "<br>")
                content += (f'<p style="color:{muted_color};font-size:13px">引用选区 · {source}<br>'
                            f'{excerpt}</p>')
            cards.append(
                anchor +
                f'<table width="100%" cellpadding="13" cellspacing="0" bgcolor="{color}">'
                f'<tr><td><span style="font-size:13px;color:{label_color}"><b>{label}</b>{badge}</span>'
                f'<p style="margin-top:7px;margin-bottom:0;line-height:155%">{content}</p>'
                '</td></tr></table><p style="font-size:6px;margin-top:0;margin-bottom:0">&nbsp;</p>'
            )
        if self._image_preview_reference is not None:
            reference = self._image_preview_reference
            if self._image_messages.get(reference[1]) != reference and self._image_preview_dialog is not None:
                self._image_preview_dialog.reject()
        if cards:
            html = "".join(cards)
        elif getattr(self.controller, "history_loading", False):
            heading = _native_color("#315b73", "#dbe8f7")
            hint = _native_color("#60758c", "#a7b5c8")
            html = (f'<p style="color:{heading};font-size:18px"><b>正在读取这段对话…</b></p>'
                    f'<p style="color:{hint}">聊天记录和草稿会保留，请稍候。</p>')
        else:
            heading = _native_color("#202020", "#e6edf5")
            html = f'<p style="font-size:21px;color:{heading}"><b>今天想聊些什么？</b></p>'
        link_style = '<style>a { color: #8cb8e8; }</style>' if current_theme() == "dark" else ""
        self.transcript.setHtml(f'<html><head>{link_style}</head><body style="color:{text_color};font-size:{self._chat_font_size}px;line-height:155%">{html}</body></html>')
        self._rendered_conversation_id = conversation_id
        self._rendered_inline_error_scope = error_scope
        scroll.setValue(scroll.maximum() if near_latest or changed_conversation else min(previous_scroll, scroll.maximum()))
        if self._pending_focus:
            self.focus_message(self._pending_focus)
        self._update_buttons()

    def _hide_event(self) -> None:
        self.event_details.hide()
        self.close_event_button.hide()

    def _open_event(self, url) -> None:
        if url.scheme() in {"aura-execution", "aura-context"}:
            message_id = unquote(url.toString().split(":", 1)[1])
            execution = self._execution_messages.get(message_id)
            if execution is not None:
                if url.scheme() == "aura-execution":
                    self._execution_expanded[message_id] = not self._execution_expanded.get(message_id, execution["state"] == "running")
                elif message_id in self._execution_context_expanded:
                    self._execution_context_expanded.remove(message_id)
                else:
                    self._execution_context_expanded.add(message_id)
                self._render_messages()
            return
        if url.scheme() == "aura-image":
            message_id = unquote(url.toString().split(":", 1)[1])
            self._open_image_preview(message_id)
            return
        if url.scheme() in {"http", "https"}:
            valid = validated_external_url(url.toString())
            if valid is not None:
                QDesktopServices.openUrl(valid)
            return
        if url.scheme() == "aura-copy":
            valid = validated_external_url(unquote(url.toString().split(":", 1)[1]))
            if valid is not None:
                QApplication.clipboard().setText(valid.toString(QUrl.ComponentFormattingOption.FullyEncoded))
            return
        if url.scheme() == "aura-retry":
            parts = url.toString().split(":", 2)
            if len(parts) == 3 and hasattr(self.controller, "retry"):
                self.controller.retry(unquote(parts[1]), unquote(parts[2]))
            return
        if url.toString() == "aura-settings:open":
            self._open_settings()
            return
        if url.scheme() != "aura-event":
            return
        message_id = unquote(url.toString().split(":", 1)[1])
        self._show_event(message_id)

    def _open_image_preview(self, message_id):
        epoch = self._sync_image_storage_epoch()
        if self._image_storage_closed or self._image_storage_maintenance():
            return False
        reference = self._image_messages.get(message_id)
        if (reference is None or reference[0] != getattr(self.controller, "current_conversation_id", None)
                or not callable(getattr(self.controller, "get_message_image", None))):
            return
        if self._image_preview_loading:
            if self._image_preview_dialog is not None:
                self._image_preview_dialog.raise_()
            return
        if self._image_preview_dialog is not None:
            self._image_preview_dialog.reject()
        available = self.screen().availableGeometry().size()
        maximum = QSize(max(160, min(1000, available.width() - 96)),
                        max(120, min(760, available.height() - 180)))
        dialog = QDialog(self)
        dialog.setObjectName("chatImagePreview")
        dialog.setWindowTitle("图片附件")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setMaximumSize(maximum.width() + 48, maximum.height() + 120)
        dialog.resize(max(320, maximum.width()), max(240, maximum.height()))
        layout = QVBoxLayout(dialog)
        status = QLabel("正在读取图片…")
        status.setObjectName("chatImagePreviewStatus")
        status.setTextFormat(Qt.TextFormat.PlainText)
        status.setWordWrap(True)
        layout.addWidget(status)
        image = _ImagePreviewLabel()
        image.setObjectName("chatImagePreviewImage")
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setMinimumSize(100, 100)
        image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(image, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._image_preview_dialog = dialog
        self._image_preview_reference = reference
        self._image_preview_loading = True

        def closed(_result):
            if self._image_preview_dialog is dialog:
                self._image_preview_dialog = None
                self._image_preview_reference = None
        dialog.finished.connect(closed)

        def loaded(result, error):
            current_epoch = self._sync_image_storage_epoch()
            self._image_preview_loading = False
            if self._image_preview_dialog is not dialog:
                return
            if current_epoch != epoch or self._image_messages.get(message_id) != reference:
                dialog.reject()
                return
            if error:
                status.setText(error if error in IMAGE_ERRORS.values() else IMAGE_ERRORS["image_unavailable"])
                return
            image.set_image(result)
            status.setText("图片附件")
        dialog.open()
        return self._submit_image_read(self._image_runner,
            lambda: _read_image_preview(self.controller, reference, maximum), loaded)

    def _show_event(self, message_id: str) -> None:
        message = self._event_messages.get(message_id)
        if not message:
            return
        event = message["screen_event"]
        self.event_details.setText(
            ("关联摄像头情境\n" if message.get("source") == "proactive_camera" else "关联屏幕情境\n")
            + f"应用：{event.get('app_name') or '未知'}\n"
            f"时间：{event.get('observed_at') or '未知'} · 类型：{event.get('event_type') or '未知'}\n"
            + str(event.get("context_summary") or "暂无情境摘要")
        )
        self.event_details.show()
        self.close_event_button.show()

    def focus_message(self, message_id: str) -> None:
        self._pending_focus = str(message_id)
        if any(str(message.get("id", "")) == str(message_id) for message in self.controller.messages):
            self.transcript.scrollToAnchor("message-" + str(message_id))
            self._show_event(str(message_id))
            self._pending_focus = None

    def _busy_changed(self, busy: bool) -> None:
        status = ("正在管理图片…" if self._image_storage_maintenance() else
                  (getattr(self.controller, "request_status", "") or "鲸鱼娘正在回复… 可继续浏览历史") if busy else
                  "正在加载对话…" if getattr(self.controller, "history_loading", False) else "准备就绪")
        self.status_label.setText(status)
        self.status_label.setToolTip(status)
        self.send_button.setText("等待回复…" if busy else "发送消息")
        self._update_buttons()

    def _request_status_changed(self, status: str) -> None:
        self._busy_changed(self.controller.busy)

    def _inline_error_scope(self):
        cid = str(getattr(self.controller, "current_conversation_id", "") or "")
        message = str(getattr(self.controller, "error", "") or "")
        target = str(getattr(self.controller, "error_message_id", "") or "")
        if (not cid or not message or not target
                or getattr(self.controller, "error_conversation_id", "") != cid):
            return None
        return cid, target, message

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        action = getattr(self.controller, "error_action", "retry")
        self.retry_button.setText({"settings": "打开设置", "history": "重新读取", "retry": "重试回复"}.get(action, "重试"))
        self.retry_button.setVisible(bool(message) and bool(action) and hasattr(self.controller, "retry"))
        scope = self._inline_error_scope()
        if self._rendered_conversation_id is not None and scope != self._rendered_inline_error_scope:
            self._rendered_inline_error_scope = scope
            self._render_messages()
        else:
            self._update_buttons()

    def _update_buttons(self) -> None:
        maintenance = self._image_storage_maintenance()
        busy = self.controller.busy or maintenance
        loading = getattr(self.controller, "history_loading", False)
        mode_supported = callable(getattr(self.controller, "set_agent_mode", None))
        agent_mode = bool(getattr(self.controller, "agent_mode", False))
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        pending = public_interaction(getattr(self.controller, "pending_agent_interaction", None), cid)
        self.agent_interaction_card.update_interaction(pending)
        can_steer = bool(not maintenance and busy and callable(getattr(self.controller, "steer_agent", None))
            and getattr(self.controller, "agent_running_conversation_id", None) == cid and pending is None)
        self.agent_mode_selector.setVisible(mode_supported)
        self.agent_mode_hint.setVisible(mode_supported and agent_mode)
        self.agent_mode_selector.setEnabled(not busy and not loading)
        self.agent_mode_selector.blockSignals(True)
        self.agent_mode_selector.setCurrentIndex(1 if agent_mode else 0)
        self.agent_mode_selector.blockSignals(False)
        self.input.setPlaceholderText("补充当前任务…" if can_steer else "告诉电脑 Agent 下一步…" if agent_mode else "发消息…")
        self.stop_button.setText("停止任务" if agent_mode else "停止生成")
        staged = self._current_staged_image()
        self.send_button.setText("补充指令" if can_steer else "等待回复…" if busy else "发送消息")
        self.send_button.setEnabled(bool(self.input.toPlainText().strip() or staged) and (not busy or can_steer and not staged) and not loading and not self._current_image_preparing())
        self.image_select_button.setEnabled(callable(getattr(self.controller, "import_local_image", None)) and not busy and not loading and not self._image_preparing)
        self.image_remove_button.setEnabled(not busy and not self._current_image_preparing())
        self._render_staged_image()
        self.clear_button.setEnabled(bool(self.controller.messages) and not busy and not loading)
        self.retry_button.setEnabled(not busy and not loading)
        if getattr(self.controller, "error_action", "") == "retry" and hasattr(self.controller, "message_retry_state"):
            retry_state = self.controller.message_retry_state(
                getattr(self.controller, "error_conversation_id", ""), getattr(self.controller, "error_message_id", ""))
            self.retry_button.setEnabled(retry_state["available"])
            self.retry_button.setToolTip(retry_state.get("status", ""))
        else:
            self.retry_button.setToolTip("")
        self.stop_button.setVisible(bool(getattr(self.controller, "can_stop", False)))
        selected = bool(self._selected_conversation_id())
        self.rename_button.setEnabled(selected and not loading)
        self.delete_button.setEnabled(selected and not busy and not loading)
        self.new_button.setEnabled(not loading)
        self.export_button.setEnabled(
            hasattr(self.controller, "export_conversation") and bool(self.controller.messages)
            and not busy and not loading and not getattr(self.controller, "export_busy", False))

    def _set_agent_mode(self, index) -> None:
        setter = getattr(self.controller, "set_agent_mode", None)
        if callable(setter) and not self.controller.busy and not getattr(self.controller, "history_loading", False):
            setter(bool(self.agent_mode_selector.itemData(index)))
        self._update_buttons()

    def _send(self) -> None:
        if self._image_storage_maintenance() or self._image_storage_closed:
            return
        text = self.input.toPlainText().strip()
        staged = self._current_staged_image()
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        draft = self._draft_owner.snapshot(cid)
        if self.controller.busy:
            steer = getattr(self.controller, "steer_agent", None)
            if (callable(steer) and text and not staged and not getattr(self.controller, "pending_agent_interaction", None)
                    and getattr(self.controller, "agent_running_conversation_id", None) == cid
                    and steer(text, conversation_id=cid)):
                if draft is not None:
                    self._draft_owner.accepted(cid, epoch=draft.epoch, revision=draft.revision)
                self._restore_editor_draft(cid)
            return
        if (not text and not staged or self._current_image_preparing()
                or self.controller.busy or getattr(self.controller, "history_loading", False)):
            return
        options = {"image_id": staged["image_id"]} if staged else {}
        if callable(getattr(self.controller, "set_agent_mode", None)):
            options["agent_mode"] = bool(getattr(self.controller, "agent_mode", False))
        if self.controller.send(text or "请描述这张图片。", **options):
            self._remove_staged_image()
            if draft is not None:
                self._draft_owner.accepted(cid, epoch=draft.epoch, revision=draft.revision)
            self._restore_editor_draft(cid)
            self.input.setFocus()

    def _agent_interaction_changed(self, *_args):
        self._update_buttons()

    def _answer_agent_interaction(self, interaction_id, answer, conversation_id):
        handler = getattr(self.controller, "answer_agent_interaction", None)
        return bool(callable(handler) and handler(interaction_id, answer, conversation_id=conversation_id))

    def _current_staged_image(self):
        return self._staged_images.get(str(getattr(self.controller, "current_conversation_id", "")))

    def _image_storage_maintenance(self):
        gate = self._image_storage_gate
        return gate is not None and gate.snapshot()["maintenance_active"]

    def _read_image_storage_epoch(self):
        value = getattr(self.controller, "image_storage_epoch", 0)
        return value if type(value) is int and 0 <= value <= 2**53 - 1 else 0

    def _sync_image_storage_epoch(self):
        epoch = self._read_image_storage_epoch()
        if epoch == self._image_content_epoch:
            return epoch
        self._image_content_epoch = epoch
        if self._image_preview_dialog is not None:
            self._image_preview_dialog.reject()
        if hasattr(self, "_thumbnail_cache"):
            self._thumbnail_cache.clear()
            self._thumbnail_cache_size = 0
            self._thumbnail_queue.clear()
            # Inflight reads still occupy their real worker slots. Their
            # callbacks compare this new epoch before publishing any pixels.
        return epoch

    def _image_content_changed(self, *_args):
        self._sync_image_storage_epoch()
        self._render_messages()
        publish = getattr(self, "_queue_web_state", None)
        if callable(publish):
            publish()

    def _finish_image_read(self, completion):
        completion()

    def _submit_image_read(self, runner, operation, callback):
        """Hold physical IO and its queued GUI completion under this owner."""
        if self._image_storage_closed:
            return False
        gate = self._image_storage_gate
        if gate is None:
            return runner.submit(operation, callback) is not None
        physical = None
        try:
            physical = gate.activity(self._image_storage_owner)
            completion = gate.activity(self._image_storage_owner)
        except ImageStorageGateError:
            if physical is not None:
                gate.release(physical)
            callback(None, IMAGE_ERRORS["image_unavailable"])
            return False
        lock = threading.Lock()
        state = {"done": False, "failed_submit": False, "delivered": False,
                 "result": None, "error": ""}

        def complete(result, error):
            with lock:
                if state["delivered"]:
                    return
                state["delivered"] = True
            try:
                if not self._image_storage_closed:
                    callback(result, error)
            finally:
                gate.release(completion)
                self._notify_image_storage_changed()

        def queue_completion():
            with lock:
                result, error = state["result"], state["error"]
            try:
                self._image_read_finished.emit(lambda: complete(result, error))
            except RuntimeError:
                gate.release(completion)

        def run():
            try:
                result = gate.run(physical, operation)
                with lock:
                    state["result"] = result
                return result
            except BaseException as exc:
                with lock:
                    state["error"] = IMAGE_ERRORS.get(getattr(exc, "code", None), IMAGE_ERRORS["image_unavailable"])
                raise
            finally:
                with lock:
                    state["done"] = True
                    fallback = state["failed_submit"]
                if fallback:
                    queue_completion()
                elif self._image_storage_closed:
                    gate.release(completion)
                self._notify_image_storage_changed()

        def submission_failed():
            with lock:
                state["failed_submit"] = True
                if not state["error"]:
                    state["error"] = IMAGE_ERRORS["image_unavailable"]
                done = state["done"]
            cancelled = gate.release(physical)
            if cancelled or done:
                queue_completion()
            self._notify_image_storage_changed()

        try:
            accepted = runner.submit(run, complete)
        except BaseException as exc:
            submission_failed()
            if not isinstance(exc, Exception):
                raise
            return False
        if accepted is None:
            submission_failed()
            return False
        self._notify_image_storage_changed()
        return True

    def _notify_image_storage_changed(self):
        notify = getattr(self.controller, "notify_image_storage_changed", None)
        if callable(notify):
            try: notify()
            except RuntimeError: pass  # The controller may be destroyed at exit.

    def _image_storage_changed(self):
        if getattr(self.controller, "_closed", False):
            self._shutdown_image_staging()
            return
        self._sync_image_storage_epoch()
        self._busy_changed(self.controller.busy)
        publish = getattr(self, "_queue_web_state", None)
        if callable(publish): publish()

    def _release_staged_image_token(self, conversation_id):
        token = self._image_stage_tokens.pop(conversation_id, None)
        if token is not None:
            self._image_storage_gate.release(token)

    def _shutdown_image_staging(self):
        """Actual shutdown only; hiding/reopening the view retains its stages."""
        if self._image_storage_closed:
            return
        self._image_storage_closed = True
        self._image_stage_serial += 1
        if self._image_storage_gate is not None:
            self._image_storage_gate.close_owner(self._image_storage_owner)
        self._image_stage_tokens.clear()
        self._staged_images.clear()
        self._image_stage_runner.shutdown()
        self._image_runner.shutdown()
        thumbnail_runner = getattr(self, "_thumbnail_runner", None)
        if thumbnail_runner is not None:
            thumbnail_runner.shutdown()
        self._notify_image_storage_changed()

    def _failed_image_stage_finished(self, serial, conversation_id):
        """Called only after cancellation-before-entry or real worker finally."""
        if serial != self._image_stage_active_serial:
            return
        self._image_preparing = False
        self._image_stage_conversation_id = None
        self._image_stage_active_serial = None
        self._image_stage_import_token = None
        self._release_staged_image_token(conversation_id)
        if (serial == self._image_stage_serial and not self._image_storage_closed
                and not getattr(self.controller, "_closed", False)
                and conversation_id not in getattr(self.controller, "_deleted_conversations", set())
                and conversation_id not in getattr(self.controller, "_invalidating_conversations", set())):
            self._image_stage_errors[conversation_id] = IMAGE_STAGE_ERROR
        self._image_stage_changed()

    def _current_image_preparing(self):
        return bool(self._image_preparing and self._image_stage_conversation_id ==
                    str(getattr(self.controller, "current_conversation_id", "")))

    @property
    def _image_stage_error(self):
        return self._image_stage_errors.get(str(getattr(self.controller, "current_conversation_id", "")), "")

    @_image_stage_error.setter
    def _image_stage_error(self, value):
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        if value: self._image_stage_errors[cid] = value
        else: self._image_stage_errors.pop(cid, None)

    def _image_stage_changed(self):
        self._update_buttons()
        publish = getattr(self, "_queue_web_state", None)
        if callable(publish):
            publish()
        self._notify_image_storage_changed()

    def _render_staged_image(self):
        staged = self._current_staged_image()
        preparing = self._current_image_preparing()
        self.image_stage_row.setVisible(bool(staged or preparing or self._image_stage_error))
        self.image_stage_label.setText("正在准备图片…" if preparing else
            self._image_stage_error or (staged["filename"] if staged else ""))
        self.image_remove_button.setVisible(bool(staged))
        preview_key = (staged["conversationId"], staged["image_id"]) if staged else None
        if preview_key == self._staged_preview_key:
            return
        self._staged_preview_key = preview_key
        self.image_stage_preview.clear()
        if staged and isinstance(staged.get("thumbnail"), dict):
            data = base64.b64decode(staged["thumbnail"]["src"].split(",", 1)[1])
            image = QPixmap()
            if image.loadFromData(data, "PNG"):
                self.image_stage_preview.setPixmap(image.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                                                               Qt.TransformationMode.SmoothTransformation))

    def _select_image(self):
        if (self._image_preparing or self.controller.busy or self._image_storage_closed
                or self._image_storage_maintenance() or getattr(self.controller, "history_loading", False)):
            return False
        if not callable(getattr(self.controller, "import_local_image", None)):
            return False
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        path, _ = QFileDialog.getOpenFileName(self, "选择聊天图片", "", "图片 (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return False
        return self._stage_local_image(path, cid)

    def _stage_local_image(self, path, conversation_id):
        """Only the native file picker supplies a path; never a web payload."""
        return self._stage_image_import(lambda: self.controller.import_local_image(path), conversation_id)

    def _stage_image_data(self, data, conversation_id):
        """Admission is cheap; byte decoding, validation and storage stay in a worker."""
        importer = getattr(self.controller, "import_image_data", None)
        if not callable(importer):
            return False
        name, mime, encoded = data.get("filename"), data.get("mimeType"), data.get("dataBase64")
        if (not isinstance(name, str) or not 1 <= len(name) <= 160
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or mime not in ("image/png", "image/jpeg", "image/webp")
                or not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_STAGE_IMAGE_BASE64
                or encoded.startswith(("data:", "file:", "http:", "https:"))):
            self._image_stage_errors[conversation_id] = IMAGE_STAGE_ERROR
            self._image_stage_changed()
            return False
        return self._stage_image_import(lambda: importer(name, mime, encoded), conversation_id)

    def _stage_image_import(self, importer, conversation_id):
        """One physical worker owns both picker and byte imports across conversations."""
        if (self._image_preparing or self._image_storage_closed
                or conversation_id != str(getattr(self.controller, "current_conversation_id", ""))
                or self.controller.busy or getattr(self.controller, "history_loading", False)
                or getattr(self.controller, "_closed", False)
                or conversation_id in getattr(self.controller, "_deleted_conversations", set())
                or conversation_id in getattr(self.controller, "_invalidating_conversations", set())
                or self._image_stage_runner._closed):
            return False
        gate, token = self._image_storage_gate, None
        if gate is not None:
            try:
                token = gate.activity(self._image_storage_owner, stage_on_success=True)
            except ImageStorageGateError:
                self._update_buttons()
                return False
        self._image_stage_serial += 1
        serial = self._image_stage_serial
        self._image_stage_active_serial = serial
        self._image_stage_import_token = token
        self._image_preparing = True
        self._image_stage_conversation_id = conversation_id
        self._staged_images.pop(conversation_id, None)
        self._image_stage_error = ""
        self._image_stage_changed()

        def import_image():
            metadata, image = importer()
            thumbnail = _image_thumbnail(_decode_image_preview(image, QSize(640, 640)))
            return {"conversationId": conversation_id, "image_id": metadata["id"],
                    **{key: metadata[key] for key in ("filename", "width", "height", "size")},
                    "thumbnail": thumbnail}

        entered, done, submission_failed = threading.Event(), threading.Event(), threading.Event()

        def read():
            entered.set()
            try:
                if submission_failed.is_set():
                    raise ImageStorageGateError("IMAGE_STORAGE_CANCELLED")
                return gate.run(token, import_image) if gate is not None else import_image()
            except ApiError as exc:
                code = exc.code if isinstance(exc.code, str) and exc.code in IMAGE_STAGE_STORAGE_ERRORS else "image_unavailable"
                return {"errorCode": code}
            finally:
                done.set()
                self._notify_image_storage_changed()
                if submission_failed.is_set():
                    try: self._image_stage_submission_finished.emit(serial, conversation_id)
                    except RuntimeError: pass

        def loaded(result, error):
            # A clear/delete invalidates the result, while the single physical
            # import remains busy until its worker has actually returned.
            if serial != self._image_stage_active_serial:
                if gate is not None: gate.release(token)
                return
            self._image_preparing = False
            self._image_stage_conversation_id = None
            self._image_stage_active_serial = None
            self._image_stage_import_token = None
            invalid = (serial != self._image_stage_serial or self._image_storage_closed
                       or getattr(self.controller, "_closed", False)
                       or conversation_id in getattr(self.controller, "_deleted_conversations", set())
                       or conversation_id in getattr(self.controller, "_invalidating_conversations", set()))
            previous = self._image_stage_tokens.pop(conversation_id, None)
            try:
                if not invalid:
                    failure = result.get("errorCode") if isinstance(result, dict) else None
                    if not error and isinstance(result, dict) and failure is None:
                        stage = gate.handoff_to_stage(token) if gate is not None else None
                        if gate is None or stage is not None:
                            if stage is not None: self._image_stage_tokens[conversation_id] = stage
                            self._staged_images[conversation_id] = result
                    else:
                        self._image_stage_errors[conversation_id] = IMAGE_STAGE_STORAGE_ERRORS.get(failure, IMAGE_STAGE_ERROR)
            finally:
                if gate is not None:
                    if previous is not None: gate.release(previous)
                    gate.release(token)
            self._image_stage_changed()

        submission_error = None
        try:
            submitted = self._image_stage_runner.submit(read, loaded)
        except BaseException as exc:
            submitted = None
            submission_error = exc
        if submitted is None:
            submission_failed.set()
            cancelled = gate.release(token) if gate is not None else not entered.is_set()
            self._notify_image_storage_changed()
            if cancelled or done.is_set():
                self._failed_image_stage_finished(serial, conversation_id)
            if submission_error is not None and not isinstance(submission_error, Exception):
                raise submission_error
            return False
        return True

    def _invalidate_staged_image(self, conversation_id):
        self._release_staged_image_token(conversation_id)
        self._staged_images.pop(conversation_id, None)
        self._image_stage_errors.pop(conversation_id, None)
        if self._image_stage_conversation_id == conversation_id:
            self._image_stage_serial += 1
            self._image_stage_conversation_id = None
            if self._image_stage_import_token is not None:
                self._image_storage_gate.release(self._image_stage_import_token)
        self._image_stage_changed()

    def _remove_staged_image(self):
        if self._current_image_preparing():
            return False
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        self._staged_images.pop(cid, None)
        self._release_staged_image_token(cid)
        self._image_stage_error = ""
        self._image_stage_changed()
        return True

    def _confirm_clear(self) -> None:
        if self.controller.busy or not self.controller.messages:
            return
        answer = QMessageBox.question(
            self, "清空当前会话", "永久清空当前会话的消息和屏幕事件？长期记忆不受影响。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.clear()
