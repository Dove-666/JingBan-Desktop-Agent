"""Pure, conservative decisions for optional proactive pet interaction.

Context events are data only. This module neither executes model output nor
accesses a GUI, network, screenshot, file, or clock on its own.
"""

from collections import deque
from dataclasses import dataclass
import math
from typing import Literal

from .settings import PerceptionSettings


MIN_CONFIDENCE = 0.60
SPEAK_IMPORTANCE = 0.65
COMPANION_IMPORTANCE = 0.10
SPEECH_WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class Decision:
    action: Literal["quiet", "expression", "motion", "speak"]
    reason: str


def _finite_number(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


class ProactiveAgent:
    def __init__(self) -> None:
        self._speeches: deque[float] = deque()
        self._last_speech_at: float | None = None
        self._last_now: float | None = None

    def _prepare(self, now: float) -> None:
        if self._last_now is not None and now < self._last_now:
            self.reset()
        self._last_now = float(now)
        while self._speeches and now - self._speeches[0] >= SPEECH_WINDOW_SECONDS:
            self._speeches.popleft()

    def decide(self, event, now: float, settings: PerceptionSettings,
               busy: bool = False, idle_seconds: float = 0) -> Decision:
        if not _finite_number(now) or not _finite_number(idle_seconds) or idle_seconds < 0:
            return Decision("quiet", "invalid_clock")
        try:
            settings.validate()
        except (AttributeError, ValueError, TypeError):
            return Decision("quiet", "invalid_settings")
        self._prepare(now)
        if not settings.enabled:
            return Decision("quiet", "disabled")
        if busy:
            return Decision("quiet", "user_busy")
        if idle_seconds >= settings.idle_pause_seconds:
            return Decision("quiet", "user_idle")
        kind = getattr(event, "kind", "")
        if not isinstance(kind, str):
            return Decision("quiet", "invalid_event")
        express = getattr(event, "express", False) is True
        if kind in {"no_change", "private", "sensitive"} or kind == "none" and not express:
            return Decision("quiet", "no_actionable_event")
        confidence = getattr(event, "confidence", None)
        importance = getattr(event, "importance", None)
        if (not _finite_number(confidence) or not 0 <= confidence <= 1
                or not _finite_number(importance) or not 0 <= importance <= 1):
            return Decision("quiet", "invalid_event")
        if confidence < MIN_CONFIDENCE:
            return Decision("quiet", "low_confidence")
        if kind == "none" and express:
            return Decision("expression", "context_expression")
        silent_action = "expression" if express else "motion"
        companion_event = (
            settings.commentary_mode == "companion"
            and kind in {"media", "interesting"}
            and importance >= COMPANION_IMPORTANCE
        )
        if importance < SPEAK_IMPORTANCE and not companion_event:
            return Decision(silent_action, "low_importance")
        reply = getattr(event, "reply", None)
        if not isinstance(reply, str) or not reply.strip():
            return Decision(silent_action, "no_reply")
        if len(self._speeches) >= settings.max_speeches_per_hour:
            return Decision(silent_action, "hourly_limit")
        if (self._last_speech_at is not None
                and now - self._last_speech_at < settings.speech_cooldown_seconds):
            return Decision(silent_action, "cooldown")
        return Decision("speak", "companion_event" if companion_event else "important_event")

    def record(self, decision: Decision, now: float) -> None:
        """Count only an action that the UI has actually performed."""
        if not _finite_number(now):
            raise ValueError("主动交互时间必须为有限数值。")
        self._prepare(now)
        if decision.action == "speak":
            self._speeches.append(float(now))
            self._last_speech_at = float(now)

    def reset(self) -> None:
        self._speeches.clear()
        self._last_speech_at = None
        self._last_now = None

    def quota_snapshot(self, now: float, settings: PerceptionSettings) -> dict:
        """Expose the real remaining limits without recording a new action."""
        self._prepare(now)
        cooldown = (max(0, settings.speech_cooldown_seconds - (now - self._last_speech_at))
                    if self._last_speech_at is not None else 0)
        return {"cooldownSeconds": math.ceil(cooldown),
                "hourlyRemaining": max(0, settings.max_speeches_per_hour - len(self._speeches))}
