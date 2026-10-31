"""A bounded, expiring set of events actually presented to the user."""

from collections import OrderedDict
import math


class EventDeduplicator:
    def __init__(self, max_entries: int = 256, ttl_seconds: float = 600) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("事件去重缓存容量必须为正整数。")
        self._validate_seconds(ttl_seconds)
        self.max_entries = max_entries
        self.ttl_seconds = float(ttl_seconds)
        self._events: OrderedDict[str, float] = OrderedDict()
        self._last_now: float | None = None

    @staticmethod
    def _validate_seconds(value: float) -> None:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError("事件去重有效期必须为正的有限秒数。")

    def _prepare(self, now: float, ttl_seconds: float) -> None:
        if (isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now)):
            raise ValueError("事件时间必须为有限数值。")
        if self._last_now is not None and now < self._last_now:
            self.reset()
        self._last_now = float(now)
        for key, recorded_at in list(self._events.items()):
            if now - recorded_at >= ttl_seconds:
                del self._events[key]

    def is_duplicate(self, event_key: str, now: float, ttl_seconds: float | None = None) -> bool:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._validate_seconds(ttl)
        self._prepare(now, ttl)
        if not isinstance(event_key, str) or not event_key.strip() or event_key not in self._events:
            return False
        # A lookup affects LRU eviction, but does not extend the event's TTL.
        self._events.move_to_end(event_key)
        return True

    def remember(self, event_key: str, now: float) -> None:
        self._prepare(now, self.ttl_seconds)
        if not isinstance(event_key, str) or not event_key.strip():
            return
        self._events[event_key] = float(now)
        self._events.move_to_end(event_key)
        while len(self._events) > self.max_entries:
            self._events.popitem(last=False)

    def reset(self) -> None:
        self._events.clear()
        self._last_now = None

