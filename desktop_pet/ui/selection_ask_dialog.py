"""A small, explicit question composer for a user-selected page snapshot.

The dialog only displays and emits plain text. Capture, persistence, networking
and opening the shared conversation remain the application's responsibility.
"""

from html import escape
from pathlib import Path
import re
from urllib.parse import unquote

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from ..config import DEFAULT_IMAGE
from ..selection.context import SelectionContext
from .chat_panel import MessageInput
from .styles import PANEL_STYLE
from .window_state import bounded_rect


def _label(text="", name="hint"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class _CompactSourceLabel(QLabel):
    """Keep source metadata on one line, without dropping the full reference."""

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setObjectName(name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def setText(self, text):
        self._full_text = str(text)
        # Tooltips also accept rich text. Escape page-supplied titles and URLs.
        self.setToolTip("<qt>" + escape(self._full_text) + "</qt>")
        self.setAccessibleDescription(self._full_text)
        self._update_text()

    def _update_text(self):
        self.ensurePolished()
        self.setFixedHeight(self.fontMetrics().height() + 2)
        super().setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()


class SelectionAskDialog(QDialog):
    """Retain the draft until the application actually accepts the question."""

    ask_requested = Signal(str, object)
    chat_requested = Signal()

    def __init__(self, context: SelectionContext | dict, *, parent=None, icon_path=None):
        super().__init__(parent)
        self._busy = False
        self._submitted = False
        self._original_context = None
        self.context = None
        self._positioned = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_to_available_geometry)
        self.setObjectName("selectionAskDialog")
        self.setWindowTitle("问问鲸鱼娘")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(380, 360)
        self.resize(540, 520)
        self.setStyleSheet(PANEL_STYLE + """
QDialog#selectionAskDialog QLabel#selectionHeading { font-size: 22px; font-weight: 600; }
QDialog#selectionAskDialog QLabel#selectionSource { font-size: 16px; color: #344054; }
QDialog#selectionAskDialog QLabel#hint { font-size: 14px; }
QDialog#selectionAskDialog QPlainTextEdit { font-size: 16px; }
QDialog#selectionAskDialog QPushButton, QDialog#selectionAskDialog QToolButton { font-size: 14px; }
QDialog#selectionAskDialog QToolButton#selectionPreviewToggle { border: none; background: transparent;
    padding: 4px 0; text-align: left; color: #36536d; }
""")
        icon = QIcon(str(Path(icon_path) if icon_path is not None else DEFAULT_IMAGE))
        if not icon.isNull():
            self.setWindowIcon(icon)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(32, 32)
        if not icon.isNull():
            self.icon_label.setPixmap(icon.pixmap(32, 32))
        else:
            self.icon_label.hide()
        heading.addWidget(self.icon_label)
        heading.addWidget(_label("问问鲸鱼娘", "selectionHeading"), 1)
        layout.addLayout(heading)

        self.context_scroll = QScrollArea()
        self.context_scroll.setObjectName("selectionContextScroll")
        self.context_scroll.setWidgetResizable(True)
        self.context_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.context_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.context_scroll.setMinimumHeight(82)
        content = QWidget()
        context_layout = QVBoxLayout(content)
        context_layout.setContentsMargins(0, 0, 4, 0)
        context_layout.setSpacing(7)
        self.source_label = _CompactSourceLabel("selectionSource")
        context_layout.addWidget(self.source_label)
        # The actual selection comes before optional metadata and explanations.
        # Long article addresses must never push the quoted text out of view.
        self.selection_toggle, self.selection_preview = self._preview(
            context_layout, "selectionTextPreview", expanded=True
        )
        self.url_label = _CompactSourceLabel("selectionSourceUrl")
        self.url_label.setOpenExternalLinks(False)
        context_layout.addWidget(self.url_label)
        self.page_toggle, self.page_preview = self._preview(
            context_layout, "selectionPagePreview", expanded=False
        )
        self.notes_label = _label()
        self.notes_label.setAccessibleName("页面内容获取说明")
        self.notes_label.hide()
        context_layout.addWidget(self.notes_label)
        self.privacy_label = _label(
            "发送时仅附上这次所选文字和有限的当前页文字，不启用连续截图。网页内容仅作为参考资料。"
        )
        context_layout.addWidget(self.privacy_label)
        context_layout.addStretch(1)
        self.context_scroll.setWidget(content)
        layout.addWidget(self.context_scroll, 1)

        shortcuts = QHBoxLayout()
        shortcuts.setSpacing(6)
        self.quick_buttons = []
        for text, prompt in (
            ("解释一下", "请结合当前页面，解释一下我选中的内容。"),
            ("总结重点", "请结合当前页面，总结我选中内容的重点。"),
            ("翻译成中文", "请把我选中的内容翻译成中文，必要时结合页面背景说明。"),
        ):
            button = QPushButton(text)
            button.setMinimumHeight(34)
            button.setToolTip("填入问题，修改后再发送")
            button.clicked.connect(lambda _checked=False, value=prompt: self._fill_question(value))
            shortcuts.addWidget(button)
            self.quick_buttons.append(button)
        layout.addLayout(shortcuts)

        self.input = MessageInput()
        self.input.setObjectName("selectionQuestionInput")
        self.input.setAccessibleName("对选中内容的问题")
        self.input.setPlaceholderText("想问什么？Enter 发送，Shift + Enter 换行")
        reading_font = QFont("Microsoft YaHei UI")
        reading_font.setPixelSize(16)
        self.input.setFont(reading_font)
        self.input.setMinimumHeight(80)
        self.input.setMaximumHeight(108)
        layout.addWidget(self.input)
        self.error_label = _label("", "error")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        footer = QHBoxLayout()
        self.chat_button = QPushButton("打开完整聊天")
        self.chat_button.setMinimumHeight(36)
        self.chat_button.clicked.connect(self.chat_requested.emit)
        footer.addWidget(self.chat_button)
        footer.addStretch(1)
        self.send_button = QPushButton("发送提问")
        self.send_button.setObjectName("primaryButton")
        self.send_button.setAccessibleName("发送这次选区提问")
        self.send_button.setMinimumHeight(36)
        self.send_button.clicked.connect(self._send)
        footer.addWidget(self.send_button)
        layout.addLayout(footer)
        self.input.textChanged.connect(self._update_buttons)
        self.input.send_requested.connect(self._send)
        self.set_context(context)

    def _fit_to_available_geometry(self, area=None, *, center=False):
        """Fit this transient composer, including its native title bar."""
        if self.isMinimized() or self.isMaximized():
            return
        if area is None:
            screen = QGuiApplication.screenAt(QCursor.pos()) if center else self.screen()
            screen = screen or QGuiApplication.primaryScreen()
            if screen is None:
                return
            area = screen.availableGeometry()
        area = QRect(area)
        if not area.isValid():
            return
        frame = self.frameGeometry()
        extra_width = max(0, frame.width() - self.width())
        extra_height = max(0, frame.height() - self.height())
        target = QRect(frame)
        if center:
            target.moveCenter(area.center())
        fitted = bounded_rect(target, [area])
        width = max(1, fitted.width() - extra_width)
        height = max(1, fitted.height() - extra_height)
        self.setMinimumSize(min(380, width), min(360, height))
        self.resize(width, height)
        self.move(fitted.topLeft())

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_to_available_geometry(center=not self._positioned)
        self._positioned = True
        # Windows finishes applying frame margins after the first show event.
        # An owned timer is also cancelled if this transient dialog is deleted.
        self._fit_timer.start(0)

    def closeEvent(self, event):
        self._fit_timer.stop()
        super().closeEvent(event)

    def _preview(self, layout, name, *, expanded):
        toggle = QToolButton()
        toggle.setObjectName("selectionPreviewToggle")
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.setCheckable(True)
        toggle.setChecked(expanded)
        toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(toggle)
        preview = QPlainTextEdit()
        preview.setObjectName(name)
        preview.setReadOnly(True)
        preview.setMinimumHeight(92)
        preview.setMaximumHeight(128)
        preview.setVisible(expanded)
        layout.addWidget(preview)

        def show_preview(checked):
            preview.setVisible(checked)
            toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

        toggle.toggled.connect(show_preview)
        return toggle, preview

    def set_context(self, context):
        """Replace the explicit snapshot without rereading the current browser."""
        parsed = SelectionContext.from_payload(context)
        self._original_context = context
        self.context = parsed
        self._submitted = False
        source = parsed.title or parsed.app_name or "当前页面"
        self.source_label.setText("来自：" + source)
        display_url = re.sub(r"[\x00-\x20\x7f]+", " ", unquote(parsed.url)).strip()
        self.url_label.setText(display_url)
        self.url_label.setToolTip("<qt>" + escape(parsed.url) + "</qt>")
        self.url_label.setAccessibleDescription(parsed.url)
        self.url_label.setVisible(bool(parsed.url))
        self.notes_label.setText("\n".join(parsed.notes))
        self.notes_label.setVisible(bool(parsed.notes))
        self.selection_preview.setPlainText(parsed.text)
        self.selection_toggle.setText(f"已选文字 · {len(parsed.text)} 字" + ("（已截取）" if parsed.text_truncated else ""))
        self.page_preview.setPlainText(parsed.page_text)
        self.page_toggle.setText(f"当前页参考 · {len(parsed.page_text)} 字" + ("（已截取）" if parsed.page_text_truncated else ""))
        self.page_toggle.setVisible(bool(parsed.page_text))
        self.page_preview.setVisible(bool(parsed.page_text) and self.page_toggle.isChecked())
        self.show_error("")
        self._update_buttons()
        self.context_scroll.verticalScrollBar().setValue(0)

    def _fill_question(self, text):
        if self._busy or self._submitted:
            return
        self.input.setPlainText(text)
        self.input.setFocus()

    def _update_buttons(self):
        available = not self._busy and not self._submitted
        self.send_button.setEnabled(available and bool(self.input.toPlainText().strip()))
        self.input.setReadOnly(not available)
        for button in self.quick_buttons:
            button.setEnabled(available)
        self.chat_button.setEnabled(available)

    def _send(self):
        question = self.input.toPlainText().strip()
        if self._busy or self._submitted or not question:
            return
        self._submitted = True
        self.show_error("")
        self._update_buttons()
        self.ask_requested.emit(question, self._original_context)

    def set_busy(self, busy):
        self._busy = bool(busy)
        if not self._busy:
            self._submitted = False
        self.send_button.setText("正在提交…" if self._busy or self._submitted else "发送提问")
        self._update_buttons()

    def show_error(self, text):
        """A rejected request retains its draft and remains available to retry."""
        if text:
            self._busy = False
            self._submitted = False
        self.error_label.setText(str(text))
        self.error_label.setVisible(bool(text))
        if hasattr(self, "send_button"):
            if text:
                self.send_button.setText("重试提问")
            self._update_buttons()
