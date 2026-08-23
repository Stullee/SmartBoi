"""Per-symbol volatility and the threshold scaling built on it.

Pure math on synthetic bars, in the same style as test_backtest.py: no
network, no engine, no clock. Every number here is checkable by hand, which
is the point -- a threshold that decides whether a trade opens should not
rest on a figure nobody can reproduce."""
from __future__ import annotations

import pytest

from smartboi.bars import DailyBar
from smartboi.volatility import (
    DEFAULT_LOOKBACK,
    MAX_SCALE,
    MIN_BARS,
    MIN_SCALE,
    REFERENCE_ATR_PCT,
    atr_pct,
    scaled_threshold,
    true_range,
    volatility_scale,
)


def _bars(closes: list[float], range_pct: float = 0.0, gap_pct: float = 0.0) -> list[DailyBar]:
    """A synthetic series. `range_pct` is the intraday high-low band around
    each close; `gap_pct` opens each session that far below the band, so the
    gap-vs-band distinction true_range exists for is exercisable."""
    out = []
    for n, close in enumerate(closes):
        half = close * range_pct / 200.0
        out.append(DailyBar(
            date=f"2026-08-{n + 1:02d}",
            open=close * (1 - gap_pct / 100.0),
            high=close + half,
            low=close - half,
            close=close,
        ))
    return out


# --- true range --------------------------------------------------------------

def test_true_range_is_the_intraday_band_when_there_is_no_gap():
    bar = DailyBar(date="2026-08-03", open=100, high=104, low=98, close=100)
    assert true_range(prev_close=100.0, bar=bar) == 6.0


def test_true_range_counts_an_overnight_gap_the_band_cannot_see():
    """The failure this function exists to prevent: a 424B5 prices after the
    bell, the stock opens 20% lower and then trades quietly. High-minus-low
    calls that a 2-point session; it was a 22-point one."""
    bar = DailyBar(date="2026-08-04", open=80, high=81, low=79, close=80)
    assert bar.high - bar.low == 2.0
    assert true_range(prev_close=100.0, bar=bar) == 21.0   # |low 79 - prev 100|


def test_true_range_counts_a_gap_up_symmetrically():
    bar = DailyBar(date="2026-08-04", open=120, high=122, low=119, close=121)
    assert true_range(prev_close=100.0, bar=bar) == 22.0   # |high 122 - prev 100|


# --- ATR ---------------------------------------------------------------------

def test_atr_pct_on_a_flat_series_with_a_constant_band():
    """A 100-priced series with a constant 4-point band and no drift has a
    true range of exactly 4 every session, so the ATR is 4 -- 4% of close,
    whatever the smoothing does, because smoothing a constant is that
    constant."""
    bars = _bars([100.0] * 30, range_pct=4.0)
    assert atr_pct(bars) == pytest.approx(4.0)


def test_atr_pct_is_relative_so_price_level_does_not_change_it():
    """KLXE near $2 and PLPC near $284 must be comparable. Same percentage
    band, same answer."""
    cheap = atr_pct(_bars([2.10] * 30, range_pct=5.0))
    dear = atr_pct(_bars([284.0] * 30, range_pct=5.0))
    assert cheap == pytest.approx(dear)
    assert cheap == pytest.approx(5.0)


def test_a_long_quiet_history_does_not_hold_down_a_recent_violent_regime():
    """Effective memory is ~lookback sessions however much history is passed.
    200 calm bars followed by 40 wild ones must read as wild -- what a 21-day
    thesis is exposed to is the regime it is entering, not the year before
    it."""
    quiet = _bars([100.0] * 200, range_pct=1.0)
    noisy = _bars([100.0] * 40, range_pct=10.0)
    reading = atr_pct(quiet + noisy)
    assert reading > 8.0, "the recent regime must dominate"
    assert reading < 10.0, "...without discarding the transition entirely"


def test_more_history_does_not_change_a_stable_reading():
    """The seed must not survive into the answer. A series that has been calm
    throughout reads the same whether 30 bars or 500 are passed."""
    short = atr_pct(_bars([100.0] * 30, range_pct=3.0))
    long_ = atr_pct(_bars([100.0] * 500, range_pct=3.0))
    assert short == pytest.approx(long_) == pytest.approx(3.0)


def test_no_single_session_dominates_because_of_where_it_falls():
    """Regression on a real seeding defect in this module's first draft.

    Seeding the recurrence on ranges[0] left ONE session -- whichever happened
    to land first in the fetched window -- carrying 0.95**19 = 38% of the
    answer. Measured on this fixture, moving a single wild session onto that
    position took the reading from 3.0% to 24.5%, which would have pinned the
    12% drift gate at its 24% ceiling: a name would have been handed the
    maximum allowance because of an artefact of where the fetch window
    happened to start.

    The wild bar sits at index -20 deliberately: with lookback 20 that is
    exactly the position the old tail slice used as its seed."""
    calm = _bars([100.0] * 40, range_pct=3.0)
    wild = list(calm)
    wild[-20] = DailyBar(date="2026-08-99", open=100, high=130, low=70, close=100)

    baseline = atr_pct(calm)
    with_spike = atr_pct(wild)
    assert baseline == pytest.approx(3.0)
    # One wild session in forty moves the estimate, but by about its fair
    # share of the window -- not by 21 percentage points.
    assert with_spike - baseline < 2.0
    assert scaled_threshold(12.0, with_spike) < 15.0


def test_a_single_violent_session_decays_rather_than_stepping_out():
    """Wilder smoothing, not a flat mean. The spike's contribution shrinks as
    it ages instead of vanishing the day it leaves an N-day window, so a
    resolved threshold does not jump when one old bar rolls off."""
    calm = _bars([100.0] * 60, range_pct=2.0)
    spike = DailyBar(date="2026-09-01", open=100, high=130, low=99, close=100)

    right_after = atr_pct(calm + [spike] + _bars([100.0] * 2, range_pct=2.0))
    much_later = atr_pct(calm + [spike] + _bars([100.0] * 18, range_pct=2.0))

    assert right_after > much_later > 2.0, "the spike must fade, not disappear"


def test_atr_pct_is_none_below_the_minimum_bar_count():
    """Three bars cannot support a threshold that decides whether money is
    committed. The caller must fall back to the configured constant, and it
    can only know to do that if this says it does not know."""
    assert atr_pct(_bars([100.0] * (MIN_BARS - 1), range_pct=4.0)) is None
    assert atr_pct(_bars([100.0] * MIN_BARS, range_pct=4.0)) is not None


def test_atr_pct_is_none_rather_than_zero_on_an_unusable_close():
    """None means "unknown", 0.0 means "this name did not move". Conflating
    them would scale a halted name's thresholds to the floor on the strength
    of missing data."""
    bars = _bars([100.0] * 30, range_pct=4.0)
    bars[-1] = DailyBar(date="2026-09-01", open=0.0, high=0.0, low=0.0, close=0.0)
    assert atr_pct(bars) is None


def test_a_genuinely_motionless_name_reads_zero_not_none():
    """The other half of the same distinction: a real, valid, quiet reading."""
    assert atr_pct(_bars([100.0] * 30, range_pct=0.0)) == pytest.approx(0.0)


def test_a_short_lookback_still_needs_the_minimum_history():
    """MIN_BARS guards the estimate's credibility, not merely the arithmetic's
    feasibility, so a caller cannot buy a low-confidence ATR by asking for a
    shorter window."""
    assert atr_pct(_bars([100.0] * 5, range_pct=4.0), lookback=3) is None


def test_gaps_dominate_the_estimate_in_a_gappy_name():
    """The universe-specific claim behind choosing true range: a name whose
    risk is overnight reads as volatile here and would read as calm on an
    intraday band alone."""
    banded = atr_pct(_bars([100.0] * 30, range_pct=6.0, gap_pct=0.0))
    gappy = atr_pct(_bars([100.0, 92.0] * 15, range_pct=1.0))
    assert gappy > banded


# --- scaling -----------------------------------------------------------------

def test_a_reference_volatility_name_keeps_todays_behaviour_exactly():
    """The calibration promise: this change redistributes the thresholds, it
    does not loosen or tighten them. A name at REFERENCE_ATR_PCT resolves to
    the configured value unchanged."""
    assert volatility_scale(REFERENCE_ATR_PCT) == pytest.approx(1.0)
    assert scaled_threshold(12.0, REFERENCE_ATR_PCT) == pytest.approx(12.0)
    assert scaled_threshold(8.0, REFERENCE_ATR_PCT) == pytest.approx(8.0)


def test_a_noisy_name_gets_more_room_and_a_quiet_one_less():
    assert scaled_threshold(12.0, REFERENCE_ATR_PCT * 1.5) == pytest.approx(18.0)
    assert scaled_threshold(12.0, REFERENCE_ATR_PCT * 0.75) == pytest.approx(9.0)


def test_the_ceiling_stops_a_wild_name_voting_itself_out_of_the_gate():
    """At 20% ATR the unclamped scale would be 5x and a 12% gate would become
    60%, which is not a gate at all."""
    assert volatility_scale(REFERENCE_ATR_PCT * 5) == MAX_SCALE
    assert scaled_threshold(12.0, 20.0) == pytest.approx(24.0)


def test_the_floor_stops_a_quiet_name_refusing_every_entry():
    """The silent failure: an over-tight gate refuses entries, refused entries
    never become trades, and a strategy that stops trading leaves no trace in
    the paper record to explain why."""
    assert volatility_scale(0.1) == MIN_SCALE
    assert scaled_threshold(12.0, 0.1) == pytest.approx(6.0)


def test_an_unknown_atr_reproduces_the_configured_threshold():
    """The fallback that makes this safe to ship: no bars, no change."""
    assert volatility_scale(None) == 1.0
    assert scaled_threshold(12.0, None) == pytest.approx(12.0)
    assert scaled_threshold(8.0, None) == pytest.approx(8.0)


def test_a_zero_atr_falls_back_rather_than_collapsing_the_threshold():
    """A halted name reads 0.0 legitimately, but a threshold of zero would
    refuse or refute everything. The scale floors instead."""
    assert scaled_threshold(12.0, 0.0) == pytest.approx(12.0)


def test_scaling_is_monotonic_across_the_clamped_range():
    """A more volatile name must never resolve to a tighter threshold than a
    calmer one -- the property every caller reasons about, asserted directly
    rather than inferred from the three sampled points above."""
    thresholds = [scaled_threshold(12.0, a / 10.0) for a in range(1, 300)]
    assert thresholds == sorted(thresholds)
    assert min(thresholds) == pytest.approx(12.0 * MIN_SCALE)
    assert max(thresholds) == pytest.approx(12.0 * MAX_SCALE)


def test_the_lookback_default_sits_inside_the_thesis_horizon():
    """A 21-day maximum horizon (settings.max_horizon_days) with a lookback
    longer than it would be measuring a regime the trade will never see."""
    assert DEFAULT_LOOKBACK <= 21
