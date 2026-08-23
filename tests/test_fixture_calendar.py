"""Guards the suite's own calendar (conftest.fixture_date).

This helper has now failed twice in the same shape: the suite goes red on a
day nobody changed anything, for a reason nothing in the diff explains. First
because fixtures were pinned to literal dates and aged past the staleness
cutoff; then because the slide that fixed THAT was by raw calendar days, so a
fixture written as a Tuesday session could land on a Saturday and be folded
back onto the preceding Friday by market_hours.session_for_quote.

Both failures share a cause: an invariant the helper is supposed to hold was
only ever checked by the tests that happened to depend on it, on the days the
run happened to fall. So the invariants are asserted here directly, swept
across every weekday a run can land on, rather than left to be rediscovered.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import _FIXTURE_ERAS, fixture_date

# One representative date per era, plus each era's own newest day. The price
# fixtures (era 2) are the ones that must stay on sessions.
_SAMPLES = (
    "2026-07-23", "2026-07-29",
    "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11",
)


def _slide(stamp: str, today: date) -> date:
    """fixture_date's arithmetic, evaluated for an arbitrary 'today'.

    Mirrors the helper rather than calling it, because the helper reads the
    real clock -- and a guard that can only ever check today's date is exactly
    the gap that let both regressions through."""
    day, _, _clock = stamp.partition("T")
    day = date.fromisoformat(day)
    era = next(e for e in _FIXTURE_ERAS if e >= day)
    yesterday = today - timedelta(days=1)
    anchor = yesterday - timedelta(days=(yesterday - era).days % 7)
    return anchor - (era - day)


def _run_days() -> list[date]:
    """Three weeks of possible run days -- every weekday, twice over."""
    return [date(2026, 8, 23) + timedelta(days=n) for n in range(21)]


def test_the_helper_agrees_with_the_arithmetic_this_module_sweeps():
    """_slide is only a valid proxy for fixture_date if it matches it today."""
    today = date.today()
    for stamp in _SAMPLES:
        assert fixture_date(stamp) == _slide(stamp, today).isoformat()


@pytest.mark.parametrize("stamp", _SAMPLES)
def test_a_slid_fixture_keeps_its_weekday(stamp):
    """The regression that broke test_synthesis_is_shown_the_price_around_its
    _earliest_evidence: era-2 fixtures are trading sessions, and a slide that
    does not preserve the weekday does not preserve trading-day-ness."""
    want = date.fromisoformat(stamp).weekday()
    for today in _run_days():
        got = _slide(stamp, today)
        assert got.weekday() == want, (
            f"{stamp} slid to {got} ({got:%A}) on a run dated {today} -- "
            f"the fixture was written as a {date.fromisoformat(stamp):%A}"
        )


@pytest.mark.parametrize("stamp", _SAMPLES)
def test_a_slid_fixture_never_lands_in_the_future(stamp):
    """A fixture carrying a wall-clock time must not be dated ahead of the run
    that reads it, at any hour of the day."""
    for today in _run_days():
        assert _slide(stamp, today) < today


@pytest.mark.parametrize("stamp", _SAMPLES)
def test_a_slid_fixture_stays_inside_the_staleness_floor(stamp):
    """Week-alignment costs up to 6 extra days of age against the old
    always-yesterday anchor. dossier._MIN_STALE_DAYS is 14, and evidence at or
    past its cutoff is dropped from the aggregate entirely -- so a fixture that
    ages past 14 days silently stops being evidence and the assertions reading
    it fail for a reason that is not in the diff."""
    for today in _run_days():
        age = (today - _slide(stamp, today)).days
        assert age <= 14, f"{stamp} is {age} days old on a run dated {today}"


def test_each_era_slides_as_one_block():
    """The spacing INSIDE an era is what the decay, dedup and ordering
    assertions read. It must survive the slide exactly."""
    era2 = [s for s in _SAMPLES if date.fromisoformat(s) > _FIXTURE_ERAS[0]]
    for today in _run_days():
        slid = [_slide(s, today) for s in era2]
        original = [date.fromisoformat(s) for s in era2]
        gaps_now = [(b - a).days for a, b in zip(slid, slid[1:])]
        gaps_then = [(b - a).days for a, b in zip(original, original[1:])]
        assert gaps_now == gaps_then
