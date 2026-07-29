"""Evaluates dossiers for a signal crossing: confidence * magnitude above
threshold, corroborated by enough independent sources -- the trading
signal is accumulated evidence crossing a bar, not any single article (see
README point 3). Signals are always logged, whether or not a price feed is
configured to actually open a hypothetical position (see paper_journal.py
and prices.py) -- so the detection layer is fully exercisable, and its
output fully visible, before IB is wired in.

Deliberately status-blind: a dossier already SIGNALED (awaiting a price
feed, or with a hypothetical position already open) re-logs an updated
signal each time newly accepted evidence keeps it above threshold. The
SIGNALED status only gates opening a paper trade (engine.py), never
logging -- otherwise a no-price-feed deployment would log exactly one
signal per symbol, ever, since only a closing trade resets the status."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smartboi.dossier import SCORING_VERSION, Dossier

log = logging.getLogger(__name__)

# US equity regular trading hours, in exchange-local time. Entries are
# gated on these (see is_regular_trading_hours) because a paper trade
# booked at a price no order could have been filled at is not a paper
# trade, it is a fabricated fill -- and the live record contains two of
# them, opened at 09:18 ET, twelve minutes before the open.
_MARKET_TZ = ZoneInfo("America/New_York")
_MARKET_OPEN = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 0)


@dataclass(frozen=True)
class SignalEvent:
    symbol: str
    direction: str
    confidence: float
    magnitude: float
    horizon_days: int
    independent_source_count: int
    thesis_summary: str
    generated_at: str
    # The bar this signal actually cleared, and the scoring rules that
    # produced the score, stamped onto the row itself.
    #
    # These are not diagnostics. Every threshold here is overridable from
    # the add-on's options.json (which wins over the code default), so the
    # documented 0.65 and the bar a given row actually cleared can differ
    # without anything recording it -- and a forward record whose admission
    # criterion is unknown per row cannot be partitioned later. That is not
    # a reporting inconvenience: SCORING_VERSION exists precisely so the
    # record can be split at a rules boundary instead of silently mixing
    # scores that mean different things, and a split needs the rules to be
    # ON the row. Forward data cannot be backfilled, so a row written
    # without them is unrecoverable -- it can never be re-stamped, because
    # nothing remembers what was in force when it was written.
    threshold_in_force: float = 0.0
    min_sources_in_force: int = 0
    scoring_version: int = 0


def evaluate(
    dossier: Dossier,
    confidence_threshold: float,
    min_independent_sources: int,
    min_independent_sources_news_only: int | None = None,
) -> SignalEvent | None:
    if dossier.direction == "NONE":
        return None
    required_sources = min_independent_sources
    primary_source_backing = dossier.has_filing_evidence or dossier.has_disclosed_link_evidence
    if min_independent_sources_news_only is not None and not primary_source_backing:
        # The elevated bar exists for ONE failure mode: two outlets
        # rewording a single wire story slipping past dedup's near-dup
        # check as two "independent" sources. It is not a general
        # "be more sure" tax, and applying it as one actively fights this
        # system's premise.
        #
        # Two things take a dossier out of that failure mode, and they are
        # equivalent for this purpose:
        #   - a filing (8-K, Form 4, 10-Q...) on the agreeing side, which
        #     is a primary disclosure and cannot be a reworded article; or
        #   - evidence propagated over a STRONGLY DISCLOSED relationship
        #     edge (see dossier.DISCLOSED_LINK_CONFIDENCE), where the
        #     causal link itself -- the part actually at risk of being
        #     wrong -- comes from a 10-K, usually with a quantified share
        #     of revenue attached.
        #
        # The second case is the whole strategy: the edge is reading one
        # high-quality fact about an anchor and inferring the effect on a
        # thinly-covered supplier BEFORE the market connects them. Waiting
        # for a third publisher to write that connection down means waiting
        # for the edge to disappear. Corroborating "did the event happen"
        # is redundant when the event is an official guidance raise;
        # corroborating "is the link real" is what matters, and a filing
        # already did it.
        required_sources = max(required_sources, min_independent_sources_news_only)
    if dossier.independent_source_count < required_sources:
        return None
    if dossier.confidence * dossier.magnitude < confidence_threshold:
        return None
    return SignalEvent(
        symbol=dossier.symbol,
        direction=dossier.direction,
        confidence=dossier.confidence,
        magnitude=dossier.magnitude,
        horizon_days=dossier.horizon_days,
        independent_source_count=dossier.independent_source_count,
        thesis_summary=dossier.thesis_summary,
        generated_at=datetime.now(timezone.utc).isoformat(),
        # `required_sources`, not min_independent_sources: the news-only
        # elevation above is part of the bar this row cleared, and stamping
        # the unelevated setting would misdescribe exactly the rows where
        # the distinction mattered.
        threshold_in_force=confidence_threshold,
        min_sources_in_force=required_sources,
        scoring_version=SCORING_VERSION,
    )


def is_regular_trading_hours(now: datetime | None = None) -> bool:
    """Whether US equities are in their regular session right now.

    An entry booked outside this window is not a fill anybody could have
    got. The price sources do not refuse to answer out of hours -- IB and
    Finnhub both return the last session's close -- so without this check
    the engine happily opens a position at a stale price and stamps it with
    the current timestamp. The live record has two: ESOA and PUMP, both
    booked 13:18Z, which is 09:18 ET, twelve minutes before the open. Every
    subsequent statistic about those trades inherits an entry price that
    was never available.

    Exchange-local rather than a fixed UTC offset, so this stays correct
    across DST: the ET session is 13:30-20:00 UTC in summer and 14:30-21:00
    in winter, and a hard-coded UTC window is wrong for half the year.

    KNOWN GAP: market holidays. They are all weekdays, so a holiday still
    passes this check and the same stale-close problem applies to that one
    day. A holiday calendar needs annual maintenance and silently rots when
    it stops getting it, which is a worse failure than the one it fixes --
    so this covers nights and weekends (where the observed damage was) and
    the residual is documented rather than half-solved."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_MARKET_TZ)
    if local.weekday() >= 5:  # Saturday/Sunday
        return False
    return _MARKET_OPEN <= local.time() < _MARKET_CLOSE


def favorable_drift_pct(direction: str, signaled_price: float, current_price: float) -> float:
    """Percent the price has already moved in the signal's favorable
    direction since it fired -- how much of the anticipated correction may
    already have been captured by the market between signal and entry.
    Positive means price moved the way the thesis expects (up for LONG,
    down for SHORT); used by engine.py to decide whether an entry would be
    chasing a move that's already largely over ("are we too late")."""
    if signaled_price <= 0:
        return 0.0
    pct = (current_price - signaled_price) / signaled_price * 100
    return pct if direction == "LONG" else -pct


def signal_expired(signaled_at: str, deadline_days: int, now: datetime | None = None) -> bool:
    """True once a signal has been waiting this long for an entry that
    never got confirmed (no reachable price feed, or price drift blocked
    it every poll) -- used to reset a dossier back to ACTIVE instead of
    leaving it stuck SIGNALED forever chasing a stale opportunity."""
    if not signaled_at:
        return False
    now = now or datetime.now(timezone.utc)
    signaled = datetime.fromisoformat(signaled_at)
    if signaled.tzinfo is None:
        signaled = signaled.replace(tzinfo=timezone.utc)
    return (now - signaled).days >= deadline_days


def log_decision(
    log_path: Path,
    event: str,
    symbol: str,
    direction: str,
    episode: str,
    price: float | None = None,
    reason: str = "",
) -> None:
    """Appends one row to the decisions ledger (logs/decisions.jsonl): what
    the engine DID with a signal episode -- "trade_opened", "drift_skip",
    or "signal_expired" -- with the price at decision time when one was in
    hand. Signals firing is only half the record: without this, a
    drift-skip or expiry survives only as a log line, and there is no way
    to ever learn whether the entry-timing guards helped (skipped moves
    that were indeed over) or hurt (skipped moves that kept going). The
    `episode` key is the dossier's signaled_at, the same key signals.jsonl
    rows carry, so the two logs join cleanly (see event_study.py)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps({
            "event": event,
            "symbol": symbol,
            "direction": direction,
            "episode": episode,
            "price": price,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }) + "\n")


def log_signal(log_path: Path, signal: SignalEvent, episode: str = "") -> None:
    """`episode` is the dossier's signaled_at timestamp: evaluation is
    status-blind (see module docstring), so one signal EPISODE re-logs a
    row on every newly accepted evidence item that keeps it above
    threshold -- without an episode key, downstream event-level analysis
    ("how did signals perform?") would count each re-log as a separate
    signal instead of collapsing them to one event."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps({**asdict(signal), "episode": episode}) + "\n")
    log.info(
        "[SIGNAL] %s %s confidence=%.2f magnitude=%.2f sources=%d: %s",
        signal.direction, signal.symbol, signal.confidence, signal.magnitude,
        signal.independent_source_count, signal.thesis_summary,
    )
