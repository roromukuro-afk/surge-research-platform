"""Request pacing for Jev through the Gateway's free tier (D-273).

The free tier rate-limits requests to ``typesafe-ai/jev``. On 2026-09-18 five
requests about 2.2 seconds apart were answered and the sixth, 11 seconds after
the first, was refused with HTTP 429; one request 45 minutes later was answered,
so the window is short. Its size is not published, and five-then-refused is not
taken to mean "5 per minute": the run sends at most one request every 15 seconds
and never more than four in any rolling 60 seconds (the user's decision,
D-273). A 429 still stops the run; nothing is retried.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

MIN_INTERVAL_SECONDS = 15.0
WINDOW_SECONDS = 60.0
MAX_IN_WINDOW = 4


class PacingError(RuntimeError):
    pass


@dataclass
class Pacer:
    min_interval: float = MIN_INTERVAL_SECONDS
    window: float = WINDOW_SECONDS
    max_in_window: int = MAX_IN_WINDOW
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    sent: list[float] = field(default_factory=list)

    def delay(self) -> float:
        """Seconds until one more request keeps both limits."""

        now = self.clock()
        waits = [0.0]
        if self.sent:
            waits.append(self.sent[-1] + self.min_interval - now)
        recent = [t for t in self.sent if now - t < self.window]
        if len(recent) >= self.max_in_window:
            waits.append(recent[-self.max_in_window] + self.window - now)
        return max(waits)

    def wait(self) -> float:
        """Sleep until the next request is allowed; the seconds waited."""

        waited = 0.0
        while (pause := self.delay()) > 0:
            self.sleep(pause)
            waited += pause
        return waited

    def in_window(self) -> int:
        now = self.clock()
        return sum(1 for t in self.sent if now - t < self.window)

    def mark_sent(self) -> int:
        """Record a request sent now; the requests in the rolling window, this one included."""

        self.sent.append(self.clock())
        count = self.in_window()
        if count > self.max_in_window:
            raise PacingError(f"{count} requests in {self.window:.0f}s; the limit is {self.max_in_window}")
        return count


__all__ = ["MAX_IN_WINDOW", "MIN_INTERVAL_SECONDS", "WINDOW_SECONDS", "Pacer", "PacingError"]
