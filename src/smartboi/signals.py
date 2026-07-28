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
from datetime import datetime, timezone
from pathlib import Path

from smartboi.dossier import Dossier

log = logging.getLogger(__name__)


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


def evaluate(dossier: Dossier, confidence_threshold: float, min_independent_sources: int) -> SignalEvent | None:
    if dossier.direction == "NONE":
        return None
    if dossier.independent_source_count < min_independent_sources:
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
    )


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
