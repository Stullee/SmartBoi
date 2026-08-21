"""Shared fixtures.

The clock pin below is the important one. The entry gate refuses to open a
paper trade outside US regular trading hours (signals.is_regular_trading_hours),
which means every test that drives a signal through to an open trade would
otherwise pass or fail depending on what time of day the suite is run --
green during a US session, red on an evening or a weekend. A test whose
result depends on the wall clock is worse than no test: it passes in
development and fails in CI for a reason nobody connects to the change that
was made.

So RTH is pinned OPEN for the whole suite, and the gate's own behaviour is
tested explicitly, with the clock passed in, in test_engine.py and
test_signals.py.

fixture_date below is the same argument applied to the calendar rather than
the time of day."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest


# --- The suite's calendar ----------------------------------------------------
#
# Evidence decays by age and goes STALE past max(horizon_days * 2, 14), so a
# fixture pinned to a literal date ages one day per real day and eventually
# crosses every threshold the tests assert is uncrossed. That is not
# hypothetical: the 2026-07-23 news fixtures had drifted 27 days out and were
# failing 30 tests in test_engine.py before this helper existed, with the rest
# queued to fall as the gap widened. Nothing in the code had changed -- only
# the date. It is the worst shape a failure can take, because the suite goes
# red for a reason nobody can connect to the commit that turned it red.
#
# So the dates stay pinned in the tests, where a reader can see the spacing
# between them, and are slid onto the running clock here at call time.
#
# Two eras, because the fixtures were written in two sittings: the news
# fixtures cluster at or below 2026-07-29, the price and filing fixtures at or
# below 2026-08-11. Each era slides as a block, so the spacing INSIDE it --
# which is what the decay, dedup and ordering assertions actually read -- is
# exactly what it was the day the test was written. Each era's newest day
# lands on YESTERDAY rather than today, so a fixture carrying a wall-clock
# time can never be dated into the future on a run before that hour.
#
# Tests that want stale evidence already ask for it in relative terms
# (now - timedelta(...)). Those are deliberate and do not go through here.
_FIXTURE_ERAS = (date(2026, 7, 29), date(2026, 8, 11))


def fixture_date(stamp: str) -> str:
    """Slide a pinned fixture timestamp onto the running clock.

    Accepts a bare date ("2026-07-23") or a full timestamp
    ("2026-07-23T13:00:00+00:00") and returns the same shape."""
    day, sep, clock = stamp.partition("T")
    day = date.fromisoformat(day)
    era = next(e for e in _FIXTURE_ERAS if e >= day)
    return ((datetime.now(timezone.utc).date() - timedelta(days=1))
            - (era - day)).isoformat() + sep + clock


@pytest.fixture(autouse=True)
def _inside_regular_trading_hours(monkeypatch):
    """Pin the market open, and the day to a weekday, for every test that
    doesn't say otherwise.

    Both matter. is_regular_trading_hours gates entries, and is_trading_day
    gates the daily price-marks pass (weekend marks duplicate Friday's close
    under a weekend date key and silently truncate every forward-return
    window that lands on one). Left unpinned, tests covering either would
    pass Monday to Friday and fail at the weekend.

    Patched at the point of USE (engine's imported names) rather than in
    signals, so a test that wants the real predicates can still import and
    call signals.is_regular_trading_hours / is_trading_day directly and get
    the truth -- which is exactly what test_signals.py does."""
    import smartboi.engine
    import smartboi.paper_journal

    monkeypatch.setattr(smartboi.engine, "is_regular_trading_hours", lambda now=None: True)
    monkeypatch.setattr(smartboi.engine, "is_trading_day", lambda now=None: True)
    # paper_journal holds its OWN reference (stop/target resolution also
    # requires the session to be open -- the daily bar does not roll until
    # the next US open, so a bar fetched overnight is still the entry
    # session's). Patching only engine's name left every close-path test on
    # the real wall clock: green in the afternoon, red after 16:00 ET.
    monkeypatch.setattr(
        smartboi.paper_journal, "is_regular_trading_hours", lambda now=None: True
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "regsho_network: exercise RegShoClient.refresh itself (against a mock "
        "transport) instead of having it stubbed out by _no_regsho_network.",
    )


@pytest.fixture(autouse=True)
def _no_regsho_network(request, monkeypatch):
    """Stop the Reg SHO refresh from making real HTTP requests.

    Same reasoning as the clock pin above, and the same reason it is autouse:
    the threshold-list refresh hangs off the daily-pass dispatch gated on
    is_trading_day, which the fixture above pins OPEN -- so every test that
    drives a tick would otherwise reach out to nasdaqtrader.com. That makes
    the suite slow, non-hermetic, and red on a machine with no egress, for a
    reason unconnected to whatever the test is about.

    Patched on the class rather than the engine attribute so it holds however
    a test builds its engine. The client's own fetching and parsing are tested
    directly against a mock transport in test_regsho.py, which opts out with
    the `regsho_network` marker -- stubbing the method there would leave those
    tests asserting against the stub."""
    if request.node.get_closest_marker("regsho_network"):
        return
    from smartboi.regsho import RegShoClient

    async def _no_refresh(self, today=None):
        return False

    monkeypatch.setattr(RegShoClient, "refresh", _no_refresh)
