"""Offline assistant-ui frontend connected to the existing Qt chat controller.

The native panel remains available as a recovery view and owns the established
file dialogs and conversation actions. No API credentials enter the web page.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from .chat_panel import ChatPanel, validated_external_url


WEB_ROOT = Path(__file__).with_name("web_chat").resolve()
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
        super().__init__(*args, **kwargs)
        self.bridge = ChatBridge(self)
        self.web_view = None
        self._web_profile = None
        self._web_initialized = False
        self._web_signals_connected = False
        self._frontend_ready = False
        self._focus_request = {"id": "", "serial": 0}
        self._web_drafts = self._drafts
        self._native_mode = False
        self.input.textChanged.connect(self._native_draft_changed)
        self._web_publish_timer = QTimer(self)
        self._web_publish_timer.setSingleShot(True)
        self._web_publish_timer.timeout.connect(self._publish_web_state)
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
                     "export_finished", "retry_availability_changed", "reading_font_changed"):
            signal = getattr(self.controller, name, None)
            if signal is not None:
                signal.connect(self._queue_web_state)
        perception = getattr(self.controller, "perception_controller", None)
        if perception is not None:
            perception.runtime_changed.connect(self._queue_web_state)
        camera = getattr(self.controller, "camera_controller", None)
        if camera is not None and hasattr(camera, "runtime_changed"):
            camera.runtime_changed.connect(self._queue_web_state)

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
            self._update_buttons()

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
        self.bridge.stateChanged.emit(self.bridge.getState())

    def web_state(self):
        chat = self.controller
        cid = str(getattr(chat, "current_conversation_id", ""))
        messages = []
        for index, message in enumerate(chat.messages):
            if message.get("role") not in ("user", "assistant"):
                continue
            row = {key: str(message.get(key) or "") for key in ("role", "content", "source")}
            row["id"] = str(message.get("id") or f"{cid}-message-{index}")
            row["metadata"] = {"request_status": str(message.get("metadata", {}).get("request_status", ""))}
            selection = message.get("metadata", {}).get("selection_context")
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
            "status": self.status_label.text(),
            "contextUsage": str(getattr(chat, "context_usage", "")),
            "exportBusy": bool(getattr(chat, "export_busy", False)),
            "exportStatus": self.export_status_label.text(),
            "canSettings": callable(self._settings_opener),
            "canMemory": self.memory_controller is not None or callable(self._memory_opener),
            "canExport": hasattr(chat, "export_conversation"),
            "canBudget": hasattr(chat, "update_context_budget"),
            "focusRequest": dict(self._focus_request),
            "drafts": dict(self._web_drafts),
            "perception": perception.runtime_snapshot() if perception is not None else None,
        }

    def dispatch_web_action(self, action, data):
        chat = self.controller
        cid = str(getattr(chat, "current_conversation_id", ""))
        loading = bool(getattr(chat, "history_loading", False))
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
            self._frontend_ready = True
            self._web_timeout.stop()
            self.loading_label.hide()
            self.reload_button.hide()
            self.compatibility_button.hide()
            if self.web_view is not None:
                self.web_view.show()
            self.web_ready.emit(True)
            return True
        if action == "send":
            text = str(data.get("text") or "").strip()
            if (not text or chat.busy or loading
                    or str(data.get("conversationId", "")) != cid):
                return False
            accepted = bool(chat.send(text))
            if accepted and self._web_drafts.get(cid, "").strip() == text:
                self._web_drafts.pop(cid, None)
                self.input.clear()
            return accepted
        if action == "draft":
            target = str(data.get("conversationId") or "")
            self._web_drafts[target] = str(data.get("text") or "")
            if target == cid:
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
            return bool(chat.open_conversation(str(data.get("id") or "")))
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
