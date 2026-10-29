"""Small, deterministic animation state machine without rendering or I/O.

The window advances this controller using its existing timer and draws its
cached sprite with ``pose``. Future frame/part renderers can consume ``state``
and ``pose`` without changing pointer handling or the animation clock.
"""

from dataclasses import dataclass
from enum import Enum
import math
from numbers import Real

from .animation_config import AnimationConfig


class AnimationState(str, Enum):
    IDLE = "idle"
    HAPPY = "happy"
    SURPRISED = "surprised"
    DRAGGING = "dragging"
    RETURNING = "returning"


@dataclass(frozen=True)
class AnimationPose:
    offset_y: float = 0.0
    scale: float = 1.0


def _smoothstep(progress: float) -> float:
    return progress * progress * (3.0 - 2.0 * progress)


def _blend(start: float, end: float, weight: float) -> float:
    # This convex form also avoids overflow for large, valid amplitudes.
    return (1.0 - weight) * start + weight * end


class AnimationController:
    """Drag > user actions > idle; all transitions preserve the current pose.

    Happy/surprised replace another action or a return, then finish in idle.
    Dragging freezes both the pose and idle phases; releasing starts a smooth
    return to the moving idle target. Calls to play actions during drag fail.
    Breathing is composed into idle, action recovery, and returning poses.
    """

    def __init__(self, config: AnimationConfig | None = None) -> None:
        self._config = config if config is not None else AnimationConfig()
        self._state = AnimationState.IDLE
        self._pose = AnimationPose()
        self._start_pose = self._pose
        self._idle_phase = 0.0
        self._breathing_phase = 0.0
        self._state_elapsed = 0.0
        self._duration = 0.0
        self._peak_y = 0.0

    @property
    def config(self) -> AnimationConfig:
        return self._config

    @property
    def state(self) -> AnimationState:
        return self._state

    @property
    def pose(self) -> AnimationPose:
        return self._pose

    def _idle_pose(self) -> AnimationPose:
        return AnimationPose(
            self.config.idle_amplitude * math.sin(self._idle_phase),
            1.0 + self.config.breathing_amplitude * math.sin(self._breathing_phase),
        )

    def advance(self, dt: float) -> AnimationPose:
        """Advance by nonnegative elapsed seconds; no fixed-frame assumption."""
        try:
            valid = isinstance(dt, Real) and not isinstance(dt, bool) and math.isfinite(dt)
        except (OverflowError, TypeError):
            valid = False
        if not valid or dt < 0:
            raise ValueError("elapsed seconds must be finite and nonnegative")
        if dt == 0 or self.state == AnimationState.DRAGGING:
            return self.pose

        # Bound the phases, even after a long suspension, before trigonometry.
        self._idle_phase = (
            self._idle_phase + math.tau * ((dt % self.config.idle_period) / self.config.idle_period)
        ) % math.tau
        self._breathing_phase = (
            self._breathing_phase
            + math.tau * ((dt % self.config.breathing_period) / self.config.breathing_period)
        ) % math.tau
        idle = self._idle_pose()

        if self.state == AnimationState.IDLE:
            self._pose = idle
            return self.pose
        if dt >= self._duration - self._state_elapsed:
            self._state = AnimationState.IDLE
            self._state_elapsed = 0.0
            self._pose = idle
            return self.pose

        self._state_elapsed += dt
        progress = self._state_elapsed / self._duration
        weight = _smoothstep(progress)
        scale = _blend(self._start_pose.scale, idle.scale, weight)
        if self.state == AnimationState.RETURNING:
            offset_y = _blend(self._start_pose.offset_y, idle.offset_y, weight)
        else:
            rise = self.config.jump_rise_fraction
            if progress <= rise:
                offset_y = _blend(
                    self._start_pose.offset_y, self._peak_y,
                    _smoothstep(progress / rise),
                )
            else:
                offset_y = _blend(
                    self._peak_y, idle.offset_y,
                    _smoothstep((progress - rise) / (1.0 - rise)),
                )
        self._pose = AnimationPose(offset_y, scale)
        return self.pose

    def start_dragging(self) -> None:
        """Freeze exactly the current pose; repeated calls are harmless."""
        if self.state != AnimationState.DRAGGING:
            self._state = AnimationState.DRAGGING
            self._state_elapsed = 0.0

    def finish_dragging(self) -> None:
        if self.state == AnimationState.DRAGGING:
            self._begin(AnimationState.RETURNING, self.config.return_duration)

    def return_to_idle(self) -> bool:
        """Neutral feedback may settle an action, but never interrupt a drag."""
        if self.state == AnimationState.DRAGGING:
            return False
        if self.state != AnimationState.IDLE:
            self._begin(AnimationState.RETURNING, self.config.return_duration)
        return True

    def _begin(self, state: AnimationState, duration: float) -> None:
        self._start_pose = self.pose
        self._state = state
        self._state_elapsed = 0.0
        self._duration = duration

    def _play(self, state: AnimationState, amplitude: float, duration: float) -> bool:
        if self.state == AnimationState.DRAGGING:
            return False
        self._begin(state, duration)
        # Use idle's bounded reference, never add a new jump to the last jump.
        # Repeated clicks therefore cannot push the sprite beyond its margins.
        self._peak_y = min(self.pose.offset_y, self._idle_pose().offset_y - amplitude)
        return True

    def play_happy(self) -> bool:
        return self._play(
            AnimationState.HAPPY, self.config.happy_amplitude, self.config.happy_duration,
        )

    def play_surprised(self) -> bool:
        return self._play(
            AnimationState.SURPRISED,
            self.config.surprised_amplitude, self.config.surprised_duration,
        )
