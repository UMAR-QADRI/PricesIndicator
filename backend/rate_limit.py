from collections import deque
from math import ceil
from time import monotonic


class SlidingWindowLimiter:
    """In-memory sliding-window limiter.

    Limits reset on restart and are not shared across worker processes.
    """

    def __init__(self):
        self._events: dict[tuple[str, str, str], deque[float]] = {}

    def hit(
        self,
        scope: str,
        kind: str,
        value: str,
        limit: int,
        window_seconds: int,
    ) -> int:
        now = monotonic()
        key = (scope, kind, value)
        events = self._events.setdefault(key, deque())

        while events and now - events[0] >= window_seconds:
            events.popleft()

        if len(events) >= limit:
            return max(1, ceil(window_seconds - (now - events[0])))

        events.append(now)
        return 0

    def reset(self) -> None:
        self._events.clear()


rate_limiter = SlidingWindowLimiter()
