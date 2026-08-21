"""Did the lagged course adjustment actually happen?

The strategy this system implements rests on one empirical claim: when
evidence about a thinly-covered name accumulates, the market reprices it
with a LAG of days to weeks, so a signal fired today still has most of the
move ahead of it. Every other statistic the repo produces measures whether
the trades made money. None of them measures the claim itself -- and the
two are not the same question. A record can be flat because the thesis was
wrong, or because the thesis was right and the repricing had already
happened by the time the signal fired. Only one of those is fixable, and
they look identical in a win-rate.

This module answers it directly, in event time, against real historical
bars (bars.py) rather than the engine's own captured marks:

    day -5 .. day -1   the run-up BEFORE the signal fired
    day 0              the signal session itself
    day +1 .. day +5   the near drift -- the window an entry actually holds
    day +6 .. day +20  the far drift

A strategy premised on lagged adjustment needs the mass of the move to sit
in the +1..+20 segments. If it sits in day 0, the market repriced the same
session and the entry is late by construction. If it sits BEFORE day 0, the
signal is following a move rather than leading it, and the evidence
pipeline is confirming what price already said.

Why real bars rather than `logs/price_marks.jsonl`: the marks log cannot be
backfilled, has holes wherever a price source was down, and carries no
intraday range (see bars.py). None of that is survivable for an event
study on two weeks of runtime -- the pre-event window predates capture
entirely, and the post-event window is exactly where the holes are. Real
bars are complete, extend backwards, and let the stop/target replay use
the intraday extremes the live path only sometimes had.

Pure offline math: no network, no engine dependency, unit-tested on
synthetic bars -- same design rules as forward_returns.py and
event_study.py, whose statistics helpers this reuses rather than
reimplements."""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from smartboi.bars import DailyBar
from smartboi.event_study import attach_outcomes, collapse_episodes
from smartboi.forward_returns import cluster_bootstrap_ci, effective_sample_count
from smartboi.market_hours import MARKET_CLOSE, MARKET_TZ
from smartboi.paper_journal import PaperTrade

# Event-time window. 5 days back is enough to see a run-up without
# swallowing the previous catalyst; 20 forward matches the longest horizon
# forward_returns.py reports and the shipped SIGNAL horizon_days default.
DEFAULT_PRE_DAYS = 5
DEFAULT_POST_DAYS = 20
# The segment boundary between "near" and "far" drift. 5 sessions is one
# calendar week -- long enough that a lagged reaction has had room to
# start, short enough that an entry is still holding.
NEAR_DRIFT_DAYS = 5

# How far a recorded entry price may sit outside the real session's
# [low, high] range before the row is flagged. Not zero: the engine's
# entry can come from a Finnhub quote whose high/low are the CONSOLIDATED
# session's while the bar provider reports the primary listing's, and the
# two disagree by a few basis points on thin names routinely. A price
# outside the range by more than this is a different problem -- a stale
# quote, a wrong symbol, or an unadjusted split -- and every one of those
# invalidates the trade record it produced.
ENTRY_TOLERANCE_PCT = 2.0

KINDS = ("opened", "drift_skip", "expired")
KIND_LABELS = {
    "opened": "opened (a real paper position)",
    "drift_skip": "drift-blocked (never opened)",
    "expired": "expired unopened",
}


@dataclass(frozen=True)
class WouldBeTrade:
    """One thing the strategy would have traded, whether or not it did.

    Deliberately wider than the paper-trade log: a signal the entry-timing
    guards blocked is still a would-be trade, and excluding those from the
    lag question would answer it only on the subset of episodes that
    survived a filter built on the same premise being tested."""

    symbol: str
    direction: str          # LONG | SHORT
    event_at: str           # full UTC timestamp of the fire/open
    kind: str               # see KINDS
    score: float            # confidence * magnitude at fire
    episode: str = ""
    entry_price: float | None = None    # what the engine recorded, opened only
    stop_price: float | None = None
    target_price: float | None = None
    horizon_days: int = 0
    cost_bps_round_trip: float = 0.0
    recorded_status: str = ""           # the engine's own outcome
    recorded_r_multiple: float | None = None


def _score(confidence: float, magnitude: float) -> float:
    return float(confidence or 0.0) * float(magnitude or 0.0)


def trades_from_paper_log(rows: list[dict]) -> list[WouldBeTrade]:
    """Closed paper trades (`logs/paper_trades.jsonl`)."""
    out = []
    for row in rows:
        if row.get("direction") not in ("LONG", "SHORT") or not row.get("symbol"):
            continue
        if not row.get("opened_at"):
            continue
        out.append(WouldBeTrade(
            symbol=row["symbol"],
            direction=row["direction"],
            event_at=row["opened_at"],
            kind="opened",
            score=_score(row.get("confidence", 0.0), row.get("magnitude", 0.0)),
            episode=row.get("episode") or "",
            entry_price=row.get("entry_price"),
            stop_price=row.get("stop_price"),
            target_price=row.get("target_price"),
            horizon_days=int(row.get("horizon_days") or 0),
            cost_bps_round_trip=float(row.get("cost_bps_round_trip") or 0.0),
            recorded_status=row.get("status") or "",
            recorded_r_multiple=row.get("r_multiple"),
        ))
    return out


def trades_from_open_state(state: dict) -> list[WouldBeTrade]:
    """Trades still open (`logs/open_paper_trades.json`). They carry no
    outcome yet, which is the whole reason to include them: on a two-week
    record the open book is most of the sample, and dropping it would
    restrict the study to trades that already resolved -- i.e. the fast
    ones, which is a selection effect pointing the wrong way for a
    question about LAG."""
    return trades_from_paper_log([row for row in state.values() if isinstance(row, dict)])


def trades_from_signal_episodes(
    signal_rows: list[dict], decision_rows: list[dict]
) -> list[WouldBeTrade]:
    """Signal episodes that never became a position -- drift-blocked or
    expired. Reuses event_study's episode collapse and outcome attachment
    so "one episode" means exactly what it means in the signal event
    study; counting each re-log separately would multiply a single thesis
    into dozens of rows (see collapse_episodes)."""
    episodes = attach_outcomes(collapse_episodes(signal_rows), decision_rows)
    out = []
    for ep in episodes:
        if ep.get("direction") not in ("LONG", "SHORT"):
            continue
        outcome = ep.get("outcome")
        if outcome == "trade_opened":
            continue  # already covered by the paper-trade log, with its real entry
        kind = {"drift_skip": "drift_skip", "signal_expired": "expired"}.get(outcome)
        if kind is None:
            continue  # untracked: pre-ledger or still awaiting a decision
        out.append(WouldBeTrade(
            symbol=ep["symbol"],
            direction=ep["direction"],
            event_at=ep["fired_at"],
            kind=kind,
            score=_score(ep.get("confidence", 0.0), ep.get("magnitude", 0.0)),
            episode=ep.get("episode") or "",
        ))
    return out


def dedup_trades(trades: list[WouldBeTrade]) -> list[WouldBeTrade]:
    """One row per (symbol, episode, kind). The open-state snapshot and the
    closed log overlap for the length of the close-crash window
    (PaperTradeJournal._load_open_state self-heals the live path, but a
    snapshot copied off the box mid-close can still contain both), and a
    trade counted twice is two votes for one thesis in every mean below.
    Keeps the row with the most information -- a closed record over an
    open one."""
    best: dict[tuple[str, str, str], WouldBeTrade] = {}
    for trade in trades:
        key = (trade.symbol, trade.episode or trade.event_at, trade.kind)
        current = best.get(key)
        if current is None or (not current.recorded_status or current.recorded_status == "OPEN"):
            best[key] = trade
    return sorted(best.values(), key=lambda t: (t.event_at, t.symbol))


# --- loading the runtime logs -----------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    """Tolerant of a partial final line: these logs are appended to by a
    long-running process, so a copy taken mid-write ends in half a row."""
    if not path.exists():
        return []
    rows = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def marks_as_series(rows: list[dict], min_marks: int = 2) -> dict[str, "Series"]:
    """The engine's own `price_marks.jsonl` as a close-only bar series --
    the zero-network fallback when no bar provider is reachable.

    These are real market prices (IB, else Finnhub /quote), captured live
    once a day, so the event-time curve and the lag decomposition are
    computable from them exactly as from fetched bars. Three things they
    cannot do, and callers must not pretend otherwise:

    - **No intraday range.** high and low are set to the close, so
      replay_exit sees only a close THROUGH a level, never a level that
      traded and recovered. Since the stop sits nearer than the target,
      that under-counts losses specifically -- do not read a replay off
      these as a win rate.
    - **No history before capture started**, so the pre-signal window is
      short or absent for the earliest signals, and the trades that HAVE
      one are a selected subset.
    - **Holes** wherever no price source answered that day.

    What they are good for, and what fetched bars are not available for
    here at all: reconcile_entry. A recorded entry that disagrees with
    the same session's captured price is a broken entry, and comparing
    the two catches it whether or not an intraday range exists.

    Weekend marks are dropped. The daily pass is gated on is_trading_day,
    but rows captured before that gate existed put a Saturday key in the
    file, holding Friday's close. Left in, each becomes an extra
    "session" and shifts every event-time offset measured through it by
    one."""
    by_symbol: dict[str, dict[str, float]] = {}
    for row in rows:
        symbol = row.get("symbol")
        price = row.get("price")
        marked_at = row.get("marked_at") or ""
        if not symbol or not price or not marked_at:
            continue
        day = marked_at[:10]
        try:
            if date.fromisoformat(day).weekday() >= 5:
                continue
        except ValueError:
            continue
        by_symbol.setdefault(symbol, {})[day] = float(price)
    return {
        symbol: Series([
            DailyBar(date=d, open=p, high=p, low=p, close=p)
            for d, p in sorted(marks.items())
        ])
        for symbol, marks in by_symbol.items()
        if len(marks) >= min_marks
    }


def load_would_be_trades(log_dir: Path, include_unopened: bool = True) -> list[WouldBeTrade]:
    """Every would-be trade the runtime logs know about, deduped.

    Reads all four logs rather than just the paper-trade one, because the
    question is about what the STRATEGY would have traded, and the engine
    declines to open a signal for reasons (drift guards, entry deadlines,
    an already-open position in the name) that are themselves premised on
    the lag being real. Answering the lag question only on the episodes
    those guards let through would be circular."""
    trades = trades_from_paper_log(read_jsonl(log_dir / "paper_trades.jsonl"))
    open_state_path = log_dir / "open_paper_trades.json"
    if open_state_path.exists():
        try:
            state = json.loads(open_state_path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        if isinstance(state, dict):
            trades += trades_from_open_state(state)
    if include_unopened:
        trades += trades_from_signal_episodes(
            read_jsonl(log_dir / "signals.jsonl"),
            read_jsonl(log_dir / "decisions.jsonl"),
        )
    return dedup_trades(trades)


# --- event-time alignment ---------------------------------------------

def actionable_session_date(timestamp: str) -> str:
    """The first session that could have ACTED on an event at `timestamp`.

    Not simply the UTC date. Two corrections, both of which silently
    manufacture returns if skipped:

    - UTC vs exchange-local. The UTC date rolls at 19:00/20:00 ET, so an
      evening signal already carries tomorrow's UTC date while the session
      it could trade in is still tomorrow's -- market_hours.py documents
      the same trap on the live path.
    - After the close. A signal fired at 21:00 ET cannot be acted on in
      that session; its bar is already printed. Treating that session as
      day 0 would score the whole day's move -- which happened BEFORE the
      signal existed -- as post-signal drift. That is look-ahead bias of
      exactly the kind this report exists to detect, so it must not be
      built into the report itself.
    """
    stamp = datetime.fromisoformat(timestamp)
    local = stamp.astimezone(MARKET_TZ)
    session = local.date()
    if local.time() >= MARKET_CLOSE:
        session += timedelta(days=1)
    return session.isoformat()


class Series:
    """A symbol's bars indexed for two lookups the event study needs: by
    position (event time) and by date (benchmark alignment)."""

    def __init__(self, bars: list[DailyBar]):
        self.bars = sorted(bars, key=lambda b: b.date)
        self.dates = [b.date for b in self.bars]

    def __len__(self) -> int:
        return len(self.bars)

    def index_on_or_after(self, session_date: str) -> int | None:
        i = bisect.bisect_left(self.dates, session_date)
        return i if i < len(self.dates) else None

    def close_on_or_before(self, session_date: str) -> float | None:
        """The last close at or before `session_date` -- used to align a
        benchmark to the subject's bar dates. On-or-BEFORE (not after) so a
        benchmark that did not trade on a day the subject did contributes
        its stale close rather than reaching forward into returns that
        have not happened yet at the subject's timestamp."""
        i = bisect.bisect_right(self.dates, session_date) - 1
        return self.bars[i].close if i >= 0 else None

    def at(self, i: int) -> DailyBar | None:
        return self.bars[i] if 0 <= i < len(self.bars) else None


def event_window(
    series: Series, session_date: str, pre_days: int, post_days: int
) -> dict[int, DailyBar]:
    """offset -> bar, with offset 0 the first session on/after
    `session_date`. Offsets are POSITIONS in the symbol's own bar series,
    so weekends, holidays and halts are handled by construction -- no
    calendar arithmetic, and none of the up-to-five-day forward reaching
    the price-marks join needs (see forward_returns._price_on_or_after,
    which stretches the measured window to paper over a missing mark)."""
    anchor = series.index_on_or_after(session_date)
    if anchor is None:
        return {}
    window: dict[int, DailyBar] = {}
    for offset in range(-pre_days, post_days + 1):
        bar = series.at(anchor + offset)
        if bar is not None:
            window[offset] = bar
    return window


def _signed(pct: float, direction: str) -> float:
    """Signed in the THESIS direction -- LONG: up is a win, SHORT: down is
    a win -- so a positive number always means "the thesis was right so
    far", exactly as in forward_returns.compute_forward_return."""
    return pct if direction == "LONG" else -pct


def _pct(from_price: float, to_price: float) -> float | None:
    if not from_price:
        return None
    return (to_price - from_price) / from_price * 100.0


def benchmark_move(
    benchmarks: list[Series], from_date: str, to_date: str
) -> float | None:
    """Mean RAW (unsigned) return of the benchmark members between two
    session dates. None when no member can price both ends -- reported as
    unbenchmarkable rather than given a fabricated 0.00, which would read
    as "no sector move" instead of "no data"."""
    moves = []
    for series in benchmarks:
        start = series.close_on_or_before(from_date)
        end = series.close_on_or_before(to_date)
        if start is None or end is None:
            continue
        move = _pct(start, end)
        if move is not None:
            moves.append(move)
    if not moves:
        return None
    return sum(moves) / len(moves)


def event_path(
    trade: WouldBeTrade,
    series: Series,
    benchmarks: list[Series] | None = None,
    pre_days: int = DEFAULT_PRE_DAYS,
    post_days: int = DEFAULT_POST_DAYS,
) -> dict | None:
    """One would-be trade -> its price path in event time.

    Three curves, because they answer three different questions and get
    confused for each other constantly:

    - `raw`: signed % from the day -1 CLOSE. The event-study baseline --
      "what did this name do around the signal", independent of whether or
      how we entered.
    - `abnormal`: `raw` minus the benchmark's move over the same dates,
      signed the same way. Strips the sector: a defense supplier that rose
      6% in a week when its whole ecosystem rose 6% adjusted to nothing.
    - `as_traded`: signed % from the price the engine actually recorded as
      the entry. Only exists for opened trades, and is the only curve that
      is a P&L. It differs from `raw` by the intraday slippage between the
      previous close and the fill, which on these names is not small.

    None when the symbol has no bar on or after the event's actionable
    session (too recent to have printed, or not carried by the provider).
    """
    session_date = actionable_session_date(trade.event_at)
    window = event_window(series, session_date, pre_days, post_days)
    if 0 not in window:
        return None
    base = window.get(-1)
    day0 = window[0]
    raw: dict[int, float] = {}
    abnormal: dict[int, float] = {}
    as_traded: dict[int, float] = {}
    if base is not None:
        for offset, bar in window.items():
            move = _pct(base.close, bar.close)
            if move is None:
                continue
            raw[offset] = _signed(move, trade.direction)
            if benchmarks:
                bench = benchmark_move(benchmarks, base.date, bar.date)
                if bench is not None:
                    abnormal[offset] = _signed(move - bench, trade.direction)
    if trade.entry_price:
        for offset, bar in window.items():
            if offset < 0:
                continue  # a pre-entry "P&L" is not a thing
            move = _pct(trade.entry_price, bar.close)
            if move is not None:
                as_traded[offset] = _signed(move, trade.direction)
    return {
        "symbol": trade.symbol,
        "direction": trade.direction,
        "kind": trade.kind,
        "score": trade.score,
        "episode": trade.episode,
        "event_at": trade.event_at,
        # Named entry_date because effective_sample_count() keys on it --
        # this is the same "one independent thesis-window" notion.
        "entry_date": day0.date,
        "session_date": session_date,
        "raw": raw,
        "abnormal": abnormal,
        "as_traded": as_traded,
        "max_offset": max(window) if window else 0,
        "has_base": base is not None,
    }


# --- aggregation -------------------------------------------------------

def mean_path(paths: list[dict], key: str = "abnormal") -> list[dict]:
    """Mean signed return at each event-time offset, with the sample size
    at THAT offset -- which shrinks as the offset grows, because a trade
    fired three days ago has no day +10 yet. Reporting one headline `n`
    over a curve whose right-hand end rests on two observations is how an
    event study lies without a single wrong number in it, so every point
    carries its own count and its own symbol-clustered CI."""
    by_offset: dict[int, dict[str, list[float]]] = {}
    for path in paths:
        for offset, value in path.get(key, {}).items():
            by_offset.setdefault(offset, {}).setdefault(path["symbol"], []).append(value)
    out = []
    for offset in sorted(by_offset):
        by_symbol = by_offset[offset]
        values = [v for vals in by_symbol.values() for v in vals]
        # Symbol-equal-weighted, matching forward_returns.bucket_returns:
        # a name that signalled four times in a fortnight would otherwise
        # cast four votes for one thesis.
        symbol_means = [sum(v) / len(v) for v in by_symbol.values()]
        out.append({
            "offset": offset,
            "n": len(values),
            "n_symbols": len(by_symbol),
            "mean_pct": sum(values) / len(values),
            "mean_pct_symbol_weighted": sum(symbol_means) / len(symbol_means),
            "ci_90": cluster_bootstrap_ci(by_symbol),
        })
    return out


def _segment(path: dict, key: str, from_offset: int, to_offset: int) -> float | None:
    """The move between two event-time offsets, as a difference of two
    points on a curve that is already signed and already benchmark-
    adjusted. Both endpoints must exist -- a segment computed from a
    missing endpoint would silently become a shorter segment."""
    curve = path.get(key, {})
    start = curve.get(from_offset)
    end = curve.get(to_offset)
    if start is None or end is None:
        return None
    return end - start


def adjustment_split(
    paths: list[dict],
    key: str = "abnormal",
    pre_days: int = DEFAULT_PRE_DAYS,
    near_days: int = NEAR_DRIFT_DAYS,
    far_days: int = DEFAULT_POST_DAYS,
) -> dict:
    """THE headline: where in event time the move actually happened.

    Four segments per trade, each signed in the thesis direction and (with
    key="abnormal") net of its benchmark:

      before   close(-pre_days) -> close(-1)   already moving when we fired
      day0     close(-1)        -> close(0)    repriced the same session
      near     close(0)         -> close(+near) the drift an entry captures
      far      close(+near)     -> close(+far)  the drift a horizon captures

    Plus `capture_ratio`: near / (day0 + near) -- of everything that moved
    from the moment the evidence was in hand, the fraction still available
    after the signal session closed. That number IS the lagged-adjustment
    premise, expressed as a fraction. Near 1.0, the market had not
    reacted yet and the entry was early enough to matter. Near 0, the
    repricing happened the same session and the strategy is buying an
    adjustment that has already occurred.

    The ratio is taken over the MEANS, not averaged per trade: a per-trade
    ratio has a denominator that crosses zero, so its mean is dominated by
    whichever trade happened to have the smallest total move. Where the
    denominator is not clearly positive the ratio is None -- there was no
    adjustment to divide up, and a ratio of noise is not a finding."""
    segments = {
        "before": (-pre_days, -1),
        "day0": (-1, 0),
        "near": (0, near_days),
        "far": (near_days, far_days),
    }
    out: dict[str, dict] = {}
    for name, (start, end) in segments.items():
        by_symbol: dict[str, list[float]] = {}
        rows: list[dict] = []
        for path in paths:
            value = _segment(path, key, start, end)
            if value is None:
                continue
            by_symbol.setdefault(path["symbol"], []).append(value)
            rows.append({"symbol": path["symbol"], "entry_date": path["entry_date"], "value": value})
        if not rows:
            out[name] = {"n": 0, "n_symbols": 0, "from_offset": start, "to_offset": end}
            continue
        values = [r["value"] for r in rows]
        symbol_means = [sum(v) / len(v) for v in by_symbol.values()]
        out[name] = {
            "n": len(values),
            "n_symbols": len(by_symbol),
            "from_offset": start,
            "to_offset": end,
            "mean_pct": sum(values) / len(values),
            "mean_pct_symbol_weighted": sum(symbol_means) / len(symbol_means),
            "hit_rate": sum(1 for v in values if v > 0) / len(values),
            "n_effective": effective_sample_count(rows, max(1, end - start)),
            "ci_90": cluster_bootstrap_ci(by_symbol),
        }
    day0 = out.get("day0", {}).get("mean_pct_symbol_weighted")
    near = out.get("near", {}).get("mean_pct_symbol_weighted")
    ratio = None
    if day0 is not None and near is not None:
        total = day0 + near
        # A denominator this small is noise, and dividing by it produces a
        # number with the authority of a statistic and the content of a
        # coin flip.
        if total > 0.5:
            ratio = near / total
    out["capture_ratio"] = ratio
    return out


# --- what the trade would actually have done --------------------------

def replay_exit(trade: WouldBeTrade, series: Series) -> dict | None:
    """Re-runs the trade's own stop/target/horizon rules against real
    bars, using paper_journal.update's exact conventions:

      - the ENTRY session never resolves (its range includes prints from
        before the position existed);
      - stop and target are checked against the intraday EXTREMES, not the
        close;
      - when both traded in one bar the STOP wins (no intraday sequencing
        exists, so the honest assumption is the loss);
      - a stop fills at the stop or the close, whichever is worse; a
        target fills exactly at the target;
      - the horizon is CALENDAR days from the open, matching
        `(now - opened_at).days >= horizon_days`.

    The point is not to second-guess the journal: it is that the live path
    could only apply those rules on days a price source answered. A symbol
    the feed dropped for four sessions had four bars' worth of stop and
    target touches that were never evaluated. This replays every session.

    Returns None for rows with no stop/target (an unopened signal has no
    levels to replay)."""
    if not (trade.entry_price and trade.stop_price and trade.target_price):
        return None
    session_date = actionable_session_date(trade.event_at)
    anchor = series.index_on_or_after(session_date)
    if anchor is None:
        return None
    opened_at = datetime.fromisoformat(trade.event_at)
    # A real PaperTrade, so the R multiple below comes out of the SAME
    # cost model the live journal books trades with rather than a second
    # copy of the formula here. Two copies is how a replay quietly stops
    # being comparable to the record it is replaying, the first time the
    # cost assumption is revised.
    model = PaperTrade(
        symbol=trade.symbol,
        direction=trade.direction,
        entry_price=trade.entry_price,
        stop_price=trade.stop_price,
        target_price=trade.target_price,
        opened_at=trade.event_at,
        horizon_days=trade.horizon_days,
        thesis_summary="",
        confidence=0.0,
        independent_source_count=0,
        cost_bps_round_trip=trade.cost_bps_round_trip,
    )
    risk = abs(trade.entry_price - trade.stop_price)
    sessions = 0
    # Start at anchor + 1: the entry session is marked, never resolved.
    for i in range(anchor + 1, len(series)):
        bar = series.at(i)
        if bar is None:
            break
        sessions += 1
        if trade.direction == "LONG":
            hit_stop = bar.low <= trade.stop_price
            hit_target = bar.high >= trade.target_price
            stop_fill = min(trade.stop_price, bar.close)
        else:
            hit_stop = bar.high >= trade.stop_price
            hit_target = bar.low <= trade.target_price
            stop_fill = max(trade.stop_price, bar.close)
        held_days = (date.fromisoformat(bar.date) - opened_at.date()).days
        timed_out = trade.horizon_days > 0 and held_days >= trade.horizon_days
        if hit_stop:
            outcome, exit_price = "LOSS", stop_fill
        elif hit_target:
            outcome, exit_price = "WIN", trade.target_price
        elif timed_out:
            outcome, exit_price = "TIMEOUT", bar.close
        else:
            continue
        return {
            "symbol": trade.symbol,
            "event_at": trade.event_at,
            "direction": trade.direction,
            "outcome": outcome,
            "exit_date": bar.date,
            "exit_price": exit_price,
            "sessions_held": sessions,
            "calendar_days_held": held_days,
            "r_multiple": round(model._net_pnl(exit_price) / risk, 3) if risk > 0 else 0.0,
            "recorded_status": trade.recorded_status,
            "recorded_r_multiple": trade.recorded_r_multiple,
            "both_levels_same_bar": hit_stop and hit_target,
        }
    last = series.at(len(series) - 1)
    return {
        "symbol": trade.symbol,
        "event_at": trade.event_at,
        "direction": trade.direction,
        "outcome": "OPEN",
        "exit_date": last.date if last else "",
        "exit_price": last.close if last else None,
        "sessions_held": sessions,
        "calendar_days_held": (date.fromisoformat(last.date) - opened_at.date()).days if last else 0,
        "r_multiple": round(model._net_pnl(last.close) / risk, 3) if last and risk > 0 else None,
        "recorded_status": trade.recorded_status,
        "recorded_r_multiple": trade.recorded_r_multiple,
        "both_levels_same_bar": False,
    }


def reconcile_entry(
    trade: WouldBeTrade, series: Series, tolerance_pct: float = ENTRY_TOLERANCE_PCT
) -> dict | None:
    """Is the price the engine recorded as an entry a price that actually
    traded that session?

    This runs before any conclusion is drawn from the record, because it
    can invalidate the record. A recorded entry outside the real session's
    range means one of: the quote was stale (the feed handed back a
    previous close during a halt), the bar provider is reporting a
    different security, or a split sits between then and now and the two
    price scales are not comparable. Every one of those makes the R
    multiples computed from that entry fiction -- and none of them is
    visible in any statistic the system currently prints.

    None for rows with no recorded entry (unopened signals)."""
    if not trade.entry_price:
        return None
    session_date = actionable_session_date(trade.event_at)
    anchor = series.index_on_or_after(session_date)
    if anchor is None:
        return None
    bar = series.at(anchor)
    if bar is None or not bar.close:
        return None
    if bar.low <= trade.entry_price <= bar.high:
        gap_pct = 0.0
    else:
        nearest = bar.low if trade.entry_price < bar.low else bar.high
        gap_pct = abs((trade.entry_price - nearest) / nearest * 100.0) if nearest else 0.0
    return {
        "symbol": trade.symbol,
        "event_at": trade.event_at,
        "session_date": bar.date,
        "recorded_entry": trade.entry_price,
        "bar_low": bar.low,
        "bar_high": bar.high,
        "bar_close": bar.close,
        "gap_pct": gap_pct,
        "outside_range": gap_pct > tolerance_pct,
    }


# --- orchestration -----------------------------------------------------

def benchmark_series_for(
    symbol: str,
    mode: str,
    ecosystem_by_symbol: dict[str, str],
    series_by_symbol: dict[str, Series],
    market_symbol: str = "IWM",
) -> tuple[list[Series], str]:
    """The benchmark for one symbol, and a label for it.

    The subject is always excluded from its own benchmark -- including a
    stock in the average it is measured against shrinks every alpha toward
    zero by ~1/N, and to exactly zero when it is the only priced member of
    its ecosystem (forward_returns.ecosystem_benchmark_return documents the
    live case where that happened, on IESC). When exclusion leaves nothing,
    the market proxy stands in rather than the row being silently dropped:
    these are all small caps, and an unadjusted return here is mostly
    Russell beta rather than anything the strategy did."""
    if mode == "none":
        return [], "none (raw returns)"
    market_symbol = market_symbol.upper()
    market = series_by_symbol.get(market_symbol)
    if mode == "market":
        return ([market], market_symbol) if market else ([], f"{market_symbol} (unavailable)")
    ecosystem = ecosystem_by_symbol.get(symbol)
    # No tag for the subject means no peer group. Grouping it with every
    # OTHER untagged symbol would build a "sector" out of whatever the
    # universe file happens not to classify -- a benchmark with a label
    # and no meaning, which is worse than reporting the row as raw. Seen
    # live: SCRNY and SCE-PN benchmarked against each other.
    if not ecosystem:
        return ([market], f"{market_symbol} (no ecosystem tag for {symbol})") if market else ([], "none available")
    peers = [
        series for peer, series in series_by_symbol.items()
        if peer != symbol and peer != market_symbol
        and ecosystem_by_symbol.get(peer)
        and ecosystem_by_symbol.get(peer) == ecosystem
    ]
    if peers:
        return peers, f"{ecosystem} peers"
    return ([market], f"{market_symbol} (no ecosystem peer priced)") if market else ([], "none available")


def analyse(
    trades: list[WouldBeTrade],
    series_by_symbol: dict[str, Series],
    ecosystem_by_symbol: dict[str, str],
    benchmark_mode: str = "ecosystem",
    market_symbol: str = "IWM",
    pre_days: int = DEFAULT_PRE_DAYS,
    far_days: int = DEFAULT_POST_DAYS,
    entry_tolerance_pct: float = ENTRY_TOLERANCE_PCT,
) -> dict:
    """Joins would-be trades to bars: event paths, exit replays, entry
    reconciliations. Pure -- the caller does the fetching and hands in the
    series, so this stays unit-testable and the same join serves the CLI
    and the dashboard button without either one reimplementing it."""
    paths, replays, reconciliations, unpriced = [], [], [], []
    labels: set[str] = set()
    for trade in trades:
        series = series_by_symbol.get(trade.symbol)
        if series is None or not len(series):
            unpriced.append(trade.symbol)
            continue
        benchmarks, label = benchmark_series_for(
            trade.symbol, benchmark_mode, ecosystem_by_symbol, series_by_symbol, market_symbol
        )
        labels.add(label)
        path = event_path(trade, series, benchmarks, pre_days=pre_days, post_days=far_days)
        if path is not None:
            paths.append(path)
        replay = replay_exit(trade, series)
        if replay is not None:
            replays.append(replay)
        reconciliation = reconcile_entry(trade, series, tolerance_pct=entry_tolerance_pct)
        if reconciliation is not None:
            reconciliations.append(reconciliation)
    # Abnormal returns need a benchmark to be abnormal. With none
    # available the report falls back to raw and SAYS so, rather than
    # printing an empty "abnormal" table that reads as "no effect".
    key = "abnormal" if benchmark_mode != "none" and any(p["abnormal"] for p in paths) else "raw"
    return {
        "paths": paths,
        "replays": replays,
        "reconciliations": reconciliations,
        "unpriced": unpriced,
        "key": key,
        "benchmark_label": ", ".join(sorted(labels)) or "none",
        "entry_tolerance_pct": entry_tolerance_pct,
    }


# --- reporting ---------------------------------------------------------

def _fmt_ci(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a"


def _sparkline(value: float, scale: float, width: int = 20) -> str:
    """A fixed-width bar around a centre line, so the SHAPE of the event
    curve -- which is the entire finding -- is readable at a glance rather
    than reconstructed from a column of numbers."""
    if scale <= 0:
        return " " * (2 * width + 1)
    n = max(-width, min(width, int(round(value / scale * width))))
    if n >= 0:
        return " " * width + "|" + "#" * n + " " * (width - n)
    return " " * (width + n) + "#" * (-n) + "|" + " " * width


def format_curve(points: list[dict], label: str) -> list[str]:
    """The event-time curve: mean cumulative move at each offset, from the
    last close BEFORE the signal session."""
    lines = [f"-- {label} (cumulative from the close before day 0, signed in thesis direction) --",
             f"{'Day':>5}  {'Mean %':>8}  {'n':>4}  {'syms':>4}  {'90% CI':>18}"]
    if not points:
        lines.append("  (no points -- no trade has both a pre-event close and a day-0 bar yet)")
        return lines
    scale = max((abs(p["mean_pct_symbol_weighted"]) for p in points), default=1.0) or 1.0
    for p in points:
        marker = " <- signal session" if p["offset"] == 0 else ""
        lines.append(
            f"{p['offset']:>+5}  {p['mean_pct_symbol_weighted']:>+8.2f}  {p['n']:>4}  "
            f"{p['n_symbols']:>4}  {_fmt_ci(p['ci_90']):>18}  {_sparkline(p['mean_pct_symbol_weighted'], scale, 12)}{marker}"
        )
    return lines


def format_split(split: dict, key: str) -> list[str]:
    """The headline table: where in event time the move happened."""
    order = [
        ("before", "Before the signal (run-up we followed)"),
        ("day0", "Signal session itself (same-day repricing)"),
        ("near", "Days +1..+N (the drift an entry captures)"),
        ("far", "Days +N..+M (the drift a horizon captures)"),
    ]
    lines = [f"-- Where the move happened ({key}, signed in thesis direction) --",
             f"{'Segment':<42} {'Days':>9}  {'Mean %':>8}  {'Hit%':>6}  {'n':>4}  {'N_eff':>5}  {'90% CI':>18}"]
    for name, label in order:
        seg = split.get(name) or {}
        if not seg.get("n"):
            lines.append(f"{label:<42} {'-':>9}  {'no data':>8}")
            continue
        span = f"{seg['from_offset']:+d}..{seg['to_offset']:+d}"
        lines.append(
            f"{label:<42} {span:>9}  {seg['mean_pct_symbol_weighted']:>+8.2f}  "
            f"{seg['hit_rate'] * 100:>5.0f}%  {seg['n']:>4}  {seg['n_effective']:>5}  {_fmt_ci(seg['ci_90']):>18}"
        )
    return lines


def interpret_split(split: dict) -> list[str]:
    """The plain-English answer to "was there a lagged course adjustment",
    stated as what the numbers show and what they do NOT support. Written
    to be quotable on its own, because this line is the one that will be
    read out of context."""
    day0 = (split.get("day0") or {}).get("mean_pct_symbol_weighted")
    near = (split.get("near") or {}).get("mean_pct_symbol_weighted")
    before = (split.get("before") or {}).get("mean_pct_symbol_weighted")
    ratio = split.get("capture_ratio")
    lines = []
    if day0 is None or near is None:
        lines.append("VERDICT: not computable yet -- no trade has both a pre-signal close and a "
                     "full post-signal segment. Re-run once the window has elapsed.")
        return lines
    total = day0 + near
    if ratio is None:
        lines.append(
            f"VERDICT: there is no adjustment to divide up. The mean move from the last close "
            f"before the signal through day +{(split.get('near') or {}).get('to_offset', NEAR_DRIFT_DAYS)} "
            f"is {total:+.2f}% -- too small (or the wrong way) for a capture ratio to mean anything. "
            f"That is a finding about the SIGNAL, not about the lag: nothing moved to be early or late for."
        )
    else:
        lines.append(
            f"VERDICT: of the {total:+.2f}% total move from the last close before the signal through "
            f"day +{(split.get('near') or {}).get('to_offset', NEAR_DRIFT_DAYS)}, "
            f"{ratio * 100:.0f}% arrived AFTER the signal session closed (capture ratio {ratio:.2f})."
        )
        if ratio >= 0.6:
            lines.append("  -> Consistent with the lagged-adjustment premise: most of the repricing was "
                         "still available when the signal fired.")
        elif ratio >= 0.3:
            lines.append("  -> Partially consistent: a real share of the move followed the signal, but a "
                         "large part of it happened the same session. Entry timing is costing some of the edge.")
        else:
            lines.append("  -> NOT consistent with the lagged-adjustment premise: the market repriced in the "
                         "signal session and the entry is late by construction. A faster entry, not a better "
                         "score, is what that implies.")
    if before is not None and before > 1.0:
        lines.append(
            f"  -> Note the {before:+.2f}% run-up in the {abs((split.get('before') or {}).get('from_offset', DEFAULT_PRE_DAYS))} "
            f"sessions BEFORE the signal: on average these names were already moving in the thesis direction when "
            f"the evidence crossed threshold. Some of what looks like prediction is the pipeline confirming price."
        )
    return lines


def format_replay(replays: list[dict], reconciliations: list[dict] | None = None) -> list[str]:
    """Real-bar outcomes vs the outcomes the live journal recorded. A
    disagreement is not a bug in either -- it is the live path having had
    no price on a day that resolved the trade.

    Trades whose recorded entry failed reconciliation are EXCLUDED from
    every aggregate here and listed on their own. Their stop and target
    were derived from a price that no session traded at, so replaying
    those levels against real bars produces a confident-looking R with no
    referent -- and pooled into a mean it is indistinguishable from a real
    result. A number that cannot be right is worse than a missing one."""
    lines = ["-- Stop/target replay against real intraday ranges --"]
    if not replays:
        lines.append("  (no opened trades with stop/target levels to replay)")
        return lines
    unreconciled = {
        (r["symbol"], r["event_at"]) for r in (reconciliations or []) if r["outside_range"]
    }
    excluded = [r for r in replays if (r.get("symbol"), r.get("event_at")) in unreconciled]
    replays = [r for r in replays if (r.get("symbol"), r.get("event_at")) not in unreconciled]
    if excluded:
        lines.append(
            f"  {len(excluded)} trade(s) excluded -- their recorded entry is outside the real session "
            f"range (see above), so their stop/target levels have no referent: "
            + ", ".join(sorted({r["symbol"] for r in excluded}))
        )
    if not replays:
        lines.append("  (nothing left to replay once those are excluded)")
        return lines
    counts: dict[str, int] = {}
    for r in replays:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    lines.append("  Replayed outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    resolved = [r for r in replays if r["r_multiple"] is not None]
    if resolved:
        mean_r = sum(r["r_multiple"] for r in resolved) / len(resolved)
        lines.append(f"  Mean R (net of the trade's own recorded cost assumption): {mean_r:+.2f} over {len(resolved)} trade(s)")
    disagreements = [
        r for r in replays
        if r["recorded_status"] and r["recorded_status"] != "OPEN" and r["recorded_status"] != r["outcome"]
    ]
    if disagreements:
        lines.append(f"  {len(disagreements)} trade(s) resolved DIFFERENTLY against real bars than the live journal recorded:")
        for r in disagreements[:10]:
            lines.append(
                f"    {r['symbol']:<6} live={r['recorded_status']:<8} real={r['outcome']:<8} "
                f"exit {r['exit_date']} @ {r['exit_price']:.2f} (R {r['r_multiple']:+.2f} vs recorded "
                f"{r['recorded_r_multiple'] if r['recorded_r_multiple'] is not None else 'n/a'})"
            )
        lines.append("    (the live journal can only apply stop/target on days a price source answered; "
                     "the replay sees every session)")
    ambiguous = [r for r in replays if r.get("both_levels_same_bar")]
    if ambiguous:
        lines.append(f"  {len(ambiguous)} trade(s) touched BOTH stop and target in one session -- scored as "
                     f"the stop, as the live journal does (no intraday sequencing exists).")
    return lines


def format_reconciliation(rows: list[dict], tolerance_pct: float = ENTRY_TOLERANCE_PCT) -> list[str]:
    """Entry prices that no real session could have filled. Printed FIRST
    in the report, because a failure here invalidates the numbers below
    it rather than merely qualifying them."""
    lines = ["-- Recorded entry prices vs the real session range --"]
    if not rows:
        lines.append("  (no opened trades with a recorded entry to check)")
        return lines
    bad = [r for r in rows if r["outside_range"]]
    lines.append(f"  {len(rows) - len(bad)} of {len(rows)} recorded entries fall inside the real session's "
                 f"[low, high] (tolerance {tolerance_pct:.1f}%).")
    for r in bad:
        lines.append(
            f"  !! {r['symbol']:<6} {r['session_date']} recorded {r['recorded_entry']:.2f}, real range "
            f"[{r['bar_low']:.2f}, {r['bar_high']:.2f}] -- {r['gap_pct']:.1f}% outside"
        )
    if bad:
        lines.append("  A recorded entry outside the real range means a stale quote, a symbol mismatch, or a "
                     "split between then and now. Every R multiple derived from it is fiction until resolved.")
    return lines


def format_report(
    trades: list[WouldBeTrade],
    paths: list[dict],
    replays: list[dict],
    reconciliations: list[dict],
    key: str = "abnormal",
    pre_days: int = DEFAULT_PRE_DAYS,
    near_days: int = NEAR_DRIFT_DAYS,
    far_days: int = DEFAULT_POST_DAYS,
    benchmark_label: str = "ecosystem peers",
    unpriced: list[str] | None = None,
    entry_tolerance_pct: float = ENTRY_TOLERANCE_PCT,
) -> str:
    """The whole report as plain text."""
    lines = ["=== Would-be trades vs real market data: was the adjustment lagged? ==="]
    if not trades:
        lines.append("No would-be trades found in the logs -- nothing to check.")
        return "\n".join(lines)
    kinds: dict[str, int] = {}
    for t in trades:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
    dates = sorted(t.event_at[:10] for t in trades)
    lines.append(
        f"{len(trades)} would-be trade(s) between {dates[0]} and {dates[-1]}: "
        + ", ".join(f"{KIND_LABELS.get(k, k)} {v}" for k, v in sorted(kinds.items()))
    )
    lines.append(f"{len(paths)} of them have real bars covering the event window; "
                 f"benchmark: {benchmark_label}; returns are {key}.")
    if unpriced:
        lines.append(f"Unpriced (no bars from the provider): {', '.join(sorted(set(unpriced)))}")
    # Window completeness. On a young record most trades simply do not have
    # a day +20 yet, and a curve whose tail is two observations must say so
    # before it is read, not in a footnote after it.
    for horizon in (near_days, far_days):
        complete = sum(1 for p in paths if p["max_offset"] >= horizon)
        lines.append(f"  day +{horizon} has elapsed for {complete} of {len(paths)} trade(s).")
    lines.append("")

    lines.extend(format_reconciliation(reconciliations, entry_tolerance_pct))
    lines.append("")

    split = adjustment_split(paths, key=key, pre_days=pre_days, near_days=near_days, far_days=far_days)
    lines.extend(format_split(split, key))
    lines.append("")
    lines.extend(interpret_split(split))
    lines.append("")

    lines.extend(format_curve(mean_path(paths, key=key), f"Event-time curve ({key})"))
    lines.append("")

    # Same question, restricted to the trades the engine actually opened.
    # If the two disagree, the entry-timing guards are selecting on
    # something -- which is exactly what the signal event study exists to
    # measure, and a reason to run it alongside this.
    opened = [p for p in paths if p["kind"] == "opened"]
    if opened and len(opened) != len(paths):
        opened_split = adjustment_split(opened, key=key, pre_days=pre_days, near_days=near_days, far_days=far_days)
        lines.append("-- Restricted to trades the engine actually OPENED --")
        lines.extend(format_split(opened_split, key)[1:])
        lines.extend(interpret_split(opened_split))
        lines.append("")

    lines.extend(format_replay(replays, reconciliations))
    lines.append("")

    near_seg = split.get("near") or {}
    n_eff = near_seg.get("n_effective", 0)
    lines.append(
        f"Independent evidence behind the near-drift number: {n_eff} non-overlapping thesis-window(s) "
        f"across {near_seg.get('n_symbols', 0)} symbol(s)."
    )
    if n_eff < 20:
        lines.append(
            "That is a DESCRIPTION of what happened, not a result. At this sample size the confidence "
            "intervals above are wider than any effect this strategy is looking for, and a capture ratio "
            "can flip on one name. Use it to see the SHAPE of the reaction and to catch broken plumbing "
            "(entries outside the real range, replay disagreements) -- not to accept or reject the premise."
        )
    return "\n".join(lines)
