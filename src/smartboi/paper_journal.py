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

from smartboi.market_hours import is_regular_trading_hours

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
# The same buckets sized for a position small enough that IMPACT is not a
# factor and the cost is essentially the half-spread each way plus
# commission. The institutional numbers above assume an order large enough
# to move a thin book; a $2-5k position in a $150M-cap NASDAQ name crosses a
# spread, not a market. Which of the two is right depends entirely on the
# size the record is meant to represent, and that is not something this
# codebase can know -- hence a setting, not a guess. Institutional stays the
# DEFAULT: an over-stated cost makes a real edge look smaller, while an
# under-stated one manufactures an edge that was never there, and only one
# of those two errors is recoverable once the forward record exists.
_RETAIL_CAP_BUCKET_BPS_PER_SIDE = ((1000.0, 15.0), (300.0, 35.0), (0.0, 75.0))
COST_PROFILES: dict[str, tuple[tuple[float, float], ...]] = {
    "institutional": _CAP_BUCKET_BPS_PER_SIDE,
    "retail": _RETAIL_CAP_BUCKET_BPS_PER_SIDE,
}
# With no market cap available (Finnhub down/unconfigured, unknown ticker)
# assume the MIDDLE bucket rather than the cheapest -- unknown liquidity is
# not a reason to assume the best case in the record this journal exists
# to keep honest. Derived from whichever profile is in force rather than
# hard-coded, so the two stay consistent by construction.
_UNKNOWN_CAP_BUCKET_INDEX = 1
# Shorts below this market cap are flagged assumes_borrow: small-caps are
# routinely hard-to-borrow, and a paper SHORT that a real account could
# not have located shares for is not a fill -- the flag keeps those
# separable in every statistic instead of silently commingled.
_BORROW_RISK_CAP_MUSD = 500.0


def cost_buckets(profile: str = "institutional") -> tuple[tuple[float, float], ...]:
    """The cap->bps/side table for a cost profile. An unrecognised name gets
    the institutional (more expensive) table rather than raising: a typo in a
    setting must not be able to make trades look cheaper than they are."""
    return COST_PROFILES.get(profile, _CAP_BUCKET_BPS_PER_SIDE)


def cost_bps_per_side_for_cap(
    market_cap_musd: float | None,
    floor_bps_per_side: float,
    profile: str = "institutional",
) -> float:
    """Per-side transaction-cost assumption for a trade in a name of the
    given market cap (in $M), never below the configured floor. None/zero
    cap (lookup failed) gets the middle bucket, not the cheapest."""
    buckets = cost_buckets(profile)
    unknown_bps = buckets[_UNKNOWN_CAP_BUCKET_INDEX][1]
    if market_cap_musd is None or market_cap_musd <= 0:
        return max(floor_bps_per_side, unknown_bps)
    for cap_floor, bps in buckets:
        if market_cap_musd >= cap_floor:
            return max(floor_bps_per_side, bps)
    return max(floor_bps_per_side, unknown_bps)


@dataclass(frozen=True)
class TradeEconomics:
    """What the configured stop/target grid is actually worth once the
    round-trip cost is charged, for one cost bucket.

    This exists because the 8%/16% grid LOOKS like 2:1 reward:risk and is
    not. Cost is charged against notional while R is measured against the
    stop distance, so the same bps figure is a far larger share of a risk
    unit on a tight stop -- and it lands on BOTH sides, shrinking the win
    and deepening the loss. At 600bp round-trip (the sub-$300M
    institutional bucket) the real payoff of a nominal 2:1 grid is
    +1.19R/-1.72R, which needs a 59% hit rate merely to break even. Nothing
    in the pipeline surfaced that, so it is computed here and printed in
    diagnostics rather than left to be discovered from a losing record."""

    r_win: float
    r_loss: float
    breakeven_win_rate: float
    cost_share_of_risk: float  # round-trip cost at the stop, as a fraction of one R


def trade_economics(
    stop_loss_pct: float,
    take_profit_pct: float,
    cost_bps_round_trip: float,
    direction: str = "LONG",
) -> TradeEconomics:
    """Net-of-cost R at the target and at the stop, and the win rate at
    which they cancel. Scale-invariant -- both P&L and cost are linear in
    the entry price, so the entry price divides out and any positive value
    gives the same answer."""
    entry = 100.0
    if direction == "LONG":
        target = entry * (1 + take_profit_pct / 100)
        stop = entry * (1 - stop_loss_pct / 100)
    else:
        target = entry * (1 - take_profit_pct / 100)
        stop = entry * (1 + stop_loss_pct / 100)
    risk = abs(entry - stop)
    if risk <= 0:
        return TradeEconomics(0.0, 0.0, 1.0, 0.0)

    def net_r(exit_price: float) -> float:
        gross = exit_price - entry if direction == "LONG" else entry - exit_price
        cost = (entry + exit_price) * (cost_bps_round_trip / 2.0) / 10_000.0
        return (gross - cost) / risk

    r_win = net_r(target)
    r_loss = net_r(stop)
    # Break-even p solves p*r_win + (1-p)*r_loss = 0. With r_loss < 0 and
    # r_win > 0 that is -r_loss / (r_win - r_loss). A grid whose win leg is
    # already negative after costs cannot break even at any hit rate, which
    # is reported as 1.0 (i.e. "not achievable") rather than a nonsense
    # fraction above 1.
    if r_win <= 0:
        breakeven = 1.0
    else:
        breakeven = min(1.0, -r_loss / (r_win - r_loss))
    stop_cost = (entry + stop) * (cost_bps_round_trip / 2.0) / 10_000.0
    return TradeEconomics(
        r_win=round(r_win, 3),
        r_loss=round(r_loss, 3),
        breakeven_win_rate=round(breakeven, 4),
        cost_share_of_risk=round(stop_cost / risk, 4),
    )


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
    # When last_price was last refreshed (UTC ISO). Surfaced so the dashboard
    # can show how stale an open trade's mark is -- the IB feed can lag or drop
    # for stretches, and a confident-looking unrealized number sitting on a
    # price from hours ago should read as exactly that.
    last_marked_at: str | None = None
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
    # Currency notional this trade represents (initial_trading_capital /
    # max_concurrent_positions at open), and the realised currency P&L
    # (notional * net-of-cost return) filled in at close. 0.0 / None on a
    # record written before the account model existed, which the stats treat
    # as "no currency data" rather than a real zero.
    position_value: float = 0.0
    currency_pnl: float | None = None
    # The strategy "generation" this trade was opened under -- a snapshot of
    # the trade-governing config at open (stops, target, conviction bar, cost
    # profile, drift, horizon) plus a display label and the app version (see
    # config.strategy_signature). Lets the closed record be segmented so a new
    # strategy's forward performance is never pooled with an old, abandoned
    # one. None on a record written before generation stamping existed -- those
    # form a single "legacy" bucket instead of contaminating the current
    # numbers (see status.gather_strategy_generations).
    strategy: dict | None = None

    def _net_return_fraction(self, exit_price: float) -> float:
        """Net-of-cost return as a fraction of the entry notional -- the
        per-currency-unit result, so position_value * this is the currency
        P&L. Entry price of 0 (never expected) yields 0 rather than raising."""
        return self._net_pnl(exit_price) / self.entry_price if self.entry_price else 0.0

    def currency_result(self, exit_price: float) -> float:
        """Currency P&L of this trade at `exit_price`: the notional times the
        net-of-cost return. 0 when the trade carries no sized notional (a
        pre-account-model record)."""
        return self.position_value * self._net_return_fraction(exit_price)

    def unrealized_currency(self) -> float | None:
        """Marked-to-market currency P&L for an open trade, net of the
        round-trip cost -- None when unpriced or unsized."""
        if self.last_price is None or not self.position_value:
            return None
        return round(self.currency_result(self.last_price), 2)

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
        position_value: float = 0.0,
        strategy: dict | None = None,
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
            position_value=position_value,
            strategy=strategy,
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
        trade.last_marked_at = now.isoformat()
        day_high = high if high is not None else current_price
        day_low = low if low is not None else current_price

        # ENTRY SESSION: mark, but never resolve. high/low is the WHOLE
        # session's range, from the open -- it has no concept of "since
        # entry". On the entry day that range includes prints from before
        # the position existed, so a stock that had already swung through
        # the stop distance earlier that morning closed the trade the
        # instant it opened, on price action that predates it. Worse, the
        # engine's own poll does exactly this: _mark_and_execute opens the
        # trade in its first loop and then marks every open trade -- the
        # new one included -- a few lines later in the same call.
        #
        # The bias is not symmetric, which is what makes it damaging rather
        # than merely noisy: the stop is nearer than the target, so a wide
        # entry-day range hits the stop first far more often, and it fires
        # precisely on the volatile days where it does most harm. Deferring
        # to the next session's bar costs a genuine same-day stop-out one
        # day of delay -- unbiased noise -- and there is no intraday data
        # anywhere in this system that could tell the two cases apart.
        #
        # TWO conditions, because the UTC date alone is not the session.
        # An earlier version of this guard checked only the date, and the
        # date rolls at 00:00 UTC while the daily BAR does not roll until
        # the next US open at ~13:30 UTC. So for the thirteen and a half
        # hours in between, the guard saw a new day and resolved against a
        # bar that was still the entry session's -- reintroducing the exact
        # bug it was written to remove, every single night, for every
        # trade, and for the whole weekend on a Friday entry. Requiring the
        # regular session means the bar in hand is always one whose range
        # postdates the entry.
        opened_at = datetime.fromisoformat(trade.opened_at)
        if opened_at.date() == now.date() or not is_regular_trading_hours(now):
            self._write_open_state()
            return

        if trade.direction == "LONG":
            hit_stop = day_low <= trade.stop_price
            hit_target = day_high >= trade.target_price
            stop_fill = min(trade.stop_price, current_price)
        else:
            hit_stop = day_high >= trade.stop_price
            hit_target = day_low <= trade.target_price
            stop_fill = max(trade.stop_price, current_price)

        timed_out = (now - opened_at).days >= trade.horizon_days

        if hit_stop:
            self._close(trade, "LOSS", stop_fill, now)
        elif hit_target:
            self._close(trade, "WIN", trade.target_price, now)
        elif timed_out:
            self._close(trade, "TIMEOUT", current_price, now)
        else:
            self._write_open_state()

    def expire_past_horizon(self, now: datetime | None = None) -> list[PaperTrade]:
        """Closes any open trade that has passed its horizon, WITHOUT needing
        a fresh price -- it exits at the last price this journal ever saw.

        `update` is the only other thing that can close a trade, and it can
        only run when a price is available. A symbol no price source can
        price (delisted, halted, unqualifiable at IB, absent from Finnhub's
        free tier) therefore produced a trade that never stopped out, never
        took profit and never timed out: it sat open forever, its dossier
        pinned at SIGNALED so no fresh signal could ever replace it, and its
        unrealized P&L quietly excluded from every closed-trade statistic.
        An open position that cannot be marked is exactly the case a
        horizon exists for, so the horizon is enforced unconditionally.

        Returns the trades it closed so the caller can alert on them."""
        now = now or datetime.now(timezone.utc)
        expired: list[PaperTrade] = []
        for symbol in list(self.open_trades):
            trade = self.open_trades[symbol]
            opened_at = datetime.fromisoformat(trade.opened_at)
            if (now - opened_at).days < trade.horizon_days:
                continue
            # No mark ever landed: exit at entry, i.e. record a flat trade
            # rather than inventing a price. A flat row is honest about
            # having learned nothing; a fabricated one is not.
            exit_price = trade.last_price if trade.last_price is not None else trade.entry_price
            log.warning(
                "[PAPER] %s: closing at the horizon on a stale mark (last price %s) -- no price "
                "source could refresh it. The position could not be marked, so its stop and target "
                "were never evaluated after that mark.",
                symbol, "none" if trade.last_price is None else f"{trade.last_price:.2f}",
            )
            self._close(trade, "TIMEOUT", exit_price, now)
            expired.append(trade)
        return expired

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
        trade.currency_pnl = round(trade.currency_result(exit_price), 2) if trade.position_value else None
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
