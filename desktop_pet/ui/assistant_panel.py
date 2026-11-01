"""Offline assistant-ui frontend connected to the existing Qt chat controller.

The native panel remains available as a recovery view and owns the established
file dialogs and conversation actions. No API credentials enter the web page.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QObject, QSize, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from .chat_panel import ChatPanel, _read_image_preview, _image_thumbnail, safe_execution, validated_external_url
from ..async_tasks import TaskRunner
from ..chat_images import IMAGE_ERRORS, IMAGE_ID
from ..agent_presentation import public_agent_metadata


WEB_ROOT = Path(__file__).with_name("web_chat").resolve()
THUMBNAIL_EDGE = 640
THUMBNAIL_CACHE_ENTRIES = 64
THUMBNAIL_CACHE_BYTES = 8 * 1024 * 1024
THUMBNAIL_QUEUE_LIMIT = 64
THUMBNAIL_WORKERS = 2


def _read_image_thumbnail(controller, reference):
    """Resolve an authorized message in a worker, returning fresh PNG pixels only."""
    decoded = _read_image_preview(controller, reference, QSize(THUMBNAIL_EDGE, THUMBNAIL_EDGE))
    return _image_thumbnail(decoded)
ASSISTANT_STYLE = """
QWidget#chatWindow, QWidget#chatPanel { background: #ffffff; color: #202734; }
QLabel#assistantLoading { color: #596575; background: #ffffff; padding: 24px; }
QPushButton#assistantReload { color: #344054; background: #f7f8fa;
    border: 1px solid #cbd5e2; border-radius: 8px; padding: 8px 16px; }
QDialog { background: #ffffff; color: #202734; }
QDialog QLabel { color: #344054; background: transparent; }
QDialog QLineEdit, QDialog QSpinBox { background: #ffffff; color: #202734;
    border: 1px solid #b9c7d8; border-radius: 6px; padding: 8px;
    selection-background-color: #d5e5f8; selection-color: #183b65; }
QDialog QPushButton { background: #f7f8fa; color: #344054;
    border: 1px solid #cbd5e2; border-radius: 6px; padding: 8px 16px; }
QDialog QPushButton:hover { background: #eaf0f6; }
"""


class ChatBridge(QObject):
    stateChanged = Signal(str)
    thumbnailReady = Signal(str)

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel

    @Slot(result=str)
    def getState(self):
        return json.dumps(self.panel.web_state(), ensure_ascii=False)

    @Slot(str, str, result=str)
    def dispatch(self, action, payload):
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                return json.dumps({"ok": False})
        except (TypeError, ValueError):
            return json.dumps({"ok": False})
        result = self.panel.dispatch_web_action(action, data)
        if action != "draft":
            self.panel._queue_web_state()
        return json.dumps({"ok": bool(result)})


class AssistantChatPanel(ChatPanel):
    """Both desktop chat views use the same offline React UI and Python state."""

    web_ready = Signal(bool)

    def __init__(self, *args, **kwargs):
        self._settings_page_opener = kwargs.pop("settings_page_opener", None)
        super().__init__(*args, **kwargs)
        if callable(self._settings_page_opener):
            self._consolidate_native_settings()
        self.bridge = ChatBridge(self)
        self.web_view = None
        self._web_profile = None
        self._web_initialized = False
        self._web_signals_connected = False
        self._frontend_ready = False
        self._focus_request = {"id": "", "serial": 0}
        self._web_drafts = self._drafts
        self._native_mode = False
        self._thumbnail_runner = TaskRunner(self)
        self._thumbnail_cache = OrderedDict()
        self._thumbnail_cache_size = 0
        self._thumbnail_queue = OrderedDict()
        self._thumbnail_inflight = {}
        self._thumbnail_conversation = str(getattr(self.controller, "current_conversation_id", ""))
        self._thumbnail_epoch = 0
        self.input.textChanged.connect(self._native_draft_changed)
        self._web_publish_timer = QTimer(self)
        self._web_publish_timer.setSingleShot(True)
        self._web_publish_timer.timeout.connect(self._publish_web_state)
        self._last_web_state_json = None
        self._web_timeout = QTimer(self)
        self._web_timeout.setSingleShot(True)
        self._web_timeout.setInterval(15000)
        self._web_timeout.timeout.connect(self._web_failed)
        self._web_start_timer = QTimer(self)
        self._web_start_timer.setSingleShot(True)
        self._web_start_timer.timeout.connect(self._ensure_web)
        # Keep native controls and drafts ready; populate their transcript only
        # when that view is used, so long web histories are not laid out twice.
        self.splitter.hide()
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().setSpacing(0)
        self.setStyleSheet(ASSISTANT_STYLE)
        self.loading_label = QLabel("正在准备聊天界面…", self)
        self.loading_label.setObjectName("assistantLoading")
        from PySide6.QtCore import Qt
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout().addWidget(self.loading_label, 1)
        self.reload_button = QPushButton("重新加载聊天界面", self)
        self.reload_button.setObjectName("assistantReload")
        self.reload_button.clicked.connect(self._reload_web)
        self.reload_button.hide()
        self.layout().addWidget(self.reload_button)
        self.compatibility_button = QPushButton("使用兼容聊天", self)
        self.compatibility_button.setObjectName("assistantCompatibility")
        self.compatibility_button.clicked.connect(self._activate_native)
        self.compatibility_button.hide()
        self.layout().addWidget(self.compatibility_button)
        for name in ("messages_changed", "conversations_changed", "conversation_changed",
                     "history_busy_changed", "busy_changed", "error_changed",
                     "request_status_changed", "context_usage_changed", "export_busy_changed",
                     "export_finished", "retry_availability_changed", "reading_font_changed", "execution_changed"):
            signal = getattr(self.controller, name, None)
            if signal is not None:
                signal.connect(self._queue_web_state)
        # The chat surface has no observation countdown/status bar. Listening
        # to these one-second timers needlessly republishes the whole history,
        # including while typing. Settings panels own their live subscriptions;
        # getState and explicit bridge actions still expose fresh visual state.

    def _consolidate_native_settings(self):
        """Keep the recovery view's navigation consistent with the web view."""
        self.sidebar.layout().insertWidget(0, self.new_button)
        settings_widgets = {self.settings_button, self.memory_button,
                            self.context_budget_button, self.context_usage_label}
        for action in self.tools_menu.actions():
            if callable(getattr(action, "defaultWidget", None)) and action.defaultWidget() in settings_widgets:
                action.setVisible(False)
        self.native_settings_button = QPushButton("设置", self.sidebar)
        self.native_settings_button.setObjectName("nativeOpenSettingsButton")
        self.native_settings_button.clicked.connect(lambda: self._settings_page_opener("general"))
        self.sidebar.layout().addWidget(self.native_settings_button)
        self.tools_button.setToolTip("当前会话操作")

    def _context_usage_changed(self, text):
        super()._context_usage_changed(text)
        if callable(self._settings_page_opener):
            self.tools_button.setToolTip("当前会话操作")

    def _show_context_budget(self):
        if callable(self._settings_page_opener):
            self._settings_page_opener("general")
        else:
            super()._show_context_budget()

    def showEvent(self, event):
        super().showEvent(event)
        if self.web_view is None and not self._native_mode:
            self._web_start_timer.start(0)

    def _render_messages(self):
        if getattr(self, "_native_mode", False):
            super()._render_messages()
        else:
            # Controller data remains authoritative. Rendering a hidden
            # QTextDocument here would block the same GUI thread that delivers
            # the WebChannel snapshot and processes keyboard/window events.
            if self._image_preview_dialog is not None or hasattr(self, "_thumbnail_cache"):
                self._refresh_web_image_references()
            self._update_buttons()

    def _conversation_changed(self):
        if (not getattr(self, "_native_mode", False)
                and getattr(self.controller, "current_conversation_id", "") != self._displayed_conversation_id):
            # Web drafts live in the shared dictionary, not the hidden editor.
            # The native handler may restore the incoming draft, but must not
            # replace the outgoing web draft with its stale document contents.
            self._displayed_conversation_id = None
        super()._conversation_changed()

    def _refresh_web_image_references(self, messages=None):
        """Keep native attachment actions current without laying out hidden HTML."""
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        references = {}
        for message in self.controller.messages if messages is None else messages:
            metadata = message.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            message_id, image_id = message.get("id"), metadata.get("image_id")
            if (message.get("role") == "user" and message.get("conversation_id", cid) == cid
                    and isinstance(message_id, str) and 0 < len(message_id) <= 200
                    and isinstance(image_id, str) and IMAGE_ID.fullmatch(image_id)):
                references[message_id] = (cid, message_id, image_id)
        self._image_messages = references
        if hasattr(self, "_thumbnail_cache"):
            if cid != self._thumbnail_conversation:
                self._thumbnail_conversation = cid
                self._thumbnail_epoch += 1
                self._thumbnail_queue.clear()
            valid = set(references.values())
            for reference in tuple(self._thumbnail_cache):
                if reference[0] == cid and reference not in valid:
                    self._drop_thumbnail(reference)
            for reference in tuple(self._thumbnail_queue):
                if reference not in valid:
                    self._thumbnail_queue.pop(reference, None)
        reference = self._image_preview_reference
        if (reference is not None and references.get(reference[1]) != reference
                and self._image_preview_dialog is not None):
            self._image_preview_dialog.reject()

    def _drop_thumbnail(self, reference):
        cached = self._thumbnail_cache.pop(reference, None)
        if cached is not None:
            self._thumbnail_cache_size -= cached[1]

    def _cache_thumbnail(self, reference, result, error):
        self._drop_thumbnail(reference)
        value = {"image_thumbnail_error": error} if error else {"image_thumbnail": result}
        cost = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if cost > THUMBNAIL_CACHE_BYTES:
            value = {"image_thumbnail_error": IMAGE_ERRORS["image_unavailable"]}
            cost = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        while self._thumbnail_cache and (len(self._thumbnail_cache) >= THUMBNAIL_CACHE_ENTRIES
                or self._thumbnail_cache_size + cost > THUMBNAIL_CACHE_BYTES):
            self._drop_thumbnail(next(iter(self._thumbnail_cache)))
        self._thumbnail_cache[reference] = (value, cost)
        self._thumbnail_cache_size += cost
        return value

    def _emit_thumbnail_ready(self, reference, value):
        # A dedicated result event cannot be lost when React batches snapshots
        # and the bounded cache has already evicted an earlier completion.
        if (reference[0] != str(getattr(self.controller, "current_conversation_id", ""))
                or self._image_messages.get(reference[1]) != reference):
            return
        payload = {"conversationId": reference[0], "messageId": reference[1], "imageId": reference[2]}
        if "image_thumbnail" in value:
            payload["thumbnail"] = value["image_thumbnail"]
        else:
            payload["error"] = value["image_thumbnail_error"]
        self.bridge.thumbnailReady.emit(json.dumps(payload, ensure_ascii=False))

    def _request_thumbnail(self, reference):
        if reference in self._thumbnail_cache:
            self._thumbnail_cache.move_to_end(reference)
            self._emit_thumbnail_ready(reference, self._thumbnail_cache[reference][0])
            return True
        epoch = self._thumbnail_epoch
        if self._thumbnail_inflight.get(reference) == epoch or self._thumbnail_queue.get(reference) == epoch:
            return True
        if len(self._thumbnail_queue) >= THUMBNAIL_QUEUE_LIMIT:
            return False
        self._thumbnail_queue[reference] = epoch
        self._pump_thumbnails()
        return True

    def _pump_thumbnails(self):
        for reference, epoch in tuple(self._thumbnail_queue.items()):
            if len(self._thumbnail_inflight) >= THUMBNAIL_WORKERS:
                break
            if reference in self._thumbnail_inflight:
                continue
            self._thumbnail_queue.pop(reference, None)
            if epoch != self._thumbnail_epoch or self._image_messages.get(reference[1]) != reference:
                continue
            self._thumbnail_inflight[reference] = epoch

            def loaded(result, error, reference=reference, epoch=epoch):
                if self._thumbnail_inflight.get(reference) == epoch:
                    self._thumbnail_inflight.pop(reference, None)
                self._refresh_web_image_references()
                if epoch == self._thumbnail_epoch and self._image_messages.get(reference[1]) == reference:
                    safe_error = error if error in (IMAGE_ERRORS["image_missing"], IMAGE_ERRORS["image_unavailable"]) else IMAGE_ERRORS["image_unavailable"]
                    value = self._cache_thumbnail(reference, result, safe_error if error or not isinstance(result, dict) else "")
                    self._emit_thumbnail_ready(reference, value)
                    self._queue_web_state()
                self._pump_thumbnails()

            self._thumbnail_runner.submit(lambda reference=reference: _read_image_thumbnail(self.controller, reference), loaded)

    def _ensure_web(self):
        try:
            self._create_web()
        except (ImportError, RuntimeError, OSError):
            self._discard_partial_web()
            self._web_failed()

    def _discard_partial_web(self):
        view, self.web_view = self.web_view, None
        connected, self._web_signals_connected = self._web_signals_connected, False
        self._web_profile = None
        self._web_initialized = False
        if view is not None:
            try:
                if connected:
                    view.loadFinished.disconnect()
                    view.renderProcessTerminated.disconnect()
                view.stop()
                view.hide()
                self.layout().removeWidget(view)
                view.deleteLater()
            except RuntimeError:
                pass

    def _create_web(self):
        if self._native_mode or self.web_view is not None and self._web_initialized:
            return
        if self.web_view is not None:
            self._discard_partial_web()
        from PySide6.QtCore import Qt
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineCore import (
            QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
            QWebEngineUrlRequestInterceptor,
        )
        from PySide6.QtWebEngineWidgets import QWebEngineView

        class LocalResources(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                url = info.requestUrl()
                allowed = url.scheme() in ("qrc", "data", "blob")
                if url.isLocalFile():
                    path = Path(url.toLocalFile()).resolve()
                    allowed = path == WEB_ROOT or WEB_ROOT in path.parents
                if not allowed:
                    info.block(True)

        class LocalPage(QWebEnginePage):
            def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
                # Markdown never navigates the privileged page to a website.
                return (url.scheme() == "about" or url.isLocalFile()
                        and Path(url.toLocalFile()).resolve() == WEB_ROOT / "index.html")

            def javaScriptConsoleMessage(page, level, message, line, source):
                # Never log user or provider messages through Chromium console.
                if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                    self._javascript_error = True

        self.web_view = QWebEngineView(self)
        self.web_view.setObjectName("assistantWebView")
        self.web_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._web_profile = QWebEngineProfile(self.web_view)
        self._web_profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self._web_profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self._resource_filter = LocalResources(self._web_profile)
        self._web_profile.setUrlRequestInterceptor(self._resource_filter)
        page = LocalPage(self._web_profile, self.web_view)
        page.setBackgroundColor(QColor("#ffffff"))
        settings = page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, False)
        channel = QWebChannel(page)
        channel.registerObject("bridge", self.bridge)
        page.setWebChannel(channel)
        self.web_view.setPage(page)
        self.web_view.loadFinished.connect(self._web_loaded)
        self.web_view.renderProcessTerminated.connect(lambda *_: self._web_failed())
        self._web_signals_connected = True
        self.layout().insertWidget(0, self.web_view, 1)
        self._web_initialized = True
        self._reload_web()

    def _reload_web(self):
        self._native_draft_changed()
        # A new document needs a fresh initial snapshot even when controller
        # data is identical to the page that is being replaced.
        self._web_publish_timer.stop()
        self._last_web_state_json = None
        self._native_mode = False
        self.splitter.hide()
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
        if self.web_view is None or not self._web_initialized:
            self._ensure_web()
            return
        self._frontend_ready = False
        self._javascript_error = False
        self.loading_label.setText("正在准备聊天界面…")
        self.reload_button.setText("重新加载聊天界面")
        self.loading_label.show()
        self.reload_button.hide()
        self.compatibility_button.hide()
        self.web_view.hide()
        self._web_timeout.start()
        self.web_view.load(QUrl.fromLocalFile(str(WEB_ROOT / "index.html")))

    def _web_loaded(self, success):
        if not success:
            self._web_failed()

    def _web_failed(self):
        if self._frontend_ready:
            self._frontend_ready = False
        self._web_timeout.stop()
        if self._native_mode:
            return
        self.loading_label.setText("聊天界面暂时未能加载。可重新加载或使用兼容聊天，草稿和记录会保留。")
        self.loading_label.show()
        self.reload_button.show()
        self.compatibility_button.show()
        if self.web_view is not None:
            self.web_view.hide()
        self.web_ready.emit(False)

    def _native_draft_changed(self):
        if getattr(self, "_native_mode", False):
            cid = str(getattr(self.controller, "current_conversation_id", ""))
            self._web_drafts[cid] = self.input.toPlainText()
            self._queue_web_state()

    def _activate_native(self):
        self._web_start_timer.stop()
        self._web_timeout.stop()
        self._native_mode = True
        self._frontend_ready = False
        if self.web_view is not None:
            self.web_view.hide()
        self.loading_label.hide()
        self.compatibility_button.hide()
        self.reload_button.setText("返回新版聊天")
        self.reload_button.show()
        cid = str(getattr(self.controller, "current_conversation_id", ""))
        self.input.setPlainText(self._web_drafts.get(cid, ""))
        self._render_messages()
        self.splitter.show()
        for shortcut in self._shortcuts:
            shortcut.setEnabled(True)
        self.input.setFocus()

    def _queue_web_state(self, *_args):
        if hasattr(self, "_web_publish_timer"):
            self._web_publish_timer.start(0)

    def _publish_web_state(self):
        payload = self.bridge.getState()
        if payload != self._last_web_state_json:
            self._last_web_state_json = payload
            self.bridge.stateChanged.emit(payload)

    def web_state(self):
        chat = self.controller
        cid = str(getattr(chat, "current_conversation_id", ""))
        source_messages = chat.messages
        self._refresh_web_image_references(source_messages)
        messages = []
        for index, message in enumerate(source_messages):
            if message.get("role") not in ("user", "assistant"):
                continue
            row = {key: str(message.get(key) or "") for key in ("role", "content", "source")}
            row["id"] = str(message.get("id") or f"{cid}-message-{index}")
            metadata = message.get("metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            row["metadata"] = {"request_status": str(metadata.get("request_status", ""))}
            # Persisted host error codes remain useful after the global error
            # banner has gone away. Never forward provider exception text.
            agent = public_agent_metadata(message)
            for name in ("agent_error_code", "agent_error"):
                if agent[name] is not None:
                    row["metadata"][name] = agent[name]
            if message.get("conversation_id", cid) == cid:
                execution = safe_execution(metadata.get("execution"), getattr(chat, "_redact_known_secrets", None))
                if execution is not None:
                    row["metadata"]["execution"] = execution
            if row["role"] == "user":
                reference = self._image_messages.get(row["id"])
                if reference is not None:
                    row["metadata"]["image_id"] = reference[2]
                    cached = self._thumbnail_cache.get(reference)
                    if cached is not None:
                        row["metadata"].update(cached[0])
                elif "image_id" in metadata:
                    row["metadata"]["image_unavailable"] = True
                image_error = metadata.get("image_error_code")
                if (row["metadata"]["request_status"] == "failed" and isinstance(image_error, str)
                        and image_error in IMAGE_ERRORS):
                    row["metadata"]["image_error"] = image_error
            selection = metadata.get("selection_context")
            if isinstance(selection, dict):
                # The archive retains the finite snapshot. UI snapshots only
                # carry a short source preview, rather than resending its page.
                notes = selection.get("notes")
                if not isinstance(notes, (list, tuple)):
                    notes = []
                row["metadata"]["selection_context"] = {
                    "text": str(selection.get("text") or "")[:2000],
                    "title": str(selection.get("title") or "")[:300],
                    "url": str(selection.get("url") or "")[:2048],
                    "page_characters": len(str(selection.get("page_text") or "")),
                    "notes": [str(note)[:200] for note in notes[:8]],
                }
            retry_state = (chat.message_retry_state(cid, row["id"]) if hasattr(chat, "message_retry_state")
                           else {"available": not chat.busy, "status": ""})
            row["retryAvailable"] = retry_state["available"]
            row["retryStatus"] = retry_state.get("status", "")
            event = message.get("screen_event")
            if isinstance(event, dict):
                row["screen_event"] = {key: str(event.get(key) or "") for key in
                                       ("app_name", "event_type", "observed_at", "context_summary", "source_kind")}
            messages.append(row)
        busy = bool(chat.busy)
        owner = getattr(chat, "_active_request_conversation", cid)
        perception = getattr(chat, "perception_controller", None)
        camera = getattr(chat, "camera_controller", None)
        error_retry = (chat.message_retry_state(getattr(chat, "error_conversation_id", ""),
                                               getattr(chat, "error_message_id", ""))
                       if hasattr(chat, "message_retry_state") else {"available": not busy, "status": ""})
        return {
            "ready": True, "conversationId": cid,
            "canCamera": camera is not None,
            "camera": camera.runtime_snapshot() if camera is not None and hasattr(camera, "runtime_snapshot") else None,
            "chatFontSize": getattr(chat, "reading_font_size", 16),
            "title": str(getattr(chat, "current_title", "和鲸鱼娘聊天")),
            "messages": messages,
            "conversations": [{"id": str(c["id"]),
                               "title": " ".join(str(c.get("title") or "新对话").split()),
                               "updatedAt": str(c.get("updated_at") or c.get("created_at") or ""),
                               "summary": str(c.get("summary") or ""),
                               "messageCount": c.get("message_count", 0)}
                              for c in getattr(chat, "conversations", [])],
            "busy": busy, "requestInCurrent": busy and owner == cid,
            "historyLoading": bool(getattr(chat, "history_loading", False)),
            "error": str(getattr(chat, "error", "")),
            "errorAction": str(getattr(chat, "error_action", "")),
            "errorConversationId": str(getattr(chat, "error_conversation_id", "")),
            "errorMessageId": str(getattr(chat, "error_message_id", "")),
            "errorRetryAvailable": error_retry["available"],
            "errorRetryStatus": error_retry.get("status", ""),
            "requestConversationId": str(getattr(chat, "request_conversation_id", owner) or ""),
            "canStop": bool(getattr(chat, "can_stop", False)),
            "canAgentMode": callable(getattr(chat, "set_agent_mode", None)),
            "agentMode": bool(getattr(chat, "agent_mode", False)),
            "status": self.status_label.text(),
            "contextUsage": str(getattr(chat, "context_usage", "")),
            "contextPolicy": "manual" if getattr(getattr(chat, "context_budget", None), "mode", "model") == "manual" else "model",
            "exportBusy": bool(getattr(chat, "export_busy", False)),
            "exportStatus": self.export_status_label.text(),
            "canSettings": callable(self._settings_opener),
            "windowChrome": bool(self.window().property("auraCustomChrome")),
            "canMemory": self.memory_controller is not None or callable(self._memory_opener),
            "canExport": hasattr(chat, "export_conversation"),
            "canBudget": hasattr(chat, "update_context_budget"),
            "focusRequest": dict(self._focus_request),
            "drafts": dict(self._web_drafts),
            "stagedImage": deepcopy(self._current_staged_image()),
            "imagePreparing": self._image_preparing,
            "imageError": self._image_stage_error,
            "perception": perception.runtime_snapshot() if perception is not None else None,
        }

    def dispatch_web_action(self, action, data):
        chat = self.controller
        cid = str(getattr(chat, "current_conversation_id", ""))
        loading = bool(getattr(chat, "history_loading", False))
        if action in {"selectImage", "removeStagedImage"}:
            if set(data) != {"conversationId"} or data.get("conversationId") != cid or loading or chat.busy:
                return False
            return self._select_image() if action == "selectImage" else self._remove_staged_image()
        if action in {"windowMove", "windowToggleMaximize"}:
            chrome = getattr(self.window(), "chrome", None)
            if chrome is None:
                return False
            if action == "windowMove":
                return chrome.start_move()
            chrome.toggle_maximized()
            return True
        if action == "openSettings":
            opener = self._settings_page_opener or self._settings_opener
            if callable(opener):
                opener()
                return True
            return False
        if action in {"openImage", "loadImageThumbnail"}:
            message_id = data.get("messageId")
            self._refresh_web_image_references()
            if (loading or data.get("conversationId") != cid
                    or not isinstance(message_id, str) or message_id not in self._image_messages
                    or not callable(getattr(chat, "get_message_image", None))):
                return False
            if action == "loadImageThumbnail":
                if set(data) != {"conversationId", "messageId"}:
                    return False
                return self._request_thumbnail(self._image_messages[message_id])
            self._open_image_preview(message_id)
            return True
        if action in {"cameraSettings", "openCameraSettings"}:
            camera = getattr(chat, "camera_controller", None)
            if camera is None:
                return False
            camera.settings_requested.emit()
            return True
        if action == "setChatFontSize" and hasattr(chat, "set_reading_font_size"):
            return bool(chat.set_reading_font_size(data.get("size")))
        if action == "setCameraPaused":
            camera = getattr(chat, "camera_controller", None)
            if camera is None or not isinstance(data.get("paused"), bool):
                return False
            camera.set_paused(data["paused"])
            return True
        if action == "pauseAllVisual":
            controllers = [getattr(chat, name, None) for name in ("perception_controller", "camera_controller")]
            for controller in controllers:
                if controller is not None:
                    controller.set_paused(True)
            return any(controller is not None for controller in controllers)
        if action == "ready":
            if self._native_mode:
                return False
            # A queued update could have been published before this document
            # attached its WebChannel listener. The ready acknowledgement must
            # not be suppressed by deduplication of that earlier update.
            self._last_web_state_json = None
            self._frontend_ready = True
            self._web_timeout.stop()
            self.loading_label.hide()
            self.reload_button.hide()
            self.compatibility_button.hide()
            if self.web_view is not None:
                self.web_view.show()
            self.web_ready.emit(True)
            return True
        if action == "setAgentMode":
            setter = getattr(chat, "set_agent_mode", None)
            if (not callable(setter) or chat.busy or loading
                    or set(data) != {"conversationId", "enabled"}
                    or data.get("conversationId") != cid or not isinstance(data.get("enabled"), bool)):
                return False
            return bool(setter(data["enabled"]))
        if action == "send":
            text = str(data.get("text") or "").strip()
            staged = self._current_staged_image()
            if (not text and not staged or chat.busy or loading or self._image_preparing
                    or str(data.get("conversationId", "")) != cid):
                return False
            options = {"image_id": staged["image_id"]} if staged else {}
            if callable(getattr(chat, "set_agent_mode", None)):
                selected_mode = bool(getattr(chat, "agent_mode", False))
                if "agentMode" in data and (not isinstance(data["agentMode"], bool) or data["agentMode"] != selected_mode):
                    return False
                options["agent_mode"] = selected_mode
            accepted = bool(chat.send(text or "请描述这张图片。", **options))
            if accepted:
                self._remove_staged_image()
            if accepted and self._web_drafts.get(cid, "").strip() == text:
                self._web_drafts.pop(cid, None)
                self.input.clear()
            return accepted
        if action == "draft":
            target = str(data.get("conversationId") or "")
            self._web_drafts[target] = str(data.get("text") or "")
            # The native document is hidden in web mode. Rebuilding it for each
            # keypress needlessly invalidates Qt layout/paint work. Switching to
            # native mode already restores the authoritative draft dictionary.
            if (self._native_mode and target == cid
                    and self.input.toPlainText() != self._web_drafts[target]):
                self.input.setPlainText(self._web_drafts[target])
            return True
        if action in {"openExternal", "copyLink"}:
            url = validated_external_url(data.get("url"))
            if url is None:
                return False
            if action == "copyLink":
                QApplication.clipboard().setText(url.toString(QUrl.ComponentFormattingOption.FullyEncoded))
                return True
            return bool(QDesktopServices.openUrl(url))
        if action == "copy":
            QApplication.clipboard().setText(str(data.get("text") or ""))
            return True
        if action == "search" and self._sessions_supported:
            chat.search_conversations(str(data.get("query") or "").strip())
            return True
        if action == "open" and self._sessions_supported and not loading:
            accepted = bool(chat.open_conversation(str(data.get("id") or "")))
            if accepted:
                # Navigation can briefly load another conversation and restore
                # the origin in one event turn. React still needs the final
                # acknowledgement to cancel any deferred per-conversation work.
                self._last_web_state_json = None
            return accepted
        if action == "new" and self._sessions_supported and not loading:
            return bool(chat.new_conversation())
        if action == "rename" and self._sessions_supported and not loading:
            title = str(data.get("title") or "").strip()
            target = str(data.get("id") or "")
            return bool(target and title and chat.rename_conversation(target, title))
        if action == "delete" and self._sessions_supported and not chat.busy and not loading:
            target = str(data.get("id") or "")
            if not target or not any(str(c["id"]) == target for c in getattr(chat, "conversations", [])):
                return False
            answer = QMessageBox.question(
                self, "删除会话", "永久删除此会话及其消息？长期记忆不受影响。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes and bool(chat.delete_conversation(target))
        if action == "clear" and not chat.busy and not loading and chat.messages:
            if str(data.get("conversationId") or "") != cid:
                return False
            answer = QMessageBox.question(
                self, "清空当前会话", "永久清空当前会话的消息？长期记忆不受影响。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes and cid == str(getattr(chat, "current_conversation_id", "")):
                chat.clear()
                return True
            return False
        if action == "retry" and not chat.busy and not loading:
            target, message_id = str(data.get("conversationId") or ""), str(data.get("messageId") or "")
            if target != cid or not message_id or not hasattr(chat, "retry"):
                return False
            return bool(chat.retry(target, message_id))
        if action == "stop" and hasattr(chat, "stop_request"):
            target = str(data.get("conversationId") or "")
            return bool(target and chat.stop_request(target))
        if action == "reloadHistory" and hasattr(chat, "reload_history"):
            return bool(chat.reload_history(str(data.get("conversationId") or "")))
        if action == "settings" and callable(self._settings_opener):
            self._open_settings()
            return True
        perception = getattr(chat, "perception_controller", None)
        if action == "setPerceptionPaused" and perception is not None and isinstance(data.get("paused"), bool):
            perception.set_paused(data["paused"])
            return True
        if action == "openPerceptionSettings" and perception is not None:
            perception.settings_requested.emit()
            return True
        if action == "memory" and (self.memory_controller is not None or callable(self._memory_opener)):
            self._open_memory()
            return True
        if action == "budget" and hasattr(chat, "update_context_budget"):
            self._show_context_budget()
            return True
        if action == "export" and str(data.get("conversationId") or "") == cid:
            if not self.export_button.isEnabled():
                return False
            self._export_conversation()
            return True
        if action == "focusEvent":
            self.focus_message(str(data.get("id") or ""))
            return True
        return False

    def focus_message(self, message_id):
        if getattr(self, "_native_mode", False):
            super().focus_message(message_id)
        else:
            self._pending_focus = str(message_id)
        if hasattr(self, "_focus_request"):
            self._focus_request = {"id": str(message_id), "serial": self._focus_request["serial"] + 1}
            self._queue_web_state()
