import math
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    """Sliding-window log: keep each client's request timestamps for the last
    `window` seconds and reject once there are `limit` of them.

    Unlike a fixed window, this can't be gamed by bursting at a window
    boundary (e.g. 30 requests at :59 and 30 more at :00).
    """

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = clock()

    def check(self, key: str) -> float | None:
        """Record a request. Returns None if allowed, else seconds to wait."""
        now = self.clock()
        self._maybe_sweep(now)
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(hits[0] + self.window - now, 0.0)
        hits.append(now)
        return None

    def retry_after_header(self, wait: float) -> str:
        return str(max(1, math.ceil(wait)))

    def _maybe_sweep(self, now: float) -> None:
        # Without this, every distinct client IP ever seen would stay in memory.
        if now - self._last_sweep < self.window:
            return
        cutoff = now - self.window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]
        self._last_sweep = now
