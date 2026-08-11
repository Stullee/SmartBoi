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
does not.

PER-CATEGORY CAPS exist because one shared pool is first-come-first-served,
and the order things arrive in has nothing to do with what they are worth.

The budget resets at UTC midnight, which is 20:00 ET -- just after the US
close. That leaves thirteen and a half hours of night before the next open,
and relationship extraction (the high-volume 150k-char pass, whichever way
its queue happens to be filled) will happily spend the entire day's budget
in them. By 09:30 ET, when news actually starts mattering and the dossier
pass is the only thing that can turn it into a position, there is nothing
left. Measured live: budget exhausted before the market opened.

Note that config.py already claimed the daily cap makes a backfill burst
"spread itself over several days instead of consuming a month of budget in
an afternoon". It does -- but it spread by starving everything else first,
which is not what that sentence means to a reader. The category caps are
what make it true.

So each caller declares a CATEGORY and each capped category gets a maximum
share of the day. Deliberately caps rather than fixed partitions: this
system's inputs are bursty (filing season versus not, news volume by day),
and a fixed partition idles 35% of the budget on a day with no filings
while the pass that matters starves. A cap wastes nothing -- the
trading-critical bucket is left UNCAPPED, so it can use the whole day when
nothing else wants it, and is guaranteed whatever the capped categories
cannot touch."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from smartboi.llm import cost_usd, permanent_failure_reason
from smartboi.state import JsonState

log = logging.getLogger(__name__)


# The five callers, one per class that holds a UsageTracker. EXTRACTION and
# RESEARCH are not time-critical -- a graph edge discovered tomorrow is worth
# exactly what it was worth today, and supplier research is discretionary.
# DOSSIER is: news decays, and reading one fact about an anchor before the
# market connects it to a supplier IS the strategy. The updater and the
# skeptic share one category on purpose -- a proposal whose verdict was
# deferred is spend already paid for with nothing to show, so splitting them
# would let the budget strand half-finished work.
CAT_EXTRACTION = "extraction"   # graph.RelationshipExtractor
CAT_DOSSIER = "dossier"         # dossier.DossierUpdater + skeptic.Skeptic
CAT_SYNTHESIS = "synthesis"     # dossier.DossierSynthesizer
CAT_RESEARCH = "research"       # research.SupplierResearcher

CATEGORIES = (CAT_DOSSIER, CAT_SYNTHESIS, CAT_EXTRACTION, CAT_RESEARCH)

# How often the circuit breaker lets a single call through to test whether an
# account-level problem has been resolved.
#
# The failure being prevented is a retry STORM -- 11,893 calls in two hours,
# three a second -- not a retry. Blocking outright until UTC midnight would
# trade that storm for the opposite fault: an operator who tops up a balance
# at 10:00 UTC would get nothing scored for fourteen hours over a problem they
# had already fixed. Thirty minutes bounds the loop at roughly two calls an
# hour (a rounding error against a 5000-call day) while recovering on its own.
BREAKER_PROBE_INTERVAL_SEC = 30 * 60


@dataclass(frozen=True)
class UsageSnapshot:
    date: str
    calls: int
    input_tokens: int
    output_tokens: int
    daily_call_budget: int
    usd_spent: float = 0.0
    daily_usd_budget: float = 0.0
    # {category: (usd, calls)} for everything that spent anything today.
    by_category: dict[str, tuple[float, int]] = field(default_factory=dict)
    # Why LLM calls are halted for the rest of the UTC day, or "" when they
    # are not. See UsageTracker.note_failure.
    breaker_reason: str = ""
    breaker_tripped_at: str = ""


class UsageTracker:
    def __init__(self, path: Path, daily_call_budget: int, daily_usd_budget: float = 0.0,
                 category_shares: dict[str, float] | None = None,
                 category_reserved: dict[str, float] | None = None):
        self._state = JsonState(path)
        self.daily_call_budget = daily_call_budget
        # 0 disables the dollar cap (call cap only) -- the pre-existing
        # behaviour, kept reachable so an operator who wants exactly the old
        # semantics can have them.
        self.daily_usd_budget = daily_usd_budget
        # {category: max fraction of the day it may spend}. A category absent
        # from this dict is UNCAPPED. 1.0 is also uncapped, stated
        # explicitly; 0.0 genuinely means "make no calls at all", which is a
        # useful thing to set for a category you don't want running
        # unsupervised.
        self.category_shares = dict(category_shares or {})
        # {category: fraction of the day RESERVED for it}, which is a
        # different and stronger thing than a ceiling.
        #
        # A ceiling protects nothing. The total-budget check runs first, so
        # once another category has spent the day, every remaining category
        # is refused no matter how much of its own share is untouched.
        # Measured live after a week on a $10 day: dossier $6.47, extraction
        # $3.54, synthesis $0.00 against a $2.50 ceiling -- and synthesis had
        # therefore never run at all, in the entire life of the system. It is
        # the one pass that asks whether N pieces of evidence are N facts or
        # one fact N times, and it was being starved by the pass whose output
        # it exists to judge.
        #
        # Timing is what makes a ceiling useless here rather than merely
        # imperfect. The daily decay pass is the only caller of synthesis and
        # is scheduled off a persisted wall clock, so whichever hour it first
        # ran at is its slot forever; land that slot late in the UTC day and
        # the budget is reliably already gone. A reservation does not care
        # when the pass runs.
        self.category_reserved = dict(category_reserved or {})

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
            self._state.set("usd_by_category", {})
            self._state.set("calls_by_category", {})
            # The breaker clears with the day, exactly like the budget. An
            # account-level failure is not self-healing, but neither is it
            # permanent -- somebody tops the balance up -- and UTC midnight is
            # already this system's retry-everything boundary. Deferred, never
            # discarded, the same contract an exhausted budget has.
            self._state.set("breaker_reason", "")
            self._state.set("breaker_tripped_at", "")

    def _reserved_elsewhere(self, category: str) -> float:
        """Dollars reserved by OTHER categories that they have not spent yet.

        Unspent only: a reservation that has been used is no longer set
        aside, so it stops shrinking everyone else's pool. That is what keeps
        this from wasting the budget -- the protection costs the other
        categories nothing once the reserved pass has actually run."""
        if self.daily_usd_budget <= 0:
            return 0.0
        total = 0.0
        for cat, share in self.category_reserved.items():
            if cat == category or share <= 0:
                continue
            spent, _ = self._category_spent(cat)
            total += max(0.0, self.daily_usd_budget * share - spent)
        return total

    def _reserved_calls_elsewhere(self, category: str) -> int:
        """Calls reserved by OTHER categories that they have not spent yet --
        the call-count analogue of _reserved_elsewhere.

        The reservation protected reserved categories' DOLLARS but not their
        CALLS, and the total CALL cap is exactly what high-volume fan-out (the
        dossier pass) exhausts first -- historically thousands of dossier calls
        against a 3000-call ceiling. Once the call cap was hit, synthesis was
        refused before it priced a single token, so the whole-evidence-body cap
        (the one pass that catches 'ten items are one fact') silently failed
        open on the busiest evidence days. The same reservation fractions now
        set aside calls as well as dollars, using floor() so rounding never
        over-reserves the discrete call budget."""
        total = 0
        for cat, share in self.category_reserved.items():
            if cat == category or share <= 0:
                continue
            _, spent_calls = self._category_spent(cat)
            total += max(0, int(self.daily_call_budget * share) - spent_calls)
        return total

    def _category_spent(self, category: str) -> tuple[float, int]:
        usd = (self._state.get("usd_by_category") or {}).get(category, 0.0)
        calls = (self._state.get("calls_by_category") or {}).get(category, 0)
        return usd, calls

    def note_failure(self, exc: object, today: str | None = None) -> bool:
        """Record an API failure; trip the breaker and return True when it is
        an account-level one that retrying cannot fix.

        Every LLM call site in this codebase catches broadly and returns None,
        which the engine reads as "transient, retry later". That is right for
        a rate limit and wrong for an exhausted balance, and the difference
        showed up as 11,893 identical billing failures in two hours -- roughly
        three requests a second against an error no retry could clear (see
        llm.permanent_failure_reason). Nothing bounded it, because every
        bounding mechanism in this system counts SUCCESSFUL calls: the budget
        meters spend, and a failed call spends nothing, so a hard failure loop
        is invisible to the one gate that could have stopped it.

        Tripping is deliberately routed through the SAME gate the budget uses,
        rather than a new check bolted onto each caller: all five call sites
        already ask budget_remaining() before every request, so one change
        covers extraction, dossier, skeptic, synthesis and research at once,
        and no future call site can forget to consult it."""
        reason = permanent_failure_reason(exc)
        if not reason:
            return False
        # Never let bookkeeping displace the failure it is recording. This runs
        # as the first statement of an `except` handler, and JsonState.update
        # does a real fsync'd write -- an OSError here (a full disk, which is
        # the characteristic Home-Assistant-on-an-SD-card failure) would
        # replace the API exception the caller is in the middle of handling
        # with a disk error, losing the diagnosis entirely.
        try:
            today = today or self._today()
            self._roll_if_new_day(today)
            already_open = bool(self._state.get("breaker_reason"))
            now = datetime.now(timezone.utc).isoformat()
            # Re-stamped on every failure, not only the first: the stamp is
            # what the probe interval counts from, so a failed probe has to
            # restart the clock or the breaker would let one call through on
            # every subsequent check.
            self._state.update({"breaker_reason": reason, "breaker_tripped_at": now})
        except OSError:
            log.exception("Could not persist the LLM circuit breaker state.")
            return True
        if already_open:
            return True  # don't re-log once per suppressed call
        log.error(
            "LLM circuit breaker OPEN: %s. Halting Claude calls rather than retrying an error "
            "that cannot succeed -- ingestion continues, evidence keeps accruing, and nothing is "
            "scored until this is resolved. One probe call is allowed every %d minutes so a "
            "resolved problem recovers on its own. Original error: %s",
            reason, BREAKER_PROBE_INTERVAL_SEC // 60, exc,
        )
        return True

    def deferral_reason(self, category: str = "", today: str | None = None) -> str:
        """Why a call in `category` would be refused right now, phrased for a
        log line, or "" when it would go through.

        Exists because every call site logged "daily LLM call budget reached"
        for any refusal, so a breaker halt -- a different problem with a
        different fix -- was reported as an exhausted budget on a day that had
        spent nothing."""
        if self.breaker_reason(today):
            return f"LLM circuit breaker open ({self.breaker_reason(today)})"
        return "" if self.budget_remaining(category, today) else "daily LLM budget reached"

    def _breaker_probe_due(self) -> bool:
        """Whether enough time has passed since the last failure to spend one
        call finding out whether the problem is still there."""
        stamp = self._state.get("breaker_tripped_at") or ""
        try:
            tripped = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return True  # unreadable stamp: probe rather than block forever
        if tripped.tzinfo is None:
            tripped = tripped.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - tripped).total_seconds()
        return age >= BREAKER_PROBE_INTERVAL_SEC

    def breaker_reason(self, today: str | None = None) -> str:
        """Why calls are halted, or "" when they are not. For the dashboard
        and the diagnostics bundle -- an operator seeing zero LLM activity
        needs this stated, not inferred from a spend of $0.

        Takes `today` like every other method here rather than reading the
        wall clock. As a bare property it rolled the day against the real
        clock, which made READING a diagnostic mutate state: on the first read
        after midnight it cleared the breaker and wrote to disk as a side
        effect of being looked at."""
        today = today or self._today()
        self._roll_if_new_day(today)
        return self._state.get("breaker_reason", "") or ""

    def budget_remaining(self, category: str = "", today: str | None = None) -> bool:
        """Whether the day still has room for a call in `category`.

        Four gates, all of which must pass: the circuit breaker, the total
        call cap, the total dollar cap, and this category's own share of each.
        Any one exhausted defers the work until UTC midnight, exactly as the
        call cap alone used to -- deferred, never discarded (see engine.py).

        An empty category, or one with no configured share, is checked
        against the TOTALS only. That is not a loophole, it is the design:
        the trading-critical bucket is meant to be able to use the whole day
        when nothing else wants it. What protects it is that the other
        categories cannot."""
        today = today or self._today()
        self._roll_if_new_day(today)
        # Checked FIRST and for every category: an account-level failure is
        # not category-specific, and a reserved category would otherwise sail
        # past a gate that exists precisely to stop the request it is about
        # to make.
        #
        # Not an absolute block, though. Waiting for UTC midnight would mean
        # an operator who tops up a balance at 10:00 UTC gets nothing scored
        # for fourteen hours over a problem they already fixed -- and the
        # thing being prevented is a retry STORM (three per second), not a
        # retry. So one probe call is let through per interval: the failure
        # loop is bounded at roughly two calls an hour, and a resolved problem
        # recovers on its own within the interval. A successful call clears
        # the breaker outright (see record); a failed probe re-stamps it.
        if self._state.get("breaker_reason"):
            if not self._breaker_probe_due():
                return False
        # The total call cap, minus whatever other categories have RESERVED and
        # not yet spent -- mirrors the dollar gate below. Without this, a
        # reserved pass (synthesis) had its dollars protected but could still
        # be refused the moment the shared call cap was hit by fan-out, which
        # is the axis that actually exhausts first.
        available_calls = self.daily_call_budget - self._reserved_calls_elsewhere(category)
        if self._state.get("calls", 0) >= available_calls:
            return False
        # The total this category may draw against is the day MINUS whatever
        # other categories have reserved and not yet spent. Their reservation
        # is not this category's to consume, which is the whole point -- a
        # bare `usd_spent >= daily_usd_budget` let the first spender of the
        # day take money that had been explicitly set aside for a pass that
        # had not run yet.
        if self.daily_usd_budget > 0:
            available = self.daily_usd_budget - self._reserved_elsewhere(category)
            if self._state.get("usd_spent", 0.0) >= available:
                return False

        share = self.category_shares.get(category)
        if share is None or share >= 1.0:
            return True
        if share <= 0:
            return False  # explicitly switched off
        spent_usd, spent_calls = self._category_spent(category)
        if spent_calls >= self.daily_call_budget * share:
            return False
        if self.daily_usd_budget > 0:
            return spent_usd < self.daily_usd_budget * share
        return True

    def record(self, input_tokens: int, output_tokens: int, today: str | None = None,
               model: str = "", category: str = "") -> None:
        """`model` is what prices the call. Defaulted so an omitted model
        records tokens and the call count but no spend, rather than raising
        -- a metering gap is recoverable, a crashed ingestion pass is not.
        `category` is defaulted for the same reason: an uncategorised call
        still counts against the totals, it just isn't attributed."""
        today = today or self._today()
        self._roll_if_new_day(today)
        # A call that actually succeeded is proof the account-level problem is
        # gone -- somebody topped the balance up, or fixed the key. Clearing
        # here rather than on a timer is what makes the probe above a recovery
        # mechanism instead of just a slower failure loop.
        if self._state.get("breaker_reason"):
            log.info("LLM circuit breaker CLOSED -- a call succeeded, resuming normal operation.")
            self._state.update({"breaker_reason": "", "breaker_tripped_at": ""})
        self._state.set("calls", self._state.get("calls", 0) + 1)
        self._state.set("input_tokens", self._state.get("input_tokens", 0) + input_tokens)
        self._state.set("output_tokens", self._state.get("output_tokens", 0) + output_tokens)
        cost = cost_usd(model, input_tokens, output_tokens) if model else 0.0
        if model:
            self._state.set("usd_spent", round(self._state.get("usd_spent", 0.0) + cost, 6))
        if category:
            # Calls are attributed even when the model is unknown (cost 0):
            # the call-share cap is the only thing standing between a
            # mispriced model and a category quietly consuming the whole day
            # without its dollar counter ever moving.
            by_usd = dict(self._state.get("usd_by_category") or {})
            by_calls = dict(self._state.get("calls_by_category") or {})
            by_usd[category] = round(by_usd.get(category, 0.0) + cost, 6)
            by_calls[category] = by_calls.get(category, 0) + 1
            self._state.set("usd_by_category", by_usd)
            self._state.set("calls_by_category", by_calls)

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
            by_category={
                cat: (round(usd, 4), (self._state.get("calls_by_category") or {}).get(cat, 0))
                for cat, usd in (self._state.get("usd_by_category") or {}).items()
            },
            breaker_reason=self._state.get("breaker_reason", "") or "",
            breaker_tripped_at=self._state.get("breaker_tripped_at", "") or "",
        )
