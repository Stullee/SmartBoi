"""Tracks Claude API usage (call count, input/output tokens, and estimated
USD) against BOTH a daily call budget and a daily dollar budget -- checked
by RelationshipExtractor/DossierUpdater/Skeptic before every API call (see
their `usage` constructor argument). Persisted so the counters survive a
restart within the same UTC day; resets automatically at UTC midnight.

The call cap alone used to be the whole budget, on the stated reasoning
that per-call token sizes are bounded by this codebase's own prompt
construction, making a call count a pricing-table-free proxy for spend.
That reasoning no longer holds and its failure mode is expensive rather
than merely inaccurate: per-call cost now varies by more than an order of
magnitude across the configured models (Haiku at $1/$5 per MTok against
Opus at $5/$25), and adaptive thinking makes output tokens unbounded by
max_tokens in practice. The same 3000-call ceiling that costs a few dollars
a day on Haiku costs several hundred on Opus at full thinking.

So spend is now metered directly, using llm.MODEL_PRICES_PER_MTOK, and an
unknown model is priced at the most expensive entry rather than zero -- a
model-string typo must never look free. The call cap is kept alongside it:
it bounds request VOLUME (rate limits, wall clock), which a dollar cap
does not."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from smartboi.llm import cost_usd
from smartboi.state import JsonState


@dataclass(frozen=True)
class UsageSnapshot:
    date: str
    calls: int
    input_tokens: int
    output_tokens: int
    daily_call_budget: int
    usd_spent: float = 0.0
    daily_usd_budget: float = 0.0


class UsageTracker:
    def __init__(self, path: Path, daily_call_budget: int, daily_usd_budget: float = 0.0):
        self._state = JsonState(path)
        self.daily_call_budget = daily_call_budget
        # 0 disables the dollar cap (call cap only) -- the pre-existing
        # behaviour, kept reachable so an operator who wants exactly the old
        # semantics can have them.
        self.daily_usd_budget = daily_usd_budget

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _roll_if_new_day(self, today: str) -> None:
        if self._state.get("date") != today:
            self._state.set("date", today)
            self._state.set("calls", 0)
            self._state.set("input_tokens", 0)
            self._state.set("output_tokens", 0)
            self._state.set("usd_spent", 0.0)

    def budget_remaining(self, today: str | None = None) -> bool:
        """Whether BOTH budgets still have room. Either one exhausted defers
        further scoring until UTC midnight, exactly as the call cap alone
        used to -- evidence is deferred, never discarded (see engine.py)."""
        today = today or self._today()
        self._roll_if_new_day(today)
        if self._state.get("calls", 0) >= self.daily_call_budget:
            return False
        if self.daily_usd_budget > 0:
            return self._state.get("usd_spent", 0.0) < self.daily_usd_budget
        return True

    def record(self, input_tokens: int, output_tokens: int, today: str | None = None,
               model: str = "") -> None:
        """`model` is what prices the call. Defaulted so an omitted model
        records tokens and the call count but no spend, rather than raising
        -- a metering gap is recoverable, a crashed ingestion pass is not."""
        today = today or self._today()
        self._roll_if_new_day(today)
        self._state.set("calls", self._state.get("calls", 0) + 1)
        self._state.set("input_tokens", self._state.get("input_tokens", 0) + input_tokens)
        self._state.set("output_tokens", self._state.get("output_tokens", 0) + output_tokens)
        if model:
            spent = self._state.get("usd_spent", 0.0) + cost_usd(model, input_tokens, output_tokens)
            self._state.set("usd_spent", round(spent, 6))

    def snapshot(self, today: str | None = None) -> UsageSnapshot:
        today = today or self._today()
        self._roll_if_new_day(today)
        return UsageSnapshot(
            date=self._state.get("date", today),
            calls=self._state.get("calls", 0),
            input_tokens=self._state.get("input_tokens", 0),
            output_tokens=self._state.get("output_tokens", 0),
            daily_call_budget=self.daily_call_budget,
            usd_spent=self._state.get("usd_spent", 0.0),
            daily_usd_budget=self.daily_usd_budget,
        )
