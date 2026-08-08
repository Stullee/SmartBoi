"""The market-session calendar and the anchor the capture passes key on.

The holiday half used to be a documented gap on the reasoning that a
hand-maintained list rots. The list is computed from the NYSE rules, so
these tests pin the rules, not a table.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from smartboi.market_hours import (
    is_market_holiday, is_session_date, last_completed_session, market_holidays,
)


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.mark.parametrize("year,expected", [
    (2026, {
        "2026-01-01",  # New Year's, Thursday
        "2026-01-19",  # MLK, 3rd Monday
        "2026-02-16",  # Washington's Birthday, 3rd Monday
        "2026-04-03",  # Good Friday (Easter 2026-04-05)
        "2026-05-25",  # Memorial, last Monday
        "2026-06-19",  # Juneteenth, Friday
        "2026-07-03",  # Independence observed -- July 4 is a Saturday
        "2026-09-07",  # Labor, 1st Monday
        "2026-11-26",  # Thanksgiving, 4th Thursday
        "2026-12-25",  # Christmas, Friday
    }),
    (2027, {
        "2027-01-01", "2027-01-18", "2027-02-15",
        "2027-03-26",  # Good Friday (Easter 2027-03-28)
        "2027-05-31", "2027-06-18",  # Juneteenth observed -- the 19th is a Saturday
        "2027-07-05",  # Independence observed -- July 4 is a Sunday
        "2027-09-06", "2027-11-25", "2027-12-24",  # Christmas observed -- the 25th is a Saturday
    }),
])
def test_the_holiday_rules_produce_the_right_calendar(year, expected):
    assert {d.isoformat() for d in market_holidays(year)} == expected


def test_juneteenth_did_not_exist_before_2022():
    assert date(2021, 6, 18) not in market_holidays(2021)
    assert date(2022, 6, 20) in market_holidays(2022)  # the 19th was a Sunday


def test_a_saturday_new_year_is_not_observed_on_the_friday_before():
    """Every other Saturday holiday moves back to Friday. New Year's is the
    NYSE's one exception -- it stays open on Dec 31."""
    assert date(2022, 1, 1).weekday() == 5
    assert date(2021, 12, 31) not in market_holidays(2022)
    assert date(2021, 12, 31) not in market_holidays(2021)


def test_session_dates_exclude_weekends_and_holidays():
    assert is_session_date(date(2026, 8, 6))       # Thursday
    assert not is_session_date(date(2026, 8, 8))   # Saturday
    assert not is_session_date(date(2026, 12, 25))  # Christmas, a Friday
    assert is_market_holiday(date(2026, 12, 25))


# --- the anchor ---

def test_before_the_close_the_last_session_is_yesterday():
    """The bug this exists to prevent: the pass used to re-anchor to just
    after ET midnight every Monday, where a quote is Friday's close but the
    row was stamped Monday."""
    monday_0030_et = _utc(2026, 8, 10, 4, 30)
    assert last_completed_session(monday_0030_et) == date(2026, 8, 7)  # Friday


def test_after_the_close_the_last_session_is_today():
    monday_1700_et = _utc(2026, 8, 10, 21, 0)
    assert last_completed_session(monday_1700_et) == date(2026, 8, 10)


def test_the_close_is_not_trusted_until_it_settles():
    """16:05 ET is still inside the settle window; 16:20 is not."""
    assert last_completed_session(_utc(2026, 8, 10, 20, 5)) == date(2026, 8, 7)
    assert last_completed_session(_utc(2026, 8, 10, 20, 20)) == date(2026, 8, 10)


def test_a_weekend_resolves_back_to_friday():
    assert last_completed_session(_utc(2026, 8, 8, 16, 0)) == date(2026, 8, 7)
    assert last_completed_session(_utc(2026, 8, 9, 16, 0)) == date(2026, 8, 7)


def test_a_holiday_resolves_past_it():
    """Christmas 2026 is a Friday, so a weekday-only test would have
    recorded a phantom session and a duplicate close."""
    assert last_completed_session(_utc(2026, 12, 25, 22, 0)) == date(2026, 12, 24)


def test_a_long_holiday_weekend_walks_all_the_way_back():
    """Thanksgiving 2026 is Thu 11-26; the Friday after is a half day (the
    market IS open, so it counts). Sunday the 29th resolves to Friday."""
    assert last_completed_session(_utc(2026, 11, 29, 18, 0)) == date(2026, 11, 27)


def test_the_anchor_never_returns_a_non_session_day():
    """Swept across a full year at three times of day."""
    for day in range(1, 366):
        for hour in (2, 14, 22):
            moment = datetime(2026, 1, 1, hour, tzinfo=timezone.utc) + \
                __import__("datetime").timedelta(days=day - 1)
            assert is_session_date(last_completed_session(moment))
