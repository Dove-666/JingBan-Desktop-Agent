"""Central animation tuning, expressed in seconds and logical Qt pixels."""

from dataclasses import dataclass
import math
from numbers import Real


@dataclass(frozen=True)
class AnimationConfig:
    idle_amplitude: float = 2.5
    idle_period: float = 4.8
    breathing_amplitude: float = 0.004
    breathing_period: float = 3.8
    happy_amplitude: float = 12.0
    happy_duration: float = 0.55
    surprised_amplitude: float = 7.0
    surprised_duration: float = 0.35
    return_duration: float = 0.4
    jump_rise_fraction: float = 0.28
    fps: int = 30

    def __post_init__(self) -> None:
        for name in (
            "idle_amplitude", "idle_period", "breathing_amplitude",
            "breathing_period", "happy_amplitude", "happy_duration",
            "surprised_amplitude", "surprised_duration", "return_duration",
            "jump_rise_fraction",
        ):
            value = getattr(self, name)
            try:
                valid = (
                    isinstance(value, Real)
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                )
            except (OverflowError, TypeError):
                valid = False
            if not valid:
                raise ValueError(f"{name} must be a finite number")
            if name.endswith("amplitude"):
                if value < 0:
                    raise ValueError(f"{name} must be nonnegative")
            elif value <= 0:
                raise ValueError(f"{name} must be positive")

        if self.breathing_amplitude >= 1:
            raise ValueError("breathing_amplitude must be less than 1")
        if not 0 < self.jump_rise_fraction < 1:
            raise ValueError("jump_rise_fraction must be between 0 and 1")
        if isinstance(self.fps, bool) or not isinstance(self.fps, int) or not 1 <= self.fps <= 120:
            raise ValueError("fps must be an integer between 1 and 120")
        if not math.isfinite(self.max_upward_offset):
            raise ValueError("combined animation amplitudes must be finite")

    @property
    def max_upward_offset(self) -> float:
        """Safe positive top margin, including interrupted/repeated actions."""
        return self.idle_amplitude + max(self.happy_amplitude, self.surprised_amplitude)

    @property
    def max_downward_offset(self) -> float:
        return self.idle_amplitude

    @property
    def max_scale(self) -> float:
        return 1.0 + self.breathing_amplitude
