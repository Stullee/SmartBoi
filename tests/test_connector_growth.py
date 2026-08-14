"""Connector growth: admitting tradeables that revive an inert anchor
(engine._grow_connectors / _review_probation).

The universe grew anchors well and tradeables barely at all, because discovery
is filing extraction and filing extraction runs upward -- a small company
discloses its big customers, so what a tradeable's 10-K yields is more anchors.
Live, that left 59 of 160 anchors with no edge to any trade target, and their
news reaching nothing.

This arm is the mirror of reconcile_universe_connectivity's GROW arm: admit a
candidate because it would connect an INERT ANCHOR. What makes it safe to run
unattended is that admission is to PROBATION -- the symbol is polled and
analysed but cannot open a position until its own 10-K discloses the
relationship that got it admitted. These cover both halves: that the arm admits
only what it should, and that probation actually withholds trading rights.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from smartboi.config import Settings
from smartboi.engine import Engine
from smartboi.graph import Relationship

from tests.fakes import FakeEdgarClient, FakeFinnhub


def _candidate(ticker="ZZZZ", name="Some Supplier Inc", related_to=("AMZN",),
               rel_types=("supplier",), recommended_as="tradeable", researched_only=True):
    return {
        "name": name, "ticker": ticker, "related_to": list(related_to),
        "rel_types": list(rel_types), "description": "disclosed supplier",
        "sources": ["https://example.com/x"], "seen_count": 1,
        "first_seen_at": "2026-07-01T00:00:00+00:00",
        "recommended_as": recommended_as, "recommendation_reason": "fits the profile",
        "researched_only": researched_only,
    }


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # DCO is the only tradeable and AMZN the only anchor, with no edge between
    # them -- so AMZN starts inert, which is the whole precondition.
    settings = Settings(
        _env_file=None, symbols="DCO", anchor_symbols="AMZN",
        enable_dashboard=False, enable_universe_autoscreen=False,
    )
    e = Engine(settings)
    e.edgar_client = FakeEdgarClient()
    e.finnhub = FakeFinnhub()
    return e


def _edge(frm, to, rel_type="supplier"):
    return Relationship(from_symbol=frm, to_symbol=to, rel_type=rel_type,
                        description="d", source="https://sec.gov/x", confidence=0.9)


# --- The want-list ---

def test_an_anchor_with_no_tradeable_edge_is_inert(engine):
    assert engine.inert_anchors() == {"AMZN"}


def test_an_edge_to_a_tradeable_revives_an_anchor(engine):
    engine.graph.relationships.append(_edge("DCO", "AMZN"))
    assert engine.inert_anchors() == set()


def test_a_probationary_symbol_does_not_revive_an_anchor(engine):
    """Otherwise opening a probation would retire its own anchor from the
    want-list, and a reversion would leave it inert again with nothing having
    been tried in the meantime."""
    engine.candidates.set("ZZZZ", _candidate())
    engine.accept_candidate("ZZZZ", "tradeable", source="connector", probationary=True)
    engine.graph.relationships.append(_edge("ZZZZ", "AMZN"))
    assert engine.inert_anchors() == {"AMZN"}


# --- Admission ---

async def test_a_candidate_that_would_connect_an_inert_anchor_is_admitted(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    spec = engine.spec_by_symbol["ZZZZ"]
    assert spec.signal_source_only is False
    assert spec.probationary is True
    assert engine.probation_state.get("ZZZZ")["anchors"] == ["AMZN"]


async def test_admission_does_not_grant_trading_rights(engine):
    """The entire safety property, at the one chokepoint every entry path
    runs through."""
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    assert engine._is_tradeable("ZZZZ") is False
    assert engine._is_tradeable("DCO") is True


async def test_a_candidate_connecting_only_a_LIVE_anchor_is_not_admitted(engine):
    """This arm exists to shrink the inert count. A name that attaches to an
    already-connected anchor is ordinary auto-accept's business, not this."""
    engine.graph.relationships.append(_edge("DCO", "AMZN"))  # AMZN now live
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_an_unverified_name_is_never_admitted(engine):
    """The Advantest->ATRO class, and worse on this side than on the anchor
    side because the endpoint is a trade target. Live, this is what stops
    "xAI" being admitted as XFLT, a closed-end fund."""
    engine.edgar_client.name_matches = False
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_a_candidate_that_screens_as_an_anchor_is_not_admitted(engine):
    """The arm may not lower the market-cap/analyst screen -- otherwise it
    becomes a route for adding mega-caps as trade targets."""
    engine.candidates.set("ZZZZ", _candidate(recommended_as="anchor"))
    await engine._grow_connectors()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_an_unscreened_candidate_is_not_admitted(engine):
    engine.candidates.set("ZZZZ", _candidate(recommended_as=None))
    await engine._grow_connectors()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_a_competitor_only_relationship_is_not_admitted(engine):
    """Two rivals do not move on each other's contract wins the way a supplier
    does. Live, this filter is what excludes an investment bank admitted for
    acting as a placement agent."""
    engine.candidates.set("ZZZZ", _candidate(rel_types=("competitor",)))
    await engine._grow_connectors()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_a_quarantined_symbol_is_not_readmitted(engine):
    engine.quarantine.set("ZZZZ", {"reasons": ["name_mismatch"]})
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_admission_is_capped_per_day(engine):
    engine.settings.connector_max_per_day = 1
    engine.candidates.set("AAAA", _candidate(ticker="AAAA"))
    engine.candidates.set("BBBB", _candidate(ticker="BBBB"))
    await engine._grow_connectors()
    assert len(engine.probation_state.data) == 1
    await engine._grow_connectors()
    assert len(engine.probation_state.data) == 1, "the daily cap must survive a re-run"


async def test_the_daily_cap_survives_a_restart(engine, tmp_path, monkeypatch):
    """An in-memory counter would hand out a fresh day's worth of admissions on
    every restart, and this deployment restarts several times a day."""
    engine.settings.connector_max_per_day = 1
    engine.candidates.set("AAAA", _candidate(ticker="AAAA"))
    await engine._grow_connectors()
    assert engine.connector_state.get("count") == 1

    revived = Engine(engine.settings)
    revived.edgar_client = FakeEdgarClient()
    assert revived.connector_state.get("count") == 1


async def test_concurrent_probations_are_capped(engine):
    engine.settings.connector_max_probationary = 1
    engine.candidates.set("AAAA", _candidate(ticker="AAAA"))
    engine.candidates.set("BBBB", _candidate(ticker="BBBB"))
    await engine._grow_connectors()
    assert len(engine.probation_state.data) == 1


async def test_growth_can_be_switched_off(engine):
    engine.settings.enable_connector_growth = False
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    assert "ZZZZ" not in engine.spec_by_symbol


# --- Leaving probation ---

async def test_a_filing_disclosed_edge_promotes_to_a_full_tradeable(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    # What the relationship backfill would write once ZZZZ's own 10-K is read.
    engine.graph.relationships.append(_edge("ZZZZ", "AMZN"))

    await engine._review_probation()

    assert "ZZZZ" not in engine.probation_state.data
    assert engine.spec_by_symbol["ZZZZ"].probationary is False
    assert engine._is_tradeable("ZZZZ") is True
    assert engine.inert_anchors() == set(), "AMZN is revived, which was the point"


async def test_an_unconfirmed_probation_is_dropped_when_it_times_out(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    stale = (datetime.now(timezone.utc)
             - timedelta(days=engine.settings.connector_probation_days + 1)).isoformat()
    engine.probation_state.set("ZZZZ", {**engine.probation_state.get("ZZZZ"),
                                        "admitted_at": stale})

    await engine._review_probation()

    assert "ZZZZ" not in engine.probation_state.data
    assert "ZZZZ" not in engine.spec_by_symbol
    assert "ZZZZ" not in engine.accepted_candidates.data
    # Dropped, not blacklisted: it stays a candidate a human can accept.
    assert engine.candidates.get("ZZZZ")["connector_unconfirmed_at"]


async def test_a_probation_inside_its_window_is_left_alone(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    await engine._review_probation()
    assert "ZZZZ" in engine.probation_state.data
    assert engine._is_tradeable("ZZZZ") is False


async def test_an_edge_to_an_unrelated_anchor_does_not_promote(engine):
    """Promotion is earned for the anchor the admission was made for, not for
    any edge at all -- otherwise a single unrelated disclosure buys trading
    rights that nothing has confirmed."""
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    engine.graph.relationships.append(_edge("ZZZZ", "SOMEONE-ELSE"))
    await engine._review_probation()
    assert "ZZZZ" in engine.probation_state.data
    assert engine._is_tradeable("ZZZZ") is False


async def test_an_unparseable_admission_stamp_does_not_make_a_probation_immortal(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()
    engine.probation_state.set("ZZZZ", {**engine.probation_state.get("ZZZZ"),
                                        "admitted_at": "not-a-date"})
    await engine._review_probation()
    assert "ZZZZ" not in engine.probation_state.data


async def test_probation_survives_a_restart(engine):
    """The one thing that must not be lost across a restart: a restart that
    forgot probation would silently promote every open one into a full trade
    target, which is exactly what this mechanism exists to prevent."""
    engine.candidates.set("ZZZZ", _candidate())
    await engine._grow_connectors()

    revived = Engine(engine.settings)
    assert revived.spec_by_symbol["ZZZZ"].probationary is True
    assert revived._is_tradeable("ZZZZ") is False
