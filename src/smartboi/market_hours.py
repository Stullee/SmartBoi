"""US equity session calendar -- the two predicates that decide when a price
is actionable, in exchange-local time.

Its own module because both signals.py (entry gating) and paper_journal.py
(stop/target resolution) need them, and "when is the market open" is neither
module's concept. Exchange-local via zoneinfo rather than a fixed UTC offset,
so it stays correct across DST: the ET session is 13:30-20:00 UTC in summer
and 14:30-21:00 in winter, and a hard-coded UTC window is wrong for half the
year.

KNOWN GAP, shared by both predicates: market holidays. They are all
weekdays, so a holiday passes both checks. A hand-maintained holiday
calendar rots the moment it stops being maintained, and a calendar that
silently claims the market is shut when it is open is a worse failure than
the one it fixes -- so nights and weekends are covered (where the observed
damage was) and the residual is documented rather than half-solved."""
from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(16, 0)


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

def session_for_quote(captured_at: datetime, sessions: set[str] | None = None) -> str | None:
    """The session whose price a quote captured at `captured_at` actually
    holds -- which is NOT always the date it was captured on.

    Neither IB nor Finnhub refuses to answer outside the session; both hand
    back the last close (the same fact is_regular_trading_hours exists for,
    on the entry path). So a quote taken before the open is the PREVIOUS
    session's close, and stamping it with the capture date labels it one
    session late.

    That is not hypothetical. The daily price-marks pass is gated on
    is_trading_day -- deliberately, because a mark taken after the close is
    that session's real close -- but nothing holds it to after the close,
    and on a live deployment the pass drifted to ~00:0x ET. From that point
    every mark in the file was the previous session's close wearing the
    current date: 3,992 of 5,025 rows, and every forward window measured
    from them was aligned one session early.

      captured >= 16:00 ET  -> that session (a real close)
      09:30-16:00 ET        -> that session (mid-flight, not a close)
      before 09:30 ET       -> the previous session's close
      weekend               -> the previous session's close

    `sessions` is an optional set of known trading dates (YYYY-MM-DD), used
    to step back over holidays as well as weekends. Without it, only
    weekends are skipped -- the same documented holiday gap the rest of this
    module carries. Returns None if no prior session can be identified.
    """
    local = captured_at.astimezone(MARKET_TZ)
    day = local.date()
    if local.weekday() < 5 and local.time() >= MARKET_OPEN:
        # Mid-session or after the close: either way it is today's session.
        return day.isoformat()
    for back in range(1, 8):
        candidate = day - timedelta(days=back)
        if candidate.weekday() >= 5:
            continue
        if sessions is None or candidate.isoformat() in sessions:
            return candidate.isoformat()
    return None


def quote_is_a_close(captured_at: datetime) -> bool:
    """Whether a quote taken at this moment is a settled session close --
    true after the close on a weekday, and true before the open (it is the
    PREVIOUS session's close). False mid-session, where the price is a
    snapshot that has not finished happening yet."""
    local = captured_at.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return True
    return not (MARKET_OPEN <= local.time() < MARKET_CLOSE)


def minutes_into_session(now: datetime | None = None) -> float | None:
    """How far into the regular session we are, in minutes. None outside it.

    Exists because "the session is open" and "the data has caught up" are
    not the same thing, and only the first is what is_regular_trading_hours
    can tell you. In the first minutes after the bell a daily-bar request
    still returns YESTERDAY's bar (there is no complete bar for today yet)
    and a delayed /quote can still be reporting the prior close. Both
    answer; neither refuses; and the price they hand back is one no order
    placed now could have been filled at -- the same failure
    is_regular_trading_hours exists to stop out of hours, arriving through
    a door that check holds open.
    """
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return None
    if not (MARKET_OPEN <= local.time() < MARKET_CLOSE):
        return None
    opened = local.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                           second=0, microsecond=0)
    return (local - opened).total_seconds() / 60.0
