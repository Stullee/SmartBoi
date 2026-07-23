"""Engine-level integration tests using scripted fakes (see fakes.py) --
covers the documented invariants pure-module unit tests can't reach: the
retry/registration semantics around a deferred LLM call, the propagation
cooldown's definitive-only recording, and the full signal -> snapshot ->
open -> close -> reset lifecycle."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from smartboi.config import Settings
from smartboi.edgar import FilingEvent
from smartboi.engine import Engine
from smartboi.graph import Relationship

from tests.fakes import (
    FakeEdgarClient,
    FakeExtractor,
    FakeFinnhub,
    FakePriceFeed,
    FakeSkeptic,
    FakeUpdater,
    proposal,
    verdict,
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A fully-constructed Engine with every optional integration wired to
    a fake -- isolated in tmp_path (data/, logs/ are relative paths in the
    real engine) so tests never touch the real repo's data/logs
    directories, and never make a real network/LLM call."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, symbols="FORM,UCTT", anchor_symbols="INTC",
        signal_confidence_threshold=0.5, min_independent_sources=2,
        enable_relationship_backfill=False, enable_universe_autoscreen=False,
        enable_dashboard=False, max_propagated_evidence_per_link=1,
        propagated_evidence_cooldown_hours=6,
    )
    e = Engine(settings)
    e.edgar_client = FakeEdgarClient()
    e.finnhub = FakeFinnhub()
    e.extractor = FakeExtractor()
    e.updater = FakeUpdater()
    e.skeptic = FakeSkeptic()
    e.price_feed = FakePriceFeed()
    return e


# --- Invariant: relationship extraction falls back to Finnhub's fuzzy
# search when EDGAR's strict SEC-title match can't resolve a ticker ---

async def test_relationship_extraction_falls_back_to_finnhub_search(engine):
    filing = FilingEvent(
        symbol="FORM", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000001", primary_document="form.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Some Uncommon Co", "counterparty_ticker": None,
        "rel_type": "customer", "description": "our largest customer, Some Uncommon Co",
        "confidence": 0.9, "quote": "our largest customer, Some Uncommon Co",
    }]
    # FakeEdgarClient.find_ticker_by_name always returns None -- the fallback
    # is the only path that can resolve this one.
    engine.finnhub.ticker_by_name["Some Uncommon Co"] = "ZZZZ"

    await engine._extract_relationships("FORM", filing, "filing text")

    candidates = list(engine.candidates.data.values())
    assert any(c.get("ticker") == "ZZZZ" for c in candidates)


# --- Invariant: evidence is registered only on definitive handling ---

async def test_deferred_updater_is_not_definitive(engine):
    engine.updater.default = None  # simulates budget exhaustion / a transient failure
    handled = await engine._process_evidence(
        origin_symbol="FORM", evidence_text="some news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    assert handled is False


async def test_refuted_evidence_is_definitive_but_does_not_merge(engine):
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=True)
    handled = await engine._process_evidence(
        origin_symbol="FORM", evidence_text="some news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    assert handled is True
    dossier = engine.dossiers.load("FORM")
    assert dossier.evidence == []  # refuted: handled, but nothing merged


async def test_accepted_evidence_is_definitive_and_merges(engine):
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=False)
    handled = await engine._process_evidence(
        origin_symbol="FORM", evidence_text="some news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    assert handled is True
    dossier = engine.dossiers.load("FORM")
    assert len(dossier.evidence) == 1


# --- Invariant: a budget-deferred skeptic call reuses the cached proposal
# instead of re-buying propose_update on retry ---

async def test_retry_after_deferred_skeptic_does_not_repropose(engine):
    engine.updater.queue(proposal())
    engine.skeptic.queue(None)  # first attempt: skeptic call itself deferred

    first = await engine._update_dossier(
        "FORM", "evidence text", "FORM", "", None, "news", "reuters.com",
        "https://x/1", "h1", "2026-07-23",
    )
    assert first is False
    assert len(engine.updater.calls) == 1

    engine.skeptic.queue(verdict(refuted=False))  # retry: skeptic now answers
    second = await engine._update_dossier(
        "FORM", "evidence text", "FORM", "", None, "news", "reuters.com",
        "https://x/1", "h1", "2026-07-23",
    )
    assert second is True
    # The cached proposal from the first attempt was reused -- propose_update
    # was never called a second time for the same evidence.
    assert len(engine.updater.calls) == 1


# --- Invariant: the propagation cooldown slot is only consumed once a
# target's evidence is DEFINITIVELY handled, never on a deferred attempt ---

async def test_propagation_slot_not_consumed_on_deferred_attempt(engine):
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, "2026-07-23"))
    engine.updater.default = proposal()
    engine.skeptic.default = None  # every attempt against FORM is deferred

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="Intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    now = time.monotonic()
    assert engine._propagation_limiter.would_allow("INTC->FORM", now)


async def test_propagation_slot_consumed_once_definitively_handled(engine):
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, "2026-07-23"))
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=True)  # definitively refused -- still "handled"

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="Intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    # max_propagated_evidence_per_link=1 in the fixture -- one definitive
    # propagation to FORM should have fully consumed the cooldown slot.
    now = time.monotonic()
    assert not engine._propagation_limiter.would_allow("INTC->FORM", now)


# --- Full lifecycle: signal -> snapshot -> open -> close -> reset ---

async def test_full_lifecycle_signal_open_close_reset(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)

    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="evidence 1", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    dossier = engine.dossiers.load("FORM")
    assert dossier.status == "ACTIVE"  # only 1 independent source so far -- not enough to signal

    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="evidence 2", source_type="news",
        source_name="bloomberg.com", url="https://x/2", headline="h2", published_at="2026-07-23",
    )
    dossier = engine.dossiers.load("FORM")
    assert dossier.status == "SIGNALED"
    assert dossier.signaled_price == 10.0  # snapshotted against the fake price feed

    signals_log = Path(engine.settings.log_dir) / "signals.jsonl"
    assert signals_log.exists()
    assert len(signals_log.read_text().splitlines()) == 1

    # Open: price hasn't moved (0% drift), no deadline expiry yet.
    await engine._mark_and_execute()
    assert engine.journal.has_open("FORM")
    trade = engine.journal.open_trades["FORM"]
    assert trade.entry_price == 10.0

    # Close: move price to the take-profit target and mark again.
    engine.price_feed.prices["FORM"] = trade.target_price + 0.01
    await engine._mark_and_execute()
    assert not engine.journal.has_open("FORM")

    dossier = engine.dossiers.load("FORM")
    assert dossier.status == "ACTIVE"
    assert dossier.signaled_at == ""
    assert dossier.signaled_price is None
