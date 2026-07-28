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
from smartboi.news import NewsArticle

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


# --- Invariant: news source identity is the real publisher (P0 fix), not
# Finnhub's own article-URL domain -- every free-tier article URL points at
# finnhub.io itself, so using the URL domain collapsed every single article
# onto one source identity and independent_source_count could never exceed
# 1 no matter how many distinct publishers actually covered a story. ---

async def test_distinct_publishers_count_as_independent_sources(engine):
    engine.finnhub.articles_by_symbol["FORM"] = [
        NewsArticle(symbol="FORM", headline="Headline A", summary="s", source="Reuters",
                    url="https://finnhub.io/api/news/1", published_at="2026-07-23T00:00:00+00:00"),
        NewsArticle(symbol="FORM", headline="Headline B", summary="s", source="Bloomberg",
                    url="https://finnhub.io/api/news/2", published_at="2026-07-23T00:00:00+00:00"),
    ]
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    await engine._poll_news()

    dossier = engine.dossiers.load("FORM")
    assert dossier.independent_source_count == 2
    assert {e.source_name for e in dossier.evidence} == {"Reuters", "Bloomberg"}


async def test_same_headline_syndication_still_collapses_to_one_source(engine):
    engine.finnhub.articles_by_symbol["FORM"] = [
        NewsArticle(symbol="FORM", headline="Same Headline", summary="s", source="Reuters",
                    url="https://finnhub.io/api/news/1", published_at="2026-07-23T00:00:00+00:00"),
        # A different publisher syndicating the exact same story, same day --
        # the dedup FINGERPRINT (symbol:normalized_headline:date) is what
        # collapses this, deliberately independent of source identity.
        NewsArticle(symbol="FORM", headline="Same Headline", summary="s", source="Yahoo",
                    url="https://finnhub.io/api/news/2", published_at="2026-07-23T00:00:00+00:00"),
    ]
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    await engine._poll_news()

    dossier = engine.dossiers.load("FORM")
    assert dossier.independent_source_count == 1
    assert len(dossier.evidence) == 1


# --- Heartbeat: a periodic INFO line so an idle-but-healthy engine is
# distinguishable in the log from a hung one ---

def test_log_heartbeat_does_not_raise_and_logs(engine, caplog):
    with caplog.at_level("INFO"):
        engine._log_heartbeat()
    assert any("heartbeat" in r.message for r in caplog.records)


# --- Invariant: the daily snapshot/price-marks passes are scheduled off a
# PERSISTED wall-clock timestamp, not a process-local timer -- a restart
# must not re-trigger an immediately-due pass and write a duplicate batch.

def test_daily_pass_due_on_first_ever_run(engine):
    assert engine._daily_pass_due("dossier_snapshot") is True


def test_daily_pass_not_due_right_after_marking_done(engine):
    engine._mark_daily_pass_done("dossier_snapshot")
    assert engine._daily_pass_due("dossier_snapshot") is False


def test_daily_pass_due_state_survives_a_restart(tmp_path, monkeypatch):
    # Simulates a restart: a second Engine instance constructed against the
    # same on-disk data/ directory must see the first instance's persisted
    # "already ran today" state, not start fresh (which would be exactly
    # the process-local-timer bug this replaces).
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, symbols="FORM", enable_dashboard=False)
    first = Engine(settings)
    first._mark_daily_pass_done("dossier_snapshot")

    second = Engine(settings)
    assert second._daily_pass_due("dossier_snapshot") is False


def test_daily_pass_due_keys_are_independent(engine):
    engine._mark_daily_pass_done("dossier_snapshot")
    assert engine._daily_pass_due("price_marks") is True


# --- Invariant: SEED_RELATIONSHIPS only ever seeds edges between symbols
# actually in the LIVE (possibly custom SYMBOLS/ANCHOR_SYMBOLS) universe --
# a custom deployment must never get default-universe edges for companies
# it never configured. ---

async def test_seed_graph_skips_relationships_outside_a_custom_universe(engine):
    # The fixture's universe (FORM, UCTT, INTC) doesn't fully contain any
    # SEED_RELATIONSHIPS pair (UCTT's seeded counterparties are AMAT/LRCX,
    # neither configured here) -- nothing should be seeded.
    engine._seed_graph()
    assert engine.graph.relationships == []


async def test_seed_graph_seeds_relationships_fully_inside_the_universe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, symbols="UCTT", anchor_symbols="AMAT", enable_dashboard=False)
    e = Engine(settings)
    e._seed_graph()
    assert any(r.from_symbol == "UCTT" and r.to_symbol == "AMAT" for r in e.graph.relationships)


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


# --- Invariant: a relationship whose rel_type isn't one of graph.REL_TYPES
# is dropped outright -- never written to the graph, never recorded as a
# candidate (the extraction tool schema declares an enum, but Anthropic
# tool use doesn't hard-enforce it) ---

async def test_extract_relationships_drops_invalid_rel_type(engine):
    filing = FilingEvent(
        symbol="FORM", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000001", primary_document="form.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "UCTT", "counterparty_ticker": "UCTT",
        "rel_type": "partner", "description": "a bogus rel_type outside the enum",
        "confidence": 0.9, "quote": "q",
    }]

    await engine._extract_relationships("FORM", filing, "filing text")

    assert engine.graph.relationships == []
    assert engine.candidates.data == {}


async def test_second_discovery_with_a_resolved_ticker_merges_the_orphan(engine):
    # First mention: no ticker resolves -- stored under the name key.
    filing = FilingEvent(
        symbol="FORM", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000001", primary_document="form.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Some Uncommon Co", "counterparty_ticker": None,
        "rel_type": "customer", "description": "our largest customer, Some Uncommon Co",
        "confidence": 0.9, "quote": "our largest customer, Some Uncommon Co",
    }]
    await engine._extract_relationships("FORM", filing, "filing text")
    assert engine.candidates.get("SOME UNCOMMON CO") is not None

    # Second mention (a different filing/symbol): this time a ticker
    # resolves -- must merge into the ticker key, not orphan the first.
    engine.extractor.default = [{
        "counterparty_name": "Some Uncommon Co", "counterparty_ticker": "ZZZZ",
        "rel_type": "supplier", "description": "our supplier, Some Uncommon Co",
        "confidence": 0.8, "quote": "our supplier, Some Uncommon Co",
    }]
    await engine._extract_relationships("UCTT", filing, "filing text")

    assert "SOME UNCOMMON CO" not in engine.candidates.data
    merged = engine.candidates.get("ZZZZ")
    assert merged is not None
    assert merged["seen_count"] == 2
    assert set(merged["related_to"]) == {"FORM", "UCTT"}


# --- Invariant: the daily candidate recheck resolves tickers for
# previously-unresolved candidates (re-keying name -> ticker, merging with
# any existing ticker-keyed entry) and recommends tradeable/anchor ---

def _seed_candidate(engine, key, **overrides):
    entry = {
        "name": "Some Uncommon Co", "ticker": "", "related_to": ["FORM"],
        "rel_types": ["customer"], "description": "d", "sources": [],
        "seen_count": 3, "first_seen_at": "2026-07-01T00:00:00+00:00",
    }
    entry.update(overrides)
    engine.candidates.set(key, entry)
    return entry


async def test_candidate_recheck_resolves_via_edgar_and_rekeys(engine):
    _seed_candidate(engine, "SOME UNCOMMON CO")
    engine.edgar_client.ticker_by_name["Some Uncommon Co"] = "ZZZZ"

    await engine._run_candidate_ticker_recheck()

    assert "SOME UNCOMMON CO" not in engine.candidates.data
    resolved = engine.candidates.get("ZZZZ")
    assert resolved is not None
    assert resolved["ticker"] == "ZZZZ"
    assert resolved["seen_count"] == 3


async def test_candidate_recheck_falls_back_to_finnhub(engine):
    _seed_candidate(engine, "SOME UNCOMMON CO")
    engine.finnhub.ticker_by_name["Some Uncommon Co"] = "ZZZZ"

    await engine._run_candidate_ticker_recheck()

    assert engine.candidates.get("ZZZZ")["ticker"] == "ZZZZ"


async def test_candidate_recheck_merges_into_existing_ticker_keyed_entry(engine):
    _seed_candidate(engine, "SOME UNCOMMON CO", seen_count=3, related_to=["FORM"])
    engine.candidates.set("ZZZZ", {
        "name": "Some Uncommon Co", "ticker": "ZZZZ", "related_to": ["UCTT"],
        "rel_types": ["supplier"], "description": "d2", "sources": [],
        "seen_count": 5, "first_seen_at": "2026-06-01T00:00:00+00:00",
        "last_seen_at": "2026-06-01T00:00:00+00:00",
    })
    engine.edgar_client.ticker_by_name["Some Uncommon Co"] = "ZZZZ"

    await engine._run_candidate_ticker_recheck()

    assert "SOME UNCOMMON CO" not in engine.candidates.data
    merged = engine.candidates.get("ZZZZ")
    assert merged["seen_count"] == 8  # 3 + 5, no data lost from either discovery path
    assert set(merged["related_to"]) == {"FORM", "UCTT"}


async def test_candidate_recheck_leaves_unresolvable_candidates_alone(engine):
    _seed_candidate(engine, "SOME UNCOMMON CO")

    await engine._run_candidate_ticker_recheck()

    assert engine.candidates.get("SOME UNCOMMON CO") is not None
    assert engine.candidates.get("SOME UNCOMMON CO")["ticker"] == ""


async def test_candidate_recheck_recommends_tradeable_for_small_cap(engine):
    _seed_candidate(engine, "ZZZZ", ticker="ZZZZ", name="Small Cap Co")
    engine.finnhub.market_cap_by_symbol["ZZZZ"] = 500.0
    engine.finnhub.analyst_count_by_symbol["ZZZZ"] = 2

    await engine._run_candidate_ticker_recheck()

    entry = engine.candidates.get("ZZZZ")
    assert entry["recommended_as"] == "tradeable"
    assert entry["recommendation_reason"]


async def test_candidate_recheck_recommends_anchor_for_large_cap(engine):
    _seed_candidate(engine, "ZZZZ", ticker="ZZZZ", name="Giant Co")
    engine.finnhub.market_cap_by_symbol["ZZZZ"] = 500_000.0

    await engine._run_candidate_ticker_recheck()

    assert engine.candidates.get("ZZZZ")["recommended_as"] == "anchor"


async def test_candidate_recheck_skips_recommendation_for_accepted_candidates(engine):
    _seed_candidate(engine, "ZZZZ", ticker="ZZZZ", name="Already Added Co")
    engine.accepted_candidates.set("ZZZZ", "tradeable")
    engine.finnhub.market_cap_by_symbol["ZZZZ"] = 500.0

    await engine._run_candidate_ticker_recheck()

    assert "recommended_as" not in engine.candidates.get("ZZZZ")


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
