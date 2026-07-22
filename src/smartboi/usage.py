"""Tracks Claude API usage (call count, input/output tokens) against a
daily call budget -- checked by RelationshipExtractor/DossierUpdater/
Skeptic before every API call (see their `usage` constructor argument).

Call-count-based rather than a dollar estimate: this codebase's own prompt
construction keeps each call's token size roughly bounded (evidence text is
truncated, max_tokens is capped), so a call cap is a robust, pricing-table-
free proxy for spend that doesn't rot the moment Anthropic changes prices.
Persisted so the count survives a restart within the same UTC day; resets
automatically at UTC midnight."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from smartboi.state import JsonState


@dataclass(frozen=True)
class UsageSnapshot:
    date: str
    calls: int
    input_tokens: int
    output_tokens: int
    daily_call_budget: int


class UsageTracker:
    def __init__(self, path: Path, daily_call_budget: int):
        self._state = JsonState(path)
        self.daily_call_budget = daily_call_budget

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _roll_if_new_day(self, today: str) -> None:
        if self._state.get("date") != today:
            self._state.set("date", today)
            self._state.set("calls", 0)
            self._state.set("input_tokens", 0)
            self._state.set("output_tokens", 0)

    def budget_remaining(self, today: str | None = None) -> bool:
        today = today or self._today()
        self._roll_if_new_day(today)
        return self._state.get("calls", 0) < self.daily_call_budget

    def record(self, input_tokens: int, output_tokens: int, today: str | None = None) -> None:
        today = today or self._today()
        self._roll_if_new_day(today)
        self._state.set("calls", self._state.get("calls", 0) + 1)
        self._state.set("input_tokens", self._state.get("input_tokens", 0) + input_tokens)
        self._state.set("output_tokens", self._state.get("output_tokens", 0) + output_tokens)

    def snapshot(self, today: str | None = None) -> UsageSnapshot:
        today = today or self._today()
        self._roll_if_new_day(today)
        return UsageSnapshot(
            date=self._state.get("date", today),
            calls=self._state.get("calls", 0),
            input_tokens=self._state.get("input_tokens", 0),
            output_tokens=self._state.get("output_tokens", 0),
            daily_call_budget=self.daily_call_budget,
        )
