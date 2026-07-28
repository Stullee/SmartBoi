"""Hypothetical ("paper") trades opened when a dossier signal crosses
threshold -- the entire point of this system: track every trade the
evidence-synthesis strategy WOULD have made, so its validity can be judged
on real forward performance before any real capital, or even a real
broker paper account, is ever involved.

This module contains NO order-placement code whatsoever -- by design, so
there is no code path through which this system could ever submit a real
order (see README's "hardcoded paper-only" guarantee). It simulates
against prices from prices.py, which is itself read-only and optional; see
that module's docstring for what happens before it's configured."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PaperTrade:
    symbol: str
    direction: str  # LONG | SHORT
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: str
    horizon_days: int
    thesis_summary: str
    confidence: float
    independent_source_count: int
    citations: list[dict] = field(default_factory=list)  # [{source_name, url, headline, published_at}, ...]
    status: str = "OPEN"  # OPEN | WIN | LOSS | TIMEOUT
    closed_at: str | None = None
    exit_price: float | None = None
    r_multiple: float | None = None
    last_price: float | None = None

    def unrealized_r_multiple(self) -> float | None:
        if self.last_price is None:
            return None
        risk = abs(self.entry_price - self.stop_price)
        if risk <= 0:
            return None
        pnl = (
            self.last_price - self.entry_price
            if self.direction == "LONG"
            else self.entry_price - self.last_price
        )
        return round(pnl / risk, 3)


class PaperTradeJournal:
    """At most one open paper trade per symbol. Mirrors TradingBot's
    ShadowTradeTracker (append-only closed-trade log + open-state snapshot
    for restart continuity), but on a weeks-not-minutes clock: `horizon_days`
    replaces a minutes-based max hold, and stop/target are plain percentage
    bands (no intraday bar/ATR data exists at this cadence)."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.open_state_path = log_path.parent / "open_paper_trades.json"
        self.open_trades: dict[str, PaperTrade] = self._load_open_state()

    def _load_open_state(self) -> dict[str, PaperTrade]:
        if not self.open_state_path.exists():
            return {}
        try:
            raw = json.loads(self.open_state_path.read_text())
            trades = {symbol: PaperTrade(**fields) for symbol, fields in raw.items()}
        except (json.JSONDecodeError, OSError, TypeError):
            log.warning("Could not read %s, starting with no open paper trades.", self.open_state_path)
            return {}
        # Self-heal the close-crash window: _close appends to the closed
        # log BEFORE rewriting the open-state snapshot, so a crash between
        # the two leaves the trade in both files -- on restart it would be
        # marked again and closed a SECOND time at a different price,
        # double-counting it in every win-rate/avg-R statistic.
        closed = self._closed_trade_keys()
        for symbol in list(trades):
            trade = trades[symbol]
            if (trade.symbol, trade.opened_at) in closed:
                log.warning("%s: dropping open-state entry already present in the closed log "
                            "(crash between close-log append and open-state write).", symbol)
                del trades[symbol]
        return trades

    def _closed_trade_keys(self) -> set[tuple[str, str]]:
        if not self.log_path.exists():
            return set()
        keys: set[tuple[str, str]] = set()
        try:
            lines = self.log_path.read_text().splitlines()
        except OSError:
            return set()
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            keys.add((row.get("symbol"), row.get("opened_at")))
        return keys

    def _write_open_state(self) -> None:
        self.open_state_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {symbol: asdict(trade) for symbol, trade in self.open_trades.items()}
        tmp = self.open_state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot))
        tmp.replace(self.open_state_path)

    def has_open(self, symbol: str) -> bool:
        return symbol in self.open_trades

    def open(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        horizon_days: int,
        thesis_summary: str,
        confidence: float,
        independent_source_count: int,
        citations: list[dict],
    ) -> PaperTrade:
        if direction == "LONG":
            stop_price = entry_price * (1 - stop_loss_pct / 100)
            target_price = entry_price * (1 + take_profit_pct / 100)
        else:
            stop_price = entry_price * (1 + stop_loss_pct / 100)
            target_price = entry_price * (1 - take_profit_pct / 100)
        trade = PaperTrade(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            opened_at=datetime.now(timezone.utc).isoformat(),
            horizon_days=horizon_days,
            thesis_summary=thesis_summary,
            confidence=confidence,
            independent_source_count=independent_source_count,
            citations=citations,
        )
        self.open_trades[symbol] = trade
        self._write_open_state()
        log.info(
            "[PAPER] Opened %s %s @ %.2f (stop=%.2f, target=%.2f, horizon=%dd, confidence=%.2f): %s",
            direction, symbol, entry_price, stop_price, target_price, horizon_days, confidence, thesis_summary,
        )
        return trade

    def update(
        self,
        symbol: str,
        current_price: float,
        now: datetime | None = None,
        high: float | None = None,
        low: float | None = None,
    ) -> None:
        """Marks an open trade against the latest price. When the day's
        `high`/`low` are provided (see prices.PriceBar), stop and target
        are checked against the intraday EXTREMES, not just the close: a
        stock that traded through the 8% stop midday and recovered by the
        close is a real stop-out for any live position, and evaluating on
        the close alone silently erased exactly those losses (while the
        matching effect on 16% targets is far rarer) -- an optimistic bias
        in precisely the statistic this journal exists to make honest.
        Conservative fill assumptions: a stop fills at the stop price or
        the close, whichever is WORSE for the trade (a gap through the
        stop can't fill at the stop); a target fills at the target price
        exactly (a limit order never fills better than its level). When
        both levels traded in the same bar the stop wins -- with no
        intraday sequencing available, assuming the loss is the honest
        choice. Without high/low this degrades to the old close-only
        behavior."""
        trade = self.open_trades.get(symbol)
        if trade is None:
            return
        now = now or datetime.now(timezone.utc)
        trade.last_price = current_price
        day_high = high if high is not None else current_price
        day_low = low if low is not None else current_price

        if trade.direction == "LONG":
            hit_stop = day_low <= trade.stop_price
            hit_target = day_high >= trade.target_price
            stop_fill = min(trade.stop_price, current_price)
        else:
            hit_stop = day_high >= trade.stop_price
            hit_target = day_low <= trade.target_price
            stop_fill = max(trade.stop_price, current_price)

        opened_at = datetime.fromisoformat(trade.opened_at)
        timed_out = (now - opened_at).days >= trade.horizon_days

        if hit_stop:
            self._close(trade, "LOSS", stop_fill, now)
        elif hit_target:
            self._close(trade, "WIN", trade.target_price, now)
        elif timed_out:
            self._close(trade, "TIMEOUT", current_price, now)
        else:
            self._write_open_state()

    def _close(self, trade: PaperTrade, status: str, exit_price: float, now: datetime) -> None:
        risk = abs(trade.entry_price - trade.stop_price)
        pnl = (
            exit_price - trade.entry_price
            if trade.direction == "LONG"
            else trade.entry_price - exit_price
        )
        trade.status = status
        trade.exit_price = exit_price
        trade.closed_at = now.isoformat()
        trade.r_multiple = round(pnl / risk, 3) if risk > 0 else 0.0
        log.info(
            "[PAPER] Closed %s %s: %s @ %.2f (R=%.2f)",
            trade.direction, trade.symbol, status, exit_price, trade.r_multiple,
        )
        self._append_to_log(trade)
        del self.open_trades[trade.symbol]
        self._write_open_state()

    def _append_to_log(self, trade: PaperTrade) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
