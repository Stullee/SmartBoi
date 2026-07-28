from smartboi.forward_returns import (
    bucket_returns,
    benchmark_relative_returns,
    compute_forward_return,
    format_report,
    pearson_correlation,
    per_symbol_breakdown,
    price_marks_by_symbol,
)


def _snapshot(symbol="FORM", direction="LONG", score=0.4, snapshotted_at="2026-07-01T00:00:00+00:00"):
    return {"symbol": symbol, "direction": direction, "score": score, "snapshotted_at": snapshotted_at}


def _marks_rows(symbol, prices_by_date):
    return [{"symbol": symbol, "marked_at": f"{d}T20:00:00+00:00", "price": p} for d, p in prices_by_date.items()]


# --- price_marks_by_symbol ---

def test_price_marks_by_symbol_indexes_by_date():
    rows = _marks_rows("FORM", {"2026-07-01": 10.0, "2026-07-02": 10.5})
    marks = price_marks_by_symbol(rows)
    assert marks == {"FORM": {"2026-07-01": 10.0, "2026-07-02": 10.5}}


def test_price_marks_by_symbol_skips_malformed_rows():
    rows = [{"symbol": "FORM", "marked_at": "", "price": 10.0}, {"symbol": None, "marked_at": "2026-07-01", "price": 10.0}]
    assert price_marks_by_symbol(rows) == {}


# --- compute_forward_return ---

def test_compute_forward_return_long_gain_is_positive():
    marks = {"FORM": {"2026-07-01": 10.0, "2026-07-06": 11.0}}
    snap = _snapshot(direction="LONG", snapshotted_at="2026-07-01T00:00:00+00:00")
    result = compute_forward_return(snap, marks, horizon_days=5)
    assert result is not None
    assert round(result["signed_return_pct"], 2) == 10.0


def test_compute_forward_return_short_gain_is_negative_price_move_positive_signed():
    # SHORT thesis: price FALLING is the win -- signed return should be positive.
    marks = {"FORM": {"2026-07-01": 10.0, "2026-07-06": 9.0}}
    snap = _snapshot(direction="SHORT", snapshotted_at="2026-07-01T00:00:00+00:00")
    result = compute_forward_return(snap, marks, horizon_days=5)
    assert result["signed_return_pct"] > 0  # price fell 10%, SHORT was right


def test_compute_forward_return_short_price_rise_is_negative_signed():
    marks = {"FORM": {"2026-07-01": 10.0, "2026-07-06": 11.0}}
    snap = _snapshot(direction="SHORT", snapshotted_at="2026-07-01T00:00:00+00:00")
    result = compute_forward_return(snap, marks, horizon_days=5)
    assert result["signed_return_pct"] < 0  # price rose, SHORT was wrong


def test_compute_forward_return_none_direction_is_unscored():
    marks = {"FORM": {"2026-07-01": 10.0, "2026-07-06": 11.0}}
    snap = _snapshot(direction="NONE")
    assert compute_forward_return(snap, marks, horizon_days=5) is None


def test_compute_forward_return_missing_entry_price_returns_none():
    marks = {"FORM": {"2026-07-06": 11.0}}
    snap = _snapshot(snapshotted_at="2026-07-01T00:00:00+00:00")
    assert compute_forward_return(snap, marks, horizon_days=5) is None


def test_compute_forward_return_missing_exit_price_returns_none():
    marks = {"FORM": {"2026-07-01": 10.0}}
    snap = _snapshot(snapshotted_at="2026-07-01T00:00:00+00:00")
    assert compute_forward_return(snap, marks, horizon_days=5) is None


def test_compute_forward_return_tolerates_a_missed_poll_day():
    # No mark on the exact entry date, but one shows up 2 days later --
    # within the lookahead window, so it should still join.
    marks = {"FORM": {"2026-07-03": 10.0, "2026-07-08": 11.0}}
    snap = _snapshot(snapshotted_at="2026-07-01T00:00:00+00:00")
    result = compute_forward_return(snap, marks, horizon_days=5)
    assert result is not None
    assert result["entry_date"] == "2026-07-03"


def test_compute_forward_return_symbol_with_no_marks_returns_none():
    snap = _snapshot(symbol="UNKNOWN")
    assert compute_forward_return(snap, {}, horizon_days=5) is None


# --- bucket_returns ---

def test_bucket_returns_groups_and_averages():
    rows = [
        {"score": 0.1, "signed_return_pct": -2.0},
        {"score": 0.15, "signed_return_pct": 2.0},
        {"score": 0.6, "signed_return_pct": 10.0},
    ]
    buckets = bucket_returns(rows)
    low = next(b for b in buckets if b["bucket"] == "[0.00, 0.20)")
    assert low["count"] == 2
    assert low["mean_return_pct"] == 0.0
    assert low["hit_rate"] == 0.5
    high = next(b for b in buckets if b["bucket"] == ">= 0.50")
    assert high["mean_return_pct"] == 10.0


def test_bucket_returns_omits_empty_buckets():
    rows = [{"score": 0.6, "signed_return_pct": 5.0}]
    buckets = bucket_returns(rows)
    assert len(buckets) == 1


# --- pearson_correlation ---

def test_pearson_correlation_perfect_positive():
    assert round(pearson_correlation([1, 2, 3], [1, 2, 3]), 4) == 1.0


def test_pearson_correlation_perfect_negative():
    assert round(pearson_correlation([1, 2, 3], [3, 2, 1]), 4) == -1.0


def test_pearson_correlation_insufficient_data_is_none():
    assert pearson_correlation([1], [1]) is None
    assert pearson_correlation([], []) is None


def test_pearson_correlation_no_variance_is_none():
    assert pearson_correlation([1, 1, 1], [1, 2, 3]) is None


# --- per_symbol_breakdown ---

def test_per_symbol_breakdown_sorts_worst_first():
    rows = [
        {"symbol": "AAA", "signed_return_pct": 5.0},
        {"symbol": "BBB", "signed_return_pct": -10.0},
        {"symbol": "AAA", "signed_return_pct": 15.0},
    ]
    breakdown = per_symbol_breakdown(rows)
    assert breakdown[0]["symbol"] == "BBB"
    assert breakdown[1]["symbol"] == "AAA"
    assert breakdown[1]["mean_return_pct"] == 10.0
    assert breakdown[1]["count"] == 2


# --- benchmark_relative_returns ---

def test_benchmark_relative_returns_subtracts_ecosystem_mean():
    rows = [
        {"symbol": "FORM", "signed_return_pct": 10.0},
        {"symbol": "UCTT", "signed_return_pct": -10.0},
        {"symbol": "DCO", "signed_return_pct": 5.0},
    ]
    ecosystems = {"FORM": "semi_equipment", "UCTT": "semi_equipment", "DCO": "defense_tier2"}
    bench = benchmark_relative_returns(rows, ecosystems)
    form_row = next(r for r in bench if r["symbol"] == "FORM")
    # semi_equipment mean = (10 + -10) / 2 = 0 -> benchmark-relative == raw
    assert form_row["benchmark_relative_pct"] == 10.0
    dco_row = next(r for r in bench if r["symbol"] == "DCO")
    # Only one defense_tier2 row -> its own mean, so relative return is 0.
    assert dco_row["benchmark_relative_pct"] == 0.0


def test_benchmark_relative_returns_unclassified_symbol_gets_question_mark():
    rows = [{"symbol": "WEIRD", "signed_return_pct": 3.0}]
    bench = benchmark_relative_returns(rows, {})
    assert bench[0]["ecosystem"] == "?"
    assert bench[0]["benchmark_relative_pct"] == 0.0


# --- format_report ---

def test_format_report_handles_no_data():
    report = format_report(5, [], {})
    assert "No joinable" in report


def test_format_report_includes_key_sections():
    rows = [
        {"symbol": "FORM", "score": 0.6, "signed_return_pct": 8.0},
        {"symbol": "UCTT", "score": 0.1, "signed_return_pct": -3.0},
    ]
    report = format_report(5, rows, {"FORM": "semi_equipment", "UCTT": "semi_equipment"})
    assert "score bucket" in report
    assert "correlation" in report
    assert "hit rate" in report.lower() or "Hit rate" in report
    assert "Per-symbol" in report
    assert "FORM" in report and "UCTT" in report
