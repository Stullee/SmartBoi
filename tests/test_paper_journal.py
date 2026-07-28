from datetime import datetime, timedelta, timezone

import pytest

from smartboi.paper_journal import PaperTradeJournal


def _journal(tmp_path):
    return PaperTradeJournal(tmp_path / "logs" / "paper_trades.jsonl")


def test_open_long_computes_stop_and_target(tmp_path):
    journal = _journal(tmp_path)
    trade = journal.open(
        "UCTT", "LONG", entry_price=100.0, stop_loss_pct=8.0, take_profit_pct=16.0,
        horizon_days=30, thesis_summary="t", confidence=0.7, independent_source_count=2, citations=[],
    )
    assert trade.stop_price == pytest.approx(92.0)
    assert trade.target_price == pytest.approx(116.0)
    assert journal.has_open("UCTT")


def test_open_short_computes_stop_and_target(tmp_path):
    journal = _journal(tmp_path)
    trade = journal.open(
        "UCTT", "SHORT", entry_price=100.0, stop_loss_pct=8.0, take_profit_pct=16.0,
        horizon_days=30, thesis_summary="t", confidence=0.7, independent_source_count=2, citations=[],
    )
    assert trade.stop_price == pytest.approx(108.0)
    assert trade.target_price == pytest.approx(84.0)


def test_target_hit_closes_as_win(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 116.0)

    assert not journal.has_open("UCTT")
    lines = journal.log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    import json
    closed = json.loads(lines[0])
    assert closed["status"] == "WIN"
    assert closed["r_multiple"] == 2.0  # (116-100)/(100-92)


def test_stop_hit_closes_as_loss(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 92.0)

    assert not journal.has_open("UCTT")


def test_timeout_closes_trade(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, horizon_days=5, thesis_summary="t", confidence=0.7, independent_source_count=2, citations=[])
    future = datetime.now(timezone.utc) + timedelta(days=6)
    journal.update("UCTT", 101.0, now=future)

    assert not journal.has_open("UCTT")


def test_open_state_persists_across_instances(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])

    reloaded = PaperTradeJournal(journal.log_path)
    assert reloaded.has_open("UCTT")
    assert reloaded.open_trades["UCTT"].entry_price == 100.0


def test_no_new_trade_while_one_is_open(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    assert journal.has_open("UCTT")
    # Caller (engine.py) is responsible for checking has_open() before
    # calling open() again -- this test documents that expectation.


# --- Regression: stops/targets are evaluated against the day's intraday
# extremes when available -- a stock that breached the stop midday and
# recovered by the close is a real stop-out, and close-only evaluation
# silently erased exactly those losses. ---

def test_intraday_stop_breach_closes_as_loss_even_if_close_recovered(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # Day: low 90 (through the 92 stop), close back at 99.
    journal.update("UCTT", 99.0, high=101.0, low=90.0)

    assert not journal.has_open("UCTT")
    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"
    assert closed["exit_price"] == 92.0  # fills at the stop, not the recovered close


def test_intraday_stop_fill_is_never_better_than_the_close(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # Gap down: closes at 85, below the 92 stop -- a resting stop can't
    # have filled at 92 on a gap through it; the close is the honest fill.
    journal.update("UCTT", 85.0, high=88.0, low=84.0)

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"
    assert closed["exit_price"] == 85.0


def test_intraday_target_touch_closes_as_win_at_target_price(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 110.0, high=117.0, low=105.0)  # touched 116 target intraday

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "WIN"
    assert closed["exit_price"] == pytest.approx(116.0)  # a limit fills at its level, not the day's high


def test_stop_wins_when_both_levels_traded_in_one_bar(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # Wild bar: both the 92 stop and 116 target traded. With no intraday
    # sequencing available, assuming the loss is the conservative choice.
    journal.update("UCTT", 100.0, high=118.0, low=91.0)

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"


def test_short_intraday_stop_breach(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "SHORT", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # SHORT stop at 108: high spiked to 109 intraday, closed back at 101.
    journal.update("UCTT", 101.0, high=109.0, low=99.0)

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"
    assert closed["exit_price"] == 108.0


def test_close_only_update_behaves_as_before(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 100.5)  # no high/low -- old behavior, stays open
    assert journal.has_open("UCTT")


# --- Regression: a crash between the closed-log append and the open-state
# rewrite left the trade in BOTH files; on restart it was closed a second
# time at a different price, double-counting it in every statistic. ---

def test_restart_drops_open_trade_already_present_in_closed_log(tmp_path):
    journal = _journal(tmp_path)
    trade = journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 116.0)  # closes as WIN
    assert not journal.has_open("UCTT")

    # Simulate the crash window: re-inject the closed trade into the
    # open-state file as if _write_open_state never ran.
    import json
    from dataclasses import asdict
    stale = asdict(trade)
    stale["status"] = "OPEN"
    stale["closed_at"] = None
    stale["exit_price"] = None
    journal.open_state_path.write_text(json.dumps({"UCTT": stale}))

    reloaded = PaperTradeJournal(journal.log_path)
    assert not reloaded.has_open("UCTT")  # self-healed, no second close possible
