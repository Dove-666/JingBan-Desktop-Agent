"""Own the pet, active persistent conversation and lazy independent panels."""

from pathlib import Path
from PySide6.QtCore import QObject, Signal

from .chat_controller import ChatController
from .config import PetConfig
from .pet_controller import PetController
from .pet_window import PetWindow
from .settings_manager import SettingsManager
from .perception.controller import PerceptionController
from .perception.camera_controller import CameraController
from .memory.service import MemoryService
from .memory_controller import MemoryController, LazyMemoryService
from .ui.debug_panel import ChatWindow, DebugPanel
from .tray import DesktopTray
from .ui.window_state import WindowPlacement
from .remote.controller import RemoteController
from .selection.ipc import SelectionInbox
from .selection.context import SelectionContext


class ApplicationController(QObject):
    finished = Signal()

    def __init__(self, config: PetConfig, settings: SettingsManager | None = None,
                 api_client=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings if settings is not None else SettingsManager()
        self.pet = PetWindow(config)
        self.pet_controller = PetController(self.pet, self.settings, parent=self)
        self.pet._placement = WindowPlacement(self.pet, self.settings.config_path, "pet", remember_size=False,
                                              fit_to_screen=self.pet._fit_to_screen,
                                              placement_bounds=lambda: self.pet.character_placement_bounds)
        memory_dir = Path(self.settings.config_path).parent / "memory"
        self.memory_service = LazyMemoryService(lambda: MemoryService(self.settings, memory_dir))
        self.memory = MemoryController(self.memory_service, parent=self)
        self.chat = ChatController(self.settings, api_client=api_client, parent=self,
                                   memory_service=self.memory_service)
        self.chat.emotion_requested.connect(self._set_chat_expression)
        self.chat.reply_received.connect(self._show_chat_reply)
        self.chat.conversation_invalidated.connect(self._dismiss_invalidated_speech)
        self.chat.memory_status_changed.connect(self.memory._status)
        self.chat.memory_saved.connect(self.memory.notify_memories_saved)
        self.perception = PerceptionController(self.settings, self.pet_controller, self.chat, parent=self)
        self.chat.perception_controller = self.perception
        self.perception.settings_requested.connect(self.open_perception_settings)
        self.camera = CameraController(self.settings, self.pet_controller, self.chat, parent=self)
        self.camera.agent = self.perception.agent
        self.camera.other_visual_controller = self.perception
        self.perception.other_visual_controller = self.camera
        self.chat.camera_controller = self.camera
        self.camera.settings_requested.connect(self.open_camera_settings)
        self.debug_panel: DebugPanel | None = None
        self.chat_window: ChatWindow | None = None
        self.memory_panel = None
        self._shutting_down = False
        self.shutdown_errors = []
        self.pet.debug_requested.connect(self.open_debug_panel)
        self.pet.chat_requested.connect(self.open_chat_window)
        self.pet.closed.connect(self.shutdown)
        self.pet.bubble.conversation_requested.connect(self.open_linked_conversation)
        self.pet.click_handler = self._pet_clicked
        self.tray = DesktopTray(self, config.image_path, self)
        self.remote = RemoteController(self, parent=self)
        self._selection_dialogs = {}
        self.selection_inbox = None
        if getattr(self.settings, "config_path", ""):
            self.selection_inbox = SelectionInbox(self.settings.config_path, self.open_selection_question, self)
            self.selection_inbox.start()

    def open_selection_question(self, context, request_id):
        if self._shutting_down:
            return False
        if request_id in self._selection_dialogs:
            self._selection_dialogs[request_id].show()
            return True
        if len(self._selection_dialogs) >= 3:
            return False
        from .ui.selection_ask_dialog import SelectionAskDialog
        context = SelectionContext.from_payload(context)
        dialog = SelectionAskDialog(context, parent=self.pet, icon_path=self.pet.config.image_path)
        self._selection_dialogs[request_id] = dialog
        dialog.ask_requested.connect(lambda question, source, dialog=dialog:
                                     self._send_selection_question(dialog, question, source))
        dialog.chat_requested.connect(self.open_chat_window)
        def released(_result):
            self._selection_dialogs.pop(request_id, None)
            dialog.deleteLater()
        dialog.finished.connect(released)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.input.setFocus()
        return True

    def _send_selection_question(self, dialog, question, context):
        if self._shutting_down:
            dialog.show_error("桌宠正在退出，请重新启动后再提问。")
            return
        if self.chat.busy or self.chat.history_loading:
            dialog.show_error("聊天正在处理其他请求，问题与选区已保留，请稍后重试。")
            return
        dialog.set_busy(True)
        conversation_id = self.chat.current_conversation_id
        if not self.chat.send(question, conversation_id=conversation_id, selection_context=context):
            dialog.show_error(self.chat.error or "提问未能提交，问题与选区已保留，请稍后再试。")
            return
        self.open_chat_window()
        dialog.accept()

    def _set_chat_expression(self, emotion: str) -> None:
        if not self._shutting_down:
            # Chat progress and emotions must not speak the expression demo's
            # fixed phrases in place of the actual assistant response.
            self.pet_controller.apply_context_expression(emotion, source="chat")

    def _show_chat_reply(self, text: str, emotion: str, conversation_link) -> None:
        if not self._shutting_down:
            self.pet_controller.respond(text, emotion, conversation_link=conversation_link)

    def _dismiss_invalidated_speech(self, conversation_id: str) -> None:
        link = self.pet.bubble._conversation_link
        if link and link.get("conversation_id") == conversation_id:
            self.pet.bubble.dismiss()

    def _pet_clicked(self, expression):
        self.pet_controller.set_expression(expression)
        self.open_chat_window()

    def open_debug_panel(self) -> None:
        if self._shutting_down:
            return
        if self.debug_panel is None:
            self.debug_panel = DebugPanel(self.pet_controller, self.settings, self.chat,
                                          perception_controller=self.perception, memory_controller=self.memory,
                                          memory_opener=self.open_memory_window, settings_opener=self.open_api_settings,
                                          camera_controller=self.camera, remote_controller=self.remote)
            self.debug_panel._placement = WindowPlacement(self.debug_panel, self.settings.config_path, "debug")
        self.debug_panel.present()

    def open_chat_window(self) -> None:
        if self._shutting_down:
            return
        if self.chat_window is None:
            self.chat_window = ChatWindow(self.chat, memory_controller=self.memory,
                                          memory_opener=self.open_memory_window, settings_opener=self.open_api_settings)
            self.chat_window._placement = WindowPlacement(self.chat_window, self.settings.config_path, "chat")
        self.chat_window.present()

    def open_api_settings(self):
        self.open_debug_panel()
        if self.debug_panel is not None and not self._shutting_down:
            self.debug_panel.tabs.setCurrentIndex(1)

    def open_perception_settings(self):
        self.open_debug_panel()
        if self.debug_panel is not None and self.debug_panel.perception_panel is not None:
            self.debug_panel.tabs.setCurrentWidget(self.debug_panel.perception_panel)

    def open_camera_settings(self):
        self.open_debug_panel()
        if self.debug_panel is not None and self.debug_panel.camera_panel is not None:
            self.debug_panel.tabs.setCurrentWidget(self.debug_panel.camera_panel)

    def open_remote_settings(self):
        self.open_debug_panel()
        if self.debug_panel is not None and self.debug_panel.remote_panel is not None:
            self.debug_panel.tabs.setCurrentWidget(self.debug_panel.remote_panel)

    def open_linked_conversation(self, conversation_id, message_id):
        if self._shutting_down:
            return
        self.open_chat_window()
        self.chat.open_conversation(conversation_id, focus_message_id=message_id)

    def open_memory_window(self):
        if self._shutting_down:
            return
        if self.memory_panel is None:
            from .ui.memory_panel import MemoryPanel
            self.memory_panel = MemoryPanel(self.memory, self.pet)
            source_requested = getattr(self.memory_panel, "conversation_requested", None)
            if source_requested is not None:
                source_requested.connect(self.open_memory_conversation)
            self.memory_panel._placement = WindowPlacement(self.memory_panel, self.settings.config_path, "memory")
        self.memory_panel.present()

    def open_memory_conversation(self, conversation_id):
        if not conversation_id or self._shutting_down:
            return
        self.open_chat_window()
        self.chat.open_conversation(conversation_id)

    def toggle_pet_visibility(self):
        # Hiding is offered only by an existing tray, so the pet remains reachable.
        if self._shutting_down or not self.tray.available:
            return
        self.pet.setVisible(not self.pet.isVisible())
        self.tray.refresh()

    def show(self) -> None:
        self.pet.show()
        self.tray.refresh()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        def safely(label, operation):
            try:
                operation()
            except Exception:
                # Provider/credential exceptions may echo sensitive material.
                # Keep only a static component label for diagnostics.
                self.shutdown_errors.append(label)
        try:
            if self.selection_inbox is not None:
                safely("selection", self.selection_inbox.shutdown)
            for dialog in list(self._selection_dialogs.values()):
                safely("selection_dialog", dialog.close)
            for label, component in (("remote", self.remote), ("tray", self.tray), ("perception", self.perception),
                                     ("camera", self.camera),
                                     ("chat", self.chat), ("memory", self.memory)):
                safely(label, component.shutdown)
            for panel in (self.debug_panel, self.chat_window, self.memory_panel):
                if panel is not None:
                    safely("panel", panel.hide)
            safely("pet", self.pet.close)
        finally:
            # One failed optional service must not leave an invisible process
            # holding the single-instance lock forever.
            self.finished.emit()
