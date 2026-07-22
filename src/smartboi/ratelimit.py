"""Sliding-window rate limiter -- used to throttle propagated-evidence
fan-out per (origin, target) link (see engine.py) so a noisy anchor doesn't
burn a dossier-update + skeptic LLM call for every single article about it
when the causal link keeps getting refused for the same underlying reason.
Time is passed in explicitly rather than read from the clock, so this is
testable without real delays."""
from __future__ import annotations

from collections import defaultdict


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_sec: float):
        self.max_events = max_events
        self.window_sec = window_sec
        self._events: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str, now: float) -> bool:
        """True (and records the event) if fewer than max_events have
        occurred for `key` within the trailing window_sec as of `now`;
        False (no-op, nothing recorded) once the cap is already reached."""
        history = self._events[key]
        cutoff = now - self.window_sec
        while history and history[0] < cutoff:
            history.pop(0)
        if len(history) >= self.max_events:
            return False
        history.append(now)
        return True
