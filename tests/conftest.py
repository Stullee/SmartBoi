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
test_signals.py."""
from __future__ import annotations

import pytest


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
