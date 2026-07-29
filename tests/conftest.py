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
    """Pin the market open for every test that doesn't say otherwise.

    Patched at the point of USE (engine's imported name) rather than in
    signals, so a test that wants the real predicate can still import and
    call signals.is_regular_trading_hours directly and get the truth."""
    import smartboi.engine

    monkeypatch.setattr(smartboi.engine, "is_regular_trading_hours", lambda now=None: True)
