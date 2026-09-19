"""Request pacing for Jev through the Gateway's free tier (D-273, D-274).

The free tier rate-limits requests to ``typesafe-ai/jev``. Twice on
2026-09-18 five requests were answered and the sixth refused with HTTP 429 -
once with the requests 2.2 seconds apart, once 15 seconds apart with never more
than four in a rolling minute. The window is longer than 75 seconds and shorter
than about 18 minutes; neither the published documentation nor the response
headers say more, and it is not probed further. Phase A therefore sends one
request every 5 minutes (the user's decision, D-274), which also keeps any 20
minutes to at most four; the rolling guard enforces that and refuses loudly if
it is ever broken. A 429 still stops the run; nothing is retried.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

MIN_INTERVAL_SECONDS = 300.0
WINDOW_SECONDS = 1200.0
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

    def count_within(self, seconds: float) -> int:
        """Requests sent in the last ``seconds``, the latest included."""

        now = self.clock()
        return sum(1 for t in self.sent if now - t < seconds)

    def in_window(self) -> int:
        return self.count_within(self.window)

    def mark_sent(self) -> int:
        """Record a request sent now; the requests in the policy's rolling window, this one included."""

        self.sent.append(self.clock())
        count = self.in_window()
        if count > self.max_in_window:
            raise PacingError(f"{count} requests in {self.window:.0f}s; the limit is {self.max_in_window}")
        return count


#: Per provider (D-275). TypeSafe's own API answered six requests two seconds
#: apart without a 429 (2026-09-19), so Phase B starts at one every 5 seconds -
#: enough for a benchmark gathered over weeks; the most the API allows is not
#: sought. The Gateway keeps Phase A's 5-minute policy.
POLICIES = {
    "typesafe-direct": {"min_interval": 5.0, "window": 60.0, "max_in_window": 12},
    "vercel-ai-gateway": {"min_interval": MIN_INTERVAL_SECONDS, "window": WINDOW_SECONDS,
                          "max_in_window": MAX_IN_WINDOW},
}


def pacer_for(provider: str, **overrides) -> Pacer:
    if provider not in POLICIES:
        raise PacingError(f"no pacing policy for {provider!r}")
    return Pacer(**{**POLICIES[provider], **overrides})


__all__ = ["MAX_IN_WINDOW", "MIN_INTERVAL_SECONDS", "POLICIES", "WINDOW_SECONDS", "Pacer", "PacingError",
           "pacer_for"]
