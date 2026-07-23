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

    def _prune(self, key: str, now: float) -> list[float]:
        history = self._events[key]
        cutoff = now - self.window_sec
        while history and history[0] < cutoff:
            history.pop(0)
        return history

    def would_allow(self, key: str, now: float) -> bool:
        """Read-only check: would an event for `key` be allowed right now,
        without recording anything. Use this to pre-filter BEFORE doing
        expensive work, then call `record` only once that work has
        definitively completed -- otherwise a retried attempt (the earlier
        one deferred by a transient failure or budget exhaustion) burns a
        second slot for what is, from the caller's perspective, the same
        underlying event, not a new one."""
        return len(self._prune(key, now)) < self.max_events

    def record(self, key: str, now: float) -> None:
        """Commits one event for `key` at `now`, unconditionally -- pair
        with a prior `would_allow` check; does not itself enforce the cap."""
        self._events[key].append(now)

    def allow(self, key: str, now: float) -> bool:
        """Convenience check-and-record in one call: True (and records the
        event) if fewer than max_events have occurred for `key` within the
        trailing window_sec as of `now`; False (no-op) once the cap is
        already reached. Use `would_allow`/`record` separately instead when
        the caller needs to defer recording until an attempt actually
        succeeds (see engine.py's propagation cooldown)."""
        if not self.would_allow(key, now):
            return False
        self.record(key, now)
        return True
