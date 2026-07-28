"""Auto-accepting discovered universe candidates (engine._auto_accept_candidates).

The engine already resolves a candidate's ticker, fetches its market cap and
analyst count, and computes a tradeable-vs-anchor recommendation; accepting by
hand applied exactly that recommendation. These cover the guards that make
acting on it automatically safe -- above all that a TRADEABLE is never added on
an unverified ticker, which is the confirmed-live Advantest->ATRO failure."""
from __future__ import annotations

import asyncio

import pytest

from smartboi.config import Settings
from smartboi.engine import Engine

from tests.fakes import FakeEdgarClient, FakeFinnhub


def _candidate(name="Some Supplier Inc", ticker="ZZZZ", recommended_as="tradeable", seen_count=3):
    return {
        "name": name, "ticker": ticker, "related_to": ["DCO"], "rel_types": ["supplier"],
        "description": "disclosed supplier", "sources": ["https://sec.gov/x"],
        "seen_count": seen_count, "first_seen_at": "2026-07-01T00:00:00+00:00",
        "recommended_as": recommended_as, "recommendation_reason": "fits the profile",
    }


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, symbols="DCO", anchor_symbols="RTX",
        enable_dashboard=False, enable_universe_autoscreen=False,
    )
    e = Engine(settings)
    e.edgar_client = FakeEdgarClient()
    e.finnhub = FakeFinnhub()
    return e


# --- The core behaviour ---

async def test_anchor_recommendation_is_auto_accepted(engine):
    """Anchors are held to the liberal bar: they can never become a trade,
    so the downside is wasted LLM spend and the upside is a live
    propagation source."""
    engine.candidates.set("ZZZZ", _candidate(recommended_as="anchor", seen_count=1))
    await engine._auto_accept_candidates()
    assert engine.spec_by_symbol["ZZZZ"].signal_source_only is True


async def test_tradeable_recommendation_is_auto_accepted_when_verified(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert "ZZZZ" in engine.spec_by_symbol
    assert engine.spec_by_symbol["ZZZZ"].signal_source_only is False


async def test_auto_accept_is_recorded_with_its_source(engine):
    """Persisted as {"as", "source"} so an auto-add is distinguishable from
    one a human chose -- that's what makes it auditable and undoable."""
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert engine.accepted_candidates.get("ZZZZ") == {"as": "tradeable", "source": "auto"}


# --- The guards ---

async def test_tradeable_is_blocked_when_the_ticker_name_does_not_verify(engine):
    """The Advantest->ATRO case: a filing named one company, the ticker
    resolved to an unrelated one. An anchor mistake is cheap; auto-adding a
    WRONG company as a trade target is not."""
    engine.edgar_client.name_matches = False
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol
    # ...and the reason is surfaced rather than the candidate looking stuck.
    assert "does not match" in engine.candidates.get("ZZZZ")["auto_accept_blocked"]


async def test_anchor_is_not_blocked_by_an_unverified_name(engine):
    """The name check gates TRADEABLES only -- deliberately asymmetric."""
    engine.edgar_client.name_matches = False
    engine.candidates.set("ZZZZ", _candidate(recommended_as="anchor"))
    await engine._auto_accept_candidates()
    assert engine.spec_by_symbol["ZZZZ"].signal_source_only is True


async def test_tradeable_needs_repeat_disclosure(engine):
    engine.candidates.set("ZZZZ", _candidate(seen_count=1))
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_unknown_recommendation_is_never_auto_accepted(engine):
    """"unknown" means there was no market data to judge on."""
    engine.candidates.set("ZZZZ", _candidate(recommended_as="unknown"))
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_candidate_without_a_ticker_is_skipped(engine):
    engine.candidates.set("SOME PRIVATE CO", _candidate(ticker=""))
    await engine._auto_accept_candidates()
    assert len(engine.spec_by_symbol) == 2  # DCO + RTX only


async def test_daily_cap_bounds_how_many_can_be_added(engine):
    """One filing naming a long list of counterparties must not be able to
    flood the universe in a single pass."""
    engine.settings.auto_accept_max_per_day = 2
    for i in range(5):
        engine.candidates.set(f"ZZZ{i}", _candidate(ticker=f"ZZZ{i}"))
    await engine._auto_accept_candidates()
    assert sum(1 for s in engine.spec_by_symbol if s.startswith("ZZZ")) == 2


async def test_daily_cap_survives_a_restart(engine, tmp_path, monkeypatch):
    """Process-local counting would let a restart loop reset the budget."""
    engine.settings.auto_accept_max_per_day = 1
    for i in range(3):
        engine.candidates.set(f"ZZZ{i}", _candidate(ticker=f"ZZZ{i}"))
    await engine._auto_accept_candidates()

    restarted = Engine(engine.settings)
    restarted.edgar_client = FakeEdgarClient()
    restarted.finnhub = FakeFinnhub()
    await restarted._auto_accept_candidates()
    assert sum(1 for s in restarted.spec_by_symbol if s.startswith("ZZZ")) == 1


async def test_disabled_by_config(engine):
    engine.settings.enable_auto_accept_candidates = False
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_tradeables_can_be_disabled_independently_of_anchors(engine):
    engine.settings.auto_accept_tradeables = False
    engine.candidates.set("TRAD", _candidate(ticker="TRAD"))
    engine.candidates.set("ANCH", _candidate(ticker="ANCH", recommended_as="anchor"))
    await engine._auto_accept_candidates()
    assert "TRAD" not in engine.spec_by_symbol
    assert "ANCH" in engine.spec_by_symbol


# --- Persisted-format compatibility ---

def test_legacy_string_entries_still_load(engine, tmp_path, monkeypatch):
    """accepted_candidates.json entries were originally a bare
    "tradeable"/"anchor" string; a deployment upgrading into this release
    must keep its existing accepted symbols."""
    engine.accepted_candidates.set("OLDT", "tradeable")
    engine.accepted_candidates.set("OLDA", "anchor")

    restarted = Engine(engine.settings)
    assert restarted.spec_by_symbol["OLDT"].signal_source_only is False
    assert restarted.spec_by_symbol["OLDA"].signal_source_only is True


# --- A recommendation is only meaningful relative to the bounds that
# produced it, and those move (the 2026-07 recalibration went 100/6 ->
# 75/10). A stale one silently keeps auto-accept on superseded thresholds.

async def test_recommendation_is_recomputed_when_the_bounds_change(engine):
    engine.finnhub.market_cap_musd = lambda symbol: asyncio.sleep(0, result=800.0)
    engine.finnhub.analyst_count = lambda symbol: asyncio.sleep(0, result=8)
    # As recorded before the recalibration: 8 analysts was > the old bound of 6.
    entry = _candidate(recommended_as="anchor")
    entry["recommendation_bounds"] = [100.0, 5000.0, 6.0]
    engine.candidates.set("ZZZZ", entry)

    await engine._run_candidate_ticker_recheck()

    # 8 analysts is inside the current bound of 10, so it's a trade target now.
    assert engine.candidates.get("ZZZZ")["recommended_as"] == "tradeable"


async def test_recommendation_is_not_recomputed_when_bounds_are_unchanged(engine):
    """Re-fetching market data for every candidate on every daily recheck
    would be pure waste -- two Finnhub calls each, against a 60/min budget
    shared with the engine's own polling."""
    calls = []

    async def counting_market_cap(symbol):
        calls.append(symbol)
        return 800.0

    engine.finnhub.market_cap_musd = counting_market_cap
    engine.finnhub.analyst_count = lambda symbol: asyncio.sleep(0, result=8)
    entry = _candidate(recommended_as="tradeable")
    entry["recommendation_bounds"] = [
        engine.settings.universe_min_market_cap_musd,
        engine.settings.universe_max_market_cap_musd,
        float(engine.settings.universe_max_analyst_count),
    ]
    engine.candidates.set("ZZZZ", entry)

    await engine._run_candidate_ticker_recheck()
    assert calls == []


async def test_a_candidate_recorded_before_bounds_existed_is_refreshed(engine):
    """Entries written before recommendation_bounds was stored at all have
    no marker, so they must be treated as stale rather than trusted."""
    engine.finnhub.market_cap_musd = lambda symbol: asyncio.sleep(0, result=800.0)
    engine.finnhub.analyst_count = lambda symbol: asyncio.sleep(0, result=8)
    engine.candidates.set("ZZZZ", _candidate(recommended_as="anchor"))  # no bounds key

    await engine._run_candidate_ticker_recheck()

    assert engine.candidates.get("ZZZZ")["recommended_as"] == "tradeable"
