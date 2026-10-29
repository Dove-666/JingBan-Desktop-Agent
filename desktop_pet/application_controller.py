"""Own the pet and lazy independent panels for one in-memory application session."""

from PySide6.QtCore import QObject, Signal

from .chat_controller import ChatController
from .config import PetConfig
from .pet_controller import PetController
from .pet_window import PetWindow
from .settings_manager import SettingsManager
from .ui.debug_panel import ChatWindow, DebugPanel


class ApplicationController(QObject):
    finished = Signal()

    def __init__(self, config: PetConfig, settings: SettingsManager | None = None,
                 api_client=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings if settings is not None else SettingsManager()
        self.pet = PetWindow(config)
        self.pet_controller = PetController(self.pet, self.settings, parent=self)
        self.chat = ChatController(self.settings, api_client=api_client, parent=self)
        self.chat.emotion_requested.connect(self.pet_controller.set_expression)
        self.debug_panel: DebugPanel | None = None
        self.chat_window: ChatWindow | None = None
        self._shutting_down = False
        self.pet.debug_requested.connect(self.open_debug_panel)
        self.pet.chat_requested.connect(self.open_chat_window)
        self.pet.closed.connect(self.shutdown)

    def open_debug_panel(self) -> None:
        if self._shutting_down:
            return
        if self.debug_panel is None:
            self.debug_panel = DebugPanel(self.pet_controller, self.settings, self.chat)
        self.debug_panel.present()

    def open_chat_window(self) -> None:
        if self._shutting_down:
            return
        if self.chat_window is None:
            self.chat_window = ChatWindow(self.chat)
        self.chat_window.present()

    def show(self) -> None:
        self.pet.show()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.chat.shutdown()
        for panel in (self.debug_panel, self.chat_window):
            if panel is not None:
                panel.hide()
        self.pet.close()
        self.finished.emit()
