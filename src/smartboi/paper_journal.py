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

# Per-side transaction-cost floors by market-cap bucket, in basis points of
# notional. A flat per-side figure calibrated for liquid names badly
# understates friction exactly where this strategy hunts: realistic
# round trips run 1-4% on sub-$300M names (wide spreads, real impact),
# i.e. a large fraction of the 16%-target/8%-stop distances. Values follow
# the 2026-07 audit's recommendation: 50bp/side above $1B, 150bp/side
# $300M-$1B, 300bp/side below $300M. The configured
# TRANSACTION_COST_BPS_PER_SIDE acts as a floor under all buckets, never a
# ceiling over them.
_CAP_BUCKET_BPS_PER_SIDE = ((1000.0, 50.0), (300.0, 150.0), (0.0, 300.0))
# With no market cap available (Finnhub down/unconfigured, unknown ticker)
# assume the MIDDLE bucket rather than the cheapest -- unknown liquidity is
# not a reason to assume the best case in the record this journal exists
# to keep honest.
_UNKNOWN_CAP_BPS_PER_SIDE = 150.0
# Shorts below this market cap are flagged assumes_borrow: small-caps are
# routinely hard-to-borrow, and a paper SHORT that a real account could
# not have located shares for is not a fill -- the flag keeps those
# separable in every statistic instead of silently commingled.
_BORROW_RISK_CAP_MUSD = 500.0


def cost_bps_per_side_for_cap(market_cap_musd: float | None, floor_bps_per_side: float) -> float:
    """Per-side transaction-cost assumption for a trade in a name of the
    given market cap (in $M), never below the configured floor. None/zero
    cap (lookup failed) gets the middle bucket, not the cheapest."""
    if market_cap_musd is None or market_cap_musd <= 0:
        return max(floor_bps_per_side, _UNKNOWN_CAP_BPS_PER_SIDE)
    for cap_floor, bps in _CAP_BUCKET_BPS_PER_SIDE:
        if market_cap_musd >= cap_floor:
            return max(floor_bps_per_side, bps)
    return max(floor_bps_per_side, _UNKNOWN_CAP_BPS_PER_SIDE)


def assumes_borrow(direction: str, market_cap_musd: float | None) -> bool:
    """Whether a hypothetical SHORT rests on an unverified borrow: below
    the borrow-risk cap (or with no cap known at all), shares may simply
    not have been locatable, so the trade's P&L is conditional on an
    assumption a real account might not have been able to satisfy."""
    if direction != "SHORT":
        return False
    return market_cap_musd is None or market_cap_musd < _BORROW_RISK_CAP_MUSD


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
    # Market cap ($M) at open, when a lookup source was available -- what
    # the cost bucket above was derived from, recorded so the derivation
    # stays auditable per trade. None when no source could price it.
    market_cap_musd: float | None = None
    # True for SHORTs in names small enough (or unknown enough) that a real
    # account might not have located borrowable shares -- see
    # assumes_borrow(). Kept per trade so win-rate/avg-R statistics can be
    # split into "clean" vs "assumes a borrow existed".
    assumes_borrow: bool = False

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
        cost_bps_round_trip: float = 0.0,
        market_cap_musd: float | None = None,
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
            market_cap_musd=market_cap_musd,
            assumes_borrow=assumes_borrow(direction, market_cap_musd),
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
