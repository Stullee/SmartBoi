"""Event-time backtest math, on synthetic bars.

Same rules as test_forward_returns.py: no network, no engine, every
number in here hand-checkable. The bars are built with round numbers so a
failure says which step is wrong rather than "0.4713 != 0.4712"."""
from __future__ import annotations

import json

import pytest

from smartboi.backtest import (
    Series,
    WouldBeTrade,
    actionable_session_date,
    adjustment_split,
    dedup_trades,
    event_path,
    event_window,
    format_replay,
    format_report,
    interpret_split,
    load_would_be_trades,
    mean_path,
    reconcile_entry,
    replay_exit,
    trades_from_signal_episodes,
)
from smartboi.bars import (
    BarClient,
    BarFetchError,
    DailyBar,
    cache_is_stale,
    parse_stooq_csv,
    parse_tiingo_json,
    read_cache,
    stooq_symbol,
    window_bounds,
    write_cache,
)

# Ten consecutive sessions (2026-08-03 is a Monday, so this is two clean
# trading weeks with no holiday in them).
SESSIONS = [
    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
    "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
]


def _series(closes, dates=None, high_mult=1.0, low_mult=1.0):
    dates = dates or SESSIONS[: len(closes)]
    return Series([
        DailyBar(date=d, open=c, high=c * high_mult, low=c * low_mult, close=c)
        for d, c in zip(dates, closes)
    ])


def _trade(**kwargs):
    base = dict(
        symbol="AAA", direction="LONG", event_at="2026-08-07T17:00:00+00:00",
        kind="opened", score=0.5,
    )
    base.update(kwargs)
    return WouldBeTrade(**base)


# --- bars.py ----------------------------------------------------------

def test_stooq_symbol_lowercases_and_suffixes():
    assert stooq_symbol("FORM") == "form.us"


def test_stooq_symbol_turns_class_dots_into_hyphens():
    # A dot reaches Stooq as a different (empty) series, not an error.
    assert stooq_symbol("BRK.B") == "brk-b.us"


def test_parse_stooq_csv_reads_bars_oldest_first():
    csv = "Date,Open,High,Low,Close,Volume\n2026-08-04,2,3,1,2.5,100\n2026-08-03,1,2,0.5,1.5,90\n"
    bars = parse_stooq_csv(csv)
    assert [b.date for b in bars] == ["2026-08-03", "2026-08-04"]
    assert bars[1] == DailyBar(date="2026-08-04", open=2.0, high=3.0, low=1.0, close=2.5)


def test_parse_stooq_csv_raises_on_a_non_table_body():
    # Stooq answers 200 with "No data" for a symbol it does not carry --
    # an empty list here would read as "this stock did not trade".
    with pytest.raises(BarFetchError):
        parse_stooq_csv("No data")


def test_parse_stooq_csv_skips_a_malformed_session_but_keeps_the_rest():
    csv = "Date,Open,High,Low,Close\n2026-08-03,1,2,0.5,1.5\n2026-08-04,N/A,N/A,N/A,N/A\n"
    assert [b.date for b in parse_stooq_csv(csv)] == ["2026-08-03"]


def test_parse_tiingo_json_uses_the_adjusted_fields():
    rows = [{"date": "2026-08-03T00:00:00.000Z", "open": 9, "high": 9, "low": 9, "close": 9,
             "adjOpen": 1, "adjHigh": 2, "adjLow": 0.5, "adjClose": 1.5}]
    assert parse_tiingo_json(rows) == [DailyBar(date="2026-08-03", open=1.0, high=2.0, low=0.5, close=1.5)]


def test_cache_round_trips(tmp_path):
    bars = [DailyBar(date="2026-08-03", open=1.0, high=2.0, low=0.5, close=1.5)]
    path = tmp_path / "AAA.csv"
    write_cache(path, bars)
    assert read_cache(path) == bars


def test_cache_is_stale_only_when_it_stops_well_short():
    bars = [DailyBar(date="2026-08-14", open=1, high=1, low=1, close=1)]
    assert not cache_is_stale(bars, "2026-08-15")   # yesterday's bar, today's request
    assert cache_is_stale(bars, "2026-08-25")
    assert cache_is_stale([], "2026-08-15")


def test_window_bounds_pads_for_weekends_and_never_asks_for_the_future():
    start, end = window_bounds(["2026-08-10"], pre_days=5, post_days=20)
    assert start < "2026-08-10"
    # Clamped at today, whenever the suite runs.
    from datetime import datetime, timezone
    assert end <= datetime.now(timezone.utc).date().isoformat()


# --- session alignment ------------------------------------------------

def test_actionable_session_is_the_same_day_during_the_session():
    # 17:00 UTC = 13:00 ET, mid-session.
    assert actionable_session_date("2026-08-07T17:00:00+00:00") == "2026-08-07"


def test_actionable_session_rolls_forward_after_the_close():
    # 21:00 UTC = 17:00 ET -- the session's bar is already printed, so
    # crediting that day's move to the signal would be look-ahead bias.
    assert actionable_session_date("2026-08-07T21:00:00+00:00") == "2026-08-08"


def test_actionable_session_uses_exchange_local_not_utc_date():
    # 01:00 UTC on the 8th is 21:00 ET on the 7th -- after the close, so
    # the actionable session is the 8th. The naive UTC date agrees here by
    # accident; the point is that both corrections are applied.
    assert actionable_session_date("2026-08-08T01:00:00+00:00") == "2026-08-08"


def test_actionable_session_before_the_open_is_that_same_session():
    # 11:00 UTC = 07:00 ET: the signal precedes the open, so the day's
    # whole move IS available to it.
    assert actionable_session_date("2026-08-07T11:00:00+00:00") == "2026-08-07"


def test_event_window_offsets_are_positions_not_calendar_days():
    # Offset +1 from Friday must be Monday: no calendar arithmetic, so
    # weekends and holidays never shorten a measured window.
    series = _series([1, 2, 3, 4, 5, 6])
    window = event_window(series, "2026-08-07", pre_days=1, post_days=1)
    assert window[0].date == "2026-08-07"   # Friday
    assert window[1].date == "2026-08-10"   # Monday


def test_event_window_is_empty_when_the_event_postdates_every_bar():
    assert event_window(_series([1, 2]), "2026-09-01", 1, 1) == {}


# --- event paths ------------------------------------------------------

def test_event_path_measures_from_the_close_before_the_signal():
    # Day -1 close 100; signal session closes 110; +1 closes 121.
    series = _series([100, 100, 100, 100, 100, 110, 121])
    path = event_path(_trade(event_at="2026-08-10T17:00:00+00:00"), series, pre_days=2, post_days=2)
    assert path["entry_date"] == "2026-08-10"
    assert path["raw"][0] == pytest.approx(10.0)
    assert path["raw"][1] == pytest.approx(21.0)
    assert path["raw"][-1] == pytest.approx(0.0)


def test_event_path_signs_a_short_so_a_fall_is_a_win():
    series = _series([100, 100, 100, 100, 100, 90])
    path = event_path(_trade(direction="SHORT", event_at="2026-08-10T17:00:00+00:00"),
                      series, pre_days=1, post_days=1)
    assert path["raw"][0] == pytest.approx(10.0)


def test_event_path_subtracts_the_benchmark():
    # The name rose 10%, its sector rose 4% -- 6% of that was the pick.
    series = _series([100, 100, 100, 100, 100, 110])
    bench = _series([50, 50, 50, 50, 50, 52])
    path = event_path(_trade(event_at="2026-08-10T17:00:00+00:00"), series, [bench],
                      pre_days=1, post_days=1)
    assert path["abnormal"][0] == pytest.approx(6.0)


def test_event_path_as_traded_runs_from_the_recorded_entry_not_the_close():
    # Entered at 105 intraday; the previous close was 100. The event
    # curve sees +10%, the P&L curve sees +4.76%.
    series = _series([100, 100, 100, 100, 100, 110])
    path = event_path(_trade(event_at="2026-08-10T17:00:00+00:00", entry_price=105.0),
                      series, pre_days=1, post_days=1)
    assert path["raw"][0] == pytest.approx(10.0)
    assert path["as_traded"][0] == pytest.approx(4.7619, abs=1e-3)
    assert -1 not in path["as_traded"]  # a pre-entry "P&L" is not a thing


def test_event_path_is_none_when_the_symbol_has_no_bar_for_the_event():
    assert event_path(_trade(event_at="2026-09-01T17:00:00+00:00"), _series([1, 2, 3])) is None


# --- aggregation ------------------------------------------------------

def _paths_for_split(pre=0.0, day0=0.0, near=0.0):
    """A single synthetic path with hand-set segment moves."""
    curve = {-5: -pre, -1: 0.0, 0: day0, 5: day0 + near}
    return [{"symbol": "AAA", "entry_date": "2026-08-10", "kind": "opened",
             "raw": curve, "abnormal": curve, "as_traded": {}, "max_offset": 5}]


def test_adjustment_split_puts_each_move_in_its_own_segment():
    split = adjustment_split(_paths_for_split(pre=2.0, day0=1.0, near=9.0), key="raw", far_days=5)
    assert split["before"]["mean_pct"] == pytest.approx(2.0)
    assert split["day0"]["mean_pct"] == pytest.approx(1.0)
    assert split["near"]["mean_pct"] == pytest.approx(9.0)


def test_capture_ratio_is_the_share_of_the_move_left_after_the_signal_session():
    split = adjustment_split(_paths_for_split(day0=1.0, near=9.0), key="raw", far_days=5)
    assert split["capture_ratio"] == pytest.approx(0.9)


def test_capture_ratio_is_none_when_there_was_no_move_to_divide_up():
    # Dividing a near-zero drift by a near-zero total produces a number
    # with the authority of a statistic and the content of a coin flip.
    split = adjustment_split(_paths_for_split(day0=0.1, near=0.1), key="raw", far_days=5)
    assert split["capture_ratio"] is None


def test_a_segment_with_a_missing_endpoint_is_dropped_not_shortened():
    paths = [{"symbol": "AAA", "entry_date": "2026-08-10", "kind": "opened",
              "raw": {-1: 0.0, 0: 1.0}, "abnormal": {}, "as_traded": {}, "max_offset": 0}]
    split = adjustment_split(paths, key="raw", far_days=5)
    assert split["day0"]["n"] == 1
    assert split["near"]["n"] == 0


def test_mean_path_weights_by_symbol_not_by_row():
    # AAA signalled twice at +10, BBB once at 0. Row-weighted that is
    # +6.67; symbol-weighted -- one vote per name -- it is +5.
    paths = [
        {"symbol": "AAA", "entry_date": "2026-08-10", "raw": {0: 10.0}},
        {"symbol": "AAA", "entry_date": "2026-08-11", "raw": {0: 10.0}},
        {"symbol": "BBB", "entry_date": "2026-08-10", "raw": {0: 0.0}},
    ]
    point = mean_path(paths, key="raw")[0]
    assert point["mean_pct"] == pytest.approx(6.667, abs=1e-3)
    assert point["mean_pct_symbol_weighted"] == pytest.approx(5.0)
    assert point["n"] == 3 and point["n_symbols"] == 2


def test_mean_path_reports_the_sample_size_at_each_offset_separately():
    # The tail of an event curve rests on fewer trades than its head.
    paths = [
        {"symbol": "AAA", "entry_date": "2026-08-10", "raw": {0: 1.0, 5: 2.0}},
        {"symbol": "BBB", "entry_date": "2026-08-10", "raw": {0: 1.0}},
    ]
    by_offset = {p["offset"]: p["n"] for p in mean_path(paths, key="raw")}
    assert by_offset == {0: 2, 5: 1}


def test_interpret_split_calls_out_a_same_session_repricing():
    split = adjustment_split(_paths_for_split(day0=9.0, near=1.0), key="raw", far_days=5)
    assert "NOT consistent" in "\n".join(interpret_split(split))


def test_interpret_split_confirms_a_lagged_one():
    split = adjustment_split(_paths_for_split(day0=1.0, near=9.0), key="raw", far_days=5)
    assert "Consistent with the lagged-adjustment premise" in "\n".join(interpret_split(split))


def test_interpret_split_flags_a_pre_signal_run_up():
    split = adjustment_split(_paths_for_split(pre=8.0, day0=1.0, near=9.0), key="raw", far_days=5)
    assert "run-up" in "\n".join(interpret_split(split))


# --- exit replay ------------------------------------------------------

def test_replay_takes_the_target_when_only_the_target_traded():
    # Entry 100, target 116, stop 92. Day +1 highs to 120.
    series = _series([100, 120], dates=["2026-08-03", "2026-08-04"], high_mult=1.0, low_mult=1.0)
    trade = _trade(event_at="2026-08-03T17:00:00+00:00", entry_price=100.0,
                   stop_price=92.0, target_price=116.0, horizon_days=20)
    result = replay_exit(trade, series)
    assert result["outcome"] == "WIN"
    assert result["exit_price"] == pytest.approx(116.0)  # a limit never fills better than its level


def test_replay_takes_the_stop_when_both_levels_traded_in_one_bar():
    # No intraday sequencing exists, so the loss is the honest assumption
    # -- the same choice paper_journal.update makes.
    series = Series([
        DailyBar(date="2026-08-03", open=100, high=100, low=100, close=100),
        DailyBar(date="2026-08-04", open=100, high=120, low=90, close=100),
    ])
    trade = _trade(event_at="2026-08-03T17:00:00+00:00", entry_price=100.0,
                   stop_price=92.0, target_price=116.0, horizon_days=20)
    result = replay_exit(trade, series)
    assert result["outcome"] == "LOSS"
    assert result["both_levels_same_bar"] is True


def test_replay_never_resolves_on_the_entry_session():
    # The entry session's range includes prints from before the position
    # existed -- resolving on it stops trades out on price action that
    # predates them.
    series = Series([
        DailyBar(date="2026-08-03", open=100, high=100, low=80, close=100),  # swung through the stop
        DailyBar(date="2026-08-04", open=100, high=100, low=100, close=100),
    ])
    trade = _trade(event_at="2026-08-03T17:00:00+00:00", entry_price=100.0,
                   stop_price=92.0, target_price=116.0, horizon_days=20)
    assert replay_exit(trade, series)["outcome"] == "OPEN"


def test_replay_stop_fills_at_the_worse_of_the_stop_and_the_close():
    # A gap through the stop cannot fill at the stop.
    series = Series([
        DailyBar(date="2026-08-03", open=100, high=100, low=100, close=100),
        DailyBar(date="2026-08-04", open=85, high=86, low=84, close=85),
    ])
    trade = _trade(event_at="2026-08-03T17:00:00+00:00", entry_price=100.0,
                   stop_price=92.0, target_price=116.0, horizon_days=20)
    assert replay_exit(trade, series)["exit_price"] == pytest.approx(85.0)


def test_replay_times_out_on_calendar_days_like_the_journal_does():
    series = _series([100, 101, 102, 103, 104])
    trade = _trade(event_at="2026-08-03T17:00:00+00:00", entry_price=100.0,
                   stop_price=50.0, target_price=200.0, horizon_days=3)
    result = replay_exit(trade, series)
    assert result["outcome"] == "TIMEOUT"
    assert result["exit_date"] == "2026-08-06"   # three calendar days after the open
    assert result["calendar_days_held"] == 3


def test_replay_is_none_without_levels_to_replay():
    # An unopened signal has no stop or target.
    assert replay_exit(_trade(kind="drift_skip"), _series([1, 2, 3])) is None


def test_replay_r_multiple_is_net_of_the_recorded_cost():
    # Entry 100, stop 92 (risk 8), target 116 -> +2R gross. A 300bp
    # round trip on a (100+116) notional pair costs 3.24/share = 0.405R.
    series = Series([
        DailyBar(date="2026-08-03", open=100, high=100, low=100, close=100),
        DailyBar(date="2026-08-04", open=116, high=120, low=116, close=118),
    ])
    trade = _trade(event_at="2026-08-03T17:00:00+00:00", entry_price=100.0,
                   stop_price=92.0, target_price=116.0, horizon_days=20,
                   cost_bps_round_trip=300.0)
    assert replay_exit(trade, series)["r_multiple"] == pytest.approx(1.595, abs=1e-3)


# --- entry reconciliation ---------------------------------------------

def test_reconciliation_passes_an_entry_inside_the_real_range():
    series = Series([DailyBar(date="2026-08-03", open=100, high=105, low=95, close=100)])
    row = reconcile_entry(_trade(event_at="2026-08-03T17:00:00+00:00", entry_price=99.0), series)
    assert row["outside_range"] is False
    assert row["gap_pct"] == pytest.approx(0.0)


def test_reconciliation_flags_an_entry_no_session_could_have_filled():
    # A stale quote, a symbol mismatch or an unadjusted split -- each
    # makes every R multiple derived from that entry fiction.
    series = Series([DailyBar(date="2026-08-03", open=100, high=105, low=95, close=100)])
    row = reconcile_entry(_trade(event_at="2026-08-03T17:00:00+00:00", entry_price=140.0), series)
    assert row["outside_range"] is True
    assert row["gap_pct"] == pytest.approx(33.33, abs=0.01)


def test_reconciliation_is_none_for_an_unopened_signal():
    series = Series([DailyBar(date="2026-08-03", open=100, high=105, low=95, close=100)])
    assert reconcile_entry(_trade(kind="expired", entry_price=None), series) is None


# --- loading the logs -------------------------------------------------

def _write_logs(tmp_path, paper=(), open_state=None, signals=(), decisions=()):
    (tmp_path / "paper_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in paper))
    if open_state is not None:
        (tmp_path / "open_paper_trades.json").write_text(json.dumps(open_state))
    (tmp_path / "signals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in signals))
    (tmp_path / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in decisions))
    return tmp_path


_CLOSED = {"symbol": "AAA", "direction": "LONG", "entry_price": 10.0, "stop_price": 9.0,
           "target_price": 12.0, "opened_at": "2026-08-03T17:00:00+00:00", "horizon_days": 20,
           "confidence": 0.8, "magnitude": 0.6, "episode": "ep1", "status": "WIN", "r_multiple": 1.4}


def test_load_reads_closed_open_and_unopened_rows(tmp_path):
    _write_logs(
        tmp_path,
        paper=[_CLOSED],
        open_state={"BBB": {**_CLOSED, "symbol": "BBB", "episode": "ep2", "status": "OPEN",
                            "r_multiple": None}},
        signals=[{"symbol": "CCC", "direction": "SHORT", "confidence": 0.7, "magnitude": 0.5,
                  "generated_at": "2026-08-04T14:00:00+00:00", "episode": "ep3"}],
        decisions=[{"symbol": "CCC", "episode": "ep3", "event": "signal_expired"}],
    )
    trades = load_would_be_trades(tmp_path)
    assert {(t.symbol, t.kind) for t in trades} == {("AAA", "opened"), ("BBB", "opened"), ("CCC", "expired")}


def test_load_scores_a_trade_by_confidence_times_magnitude(tmp_path):
    _write_logs(tmp_path, paper=[_CLOSED])
    assert load_would_be_trades(tmp_path)[0].score == pytest.approx(0.48)


def test_load_drops_the_open_snapshot_copy_of_an_already_closed_trade(tmp_path):
    # The close path appends to the log before rewriting the snapshot, so
    # a copy taken between the two contains both. Counted twice, one
    # thesis casts two votes in every mean.
    _write_logs(tmp_path, paper=[_CLOSED], open_state={"AAA": {**_CLOSED, "status": "OPEN", "r_multiple": None}})
    trades = load_would_be_trades(tmp_path)
    assert len(trades) == 1
    assert trades[0].recorded_status == "WIN"   # the closed record wins, it knows more


def test_load_keeps_two_separate_episodes_in_the_same_symbol(tmp_path):
    second = {**_CLOSED, "episode": "ep2", "opened_at": "2026-08-06T17:00:00+00:00"}
    _write_logs(tmp_path, paper=[_CLOSED, second])
    assert len(load_would_be_trades(tmp_path)) == 2


def test_load_can_exclude_the_unopened_signals(tmp_path):
    _write_logs(
        tmp_path, paper=[_CLOSED],
        signals=[{"symbol": "CCC", "direction": "SHORT", "generated_at": "2026-08-04T14:00:00+00:00",
                  "episode": "ep3"}],
        decisions=[{"symbol": "CCC", "episode": "ep3", "event": "drift_skip"}],
    )
    assert [t.symbol for t in load_would_be_trades(tmp_path, include_unopened=False)] == ["AAA"]


def test_load_survives_a_half_written_final_line(tmp_path):
    _write_logs(tmp_path, paper=[_CLOSED])
    with (tmp_path / "paper_trades.jsonl").open("a") as f:
        f.write('{"symbol": "BBB", "direct')   # copied mid-append
    assert [t.symbol for t in load_would_be_trades(tmp_path)] == ["AAA"]


def test_load_of_an_empty_directory_is_empty_not_an_error(tmp_path):
    assert load_would_be_trades(tmp_path) == []


def test_an_episode_that_opened_is_not_double_counted_as_unopened():
    # It is already in the paper-trade log, WITH its real entry price.
    signals = [{"symbol": "AAA", "direction": "LONG", "generated_at": "2026-08-03T14:00:00+00:00",
                "episode": "ep1"}]
    decisions = [{"symbol": "AAA", "episode": "ep1", "event": "drift_skip"},
                 {"symbol": "AAA", "episode": "ep1", "event": "trade_opened"}]
    assert trades_from_signal_episodes(signals, decisions) == []


def test_dedup_prefers_the_row_that_knows_the_outcome():
    open_row = _trade(episode="ep1", recorded_status="OPEN")
    closed_row = _trade(episode="ep1", recorded_status="LOSS")
    assert dedup_trades([open_row, closed_row])[0].recorded_status == "LOSS"
    assert dedup_trades([closed_row, open_row])[0].recorded_status == "LOSS"


# --- report -----------------------------------------------------------

def test_report_says_so_rather_than_printing_empty_tables():
    assert "nothing to check" in format_report([], [], [], [])


def test_report_states_how_many_trades_have_reached_each_horizon():
    # A curve whose tail rests on two observations has to say so before
    # it is read, not in a footnote after it.
    trade = _trade(event_at="2026-08-10T17:00:00+00:00")
    paths = _paths_for_split(day0=1.0, near=9.0)
    report = format_report([trade], paths, [], [], key="raw", far_days=20)
    assert "day +20 has elapsed for 0 of 1 trade(s)" in report
    assert "day +5 has elapsed for 1 of 1 trade(s)" in report


def test_report_refuses_to_call_a_tiny_sample_a_result():
    trade = _trade(event_at="2026-08-10T17:00:00+00:00")
    report = format_report([trade], _paths_for_split(day0=1.0, near=9.0), [], [], key="raw")
    assert "DESCRIPTION of what happened, not a result" in report


def test_replay_excludes_trades_whose_entry_failed_reconciliation():
    # Stop and target were derived from a price no session traded at, so
    # replaying those levels produces a confident-looking R with nothing
    # behind it. Pooled into a mean it is indistinguishable from a result.
    replays = [
        {"symbol": "AAA", "event_at": "2026-08-03T17:00:00+00:00", "outcome": "WIN",
         "exit_date": "2026-08-04", "exit_price": 116.0, "r_multiple": 2.0,
         "recorded_status": "WIN", "recorded_r_multiple": 2.0, "both_levels_same_bar": False},
        {"symbol": "BBB", "event_at": "2026-08-03T17:00:00+00:00", "outcome": "WIN",
         "exit_date": "2026-08-04", "exit_price": 31.0, "r_multiple": 1.8,
         "recorded_status": "TIMEOUT", "recorded_r_multiple": -0.2, "both_levels_same_bar": False},
    ]
    reconciliations = [
        {"symbol": "BBB", "event_at": "2026-08-03T17:00:00+00:00", "outside_range": True},
    ]
    text = "\n".join(format_replay(replays, reconciliations))
    assert "1 trade(s) excluded" in text and "BBB" in text
    assert "+2.00 over 1 trade(s)" in text
    # ...and its live-vs-real disagreement is not reported as a finding either.
    assert "resolved DIFFERENTLY" not in text


def test_replay_still_reports_disagreements_for_reconciled_trades():
    replays = [
        {"symbol": "AAA", "event_at": "2026-08-03T17:00:00+00:00", "outcome": "LOSS",
         "exit_date": "2026-08-04", "exit_price": 92.0, "r_multiple": -1.0,
         "recorded_status": "TIMEOUT", "recorded_r_multiple": 0.1, "both_levels_same_bar": False},
    ]
    text = "\n".join(format_replay(replays, [{"symbol": "AAA", "event_at": "2026-08-03T17:00:00+00:00",
                                              "outside_range": False}]))
    assert "resolved DIFFERENTLY" in text


# --- the fetch path, against a mock transport -------------------------

_STOOQ_CSV = "Date,Open,High,Low,Close,Volume\n2026-08-13,1,2,0.5,1.5,10\n2026-08-14,2,3,1,2.5,20\n"


def _mock_client(handler):
    import httpx
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_bar_client_fetches_parses_and_caches(tmp_path):
    import httpx
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=_STOOQ_CSV)

    client = BarClient(cache_dir=tmp_path)
    client._client = _mock_client(handler)
    bars = await client.bars_for("FORM", "2026-08-01", "2026-08-14")
    await client.aclose()
    assert [b.date for b in bars] == ["2026-08-13", "2026-08-14"]
    assert "form.us" in seen[0]
    # Cached, so a re-run costs nothing.
    assert read_cache(tmp_path / "FORM.csv") == bars


async def test_bar_client_serves_a_fresh_cache_without_a_request(tmp_path):
    write_cache(tmp_path / "FORM.csv", parse_stooq_csv(_STOOQ_CSV))

    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("hit the network with a fresh cache in hand")

    client = BarClient(cache_dir=tmp_path)
    client._client = _mock_client(handler)
    bars = await client.bars_for("FORM", "2026-08-01", "2026-08-14")
    await client.aclose()
    assert len(bars) == 2


async def test_bar_client_records_a_failure_instead_of_raising(tmp_path):
    import httpx

    def handler(request):
        return httpx.Response(200, text="No data")   # Stooq's answer for a symbol it lacks

    client = BarClient(cache_dir=tmp_path)
    client._client = _mock_client(handler)
    bars = await client.bars_for("NOPE", "2026-08-01", "2026-08-14")
    await client.aclose()
    # One unfetchable symbol must not sink a whole-universe pass...
    assert bars == []
    # ...but it must not vanish silently either.
    assert "NOPE" in client.failures


async def test_bar_client_offline_never_opens_a_socket(tmp_path):
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("opened a socket with --offline set")

    client = BarClient(cache_dir=tmp_path, offline=True)
    client._client = _mock_client(handler)
    assert await client.bars_for("FORM", "2026-08-01", "2026-08-14") == []
    await client.aclose()
    assert "FORM" in client.failures


async def test_bar_client_merges_a_refetch_over_the_cache(tmp_path):
    import httpx
    # A stale cache whose last bar the provider has since revised.
    write_cache(tmp_path / "FORM.csv", [DailyBar(date="2026-08-13", open=9, high=9, low=9, close=9)])

    def handler(request):
        return httpx.Response(200, text=_STOOQ_CSV)

    client = BarClient(cache_dir=tmp_path)
    client._client = _mock_client(handler)
    bars = await client.bars_for("FORM", "2026-08-01", "2026-09-30")
    await client.aclose()
    # The revised bar wins; the fetch is not merely appended around it.
    assert [(b.date, b.close) for b in bars] == [("2026-08-13", 1.5), ("2026-08-14", 2.5)]


async def test_tiingo_needs_a_token(tmp_path):
    with pytest.raises(ValueError):
        BarClient(cache_dir=tmp_path, provider="tiingo")


async def test_an_unknown_provider_is_refused_at_construction(tmp_path):
    with pytest.raises(ValueError):
        BarClient(cache_dir=tmp_path, provider="madeup")


# --- the dashboard/CLI entry point ------------------------------------

async def test_run_backtest_end_to_end_offline(tmp_path):
    """The whole path the dashboard button takes, with the network shut
    off: logs -> cached bars -> report."""
    from smartboi.tools import run_backtest
    from smartboi.universe import CompanySpec

    logs = tmp_path / "logs"
    logs.mkdir()
    cache = tmp_path / "data" / "bars"
    cache.mkdir(parents=True)

    # Flat, then a same-session pop, then a steady drift: the shape the
    # report exists to distinguish. Day -1 close 100, day 0 close 101,
    # then +1/day -- so 1% of the move was same-session and ~5% followed.
    closes = [100.0] * 5 + [101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    write_cache(cache / "AAA.csv", [
        DailyBar(date=d, open=c, high=c, low=c, close=c)
        for d, c in zip(SESSIONS + ["2026-08-17"], closes)
    ])
    (logs / "paper_trades.jsonl").write_text(json.dumps({
        "symbol": "AAA", "direction": "LONG", "entry_price": 101.0, "stop_price": 92.92,
        "target_price": 117.16, "opened_at": "2026-08-10T17:00:00+00:00", "horizon_days": 20,
        "confidence": 0.8, "magnitude": 0.7, "episode": "ep1", "status": "OPEN",
    }) + "\n")

    universe = [CompanySpec(symbol="AAA", name="A", ecosystem="semi_equipment")]
    report = await run_backtest(logs, universe, cache_dir=cache, offline=True, benchmark="none")
    assert "1 would-be trade(s)" in report
    # 1% same-session, 5.05% after -> the lagged case.
    assert "Consistent with the lagged-adjustment premise" in report


async def test_run_backtest_says_so_when_nothing_has_signalled(tmp_path):
    """And does it without reaching the network -- there is nothing to
    fetch bars for. offline is NOT set here, so a fetch would be a real
    request."""
    from smartboi.tools import run_backtest

    report = await run_backtest(tmp_path, [])
    assert "No would-be trades logged yet" in report


def test_event_window_does_not_wrap_round_to_the_end_of_the_series():
    """A signal in the first sessions of a series has no day -5. A negative
    list index silently returns a bar from the END -- which would price the
    event against a session weeks LATER and read as a huge pre-event
    run-up."""
    series = _series([1, 2, 3, 4])
    window = event_window(series, "2026-08-03", pre_days=5, post_days=1)
    assert min(window) == 0
    assert [window[o].date for o in sorted(window)] == ["2026-08-03", "2026-08-04"]


def test_event_path_without_a_prior_close_yields_no_curve_rather_than_a_wrong_one():
    # Nothing to measure "cumulative from the close before day 0" against.
    series = _series([100, 110])
    path = event_path(_trade(event_at="2026-08-03T17:00:00+00:00", entry_price=100.0),
                      series, pre_days=5, post_days=1)
    assert path["has_base"] is False
    assert path["raw"] == {}
    # ...but the P&L curve, which runs from the recorded entry, still works.
    assert path["as_traded"][1] == pytest.approx(10.0)


# --- regressions found by running against a real deployment's logs -----

def test_an_untagged_symbol_is_not_benchmarked_against_other_untagged_ones():
    """Seen live: two symbols the universe file does not classify (SCRNY,
    SCE-PN) were grouped into a shared "None" ecosystem and benchmarked
    against each other -- a sector built from whatever happens to be
    unclassified, carrying the authority of a sector control and none of
    the meaning."""
    from smartboi.backtest import benchmark_series_for
    series = {"AAA": _series([1, 2]), "BBB": _series([1, 2]), "CCC": _series([1, 2])}
    eco = {"CCC": "semi_equipment"}          # AAA and BBB are untagged
    benchmarks, label = benchmark_series_for("AAA", "ecosystem", eco, series, market_symbol="IWM")
    assert benchmarks == []
    assert "no ecosystem tag" in label or label == "none available"


def test_a_tagged_symbol_still_gets_its_peers():
    from smartboi.backtest import benchmark_series_for
    series = {f"P{i}": _series([1, 2]) for i in range(5)}
    series["AAA"] = _series([1, 2])
    series["CCC"] = _series([1, 2])
    eco = {"AAA": "semi_equipment", "CCC": "defense_tier2"}
    eco.update({f"P{i}": "semi_equipment" for i in range(5)})
    benchmarks, label = benchmark_series_for("AAA", "ecosystem", eco, series)
    assert len(benchmarks) == 5          # the five peers: subject excluded, CCC is another sector
    assert label == "semi_equipment tradeable peers"


def test_anchors_are_left_out_of_the_benchmark_when_enough_tradeables_remain():
    """An anchor is in the universe to be a news source, not a comparable.
    Measured on a live record, tradeable-only peers track the traded names
    better (mean daily-return correlation 0.470 vs 0.426) despite being
    fewer."""
    from smartboi.backtest import benchmark_series_for
    series = {f"T{i}": _series([1, 2]) for i in range(4)}
    series.update({f"A{i}": _series([1, 2]) for i in range(9)})
    series["AAA"] = _series([1, 2])
    eco = {"AAA": "semi_equipment"}
    eco.update({s: "semi_equipment" for s in list(series) if s != "AAA"})
    anchors = {f"A{i}" for i in range(9)}
    benchmarks, label = benchmark_series_for("AAA", "ecosystem", eco, series, anchors=anchors)
    assert len(benchmarks) == 4
    assert label == "semi_equipment tradeable peers"


def test_the_benchmark_widens_to_anchors_when_tradeables_are_too_thin():
    """A median over three names is not a sector. Widening is preferred to
    a degenerate control -- but it is labelled, so a row built on the
    weaker benchmark is visible rather than silently pooled."""
    from smartboi.backtest import benchmark_series_for
    series = {"T0": _series([1, 2]), "T1": _series([1, 2]), "AAA": _series([1, 2])}
    series.update({f"A{i}": _series([1, 2]) for i in range(6)})
    eco = {s: "semi_equipment" for s in series}
    anchors = {f"A{i}" for i in range(6)}
    benchmarks, label = benchmark_series_for("AAA", "ecosystem", eco, series, anchors=anchors)
    assert len(benchmarks) == 8          # widened: 2 tradeables + 6 anchors
    assert "widened" in label


def test_the_benchmark_move_is_a_median_not_a_mean():
    """One megacap in a peer group spanning four orders of magnitude of
    market cap should not stand in for what the sector did."""
    from smartboi.backtest import benchmark_move
    flat = [_series([100.0, 100.0]) for _ in range(4)]
    outlier = _series([100.0, 200.0])          # +100%
    move = benchmark_move(flat + [outlier], "2026-08-03", "2026-08-04")
    assert move == pytest.approx(0.0)          # a mean would say +20%


def test_the_report_prints_the_tolerance_it_actually_applied():
    """It printed the module default while applying whatever the caller
    passed -- so a close-only run flagged at 5% and told the reader 2%."""
    trade = _trade(event_at="2026-08-10T17:00:00+00:00", entry_price=100.0)
    recon = [{"symbol": "AAA", "event_at": "2026-08-10T17:00:00+00:00", "session_date": "2026-08-10",
              "recorded_entry": 100.0, "bar_low": 99.0, "bar_high": 101.0, "bar_close": 100.0,
              "gap_pct": 0.0, "outside_range": False}]
    report = format_report([trade], _paths_for_split(day0=1.0, near=9.0), [], recon,
                           key="raw", entry_tolerance_pct=5.0)
    assert "tolerance 5.0%" in report


async def test_marks_source_needs_no_network_at_all(tmp_path):
    """The zero-egress path: price_marks.jsonl stands in for fetched bars.
    The handler must never construct a BarClient in this mode."""
    from smartboi.tools import run_backtest
    from smartboi.universe import CompanySpec

    logs = tmp_path / "logs"
    logs.mkdir()
    closes = [100.0] * 5 + [101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    logs.joinpath("price_marks.jsonl").write_text("".join(
        json.dumps({"symbol": "AAA", "price": c, "marked_at": f"{d}T20:00:00+00:00"}) + "\n"
        for d, c in zip(SESSIONS + ["2026-08-17"], closes)))
    logs.joinpath("paper_trades.jsonl").write_text(json.dumps({
        "symbol": "AAA", "direction": "LONG", "entry_price": 101.0, "stop_price": 92.92,
        "target_price": 117.16, "opened_at": "2026-08-10T17:00:00+00:00", "horizon_days": 20,
        "confidence": 0.8, "magnitude": 0.7, "episode": "ep1", "status": "OPEN",
    }) + "\n")

    universe = [CompanySpec(symbol="AAA", name="A", ecosystem="semi_equipment")]
    report = await run_backtest(logs, universe, source="marks", benchmark="none")
    assert "1 would-be trade(s)" in report
    assert "Consistent with the lagged-adjustment premise" in report


async def test_marks_source_says_so_when_no_marks_captured(tmp_path):
    from smartboi.tools import run_backtest
    from smartboi.universe import CompanySpec
    logs = tmp_path / "logs"
    logs.mkdir()
    logs.joinpath("paper_trades.jsonl").write_text(json.dumps({
        "symbol": "AAA", "direction": "LONG", "entry_price": 10.0, "stop_price": 9.0,
        "target_price": 12.0, "opened_at": "2026-08-10T17:00:00+00:00", "horizon_days": 20,
    }) + "\n")
    report = await run_backtest(logs, [CompanySpec(symbol="AAA", name="A", ecosystem="x")],
                                source="marks")
    assert "No price marks captured yet" in report


def test_marks_as_series_drops_weekend_marks():
    from smartboi.backtest import marks_as_series
    rows = [{"symbol": "A", "price": 10, "marked_at": "2026-08-07T20:00:00+00:00"},   # Fri
            {"symbol": "A", "price": 10, "marked_at": "2026-08-08T20:00:00+00:00"},   # Sat
            {"symbol": "A", "price": 11, "marked_at": "2026-08-10T20:00:00+00:00"}]   # Mon
    assert [b.date for b in marks_as_series(rows)["A"].bars] == ["2026-08-07", "2026-08-10"]


def test_marks_as_series_sets_the_range_to_the_close():
    from smartboi.backtest import marks_as_series
    rows = [{"symbol": "A", "price": 10, "marked_at": "2026-08-07T20:00:00+00:00"},
            {"symbol": "A", "price": 11, "marked_at": "2026-08-10T20:00:00+00:00"}]
    bar = marks_as_series(rows)["A"].bars[0]
    # No intraday range exists, so a replay can only ever see a close
    # THROUGH a level -- never one that traded and recovered.
    assert bar.high == bar.low == bar.close == 10.0


# --- the session a quote actually belongs to --------------------------

def test_a_pre_open_mark_is_filed_under_the_previous_session():
    """The live failure: the daily marks pass drifted to ~00:0x ET, where
    Finnhub hands back the previous session's close. Filed under the
    capture date, 79% of a real deployment's marks sat one session late
    and every forward window measured from them was aligned one session
    early."""
    from smartboi.backtest import marks_as_series
    rows = [{"symbol": "A", "price": 9, "marked_at": "2026-08-20T04:15:00+00:00"},    # 00:15 ET -> 08-19 close
            {"symbol": "A", "price": 10, "marked_at": "2026-08-21T04:22:00+00:00"},   # 00:22 ET -> 08-20 close
            {"symbol": "A", "price": 11, "marked_at": "2026-08-24T04:10:00+00:00"}]   # Mon    -> 08-21 close
    bars = {b.date: b.close for b in marks_as_series(rows)["A"].bars}
    # Filed by capture date these would read 08-20/08-21/08-24 -- one session
    # late throughout, and inventing a Monday session that holds Friday.
    assert bars == {"2026-08-19": 9.0, "2026-08-20": 10.0, "2026-08-21": 11.0}


def test_a_settled_close_beats_a_mid_session_snapshot_of_the_same_session():
    from smartboi.backtest import marks_as_series
    rows = [{"symbol": "A", "price": 10, "marked_at": "2026-08-20T15:55:00+00:00"},   # 11:55 ET, mid-flight
            {"symbol": "A", "price": 11, "marked_at": "2026-08-21T04:22:00+00:00"},   # 00:22 ET -> 08-20 close
            {"symbol": "A", "price": 12, "marked_at": "2026-08-24T04:10:00+00:00"}]   # Mon -> 08-21 close
    bars = {b.date: b.close for b in marks_as_series(rows)["A"].bars}
    assert bars == {"2026-08-20": 11.0, "2026-08-21": 12.0}


def test_a_weekend_mark_is_fridays_close_not_a_discarded_row():
    # It holds a real price -- Friday's close. Dropping it threw that away;
    # stamping it Saturday invented a session.
    from smartboi.backtest import marks_as_series
    rows = [{"symbol": "A", "price": 9, "marked_at": "2026-08-20T20:30:00+00:00"},
            {"symbol": "A", "price": 10, "marked_at": "2026-08-22T19:15:00+00:00"}]   # Sat 15:15 ET
    bars = {b.date: b.close for b in marks_as_series(rows)["A"].bars}
    assert bars == {"2026-08-20": 9.0, "2026-08-21": 10.0}


def test_an_explicit_session_field_is_trusted_over_the_capture_time():
    from smartboi.backtest import marks_as_series
    rows = [{"symbol": "A", "price": 10, "session": "2026-08-19", "marked_at": "2026-08-21T04:22:00+00:00"},
            {"symbol": "A", "price": 11, "session": "2026-08-20", "marked_at": "2026-08-24T04:10:00+00:00"}]
    assert [b.date for b in marks_as_series(rows)["A"].bars] == ["2026-08-19", "2026-08-20"]


def test_session_for_quote_maps_each_capture_hour_to_its_session():
    from datetime import datetime
    from smartboi.market_hours import quote_is_a_close, session_for_quote
    cases = [
        ("2026-08-21T04:22:00+00:00", "2026-08-20", True),    # 00:22 ET, before the open
        ("2026-08-21T15:55:00+00:00", "2026-08-21", False),   # 11:55 ET, mid-session
        ("2026-08-21T21:00:00+00:00", "2026-08-21", True),    # 17:00 ET, after the close
        ("2026-08-22T19:15:00+00:00", "2026-08-21", True),    # Saturday
        ("2026-08-24T04:10:00+00:00", "2026-08-21", True),    # Monday pre-open -> Friday
    ]
    for iso, expected, is_close in cases:
        at = datetime.fromisoformat(iso)
        assert session_for_quote(at) == expected, iso
        assert quote_is_a_close(at) is is_close, iso


def test_session_for_quote_steps_over_a_known_holiday():
    from datetime import datetime
    from smartboi.market_hours import session_for_quote
    # Weekday, but not a session -- callers that know the calendar can say so.
    sessions = {"2026-08-19", "2026-08-21"}   # 08-20 was a holiday
    at = datetime.fromisoformat("2026-08-21T04:00:00+00:00")   # pre-open on the 21st
    assert session_for_quote(at, sessions) == "2026-08-19"


def test_dedup_collapses_repeat_episodes_landing_on_one_session():
    """A dossier that keeps re-crossing the threshold fires a new episode
    each time, and several land on one session. Seen live: PLPC produced
    three episodes all resolving to day 0 = 2026-08-03, with identical
    price paths -- three votes for one thesis in every mean."""
    rows = [_trade(episode=f"ep{i}", event_at=f"2026-08-0{d}T{h}:00:00+00:00", kind="expired")
            for i, (d, h) in enumerate([(1, "23"), (2, "05"), (2, "18")])]
    # All three are actionable on 2026-08-03: the first two fire after the
    # close on 08-01/08-02 (a weekend), the third after the close on 08-02.
    assert len({actionable_session_date(t.event_at) for t in rows}) == 1
    assert len(dedup_trades(rows)) == 1


def test_dedup_collapses_an_expired_and_an_opened_row_for_one_thesis():
    """They are different episodes and different kinds, so keying on those
    let both through -- KLXE 2026-07-30 SHORT contributed its path twice."""
    expired = _trade(symbol="KLXE", direction="SHORT", kind="expired", episode="ep1",
                     event_at="2026-07-30T14:00:00+00:00")
    opened = _trade(symbol="KLXE", direction="SHORT", kind="opened", episode="ep2",
                    event_at="2026-07-30T15:00:00+00:00", entry_price=2.10,
                    recorded_status="LOSS")
    kept = dedup_trades([expired, opened])
    assert len(kept) == 1
    # The opened row knows the real entry price; the expiry knows nothing.
    assert kept[0].kind == "opened" and kept[0].entry_price == 2.10


def test_dedup_keeps_both_directions_on_one_symbol_and_session():
    # A LONG and a SHORT on the same name the same day are two theses,
    # contradictory ones -- collapsing them would hide the contradiction.
    long_ = _trade(direction="LONG", episode="a")
    short = _trade(direction="SHORT", episode="b")
    assert len(dedup_trades([long_, short])) == 2


def test_dedup_keeps_the_same_symbol_on_different_sessions():
    a = _trade(episode="a", event_at="2026-08-03T17:00:00+00:00")
    b = _trade(episode="b", event_at="2026-08-06T17:00:00+00:00")
    assert len(dedup_trades([a, b])) == 2


# --- the opening-minutes staleness window -----------------------------

def test_minutes_into_session_measures_from_the_bell():
    from datetime import datetime
    from smartboi.market_hours import minutes_into_session
    # 13:36 UTC = 09:36 ET, six minutes after the open -- the exact window
    # seven live entries were booked in on 2026-07-30.
    assert minutes_into_session(datetime.fromisoformat("2026-07-30T13:36:00+00:00")) == 6.0
    assert minutes_into_session(datetime.fromisoformat("2026-07-30T15:30:00+00:00")) == 120.0


def test_minutes_into_session_is_none_when_the_session_is_shut():
    from datetime import datetime
    from smartboi.market_hours import minutes_into_session
    for iso in ("2026-07-30T12:00:00+00:00",   # 08:00 ET, pre-open
                "2026-07-30T21:00:00+00:00",   # 17:00 ET, after the close
                "2026-08-22T15:00:00+00:00"):  # Saturday
        assert minutes_into_session(datetime.fromisoformat(iso)) is None
