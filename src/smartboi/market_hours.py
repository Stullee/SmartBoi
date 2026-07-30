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

from datetime import datetime, time as dt_time, timezone
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
