"""One interface for panel, mouse, and chat-driven character changes."""

from PySide6.QtCore import QObject, Signal

from .animation_controller import AnimationState
from .config import DEFAULT_IMAGE, PROJECT_ROOT
from .expression_manager import EXPRESSIONS, Expression, ExpressionManager
from .frame_animation import FrameAnimationPlayer
from .idle_behavior import IDLE_DURATION_SECONDS, IdleBehaviorPolicy
from .pet_window import PetWindow
from .settings_manager import SettingsManager


FEEDBACK = {
    "neutral": "你好呀，我来啦！", "happy": "好开心呀！",
    "surprised": "咦，发生什么啦？", "sleepy": "有一点困啦…",
    "angry": "哼，我有点生气啦。", "thinking": "让我想一想…",
    "talking": "我在认真回应你。",
}

# Zero-based, half-open range of the installed typing clip. The desk is fully
# present throughout these frames, including the source's brief eye blink.
TYPING_WORK_LOOP_SEGMENT = (14, 55)


class PetController(QObject):
    scale_changed = Signal(int)
    reading_font_changed = Signal(int)
    expression_changed = Signal(str, str)
    error_occurred = Signal(str)
    appearance_changed = Signal(str, str)
    asset_animation_changed = Signal(str, str)
    auto_expression_changed = Signal(bool)
    auto_expression_status_changed = Signal(str)

    def __init__(self, pet: PetWindow, settings: SettingsManager,
                 expressions: ExpressionManager | None = None, parent=None, *, animation_directory=None) -> None:
        super().__init__(parent)
        self.pet = pet
        self.settings = settings
        self.expressions = expressions or ExpressionManager()
        auto_loader = getattr(settings, "load_auto_expression", lambda: True)
        self._auto_expression_enabled = auto_loader() is True
        self._auto_expression_status = ("已开启：由 API 根据当前语境选择表情。" if self._auto_expression_enabled
                                        else "已关闭：保留手动选择的表情。")
        self._last_context_code = None
        self._pending_context_restore = None
        self._original = pet._source
        self._uses_project_character = pet.config.image_path.expanduser().resolve() == DEFAULT_IMAGE.resolve()
        # An injected, separate expression directory is an explicit character
        # pack. The project's default pack must never replace --image artwork.
        self._has_custom_expression_pack = (expressions is not None and expressions.directory.resolve()
                                            != (PROJECT_ROOT / "assets" / "expressions").resolve())
        original = self.expressions.resolve_static("original")
        self._default_character = (original.pixmap if self._uses_project_character and original.pixmap is not None
                                   else self._original)
        self._base_character = self._default_character
        self.appearance = "original"
        self.appearance_status = original.status if self._uses_project_character else "使用 --image 指定的自定义图片。"
        self.asset_player = FrameAnimationPlayer(pet, animation_directory, self)
        self.asset_player.status_changed.connect(self.asset_animation_changed.emit)
        self.behavior = IdleBehaviorPolicy()
        self._conversation_busy = False
        self._task_working = False
        self._work_animation_pending = False
        self._work_clip_owned = False
        self._idle_clip_owned = False
        self._idle_exit_pending = False
        self._behavior_applying = False
        self._queued_context = None
        self._behavior_closed = False
        self._unavailable_idle = set()
        self._pending_expression = None
        self._pending_expression_animate = True
        pet.frame_tick.connect(self._finish_deferred_expression)
        self._scale_percent = settings.load_scale()
        font_loader = getattr(settings, "load_reading_font", lambda: 14)
        self._reading_font_points = font_loader()
        if type(self._reading_font_points) is not int or not 12 <= self._reading_font_points <= 22:
            self._reading_font_points = 14
        pet.bubble.set_font_points(self._reading_font_points)
        self._expression = "neutral"
        neutral = self._resolve_expression("neutral")
        self._expression_status = neutral.status
        pet.set_scale_percent(self._scale_percent)
        pet.set_character_source(neutral.pixmap if neutral.pixmap is not None else self._base_character)
        pet.click_handler = self.set_expression
        pet.part_clicked.connect(self._on_part_clicked)
        pet.interaction_started.connect(lambda: self.behavior.register_activity())
        pet.frame_tick.connect(self._advance_behavior)
        pet.closed.connect(self._stop_behavior)

    def set_conversation_busy(self, busy: bool) -> None:
        self._conversation_busy = bool(busy)
        # busy=False is emitted just before the reply's expression. Reset the
        # quiet period, rather than starting an idle clip inside that signal.
        self.behavior.register_activity()
        if busy and self._idle_clip_owned:
            self.asset_player.stop(smooth=True)

    def set_task_working(self, working: bool) -> None:
        """Keep the typing workstation present throughout an Agent task."""
        working = bool(working)
        if self._behavior_closed or working == self._task_working:
            return
        self._task_working = working
        self._work_animation_pending = working
        self.behavior.register_activity()
        if working:
            self.behavior.cancel_anger()
            self._queued_context = None
            if self.asset_player.busy:
                self.asset_player.stop(smooth=True)
            self._start_work_animation()
        else:
            if self._work_clip_owned:
                self.asset_player.stop(smooth=True)
            self._work_clip_owned = False
            context, self._queued_context = self._queued_context, None
            if context is not None:
                self.apply_context_expression(context[0], source=context[1])

    def _start_work_animation(self) -> None:
        if (not self._work_animation_pending or not self._task_working
                or not self._uses_project_character or self.appearance != "original"
                or not self.pet.isVisible() or self.asset_player.busy
                or self._pending_expression is not None
                or self.pet.animation.state != AnimationState.IDLE):
            return
        # Attempt once per task. Missing or invalid artwork must not create a
        # per-frame retry loop or interfere with the actual request.
        self._work_animation_pending = False
        if self.asset_player.play("idle_typing_work", loop_segment=TYPING_WORK_LOOP_SEGMENT):
            self._work_clip_owned = self._idle_clip_owned = True
            self.pet.sequence_part_hits_enabled = False

    def _stop_behavior(self) -> None:
        self._behavior_closed = True
        self._task_working = self._work_animation_pending = self._work_clip_owned = False
        self._queued_context = None
        self.pet.sequence_part_hits_enabled = False

    def _behavior_expression(self, code: str) -> None:
        self._behavior_applying = True
        try:
            self.set_expression(code, feedback=False, animate=False)
        finally:
            self._behavior_applying = False

    def _on_part_clicked(self, part: str) -> None:
        if self._behavior_closed:
            return
        event = self.behavior.record_part_click(part)
        if self._idle_clip_owned:
            self.asset_player.stop(smooth=True)
        if event is not None:
            self._queued_context = None
            self._behavior_expression("angry")

    def _advance_behavior(self, seconds: float) -> None:
        if self._behavior_closed:
            return
        if self._idle_clip_owned and not self.asset_player.busy:
            self._idle_clip_owned = False
            self._work_clip_owned = False
            self.pet.sequence_part_hits_enabled = False
        self._start_work_animation()
        allowed = (self._uses_project_character and self.appearance == "original"
                   and self.pet.isVisible() and not self._conversation_busy and not self._task_working
                   and not self.pet.bubble.isVisible() and self._pending_expression is None
                   and self.pet.animation.state == AnimationState.IDLE
                   and not self.asset_player.busy
                   and (self._auto_expression_enabled or self._expression == "neutral"))
        event = self.behavior.advance(seconds, idle_allowed=allowed)
        if event is None:
            return
        kind, name = event
        if kind == "calmed":
            context, self._queued_context = self._queued_context, None
            if (context is not None and self._auto_expression_enabled
                    and (self._conversation_busy or self.pet.bubble.isVisible())):
                self.apply_context_expression(context[0], source=context[1])
            else:
                self._behavior_expression("neutral")
        elif kind == "idle" and name not in self._unavailable_idle:
            # Every prepared idle clip starts and ends on the neutral canvas.
            # Clear expression de-duplication so a later identical API emotion
            # still replaces this neutral state.
            if self._expression != "neutral":
                self._behavior_expression("neutral")
            self._last_context_code = None
            if self.asset_player.play(name, once=True, duration_seconds=IDLE_DURATION_SECONDS):
                self._idle_clip_owned = True
                self.pet.sequence_part_hits_enabled = True
            else:
                self._unavailable_idle.add(name)

    @property
    def scale_percent(self) -> int:
        return self._scale_percent

    @property
    def expression(self) -> str:
        return self._expression

    @property
    def expression_status(self) -> str:
        return self._expression_status

    @property
    def auto_expression_enabled(self) -> bool:
        return self._auto_expression_enabled

    @property
    def auto_expression_status(self) -> str:
        return self._auto_expression_status

    def _set_auto_expression_status(self, message: str) -> None:
        self._auto_expression_status = message
        self.auto_expression_status_changed.emit(message)

    def set_auto_expression_enabled(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            return False
        if enabled == self._auto_expression_enabled:
            return True
        self._auto_expression_enabled = enabled
        self._last_context_code = None
        # Do not apply a queued model expression after the user turns it off.
        if not enabled and self._pending_context_restore is not None:
            restored, self._pending_context_restore = self._pending_context_restore, None
            self._pending_expression = None
            self._expression, self._expression_status = restored.code, restored.status
            self.expression_changed.emit(restored.code, restored.status)
        if not enabled:
            self._queued_context = None
        self._set_auto_expression_status("已开启：下次 API 分析时按语境选择表情。" if enabled
                                         else "已关闭：保留手动选择的表情。")
        self.auto_expression_changed.emit(enabled)
        try:
            saver = getattr(self.settings, "save_auto_expression", None)
            if saver is not None:
                saver(enabled)
        except Exception:
            self.error_occurred.emit("自动表情开关已生效，但未能保存；请检查配置目录权限。")
        return True

    def apply_context_expression(self, code: str, *, source: str = "chat") -> bool:
        sources = {"chat": "当前对话", "screen": "屏幕情境", "camera": "摄像头情境", "game": "棋局互动"}
        if (not self._auto_expression_enabled or not isinstance(code, str) or code not in EXPRESSIONS
                or not isinstance(source, str) or source not in sources):
            return False
        self.behavior.register_activity(hold_seconds=8.0)
        if self._task_working and (self._work_animation_pending or self._work_clip_owned):
            self._queued_context = (code, source)
            self._set_auto_expression_status(f"API · {sources[source]}：{EXPRESSIONS[code][0]}；任务结束后显示。")
            return True
        if self.behavior.is_angry:
            self._queued_context = (code, source)
            self._set_auto_expression_status(f"API · {sources[source]}：{EXPRESSIONS[code][0]}；互动生气结束后恢复。")
            return True
        if self._last_context_code != code or self._expression != code:
            restore = self._pending_context_restore or self._resolve_expression(self._expression)
            self.set_expression(code, feedback=False, animate=False)
            if self._pending_expression is not None:
                self._pending_context_restore = restore
            self._last_context_code = code
        self._set_auto_expression_status(f"API · {sources[source]}：{EXPRESSIONS[code][0]}（{code}）。")
        return True

    @property
    def reading_font_points(self) -> int:
        return self._reading_font_points

    def set_reading_font_points(self, points: int) -> None:
        if type(points) is not int or not 12 <= points <= 22:
            self.error_occurred.emit("气泡字号必须为 12～22 pt。")
            return
        self.pet.bubble.set_font_points(points)
        self._reading_font_points = points
        self.reading_font_changed.emit(points)
        try:
            saver = getattr(self.settings, "save_reading_font", None)
            if saver is not None:
                saver(points)
        except Exception:
            self.error_occurred.emit("气泡字号已调整，但未能保存到本地；请检查配置目录权限。")

    def reset_reading_font(self) -> None:
        self.set_reading_font_points(14)

    def _resolve_expression(self, code: str) -> Expression:
        if code not in EXPRESSIONS:
            raise ValueError("未知表情状态。")
        if self._uses_project_character or self.appearance != "original" or self._has_custom_expression_pack:
            return self.expressions.resolve(code)
        return Expression(code, EXPRESSIONS[code][0], None,
                          "自定义角色的表情素材尚未提供；保留 --image 图片，使用轻量动作或气泡反馈，未改变面部表情。")

    def expression_catalog(self) -> list[tuple[str, str, str]]:
        return [(code, EXPRESSIONS[code][0], self._resolve_expression(code).status) for code in EXPRESSIONS]

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

    def set_expression(self, code: str, *, feedback: bool = True, animate: bool = True) -> None:
        if not self._behavior_applying:
            self._work_animation_pending = self._work_clip_owned = False
            self.behavior.cancel_anger()
            self.behavior.register_activity(hold_seconds=8.0)
            self._queued_context = None
        self._last_context_code = None
        self._pending_context_restore = None
        try:
            expression = self._resolve_expression(code)
        except ValueError:
            expression = self._resolve_expression("neutral")
        self._expression = expression.code
        self._expression_status = expression.status
        if self.pet.animation.state in {AnimationState.DRAGGING, AnimationState.RETURNING}:
            # A late API reply may update logical state/bubble while dragging,
            # but must not replace the frozen sprite beneath the mouse.
            self._pending_expression = expression
            self._pending_expression_animate = animate
        else:
            self._pending_expression = None
            self._render_expression(expression, animate=animate)
        if feedback:
            self.pet.bubble.say(FEEDBACK[expression.code], self.pet.config.bubble_duration_ms)
        self.expression_changed.emit(expression.code, expression.status)

    def _finish_deferred_expression(self, _seconds):
        if (self._pending_expression is not None
                and not (self._idle_exit_pending and self.asset_player.playing)
                and self.pet.animation.state not in {AnimationState.DRAGGING, AnimationState.RETURNING}):
            expression, self._pending_expression = self._pending_expression, None
            self._idle_exit_pending = False
            self._pending_context_restore = None
            self._render_expression(expression, animate=self._pending_expression_animate)

    def _render_expression(self, expression, *, animate=True):
        if self._idle_clip_owned and self.asset_player.playing:
            self._pending_expression = expression
            self._pending_expression_animate = animate
            self._idle_exit_pending = True
            self.asset_player.stop(smooth=True)
            return
        self._idle_clip_owned = False
        self._idle_exit_pending = False
        self.pet.sequence_part_hits_enabled = False
        self.asset_player.stop()
        self.pet.set_character_source(expression.pixmap if expression.pixmap is not None else self._base_character)
        animation = self.pet.animation
        if animate and expression.code == "happy":
            animation.play_happy()
        elif animate and expression.code == "surprised":
            animation.play_surprised()
        elif animation.state != AnimationState.DRAGGING:
            animation.return_to_idle()
        if (animate and expression.code == "thinking" and expression.pixmap is None
                and (self._uses_project_character or self.appearance != "original" or self._has_custom_expression_pack)):
            # Use the optional clip only as a fallback for missing expression
            # art. Explicit animation controls remain available with a PNG.
            self.asset_player.play("think_scratch_head")

    def static_catalog(self):
        return [(code, "自定义角色" if code == "original" and not self._uses_project_character else label,
                 "使用 --image 指定的自定义图片。" if code == "original" and not self._uses_project_character else status)
                for code, label, status in self.expressions.static_catalog()]

    def animation_catalog(self):
        return self.asset_player.catalog()

    def set_static_character(self, code: str) -> bool:
        if self.pet.animation.state in {AnimationState.DRAGGING, AnimationState.RETURNING}:
            self.error_occurred.emit("正在拖动或平滑恢复，请角色恢复后再切换静态素材。")
            return False
        try:
            appearance = self.expressions.resolve_static(code)
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return False
        if code != "original" and appearance.pixmap is None:
            self.appearance_changed.emit(self.appearance, appearance.status)
            return False
        self._work_animation_pending = self._work_clip_owned = False
        self.asset_player.stop()
        self.behavior.cancel_anger()
        self.behavior.register_activity(hold_seconds=8.0)
        self._queued_context = None
        self._idle_clip_owned = self._idle_exit_pending = False
        self.pet.sequence_part_hits_enabled = False
        self._pending_expression = None
        self._last_context_code = None
        self._pending_context_restore = None
        self.appearance = code
        self.appearance_status = (appearance.status if code != "original" or self._uses_project_character
                                  else "使用 --image 指定的自定义图片。")
        self._base_character = appearance.pixmap if code != "original" else self._default_character
        self.pet.set_character_source(self._base_character)
        # Keep semantic state separate; future API expressions use this base
        # when no matching facial PNG has been supplied.
        self.appearance_changed.emit(code, self.appearance_status)
        return True

    def play_asset_animation(self, name: str) -> bool:
        accepted = self.asset_player.play(name)
        if accepted:
            self._work_animation_pending = self._work_clip_owned = False
            self.behavior.cancel_anger()
            self.behavior.register_activity(hold_seconds=8.0)
            self._queued_context = None
            self._idle_clip_owned = self._idle_exit_pending = False
            self.pet.sequence_part_hits_enabled = False
            self._pending_expression = None
            self._last_context_code = None
            self._pending_context_restore = None
        return accepted

    def stop_asset_animation(self) -> None:
        self._work_animation_pending = self._work_clip_owned = False
        self.behavior.register_activity(hold_seconds=8.0)
        self.asset_player.stop(smooth=True)

    def test_expression(self) -> None:
        self.set_expression(self._expression)

    def play_motion(self, emotion: str = "neutral", *, source: str = "screen") -> None:
        changed = self._last_context_code != emotion or self._expression != emotion
        if not self.apply_context_expression(emotion, source=source):
            return
        if self.behavior.is_angry or (self._task_working and self._work_clip_owned):
            return
        if not changed:
            return
        if self._expression == "happy":
            self.pet.animation.play_happy()
        elif self._expression == "surprised":
            self.pet.animation.play_surprised()
        else:
            # A gentle hop provides actual feedback even without expression art.
            # The animation controller refuses it while dragging.
            self.pet.animation.play_surprised()

    def respond(self, text: str, emotion: str = "neutral", *, conversation_link=None) -> None:
        """Common character feedback interface; never adds perception to chat history."""
        source = conversation_link.get("source_kind", "chat") if isinstance(conversation_link, dict) else "chat"
        self.apply_context_expression(emotion, source=source)
        if isinstance(text, str) and text.strip():
            self.pet.bubble.say(text, max(6000, self.pet.config.bubble_duration_ms,
                                         1800 + len(text) * 140), conversation_link=conversation_link)

