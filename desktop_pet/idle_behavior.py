"""Idle choices and temporary annoyance, driven by the pet's existing clock.

This policy does not import Qt, inspect assets, or start a timer. The controller
owns playback and supplies whether the current pose may accept an idle action.
Time spent talking, dragging, or playing a clip never creates an idle backlog.
"""

from collections import deque
import math
from numbers import Real
import random
from typing import Iterable


DEFAULT_IDLE_ACTIONS = (
    "idle_puff_cheeks",
    "idle_tiptoe_look",
    "idle_pinch_cheeks",
    "idle_sleepy_nod",
    "idle_tail_blanket",
    "idle_balloon_float",
    "idle_proud_pose",
    "idle_tsundere",
)
BehaviorEvent = tuple[str, str | None]


def _seconds(value: float) -> float:
    try:
        valid = (isinstance(value, Real) and not isinstance(value, bool)
                 and math.isfinite(value) and value >= 0)
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise ValueError("elapsed seconds must be finite and nonnegative")
    return float(value)


class IdleBehaviorPolicy:
    """Shuffle idle clips; react to three ear/tail clicks within three seconds.

    ``advance`` returns at most one event. A blocked pose pauses the idle
    countdown, while an anger deadline continues even during a drag or reply.
    Annoyance lasts eight seconds from its first trigger; further clicks do not
    extend it. After calming down there is a fresh quiet period before any clip.
    """

    CLICK_WINDOW = 3.0
    CLICK_THRESHOLD = 3
    ANGER_SECONDS = 8.0
    CLICK_PARTS = frozenset({"left_ear", "right_ear", "tail"})

    def __init__(self, rng: random.Random | None = None,
                 idle_actions: Iterable[str] = DEFAULT_IDLE_ACTIONS) -> None:
        actions = tuple(idle_actions)
        if not actions or any(not isinstance(name, str) or not name for name in actions):
            raise ValueError("idle_actions must contain nonempty action names")
        if len(set(actions)) != len(actions):
            raise ValueError("idle_actions must not contain duplicates")
        self._actions = actions
        self._rng = rng if rng is not None else random.Random()
        self._click_ages: deque[float] = deque()
        self._bag: list[str] = []
        self._last_action: str | None = None
        self._anger_remaining = 0.0
        self._hold_remaining = 0.0
        self._idle_remaining = self._rng.uniform(4.0, 7.0)

    @property
    def is_angry(self) -> bool:
        return self._anger_remaining > 0.0

    @property
    def angry_remaining(self) -> float:
        return self._anger_remaining

    @property
    def next_idle_in(self) -> float:
        """Unblocked quiet time still required, including an activity hold."""
        return self._hold_remaining + self._idle_remaining

    def register_activity(self, hold_seconds: float = 0.0) -> None:
        """Wait for fresh quiet, retaining any longer existing activity hold.

        This does not clear click history: three clicks may alternate between
        the ears and tail and still belong to one short burst.
        """
        hold_seconds = _seconds(hold_seconds)
        self._hold_remaining = max(self._hold_remaining, hold_seconds)
        self._idle_remaining = self._rng.uniform(4.0, 7.0)

    def record_part_click(self, part: str) -> BehaviorEvent | None:
        if part not in self.CLICK_PARTS:
            return None
        self.register_activity()
        if self.is_angry:
            return None
        self._click_ages.append(0.0)
        if len(self._click_ages) < self.CLICK_THRESHOLD:
            return None
        self._click_ages.clear()
        self._anger_remaining = self.ANGER_SECONDS
        return ("angry", None)

    def advance(self, seconds: float, *, idle_allowed: bool) -> BehaviorEvent | None:
        seconds = _seconds(seconds)
        self._age_clicks(seconds)
        held = min(seconds, self._hold_remaining)
        self._hold_remaining -= held
        if self.is_angry:
            self._anger_remaining = max(0.0, self._anger_remaining - seconds)
            if not self.is_angry:
                self._click_ages.clear()
                self._idle_remaining = self._rng.uniform(4.0, 7.0)
                return ("calmed", None)
            return None
        if not idle_allowed or seconds == 0:
            return None
        self._idle_remaining = max(0.0, self._idle_remaining - (seconds - held))
        if self._idle_remaining > 0:
            return None
        action = self._next_action()
        # Drop time beyond this event, e.g. after laptop sleep. Never catch up
        # by issuing several clips or by making the following call immediately due.
        self._idle_remaining = self._rng.uniform(8.0, 16.0)
        return ("idle", action)

    def cancel_anger(self) -> bool:
        """Clear temporary annoyance, e.g. when replacing the character."""
        was_angry = self.is_angry
        self._anger_remaining = 0.0
        self._click_ages.clear()
        self.register_activity()
        return was_angry

    def reset(self) -> None:
        """Forget prior clicks, holds, annoyance and clip order."""
        self._click_ages.clear()
        self._bag.clear()
        self._last_action = None
        self._anger_remaining = 0.0
        self._hold_remaining = 0.0
        self._idle_remaining = self._rng.uniform(4.0, 7.0)

    def _age_clicks(self, seconds: float) -> None:
        # Relative, bounded ages avoid loss of precision or clock overflow after
        # long suspension. At most two pending clicks can be stored.
        self._click_ages = deque(age + seconds for age in self._click_ages
                                 if seconds <= self.CLICK_WINDOW - age)

    def _next_action(self) -> str:
        if not self._bag:
            self._bag = list(self._actions)
            self._rng.shuffle(self._bag)
            # Pop from the end; exchange its next item if a new bag would repeat
            # the previous bag's final clip. Each bag still contains every clip.
            if len(self._bag) > 1 and self._bag[-1] == self._last_action:
                self._bag[0], self._bag[-1] = self._bag[-1], self._bag[0]
        self._last_action = self._bag.pop()
        return self._last_action
