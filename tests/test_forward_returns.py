import pytest

from smartboi.forward_returns import (
    benchmark_relative_returns,
    bucket_returns,
    compute_forward_return,
    dedup_snapshots,
    ecosystem_benchmark_return,
    filter_by_scoring_version,
    format_report,
    market_benchmark_return,
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
    high = next(b for b in buckets if b["bucket"] == "[0.50, 0.65)")
    assert high["mean_return_pct"] == 10.0


def test_bucket_boundary_matches_signal_threshold():
    # The top bucket starts exactly at the default signal threshold (0.65)
    # so the report can answer "does the region that actually trades beat
    # the region just below it?" -- a bucket straddling the threshold
    # structurally couldn't.
    rows = [
        {"score": 0.64, "signed_return_pct": 1.0},
        {"score": 0.65, "signed_return_pct": 5.0},
    ]
    buckets = bucket_returns(rows)
    below = next(b for b in buckets if b["bucket"] == "[0.50, 0.65)")
    trading = next(b for b in buckets if b["bucket"] == ">= 0.65")
    assert below["count"] == 1 and below["mean_return_pct"] == 1.0
    assert trading["count"] == 1 and trading["mean_return_pct"] == 5.0


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
    # Two bugs, both fixed here: (1) with only dossier-having symbols in
    # the "population," a single-dossier ecosystem's benchmark equaled its
    # own return and alpha was always exactly 0.00; (2) even with POWL's
    # price included, IESC being part of its OWN benchmark shrank measured
    # alpha toward zero by 1/N. The benchmark is now "the REST of the
    # ecosystem": for IESC, that's POWL alone.
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
    # benchmark = POWL alone = +10% -> alpha = -10 - 10 = -20
    assert bench[0]["benchmark_relative_pct"] == -20.0


def test_benchmark_excludes_the_subject_symbol():
    # Direct check on ecosystem_benchmark_return's exclusion: with the
    # subject excluded, only the OTHER symbol's return counts.
    price_marks = {
        "IESC": {"2026-07-01": 100.0, "2026-07-06": 90.0},   # -10%
        "POWL": {"2026-07-01": 50.0, "2026-07-06": 55.0},    # +10%
    }
    ecosystems = {"IESC": "grid_datacenter", "POWL": "grid_datacenter"}
    result = ecosystem_benchmark_return(
        price_marks, "2026-07-01", 5, "grid_datacenter", ecosystems, exclude_symbol="IESC"
    )
    assert result == 10.0


def test_benchmark_is_none_when_subject_is_the_only_priced_symbol():
    # A one-symbol ecosystem must produce NO benchmark (row reported as
    # unbenchmarkable), never a fabricated 0.00 alpha from comparing the
    # pick against itself.
    rows = [{
        "symbol": "IESC", "direction": "LONG", "signed_return_pct": -10.0,
        "entry_date": "2026-07-01", "horizon_days": 5,
    }]
    price_marks = {"IESC": {"2026-07-01": 100.0, "2026-07-06": 90.0}}
    ecosystems = {"IESC": "grid_datacenter"}
    bench = benchmark_relative_returns(rows, price_marks, ecosystems)
    assert bench[0]["benchmark_relative_pct"] is None


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
    # benchmark = the REST of the ecosystem = BBB alone = -8% raw
    # -> SHORT-signed benchmark = +8% -> alpha = 12 - 8 = 4
    assert round(bench[0]["benchmark_relative_pct"], 4) == 4.0


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


# --- Statistical honesty: effective sample counts and clustered CIs ---

from smartboi.forward_returns import cluster_bootstrap_ci, effective_sample_count


def test_effective_sample_count_collapses_overlapping_windows():
    # 10 consecutive daily rows of one thesis with a 5-day horizon are ~2
    # independent windows, not 10 observations.
    rows = [
        {"symbol": "FORM", "entry_date": f"2026-07-{d:02d}"} for d in range(1, 11)
    ]
    assert effective_sample_count(rows, horizon_days=5) == 2


def test_effective_sample_count_sums_across_symbols():
    rows = [
        {"symbol": "FORM", "entry_date": "2026-07-01"},
        {"symbol": "UCTT", "entry_date": "2026-07-01"},
    ]
    assert effective_sample_count(rows, horizon_days=5) == 2


def test_cluster_bootstrap_ci_requires_two_symbols():
    assert cluster_bootstrap_ci({"FORM": [1.0, 2.0, 3.0]}) is None


def test_cluster_bootstrap_ci_brackets_the_mean():
    values = {"AAA": [4.0, 6.0], "BBB": [5.0, 7.0], "CCC": [3.0, 5.0]}
    ci = cluster_bootstrap_ci(values)
    assert ci is not None
    lo, hi = ci
    assert lo <= 5.0 <= hi  # overall mean = 5.0
    assert lo < hi


def test_format_report_shows_join_accounting():
    rows = [
        {"symbol": "FORM", "direction": "LONG", "score": 0.7, "signed_return_pct": 8.0,
         "entry_date": "2026-07-01", "horizon_days": 5},
    ]
    price_marks = {"FORM": {"2026-07-01": 10.0, "2026-07-06": 10.8}}
    report = format_report(5, rows, price_marks, {"FORM": "semi_equipment"}, attempted=4)
    assert "1 of 4" in report
    assert "3 unjoinable" in report
    assert "N_eff" in report


# --- session-keyed joins, the market benchmark, and the version filter ---

def test_the_join_uses_the_session_not_the_write_time():
    """The lookahead this closes: a mark WRITTEN at 00:30 ET Monday holds
    Friday's close. Keyed on marked_at it looked like a Monday price and a
    Monday snapshot joined to it; keyed on the session it is Friday's, and
    joins to Friday's snapshot."""
    marks = price_marks_by_symbol([
        {"marked_at": "2026-08-10T04:30:00+00:00", "session_date": "2026-08-07",
         "symbol": "FORM", "price": 10.0},
        {"marked_at": "2026-08-17T04:30:00+00:00", "session_date": "2026-08-14",
         "symbol": "FORM", "price": 11.0},
    ])
    assert marks["FORM"] == {"2026-08-07": 10.0, "2026-08-14": 11.0}

    row = compute_forward_return(
        {"symbol": "FORM", "direction": "LONG", "score": 0.8,
         "snapshotted_at": "2026-08-10T04:31:00+00:00", "session_date": "2026-08-07"},
        marks, horizon_days=7,
    )
    assert row["entry_date"] == "2026-08-07"
    assert row["entry_price"] == 10.0
    assert row["signed_return_pct"] == pytest.approx(10.0)


def test_rows_written_before_the_session_anchor_still_join():
    """Backward compatibility: pre-anchor rows have no session_date and
    must fall back to the wall-clock key rather than vanishing."""
    marks = price_marks_by_symbol([
        {"marked_at": "2026-07-01T20:00:00+00:00", "symbol": "FORM", "price": 10.0},
        {"marked_at": "2026-07-08T20:00:00+00:00", "symbol": "FORM", "price": 12.0},
    ])
    row = compute_forward_return(
        {"symbol": "FORM", "direction": "LONG", "score": 0.8,
         "snapshotted_at": "2026-07-01T20:05:00+00:00"},
        marks, horizon_days=7,
    )
    assert row is not None and row["signed_return_pct"] == pytest.approx(20.0)


def test_dedup_collapses_on_the_session_not_the_clock():
    rows = dedup_snapshots([
        {"symbol": "FORM", "snapshotted_at": "2026-08-07T20:30:00+00:00", "session_date": "2026-08-07"},
        {"symbol": "FORM", "snapshotted_at": "2026-08-08T02:00:00+00:00", "session_date": "2026-08-07"},
    ])
    assert len(rows) == 1, "two writes describing one session are one observation"


def test_market_relative_separates_alpha_from_beta():
    """The point of marking IWM. A LONG that gained 10% while the market
    gained 10% has no alpha, however good the raw number looks."""
    marks = price_marks_by_symbol([
        {"session_date": "2026-08-07", "symbol": "FORM", "price": 10.0, "marked_at": "x"},
        {"session_date": "2026-08-14", "symbol": "FORM", "price": 11.0, "marked_at": "x"},
        {"session_date": "2026-08-07", "symbol": "IWM", "price": 100.0, "marked_at": "x"},
        {"session_date": "2026-08-14", "symbol": "IWM", "price": 110.0, "marked_at": "x"},
    ])
    row = compute_forward_return(
        {"symbol": "FORM", "direction": "LONG", "score": 0.8, "session_date": "2026-08-07"},
        marks, horizon_days=7,
    )
    (out,) = benchmark_relative_returns([row], marks, {"FORM": "semi_equipment"})
    assert out["signed_return_pct"] == pytest.approx(10.0)
    assert out["market_benchmark_pct"] == pytest.approx(10.0)
    assert out["market_relative_pct"] == pytest.approx(0.0)


def test_market_relative_is_sign_matched_for_shorts():
    marks = price_marks_by_symbol([
        {"session_date": "2026-08-07", "symbol": "FORM", "price": 10.0, "marked_at": "x"},
        {"session_date": "2026-08-14", "symbol": "FORM", "price": 9.0, "marked_at": "x"},
        {"session_date": "2026-08-07", "symbol": "IWM", "price": 100.0, "marked_at": "x"},
        {"session_date": "2026-08-14", "symbol": "IWM", "price": 95.0, "marked_at": "x"},
    ])
    row = compute_forward_return(
        {"symbol": "FORM", "direction": "SHORT", "score": 0.8, "session_date": "2026-08-07"},
        marks, horizon_days=7,
    )
    (out,) = benchmark_relative_returns([row], marks, {"FORM": "semi_equipment"})
    # Stock -10%, market -5%: the short made 10 but a short of the index
    # would have made 5, so alpha is +5.
    assert out["signed_return_pct"] == pytest.approx(10.0)
    assert out["market_relative_pct"] == pytest.approx(5.0)


def test_a_missing_benchmark_is_none_not_zero():
    """Before IWM was marked there are no index prices. That must read as
    'unknown', never as 'zero alpha'."""
    marks = price_marks_by_symbol([
        {"session_date": "2026-08-07", "symbol": "FORM", "price": 10.0, "marked_at": "x"},
        {"session_date": "2026-08-14", "symbol": "FORM", "price": 11.0, "marked_at": "x"},
    ])
    row = compute_forward_return(
        {"symbol": "FORM", "direction": "LONG", "score": 0.8, "session_date": "2026-08-07"},
        marks, horizon_days=7,
    )
    (out,) = benchmark_relative_returns([row], marks, {"FORM": "semi_equipment"})
    assert out["market_relative_pct"] is None


def test_the_scoring_version_filter_actually_filters():
    """It was written at three sites and read at zero, so every report
    silently pooled incompatible rule sets."""
    rows = [
        {"symbol": "A", "scoring_version": 3, "session_date": "2026-08-01"},
        {"symbol": "B", "scoring_version": 4, "session_date": "2026-08-05"},
        {"symbol": "C", "scoring_version": 4, "session_date": "2026-08-07"},
    ]
    assert [r["symbol"] for r in filter_by_scoring_version(rows, 4)] == ["B", "C"]
    assert [r["symbol"] for r in filter_by_scoring_version(rows, 3)] == ["A"]
    assert len(filter_by_scoring_version(rows, None)) == 3, "None pools deliberately"


def test_the_since_filter_cuts_on_the_session():
    rows = [
        {"symbol": "A", "scoring_version": 4, "session_date": "2026-08-01"},
        {"symbol": "B", "scoring_version": 4, "session_date": "2026-08-07"},
    ]
    assert [r["symbol"] for r in filter_by_scoring_version(rows, 4, since="2026-08-05")] == ["B"]
