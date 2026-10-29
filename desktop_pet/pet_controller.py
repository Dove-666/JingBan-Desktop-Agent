"""One interface for panel, mouse, and chat-driven character changes."""

from PySide6.QtCore import QObject, Signal

from .animation_controller import AnimationState
from .expression_manager import ExpressionManager
from .pet_window import PetWindow
from .settings_manager import SettingsManager


FEEDBACK = {
    "neutral": "你好呀，我来啦！", "happy": "好开心呀！",
    "surprised": "咦，发生什么啦？", "sleepy": "有一点困啦…",
    "angry": "哼，我有点生气啦。", "thinking": "让我想一想…",
    "talking": "我在认真回应你。",
}


class PetController(QObject):
    scale_changed = Signal(int)
    expression_changed = Signal(str, str)
    error_occurred = Signal(str)

    def __init__(self, pet: PetWindow, settings: SettingsManager,
                 expressions: ExpressionManager | None = None, parent=None) -> None:
        super().__init__(parent)
        self.pet = pet
        self.settings = settings
        self.expressions = expressions or ExpressionManager()
        self._original = pet._source
        self._scale_percent = settings.load_scale()
        self._expression = "neutral"
        neutral = self.expressions.resolve("neutral")
        self._expression_status = neutral.status
        pet.set_scale_percent(self._scale_percent)
        if neutral.pixmap is not None:
            pet.set_character_source(neutral.pixmap)
        pet.click_handler = self.set_expression

    @property
    def scale_percent(self) -> int:
        return self._scale_percent

    @property
    def expression(self) -> str:
        return self._expression

    @property
    def expression_status(self) -> str:
        return self._expression_status

    def expression_catalog(self) -> list[tuple[str, str, str]]:
        return self.expressions.catalog()

    def set_scale(self, percent: int) -> None:
        if isinstance(percent, bool) or not isinstance(percent, int) or not 50 <= percent <= 200:
            self.error_occurred.emit("大小必须为 50%～200%。")
            return
        self.pet.set_scale_percent(percent)
        self._scale_percent = percent
        self.scale_changed.emit(percent)
        try:
            self.settings.save_scale(percent)
        except Exception:
            self.error_occurred.emit("大小已调整，但未能保存到本地；请检查配置目录权限。")

    def reset_scale(self) -> None:
        self.set_scale(100)

    def set_expression(self, code: str) -> None:
        try:
            expression = self.expressions.resolve(code)
        except ValueError:
            expression = self.expressions.resolve("neutral")
        self._expression = expression.code
        self._expression_status = expression.status
        self.pet.set_character_source(expression.pixmap if expression.pixmap is not None else self._original)
        animation = self.pet.animation
        if expression.code == "happy":
            animation.play_happy()
        elif expression.code == "surprised":
            animation.play_surprised()
        elif animation.state != AnimationState.DRAGGING:
            animation.return_to_idle()
        self.pet.bubble.say(FEEDBACK[expression.code], self.pet.config.bubble_duration_ms)
        self.expression_changed.emit(expression.code, expression.status)

    def test_expression(self) -> None:
        self.set_expression(self._expression)

