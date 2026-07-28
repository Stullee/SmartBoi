from smartboi.forward_returns import (
    bucket_returns,
    benchmark_relative_returns,
    compute_forward_return,
    dedup_snapshots,
    ecosystem_benchmark_return,
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


# --- dedup_snapshots ---

def test_dedup_snapshots_collapses_same_symbol_same_day():
    rows = [
        {"symbol": "IESC", "snapshotted_at": "2026-07-23T00:00:00+00:00", "score": 0.4},
        {"symbol": "IESC", "snapshotted_at": "2026-07-23T08:00:00+00:00", "score": 0.4},  # restart duplicate
        {"symbol": "IESC", "snapshotted_at": "2026-07-24T00:00:00+00:00", "score": 0.4},  # genuinely next day
    ]
    result = dedup_snapshots(rows)
    assert len(result) == 2
    assert [r["snapshotted_at"] for r in result] == ["2026-07-23T00:00:00+00:00", "2026-07-24T00:00:00+00:00"]


def test_dedup_snapshots_keeps_different_symbols_same_day():
    rows = [
        {"symbol": "IESC", "snapshotted_at": "2026-07-23T00:00:00+00:00"},
        {"symbol": "FORM", "snapshotted_at": "2026-07-23T00:00:00+00:00"},
    ]
    assert len(dedup_snapshots(rows)) == 2


def test_dedup_snapshots_skips_malformed_rows():
    rows = [{"symbol": None, "snapshotted_at": "2026-07-23"}, {"symbol": "IESC", "snapshotted_at": ""}]
    assert dedup_snapshots(rows) == []


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


# --- ecosystem_benchmark_return ---

def test_ecosystem_benchmark_return_uses_every_priced_symbol_not_just_dossiers():
    # IESC and POWL are both grid_datacenter; only IESC has a dossier, but
    # POWL still gets a daily price mark and must count toward the benchmark.
    price_marks = {
        "IESC": {"2026-07-01": 100.0, "2026-07-06": 90.0},  # -10%
        "POWL": {"2026-07-01": 50.0, "2026-07-06": 55.0},   # +10%
    }
    ecosystems = {"IESC": "grid_datacenter", "POWL": "grid_datacenter"}
    result = ecosystem_benchmark_return(price_marks, "2026-07-01", 5, "grid_datacenter", ecosystems)
    assert result == 0.0  # mean of -10% and +10%


def test_ecosystem_benchmark_return_none_when_nothing_priced():
    assert ecosystem_benchmark_return({}, "2026-07-01", 5, "grid_datacenter", {}) is None


def test_ecosystem_benchmark_return_ignores_other_ecosystems():
    price_marks = {
        "IESC": {"2026-07-01": 100.0, "2026-07-06": 110.0},
        "DCO": {"2026-07-01": 50.0, "2026-07-06": 25.0},  # defense_tier2, wildly different -- must not leak in
    }
    ecosystems = {"IESC": "grid_datacenter", "DCO": "defense_tier2"}
    result = ecosystem_benchmark_return(price_marks, "2026-07-01", 5, "grid_datacenter", ecosystems)
    assert result == 10.0


# --- benchmark_relative_returns ---

def test_benchmark_relative_returns_not_zero_by_construction_for_a_single_dossier_ecosystem():
    # The old bug: with only IESC's own signed return counted as the
    # "population," its benchmark equaled itself and alpha was always
    # exactly 0.00. Now POWL's price (no dossier, but still marked daily)
    # is part of the benchmark, so alpha reflects something real.
    rows = [{
        "symbol": "IESC", "direction": "LONG", "signed_return_pct": -10.0,
        "entry_date": "2026-07-01", "horizon_days": 5,
    }]
    price_marks = {
        "IESC": {"2026-07-01": 100.0, "2026-07-06": 90.0},
        "POWL": {"2026-07-01": 50.0, "2026-07-06": 55.0},
    }
    ecosystems = {"IESC": "grid_datacenter", "POWL": "grid_datacenter"}
    bench = benchmark_relative_returns(rows, price_marks, ecosystems)
    # ecosystem raw mean = mean(-10%, +10%) = 0% -> alpha = -10 - 0 = -10
    assert bench[0]["benchmark_relative_pct"] == -10.0


def test_benchmark_relative_returns_sign_matches_short_direction():
    # SHORT that fell 12% while its ecosystem's raw mean fell only 10% --
    # captured MORE downside than the sector move -> positive alpha.
    rows = [{
        "symbol": "AAA", "direction": "SHORT", "signed_return_pct": 12.0,
        "entry_date": "2026-07-01", "horizon_days": 5,
    }]
    price_marks = {
        "AAA": {"2026-07-01": 100.0, "2026-07-06": 88.0},   # -12%
        "BBB": {"2026-07-01": 100.0, "2026-07-06": 92.0},   # -8%
    }
    ecosystems = {"AAA": "defense_tier2", "BBB": "defense_tier2"}
    bench = benchmark_relative_returns(rows, price_marks, ecosystems)
    # raw ecosystem mean = mean(-12%, -8%) = -10% -> SHORT-signed benchmark = +10%
    # alpha = 12 - 10 = 2
    assert round(bench[0]["benchmark_relative_pct"], 4) == 2.0


def test_benchmark_relative_returns_none_when_ecosystem_unpriceable():
    rows = [{
        "symbol": "ZZZZ", "direction": "LONG", "signed_return_pct": 5.0,
        "entry_date": "2026-07-01", "horizon_days": 5,
    }]
    bench = benchmark_relative_returns(rows, {}, {})
    assert bench[0]["ecosystem_benchmark_pct"] is None
    assert bench[0]["benchmark_relative_pct"] is None


def test_benchmark_relative_returns_unclassified_symbol_gets_question_mark():
    rows = [{
        "symbol": "WEIRD", "direction": "LONG", "signed_return_pct": 3.0,
        "entry_date": "2026-07-01", "horizon_days": 5,
    }]
    bench = benchmark_relative_returns(rows, {}, {})
    assert bench[0]["ecosystem"] == "?"


# --- format_report ---

def test_format_report_handles_no_data():
    report = format_report(5, [], {}, {})
    assert "No joinable" in report


def test_format_report_includes_key_sections():
    rows = [
        {"symbol": "FORM", "direction": "LONG", "score": 0.6, "signed_return_pct": 8.0,
         "entry_date": "2026-07-01", "horizon_days": 5},
        {"symbol": "UCTT", "direction": "LONG", "score": 0.1, "signed_return_pct": -3.0,
         "entry_date": "2026-07-01", "horizon_days": 5},
    ]
    price_marks = {
        "FORM": {"2026-07-01": 10.0, "2026-07-06": 10.8},
        "UCTT": {"2026-07-01": 20.0, "2026-07-06": 19.4},
    }
    report = format_report(5, rows, price_marks, {"FORM": "semi_equipment", "UCTT": "semi_equipment"})
    assert "score bucket" in report
    assert "correlation" in report
    assert "hit rate" in report.lower() or "Hit rate" in report
    assert "Per-symbol" in report
    assert "FORM" in report and "UCTT" in report
