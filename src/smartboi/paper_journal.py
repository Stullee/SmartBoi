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
    r_multiple: float | None = None            # NET of transaction costs
    r_multiple_gross: float | None = None      # before costs, for comparison
    last_price: float | None = None
    # Round-trip cost charged against this trade, in basis points of notional
    # (both sides combined). Recorded per trade rather than assumed, so a
    # record stays interpretable if the assumption is ever changed.
    cost_bps_round_trip: float = 0.0

    def _net_pnl(self, exit_price: float) -> float:
        """P&L per share after the round-trip transaction cost.

        Costs are charged as a fraction of notional on BOTH sides, which is
        the standard way to express spread-crossing plus impact. This
        matters more here than in most systems: the strategy deliberately
        targets small, thinly-covered names, and that is exactly the size
        bucket where spreads are widest -- the published lead-lag anomaly
        this implements is documented to be statistically insignificant for
        the largest, most liquid names, so the cost drag cannot be escaped
        by trading bigger."""
        gross = (
            exit_price - self.entry_price
            if self.direction == "LONG"
            else self.entry_price - exit_price
        )
        cost = (self.entry_price + exit_price) * (self.cost_bps_round_trip / 2.0) / 10_000.0
        return gross - cost

    def unrealized_r_multiple(self) -> float | None:
        """Net of the round-trip cost, because that is the number that
        decides whether the thesis paid -- an unrealized figure quoted gross
        systematically flatters an open book."""
        if self.last_price is None:
            return None
        risk = abs(self.entry_price - self.stop_price)
        if risk <= 0:
            return None
        return round(self._net_pnl(self.last_price) / risk, 3)


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
            return {symbol: PaperTrade(**fields) for symbol, fields in raw.items()}
        except (json.JSONDecodeError, OSError, TypeError):
            log.warning("Could not read %s, starting with no open paper trades.", self.open_state_path)
            return {}

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
        cost_bps_round_trip: float = 0.0,
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
            cost_bps_round_trip=cost_bps_round_trip,
        )
        self.open_trades[symbol] = trade
        self._write_open_state()
        log.info(
            "[PAPER] Opened %s %s @ %.2f (stop=%.2f, target=%.2f, horizon=%dd, confidence=%.2f): %s",
            direction, symbol, entry_price, stop_price, target_price, horizon_days, confidence, thesis_summary,
        )
        return trade

    def update(self, symbol: str, current_price: float, now: datetime | None = None) -> None:
        trade = self.open_trades.get(symbol)
        if trade is None:
            return
        now = now or datetime.now(timezone.utc)
        trade.last_price = current_price

        if trade.direction == "LONG":
            hit_target = current_price >= trade.target_price
            hit_stop = current_price <= trade.stop_price
        else:
            hit_target = current_price <= trade.target_price
            hit_stop = current_price >= trade.stop_price

        opened_at = datetime.fromisoformat(trade.opened_at)
        timed_out = (now - opened_at).days >= trade.horizon_days

        if hit_target:
            self._close(trade, "WIN", current_price, now)
        elif hit_stop:
            self._close(trade, "LOSS", current_price, now)
        elif timed_out:
            self._close(trade, "TIMEOUT", current_price, now)
        else:
            self._write_open_state()

    def _close(self, trade: PaperTrade, status: str, exit_price: float, now: datetime) -> None:
        risk = abs(trade.entry_price - trade.stop_price)
        gross = (
            exit_price - trade.entry_price
            if trade.direction == "LONG"
            else trade.entry_price - exit_price
        )
        trade.status = status
        trade.exit_price = exit_price
        trade.closed_at = now.isoformat()
        trade.r_multiple_gross = round(gross / risk, 3) if risk > 0 else 0.0
        trade.r_multiple = round(trade._net_pnl(exit_price) / risk, 3) if risk > 0 else 0.0
        log.info(
            "[PAPER] Closed %s %s: %s @ %.2f (R=%.2f net, %.2f gross, %.0fbp round-trip)",
            trade.direction, trade.symbol, status, exit_price,
            trade.r_multiple, trade.r_multiple_gross, trade.cost_bps_round_trip,
        )
        self._append_to_log(trade)
        del self.open_trades[trade.symbol]
        self._write_open_state()

    def _append_to_log(self, trade: PaperTrade) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
