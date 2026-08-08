"""US equity session calendar -- the two predicates that decide when a price
is actionable, in exchange-local time.

Its own module because both signals.py (entry gating) and paper_journal.py
(stop/target resolution) need them, and "when is the market open" is neither
module's concept. Exchange-local via zoneinfo rather than a fixed UTC offset,
so it stays correct across DST: the ET session is 13:30-20:00 UTC in summer
and 14:30-21:00 in winter, and a hard-coded UTC window is wrong for half the
year.

Market holidays were previously a documented gap here, on the reasoning
that a hand-maintained list rots. That reasoning is right and the
conclusion was wrong: the NYSE calendar is RULE-based, not a list, so it
can be computed and cannot go stale (see is_market_holiday). The one
genuinely unpredictable case -- an unscheduled closure for a funeral or a
hurricane -- is rare, and its failure mode is a duplicate price mark, not a
wrong one.

The holiday calendar is currently consumed by the CAPTURE side only
(last_completed_session, used by the daily snapshot and price-mark passes).
is_regular_trading_hours deliberately still ignores it: that predicate
gates entries, so changing it changes which trades exist, and that belongs
in a batched strategy change rather than a capture fix."""
from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(16, 0)
# How long after the close a quote is trusted to be that session's settled
# close rather than a still-moving late print.
SESSION_SETTLE = timedelta(minutes=15)


def is_trading_day(now: datetime | None = None) -> bool:
    """Whether this is a weekday in exchange-local time.

    Weaker than is_regular_trading_hours on purpose: the daily
    forward-validation marks want "is there a session today", not "is it open
    right now" -- they run once a day at whatever hour the tick lands on, and
    a mark taken after the close is that session's real close."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(MARKET_TZ).weekday() < 5


def is_regular_trading_hours(now: datetime | None = None) -> bool:
    """Whether US equities are in their regular session right now.

    Two things depend on this, for the same underlying reason -- a price
    quoted outside the session is the LAST session's close, not a price
    anything could transact at now:

    - Entries. An entry booked out of hours is not a fill anybody could have
      got. The price sources do not refuse to answer; IB and Finnhub both
      hand back the last close, so without this check the engine opens a
      position at a stale price and stamps it with the current time. The live
      record has two, booked 13:18Z = 09:18 ET.
    - Stop/target resolution. The daily bar does not roll to a new session
      until the next open, so a bar fetched at 02:00 UTC is still the
      PREVIOUS session's -- including, for a trade opened in that session,
      prints from before the position existed."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MARKET_TZ)
    if local.weekday() >= 5:  # Saturday/Sunday
        return False
    return MARKET_OPEN <= local.time() < MARKET_CLOSE


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-based) `weekday` of a month. n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm. Needed only for Good Friday, which is
    the one NYSE holiday with no fixed date and no nth-weekday rule."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, 0
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(holiday: date) -> date:
    """NYSE observance: a holiday on Saturday moves to the preceding Friday,
    one on Sunday to the following Monday."""
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def market_holidays(year: int) -> set[date]:
    """The NYSE full-day closures for `year`, computed from the rules rather
    than listed, so this file never needs maintaining.

    Juneteenth is included from 2022, the first year the NYSE observed it.
    Excluded deliberately: half-days (the market IS open, and a close on a
    half-day is a real close), and unscheduled closures, which no rule can
    predict."""
    holidays = {
        _observed(date(year, 1, 1)),                  # New Year's Day
        _nth_weekday(year, 1, 0, 3),                  # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),                  # Washington's Birthday
        _easter(year) - timedelta(days=2),            # Good Friday
        _nth_weekday(year, 5, 0, -1),                 # Memorial Day
        _observed(date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                  # Labor Day
        _nth_weekday(year, 11, 3, 4),                 # Thanksgiving
        _observed(date(year, 12, 25)),                # Christmas
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))    # Juneteenth
    # A New Year's Day that falls on a Saturday is NOT observed on the
    # preceding Friday -- the NYSE simply stays open on Dec 31 and takes no
    # holiday. Every other Saturday holiday does move back.
    if date(year, 1, 1).weekday() == 5:
        holidays.discard(date(year - 1, 12, 31))
        holidays.discard(date(year, 1, 1) - timedelta(days=1))
    return holidays


def is_market_holiday(day: date) -> bool:
    return day in market_holidays(day.year)


def is_session_date(day: date) -> bool:
    """Whether US equities held a regular session on this calendar date."""
    return day.weekday() < 5 and not is_market_holiday(day)


def last_completed_session(now: datetime | None = None) -> date:
    """The most recent exchange-local date whose regular session has closed
    and settled.

    This is the anchor the daily capture passes key on, and it exists to
    close a lookahead that silently biased the whole forward dataset. Those
    passes used to be scheduled purely on "at least 86400s since MY last
    run", with no time-of-day anchor and separate state keys, so each drifted
    independently. `is_trading_day` is only a weekday test with no hour
    bound, so after every weekend the price-mark pass re-anchored to the
    first tick where ET weekday < 5 -- roughly 00:00-01:00 ET Monday -- and
    then held that hour all week. A Finnhub quote at 00:30 ET returns the
    PREVIOUS session's close, and the row was stamped with today's date.

    The result was that a snapshot dated D could be joined to a price from
    D-1's close, while the score on that snapshot already reflected every
    8-K and news item ingested during D. 8-Ks and earnings cluster after the
    close, and this system's inputs are precisely 8-Ks and news -- so the
    dataset bought at the last pre-announcement print and measured the
    announcement gap as forward return.

    Keying on the session instead makes that structurally impossible: at
    00:30 ET Monday the last completed session is Friday, which was already
    captured, so the pass does not run at all."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MARKET_TZ)
    day = local.date()
    close_today = datetime.combine(day, MARKET_CLOSE, tzinfo=MARKET_TZ) + SESSION_SETTLE
    if not is_session_date(day) or local < close_today:
        day -= timedelta(days=1)
    while not is_session_date(day):
        day -= timedelta(days=1)
    return day
