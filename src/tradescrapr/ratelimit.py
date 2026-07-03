"""Minimum-interval throttle. A single sequential worker never bursts, so a
token bucket would add state for nothing."""

from __future__ import annotations

import time


class Throttle:
    def __init__(self, rps: float):
        if rps <= 0:
            raise ValueError(f"rps must be positive, got {rps}")
        self._min_interval = 1.0 / rps
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        remaining = self._min_interval - (now - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()
