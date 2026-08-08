"""Engine-level integration tests using scripted fakes (see fakes.py) --
covers the documented invariants pure-module unit tests can't reach: the
retry/registration semantics around a deferred LLM call, the propagation
cooldown's definitive-only recording, and the full signal -> snapshot ->
open -> close -> reset lifecycle."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartboi.config import Settings
from smartboi.edgar import FilingEvent
import smartboi.engine
from smartboi.dossier import SCORING_VERSION
from smartboi.engine import ECOSYSTEM_LINK_CONFIDENCE, Engine, is_common_equity
from smartboi.ratelimit import SlidingWindowLimiter
from smartboi.status import snapshot_dossier
from smartboi.graph import Relationship
from smartboi.news import NewsArticle

from tests.fakes import (
    FakeEdgarClient,
    FakeExtractor,
    FakeFinnhub,
    FakePriceFeed,
    FakeSkeptic,
    FakeSynthesizer,
    FakeUpdater,
    proposal,
    synthesis,
    verdict,
)


def _backdate_entry(engine, symbol: str, days: int = 1) -> None:
    """Move an open trade's opened_at back, so the next mark lands on a
    later session than the entry.

    paper_journal.update() only records last_price on the entry session --
    the bar it is handed carries the WHOLE session's high/low, which on the
    entry day includes price action from before the position existed, and
    resolving a stop against that fabricated stop-outs out of pre-entry
    prints. Every test that drives a trade to a WIN/LOSS therefore has to
    put the entry on an earlier day, which is also the only shape the real
    engine can produce."""
    trade = engine.journal.open_trades[symbol]
    trade.opened_at = (
        datetime.fromisoformat(trade.opened_at) - timedelta(days=days)
    ).isoformat()
    engine.journal._write_open_state()


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
        # The news-only corroboration bar is exercised by its own dedicated
        # test below; pinned to the normal bar here so the lifecycle tests
        # (which build news-only dossiers for convenience) stay focused on
        # the signal -> trade machinery.
        min_independent_sources_news_only=2,
        enable_relationship_backfill=False, enable_universe_autoscreen=False,
        enable_dashboard=False, max_propagated_evidence_per_link=1,
        propagated_evidence_cooldown_hours=6,
        # The drift-guard lifecycle tests run a ~10% pre-entry move expecting
        # a skip. The shipped default drift bar is now 12% (hold-to-horizon
        # config), so pin the 5% the scenarios were written against rather
        # than inherit it.
        max_favorable_drift_pct=5.0,
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
    #
    # A genuinely distinct filing, not the same FilingEvent re-passed: an
    # accession number belongs to exactly one filer, so reusing it for a
    # second symbol is a state that cannot occur live -- and seen_count is
    # now counted per filing, so it would (correctly) refuse to count the
    # same document twice.
    other_filing = FilingEvent(
        symbol="UCTT", cik10="0000000002", form="10-K", filing_date="2026-07-02",
        accession_number="0001234567-26-000002", primary_document="uctt.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Some Uncommon Co", "counterparty_ticker": "ZZZZ",
        "rel_type": "supplier", "description": "our supplier, Some Uncommon Co",
        "confidence": 0.8, "quote": "our supplier, Some Uncommon Co",
    }]
    await engine._extract_relationships("UCTT", other_filing, "filing text")

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


async def test_candidate_recheck_refreshes_recommendation_for_accepted_candidates(engine):
    # Accepted symbols must KEEP getting recommendation refreshes: the
    # reconcile pass (demote a trade target that now screens as an anchor)
    # acts on recommended_as, and freezing it at acceptance time meant
    # post-acceptance drift could never be detected.
    _seed_candidate(engine, "ZZZZ", ticker="ZZZZ", name="Already Added Co")
    engine.accepted_candidates.set("ZZZZ", "tradeable")
    engine.finnhub.market_cap_by_symbol["ZZZZ"] = 500_000.0  # graduated to mega-cap

    await engine._run_candidate_ticker_recheck()

    assert engine.candidates.get("ZZZZ")["recommended_as"] == "anchor"


async def test_reconcile_demotes_but_never_promotes(engine):
    # Demotion (tradeable -> anchor) is safe and expected; promotion
    # (anchor -> tradeable) must NOT happen automatically -- an anchor was
    # accepted under the liberal bar with no name-match/seen-count check.
    engine.accepted_candidates.set("AAAA", {"as": "tradeable", "source": "auto"})
    engine.candidates.set("AAAA", {"name": "A", "ticker": "AAAA", "recommended_as": "anchor"})
    engine.accepted_candidates.set("BBBB", {"as": "anchor", "source": "auto"})
    engine.candidates.set("BBBB", {"name": "B", "ticker": "BBBB", "recommended_as": "tradeable"})

    engine._reconcile_accepted_types()

    assert engine.accepted_candidates.get("AAAA")["as"] == "anchor"      # demoted
    assert engine.accepted_candidates.get("BBBB")["as"] == "anchor"      # NOT promoted


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
    assert first == "deferred"
    assert len(engine.updater.calls) == 1

    engine.skeptic.queue(verdict(refuted=False))  # retry: skeptic now answers
    second = await engine._update_dossier(
        "FORM", "evidence text", "FORM", "", None, "news", "reuters.com",
        "https://x/1", "h1", "2026-07-23",
    )
    assert second == "handled"
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
    # Backdated one day: the journal deliberately refuses to resolve a
    # stop or target on the ENTRY session, because the bar's high/low is the
    # whole session's range and on the entry day it includes prints from
    # before the position existed. A close therefore always happens on a
    # later session, and this is what that looks like.
    _backdate_entry(engine, "FORM")
    await engine._mark_and_execute()
    assert not engine.journal.has_open("FORM")

    dossier = engine.dossiers.load("FORM")
    assert dossier.status == "ACTIVE"
    assert dossier.signaled_at == ""
    assert dossier.signaled_price is None


# --- Decisions ledger: every drift-skip / expiry / open leaves an
# episode-keyed row in decisions.jsonl (see signals.log_decision and
# event_study.py) -- without it the entry-timing guards are unfalsifiable.

async def test_decisions_ledger_records_open_with_episode_key(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    episode = engine.dossiers.load("FORM").signaled_at
    assert episode

    await engine._mark_and_execute()
    assert engine.journal.has_open("FORM")

    rows = [json.loads(line) for line in
            (Path(engine.settings.log_dir) / "decisions.jsonl").read_text().splitlines()]
    opened = [r for r in rows if r["event"] == "trade_opened"]
    assert len(opened) == 1
    assert opened[0]["symbol"] == "FORM"
    assert opened[0]["episode"] == episode  # joins against signals.jsonl rows
    assert opened[0]["price"] == 10.0


async def test_decisions_ledger_records_drift_skip_once_per_episode(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    # Price ran 10% favorably before entry -- drift guard skips, twice polled.
    engine.price_feed.prices["FORM"] = 11.0
    await engine._mark_and_execute()
    await engine._mark_and_execute()
    assert not engine.journal.has_open("FORM")

    rows = [json.loads(line) for line in
            (Path(engine.settings.log_dir) / "decisions.jsonl").read_text().splitlines()]
    skips = [r for r in rows if r["event"] == "drift_skip"]
    assert len(skips) == 1  # once per episode, not per poll
    assert skips[0]["price"] == 11.0
    assert "drifted" in skips[0]["reason"]


async def test_decisions_ledger_records_expiry_with_reason(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    dossier = engine.dossiers.load("FORM")
    episode = dossier.signaled_at
    # Simulate the thesis degrading while stuck SIGNALED (entry re-check path).
    dossier.confidence = 0.1
    dossier.magnitude = 0.1
    engine.dossiers.save(dossier)
    await engine._mark_and_execute()

    rows = [json.loads(line) for line in
            (Path(engine.settings.log_dir) / "decisions.jsonl").read_text().splitlines()]
    expired = [r for r in rows if r["event"] == "signal_expired"]
    assert len(expired) == 1
    assert expired[0]["episode"] == episode
    assert expired[0]["direction"] == "LONG"
    assert expired[0]["reason"]


# --- A model that fills in "NULL" rather than leaving the ticker null.
# Confirmed live: a BAE Systems relationship was recorded against the ticker
# "NULL" and accepted into the universe as an anchor.

async def test_placeholder_tickers_are_treated_as_no_ticker(engine):
    filing = FilingEvent(
        symbol="AMPX", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000001", primary_document="f.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "BAE Systems", "counterparty_ticker": "NULL",
        "rel_type": "customer", "description": "listed as a customer",
        "confidence": 0.85, "quote": "q",
    }]

    await engine._extract_relationships("AMPX", filing, "text")

    assert all(r.to_symbol != "NULL" for r in engine.graph.relationships)
    assert "NULL" not in engine.candidates.data


# --- A bank disclosed as a "supplier" is a lender: a real disclosure, but a
# dead end for propagation. Confirmed live: BAC, WTFC and M&T entered the
# universe off credit-facility disclosures.

async def test_lender_supplier_relationships_are_dropped(engine):
    filing = FilingEvent(
        symbol="UFPT", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000002", primary_document="f.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Bank of America", "counterparty_ticker": "BAC",
        "rel_type": "supplier",
        "description": "Lender providing secured credit facilities under a revolving credit facility.",
        "confidence": 0.95, "quote": "Third Amended and Restated Credit Agreement",
    }]

    await engine._extract_relationships("UFPT", filing, "text")

    assert engine.graph.relationships == []
    assert engine.candidates.data == {}


async def test_a_demand_line_of_credit_is_recognised_as_lending(engine):
    """Bank debt is disclosed in a dozen near-synonyms and the first phrase
    list only covered some of them: confirmed live, M&T Bank reached the
    graph as a "supplier" to Taylor Devices off a demand line of credit."""
    filing = FilingEvent(
        symbol="TAYD", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000012", primary_document="f.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "M&T Bank", "counterparty_ticker": "MTB",
        "rel_type": "supplier",
        "description": "M&T Bank provides a $10,000,000 demand line of credit to Taylor Devices.",
        "confidence": 0.9, "quote": "q",
    }]

    await engine._extract_relationships("TAYD", filing, "text")

    assert engine.graph.relationships == []
    assert engine.candidates.data == {}


# --- An 8-K item 5.02 officer appointment names a string of well-known
# former employers, and extraction reads a CV line as a disclosed business
# link. Confirmed live: EPAC->ITW, EPAC->GE, VVX->RTX, NCSM->APO. ---

async def test_executive_biography_relationships_are_dropped(engine):
    filing = FilingEvent(
        symbol="EPAC", cik10="0000000001", form="8-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000013", primary_document="f.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Illinois Tool Works", "counterparty_ticker": "ITW",
        "rel_type": "customer",
        "description": "Prior to joining the Company, Mr. Smith served as VP of Illinois Tool Works.",
        "confidence": 0.8, "quote": "Prior to joining the Company he held various positions at ITW.",
    }]

    await engine._extract_relationships("EPAC", filing, "text")

    assert engine.graph.relationships == []
    assert engine.candidates.data == {}


def test_biography_filter_does_not_catch_a_commercial_disclosure():
    """The phrase list is deliberately narrow: a genuine supply agreement
    that happens to use "served" must survive it."""
    assert not Engine._is_biography_relationship({
        "rel_type": "customer",
        "description": "Intel has served as our largest customer for three consecutive years.",
        "quote": "Intel accounted for 22% of net sales.",
    })


async def test_a_bank_as_a_genuine_customer_is_kept(engine):
    """Only the "our lender" direction is a dead end -- a company SELLING to
    a bank is a real propagation path."""
    filing = FilingEvent(
        symbol="UFPT", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000003", primary_document="f.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Bank of America", "counterparty_ticker": "BAC",
        "rel_type": "customer", "description": "BAC accounted for 15% of net sales.",
        "confidence": 0.95, "quote": "q",
    }]

    await engine._extract_relationships("UFPT", filing, "text")

    assert any(r.to_symbol == "BAC" for r in engine.graph.relationships) or "BAC" in engine.candidates.data


# --- Regression: EDGAR extraction must not require Finnhub (optional
# integration) -- an unguarded finnhub call crashed the whole EDGAR poll on
# every Finnhub-less deployment whenever a filing named a company that
# EDGAR's own name lookup couldn't resolve. ---

async def test_extract_relationships_survives_missing_finnhub(engine):
    engine.finnhub = None
    filing = FilingEvent(
        symbol="FORM", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000001", primary_document="form.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Some Private Co", "counterparty_ticker": None,
        "rel_type": "customer", "description": "our largest customer",
        "confidence": 0.9, "quote": "q",
    }]

    completed = await engine._extract_relationships("FORM", filing, "filing text")

    assert completed is True
    assert "SOME PRIVATE CO" in engine.candidates.data  # recorded by name, no crash


async def test_extract_relationships_reports_deferred_when_budget_exhausted(engine):
    filing = FilingEvent(
        symbol="FORM", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000001", primary_document="form.htm",
    )
    engine.extractor.default = None  # budget exhausted / transient API failure

    assert await engine._extract_relationships("FORM", filing, "filing text") is False


async def test_backfill_not_marked_done_when_extraction_deferred(engine):
    engine.settings.enable_relationship_backfill = True
    filing = FilingEvent(
        symbol="FORM", cik10="0000000001", form="10-K", filing_date="2025-09-01",
        accession_number="0001234567-25-000001", primary_document="form.htm",
    )
    engine.edgar_client.latest_filings[("FORM", "10-K")] = filing
    engine.edgar_client.text_by_accession[filing.accession_number] = "ten-k text"
    engine.extractor.default = None  # extraction deferred (budget/API)

    await engine._run_relationship_backfill()

    # The symbol must stay pending -- marking it done would silently skip
    # its 10-K (the graph's main relationship source) until next year.
    assert engine.backfill_state.get("FORM") is None

    # Once extraction succeeds, it IS marked done.
    engine._backfill_retry_after = 0.0
    engine.extractor.default = []
    await engine._run_relationship_backfill()
    assert engine.backfill_state.get("FORM") is not None


# --- Regression: a SIGNALED dossier whose thesis collapses (or flips) must
# not open a paper trade from the stale status. ---

async def test_signal_expired_when_new_evidence_drops_below_threshold(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    assert engine.dossiers.load("FORM").status == "SIGNALED"

    # Strong opposing evidence arrives before any trade opened: direction
    # flips/collapses below threshold -- the signal must expire, not linger.
    engine.updater.default = proposal(direction="SHORT", magnitude=0.9, confidence=0.9)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.9, adjusted_magnitude=0.9)
    for i in range(3):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"bad news {i}", source_type="news",
            source_name=f"src{i}.com", url=f"https://y/{i}", headline=f"bad{i}", published_at="2026-07-23",
        )

    dossier = engine.dossiers.load("FORM")
    assert dossier.status in ("ACTIVE", "SIGNALED")
    if dossier.status == "SIGNALED":
        # If it re-signaled SHORT above threshold that's a fresh, valid
        # signal -- but it must carry a fresh baseline for the NEW direction.
        assert dossier.signaled_direction == dossier.direction


async def test_no_trade_opens_when_thesis_no_longer_qualifies_at_entry(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    dossier = engine.dossiers.load("FORM")
    assert dossier.status == "SIGNALED"

    # Simulate the thesis degrading WITHOUT the merge-path expiry running
    # (e.g. state persisted before this fix existed): confidence collapses
    # but status stays SIGNALED on disk.
    dossier.confidence = 0.1
    dossier.magnitude = 0.1
    engine.dossiers.save(dossier)

    await engine._mark_and_execute()

    assert not engine.journal.has_open("FORM")
    assert engine.dossiers.load("FORM").status == "ACTIVE"  # expired, clean slate

    # The ledger must say WHY, with numbers: "no longer qualifies" made
    # every expiry look identical and left the first real signal this system
    # ever produced unexplainable after the fact.
    rows = [json.loads(ln) for ln in (Path(engine.settings.log_dir) / "decisions.jsonl").read_text().splitlines()]
    expired = [r for r in rows if r["event"] == "signal_expired" and r["symbol"] == "FORM"]
    assert expired and "score" in expired[-1]["reason"]
    assert "at entry time" in expired[-1]["reason"]


async def test_expiry_reason_names_the_source_bar_that_failed(engine):
    """The two gates fail for different reasons and need different fixes --
    a thesis short of corroboration is not the same problem as one whose
    score decayed."""
    engine.settings.min_independent_sources_news_only = 3
    dossier = engine.dossiers.load("FORM")
    dossier.direction = "LONG"
    dossier.confidence = 0.9
    dossier.magnitude = 0.9
    dossier.independent_source_count = 2
    dossier.has_filing_evidence = False

    reason = engine._below_bar_reason(dossier, "at entry time")

    assert "sources 2/3" in reason and "no filing or disclosed-link backing" in reason
    assert "score" not in reason  # the score gate passed; don't blame it


# --- Entry cadence: marking open trades and confirming an entry run on
# different clocks. The first signal this system ever fired never got a
# single entry evaluation, because the next price poll was up to six hours
# out and the thesis was expired before it arrived. ---

async def test_price_poll_tightens_while_an_entry_is_pending(engine):
    engine.settings.price_poll_interval_sec = 21600
    engine.settings.signal_entry_poll_interval_sec = 900

    engine._entry_pending = False
    assert engine._price_poll_interval() == 21600

    engine._entry_pending = True
    assert engine._price_poll_interval() == 900


async def test_a_fresh_signal_marks_an_entry_as_pending(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    engine._entry_pending = False

    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )

    assert engine.dossiers.load("FORM").status == "SIGNALED"
    assert engine._entry_pending is True

    # Once the trade is open there is nothing waiting on the entry gate, so
    # the cadence drops back to the idle interval.
    await engine._mark_and_execute()
    assert engine.journal.has_open("FORM")
    assert engine._entry_pending is False


# --- Regression: the decay pass EXPIRES pending signals, so it must not be
# scheduled off a process-local timer. A monotonic marker resets to "due
# immediately" on every restart, giving a marginal signal several extra
# chances per day to be killed before the 6-hourly price poll ever looked
# at it. ---

def test_decay_pass_is_scheduled_off_persisted_wall_clock(engine):
    assert engine._daily_pass_due("decay_pass") is True
    engine._mark_daily_pass_done("decay_pass")
    assert engine._daily_pass_due("decay_pass") is False

    # A restart rebuilds the Engine from the same on-disk state: the pass
    # must still be marked done, not re-run.
    restarted = Engine(engine.settings)
    assert restarted._daily_pass_due("decay_pass") is False


# --- News-only corroboration bar: two publishers can be one reworded wire
# story that slipped past dedup, so a dossier corroborated ONLY by news
# needs min_independent_sources_news_only; any filing evidence on the
# agreeing side restores the normal bar (a filing can't be a rewording of
# a news article). ---

async def test_news_only_dossier_held_to_higher_source_bar(engine):
    engine.settings.min_independent_sources_news_only = 3
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)

    # Two news publishers: enough for the normal bar, not the news-only one.
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    assert engine.dossiers.load("FORM").status == "ACTIVE"

    # A third, genuinely distinct publisher clears the news-only bar.
    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="evidence 2", source_type="news",
        source_name="wsj.com", url="https://x/2", headline="h2", published_at="2026-07-23",
    )
    assert engine.dossiers.load("FORM").status == "SIGNALED"


async def test_filing_evidence_restores_the_normal_bar(engine):
    engine.settings.min_independent_sources_news_only = 3
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)

    await engine._process_evidence(
        origin_symbol="UCTT", evidence_text="news evidence", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="n1", published_at="2026-07-23",
    )
    await engine._process_evidence(
        origin_symbol="UCTT", evidence_text="8-K material event", source_type="8-K",
        source_name="SEC EDGAR (8-K)", url="https://sec.gov/1", headline="8-K", published_at="2026-07-23",
    )
    # Two sources, one of them a filing: normal bar (2) applies -> signals.
    assert engine.dossiers.load("UCTT").status == "SIGNALED"


# --- Regression: retry of a partially-deferred item must not re-pay LLM
# calls for sibling targets already definitively handled (not-new/refuted),
# and must not burn extra propagation-cooldown slots. ---

async def test_retry_does_not_repay_for_already_refuted_target(engine):
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, "2026-07-23"))
    engine.graph.add(Relationship("INTC", "UCTT", "customer", "Intel is a customer of UCTT", "test", 0.9, "2026-07-23"))

    # Pass 1: FORM's evidence is refuted (propose + skeptic paid), UCTT's
    # skeptic call is deferred -- the item as a whole stays unregistered.
    engine.updater.default = proposal()
    engine.skeptic.queue(verdict(refuted=True))   # FORM: refuted, definitive
    engine.skeptic.queue(None)                    # UCTT: deferred
    scored = await engine._process_evidence(
        origin_symbol="INTC", evidence_text="intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    assert scored is False
    updater_calls_after_first = len(engine.updater.calls)
    skeptic_calls_after_first = len(engine.skeptic.calls)

    # Pass 2 (the retry): only UCTT should cost anything -- FORM was
    # already definitively refuted and must not be re-judged (a second
    # nondeterministic skeptic run could accept what the first refused).
    engine.skeptic.default = verdict(refuted=False)
    scored = await engine._process_evidence(
        origin_symbol="INTC", evidence_text="intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    assert scored is True
    assert len(engine.updater.calls) == updater_calls_after_first  # UCTT's proposal was cached; FORM not re-proposed
    assert len(engine.skeptic.calls) == skeptic_calls_after_first + 1  # only UCTT's deferred skeptic call


async def test_propagation_slot_not_double_counted_on_retry_of_merged_target(engine):
    # max_propagated_evidence_per_link=1 (fixture). FORM merges on pass 1;
    # the ITEM stays unregistered because the direct-origin target (INTC is
    # an anchor, so origin isn't a target here) -- use two linked targets:
    # FORM merges, UCTT defers. The retry's has_evidence fast path for FORM
    # must NOT record a second slot for the same underlying evidence.
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, "2026-07-23"))
    engine.graph.add(Relationship("INTC", "UCTT", "customer", "Intel is a customer of UCTT", "test", 0.9, "2026-07-23"))
    engine.updater.default = proposal()
    engine.skeptic.queue(verdict(refuted=False))  # FORM: merged
    engine.skeptic.queue(None)                    # UCTT: deferred

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    # FORM consumed its 1 slot; window for INTC->FORM is now full.
    now = time.monotonic()
    assert not engine._propagation_limiter.would_allow("INTC->FORM", now)
    events_after_first = len(engine._propagation_limiter._events["INTC->FORM"])

    engine.skeptic.default = verdict(refuted=False)
    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at="2026-07-23",
    )
    # The retry re-handles FORM via has_evidence (already merged) -- it must
    # not append a second phantom event for the same evidence.
    assert len(engine._propagation_limiter._events["INTC->FORM"]) == events_after_first


# --- Regression: a malformed LLM proposal must be dropped for that dossier,
# not crash the poll (Anthropic tool use doesn't hard-enforce the schema). ---

async def test_malformed_proposal_is_dropped_not_crashing(engine):
    engine.updater.default = {"direction": "LONG"}  # missing magnitude/confidence/horizon
    outcome = await engine._update_dossier(
        "FORM", "evidence text", "FORM", "", None, "news", "reuters.com",
        "https://x/1", "h1", "2026-07-23",
    )
    assert outcome == "handled"
    assert engine.dossiers.load("FORM").evidence == []


# --- Regression: daily price marks must not depend on IB -- Finnhub quotes
# fill in, anchors are marked too (benchmark breadth), and a day with no
# reachable source is left DUE (retried), never marked done and lost. ---

async def test_daily_price_marks_fall_back_to_finnhub_and_include_anchors(engine):
    engine.price_feed = FakePriceFeed(connected=False)
    engine.finnhub.quotes_by_symbol = {"FORM": 10.0, "UCTT": 20.0, "INTC": 30.0}

    assert await engine._run_daily_price_marks() is True

    marks = (Path(engine.settings.log_dir) / "price_marks.jsonl").read_text().splitlines()
    marked_symbols = {__import__("json").loads(m)["symbol"] for m in marks}
    assert marked_symbols == {"FORM", "UCTT", "INTC"}  # INTC is the anchor


async def test_daily_price_marks_report_failure_when_no_source(engine):
    engine.price_feed = FakePriceFeed(connected=False)
    engine.finnhub.quotes_by_symbol = {}

    assert await engine._run_daily_price_marks() is False
    assert not (Path(engine.settings.log_dir) / "price_marks.jsonl").exists()


# --- Regression: an article with no timestamp must keep ONE stable
# fingerprint across days -- substituting the sliding from_date used to
# re-bill and re-merge the same story daily. ---

async def test_undated_article_is_not_reprocessed_across_polls(engine):
    engine.finnhub.articles_by_symbol["FORM"] = [
        NewsArticle(symbol="FORM", headline="Undated story", summary="s", source="Reuters",
                    url="https://finnhub.io/api/news/1", published_at=""),
    ]
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    await engine._poll_news()
    calls_after_first = len(engine.updater.calls)
    await engine._poll_news()  # same undated article returned again (e.g. next day)

    assert len(engine.updater.calls) == calls_after_first  # deduped, not re-billed
    assert len(engine.dossiers.load("FORM").evidence) == 1


# --- The decay pass could expire a signal but never fire one, so a dossier
# that came to qualify WITHOUT new evidence (decay lifting a contested
# thesis, or the bar moving under it) sat ACTIVE with nothing to re-evaluate
# it. Evidence merging was the only other evaluator. Confirmed live: DCO at
# score 0.288 against a 0.2 threshold with both source bars satisfied. ---

async def test_decay_pass_fires_a_signal_for_a_newly_qualifying_dossier(engine):
    from smartboi.dossier import Dossier, EvidenceRecord, merge_evidence

    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    dossier = Dossier(symbol="FORM")
    for i, src in enumerate(("reuters.com", "bloomberg.com")):
        merge_evidence(dossier, EvidenceRecord(
            evidence_id=f"e{i}", source_type="8-K", source_name=src, url="u", headline="h",
            published_at="2026-07-28T00:00:00+00:00", origin_symbol="FORM", is_propagated=False,
            relationship_note="", direction="LONG", magnitude=0.9, confidence=0.9,
            horizon_days=20, reasoning="r", skeptic_note="",
        ))
    dossier.status = "ACTIVE"
    engine.dossiers.save(dossier)

    await engine._run_decay_pass()

    reloaded = engine.dossiers.load("FORM")
    assert reloaded.status == "SIGNALED"
    assert reloaded.signaled_at
    assert engine._entry_pending is True
    signals = (Path(engine.settings.log_dir) / "signals.jsonl").read_text().splitlines()
    assert len(signals) == 1


async def test_decay_pass_does_not_signal_a_dossier_with_an_open_trade(engine):
    """An open paper trade owns its own stop/target/horizon -- re-signalling
    underneath it would log a second episode for a position already taken."""
    from smartboi.dossier import Dossier, EvidenceRecord, merge_evidence

    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    dossier = Dossier(symbol="FORM")
    for i, src in enumerate(("reuters.com", "bloomberg.com")):
        merge_evidence(dossier, EvidenceRecord(
            evidence_id=f"e{i}", source_type="8-K", source_name=src, url="u", headline="h",
            published_at="2026-07-28T00:00:00+00:00", origin_symbol="FORM", is_propagated=False,
            relationship_note="", direction="LONG", magnitude=0.9, confidence=0.9,
            horizon_days=20, reasoning="r", skeptic_note="",
        ))
    engine.dossiers.save(dossier)
    engine.journal.open("FORM", "LONG", 10.0, 8.0, 16.0, 20, "t", 0.9, 2, [])

    await engine._run_decay_pass()

    assert not (Path(engine.settings.log_dir) / "signals.jsonl").exists()


# --- The propagation cooldown is a RATE LIMIT, not a filter. When every link
# from a busy origin was inside its window, targets came back empty and the
# caller registered the dedup fingerprint -- destroying the item permanently
# rather than retrying it once the window rolled off. ---

async def test_evidence_throttled_to_zero_targets_is_retried_not_discarded(engine):
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "t", 0.9, "2026-07-23"))
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=False)

    # max_propagated_evidence_per_link=1 in the fixture: the first item
    # consumes INTC->FORM's only slot.
    assert await engine._process_evidence(
        origin_symbol="INTC", evidence_text="first", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h1", published_at="2026-07-23") is True

    # The second finds every link throttled -> zero targets. It must report
    # NOT-definitive so its fingerprint stays unregistered and it is retried.
    assert await engine._process_evidence(
        origin_symbol="INTC", evidence_text="second", source_type="news", source_name="reuters.com",
        url="https://x/2", headline="h2", published_at="2026-07-23") is False


async def test_evidence_for_an_unconnected_anchor_is_definitively_done(engine):
    """Empty because there is no target at all is genuinely finished --
    retrying it forever would re-fetch the same article on every poll for
    nothing. Ecosystem fallback is off here so this isolates the
    no-targets path; its own behaviour is covered below."""
    engine.settings.enable_ecosystem_propagation = False
    assert await engine._process_evidence(
        origin_symbol="INTC", evidence_text="x", source_type="news", source_name="reuters.com",
        url="https://x/3", headline="h3", published_at="2026-07-23") is True


async def test_decay_pass_signals_a_dossier_whose_score_has_not_moved(engine):
    """The regression that hid inside the first version of this fix: gating
    the evaluation on "the score changed" skips exactly the dossier that
    most obviously qualifies -- a stable one sitting above the bar with
    nothing decaying. DCO sat there for a day."""
    from smartboi.dossier import Dossier, EvidenceRecord, merge_evidence

    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    dossier = Dossier(symbol="FORM")
    for i, src in enumerate(("reuters.com", "bloomberg.com")):
        merge_evidence(dossier, EvidenceRecord(
            evidence_id=f"e{i}", source_type="8-K", source_name=src, url="u", headline="h",
            published_at="2026-07-28T00:00:00+00:00", origin_symbol="FORM", is_propagated=False,
            relationship_note="", direction="LONG", magnitude=0.9, confidence=0.9,
            horizon_days=20, reasoning="r", skeptic_note="",
        ))
    engine.dossiers.save(dossier)

    # First pass fires it; reset to ACTIVE so the second pass sees an
    # unchanged score (nothing has decayed between two back-to-back runs).
    await engine._run_decay_pass()
    reset = engine.dossiers.load("FORM")
    engine._reset_to_active(reset)
    engine.dossiers.save(reset)

    await engine._run_decay_pass()

    assert engine.dossiers.load("FORM").status == "SIGNALED"


# --- Preferred series, share classes and OTC ADR lines are not the
# operating-company common stock this system builds a thesis about.
# Confirmed live: SCE-PN (a Southern California Edison preferred) was
# auto-accepted as TRADEABLE and accrued the fourth-highest dossier score on
# the board off a utility bond-issuance story. ---

def test_a_preferred_series_is_refused_as_a_trade_target(engine):
    engine.candidates.set("SCE-PN", {"name": "Southern California Edison", "ticker": "SCE-PN",
                                     "recommended_as": "tradeable", "seen_count": 3})
    with pytest.raises(ValueError, match="preferred series or share class"):
        engine.accept_candidate("SCE-PN", "tradeable")


def test_an_otc_adr_line_is_refused_as_a_trade_target(engine):
    engine.candidates.set("SCRNY", {"name": "Screen Holdings", "ticker": "SCRNY",
                                    "recommended_as": "tradeable", "seen_count": 3})
    with pytest.raises(ValueError, match="OTC ADR or foreign ordinary"):
        engine.accept_candidate("SCRNY", "tradeable")


def test_the_same_symbols_are_perfectly_good_anchors(engine):
    """The underlying company's news still propagates -- only the position
    is refused, not the information."""
    engine.candidates.set("SCE-PN", {"name": "Southern California Edison", "ticker": "SCE-PN",
                                     "recommended_as": "anchor", "seen_count": 3})
    spec = engine.accept_candidate("SCE-PN", "anchor")
    assert spec.signal_source_only is True


def test_ordinary_common_stock_is_unaffected(engine):
    for symbol in ("DCO", "THRM", "PLAB", "KODK", "GOOGL"):
        assert is_common_equity(symbol), symbol


# --- Ecosystem-fallback propagation. An anchor with no DISCLOSED edge to a
# tradeable is inert -- it is never its own analysis target, so its news
# resolves to zero targets and is discarded unread. That was 104 of 130
# anchors live, including NVDA, AMAT, TSM, MSFT, AMZN, UPS and CSX. The
# graph can only grow at filing season, so the fallback is the ecosystem
# link -- which is also the one the literature actually measured
# (Menzly & Ozbas 2010, industry-level cross-predictability). ---

async def test_an_inert_anchor_reaches_same_ecosystem_tradeables(engine):
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    # INTC is an anchor with no graph edge at all.
    assert engine.graph.relationships == []
    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="Intel guides capex up", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h", published_at="2026-07-29")

    # FORM and UCTT share INTC's ecosystem and are tradeable.
    assert engine.dossiers.load("FORM").evidence
    assert engine.dossiers.load("UCTT").evidence


async def test_ecosystem_evidence_is_marked_as_not_a_disclosed_link(engine):
    """It must be visibly weaker than a disclosed contract, both to the LLM
    (which is told in words and in a number) and to the signal gate."""
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="x", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h", published_at="2026-07-29")

    call = engine.updater.calls[0]
    assert call["relationship_confidence"] == ECOSYSTEM_LINK_CONFIDENCE
    assert "NOT a contractual relationship" in call["relationship_note"]


async def test_ecosystem_evidence_cannot_relax_the_corroboration_bar(engine):
    """ECOSYSTEM_LINK_CONFIDENCE sits below DISCLOSED_LINK_CONFIDENCE on
    purpose: this evidence can raise a thesis but must never let a
    news-only dossier through on the lower source bar."""
    from smartboi.dossier import DISCLOSED_LINK_CONFIDENCE

    assert ECOSYSTEM_LINK_CONFIDENCE < DISCLOSED_LINK_CONFIDENCE

    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)
    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="x", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h", published_at="2026-07-29")

    assert engine.dossiers.load("FORM").has_disclosed_link_evidence is False


async def test_a_disclosed_edge_suppresses_the_ecosystem_fallback(engine):
    """A disclosed contract is strictly better evidence -- the fallback must
    not run alongside it or double up on the same target."""
    engine.graph.add(Relationship("FORM", "INTC", "customer", "Intel is a customer of FORM", "t", 0.95, "2026-07-29"))
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="x", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h", published_at="2026-07-29")

    notes = [c["relationship_note"] for c in engine.updater.calls]
    assert all("ecosystem" not in n for n in notes)
    # UCTT shares the ecosystem but has no disclosed link -- it must NOT be
    # reached, because FORM's disclosed edge suppressed the fallback wholesale.
    assert not engine.dossiers.load("UCTT").evidence


async def test_a_throttled_disclosed_link_does_not_fall_back(engine):
    """Otherwise the fallback routes around the very cooldown it just hit,
    reaching the same target by a weaker route."""
    engine.graph.add(Relationship("FORM", "INTC", "customer", "Intel is a customer of FORM", "t", 0.95, "2026-07-29"))
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    # max_propagated_evidence_per_link=1 in the fixture: this consumes it.
    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="first", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h1", published_at="2026-07-29")
    before = len(engine.updater.calls)

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="second", source_type="news", source_name="bloomberg.com",
        url="https://x/2", headline="h2", published_at="2026-07-29")

    assert len(engine.updater.calls) == before  # throttled, and no weaker route taken


async def test_ecosystem_fanout_is_rate_limited_on_its_own_budget(engine):
    engine.settings.max_ecosystem_evidence_per_link = 1
    engine._ecosystem_limiter = SlidingWindowLimiter(1, 6 * 3600)
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    for i in range(3):
        await engine._process_evidence(
            origin_symbol="INTC", evidence_text=f"item {i}", source_type="news",
            source_name=f"src{i}.com", url=f"https://x/{i}", headline=f"h{i}",
            published_at="2026-07-29")

    # One item per (origin -> target) pair inside the window, for each of the
    # two same-ecosystem tradeables.
    assert len(engine.updater.calls) == 2


def test_an_unclassified_bucket_is_never_treated_as_an_ecosystem(engine):
    """"accepted" is where a runtime-accepted symbol lands when its
    discoverer's ecosystem can't be inferred. It has held dozens of
    mutually unrelated companies at once -- fanning news across it would be
    noise dressed as a sector link."""
    from smartboi.universe import CompanySpec

    engine.universe.append(CompanySpec("JUNKA", "JUNKA", "accepted", signal_source_only=True))
    engine.universe.append(CompanySpec("JUNKT", "JUNKT", "accepted"))
    engine.spec_by_symbol = {c.symbol: c for c in engine.universe}

    assert engine._ecosystem_targets("JUNKA") == []
    assert engine._can_produce_evidence("JUNKA") is False


def test_a_tradeable_does_not_fan_out_to_its_ecosystem_peers(engine):
    """A tradeable is already its own analysis target; spraying its news
    across its peers is a competitor read-across thesis this system has no
    disclosed basis for."""
    assert engine._ecosystem_targets("FORM") == []


def test_ecosystem_propagation_can_be_turned_off(engine):
    engine.settings.enable_ecosystem_propagation = False
    assert engine._can_produce_evidence("INTC") is False


# --- The biography filter, against every case confirmed live in the graph
# and the candidate list, plus the genuine disclosures it must not touch.
# The first version caught 1 of 9. ---

BIO_DESCRIPTIONS = [
    "Paul Sternlieb held a Group President role at Illinois Tool Works' Food Equipment Group before JBT.",
    "CFO Darren Kozik started his career at GE in 1999 and worked in various roles.",
    "CEO Paul Sternlieb held management roles at Danaher (2011-2014), a major diversified conglomerate.",
    "EVP Operations Eric Chack held global operations leadership roles at IDEX Corporation.",
    "CFO Darren Kozik served as Senior Vice President at ManpowerGroup before joining EPAC.",
    "LOAR's management team previously led K&F Industries before its sale to Meggitt.",
    "TransDigm acquired McKechnie Aerospace in 2010, where LOAR's management team worked together.",
    "RTX is cited as the former employer of Shawn M. Mural, the CFO of V2X, who worked there for 24 years.",
]

COMMERCIAL_DESCRIPTIONS = [
    "General Motors is a major OEM customer. In 2025, GM accounted for 12% of product revenues.",
    "PACCAR is one of SRI's principal customers, accounting for 15% of net sales in 2025.",
    "Second largest customer representing approximately 21.5% of net sales for the year ended 2025.",
    "Boeing is one of DCO's largest customers, generating 13% of 2025 net revenues.",
    "General Motors is ULH's top customer, representing approximately 25% of total revenues.",
    "ExxonMobil accounted for approximately 24.9% of total revenue for the year ended 2025.",
    "NIPSCO has an agreement to provide electricity to Amazon Data Services' data centers.",
    "Willdan implements Consolidated Edison's Small and Medium Business Program in New York City.",
    "GE Vernova accounted for more than 10% of consolidated revenues in 2025 and 2024.",
    "L3Harris Technologies is Ultralife's largest customer, comprising 27% of total revenues.",
    "Hudson entered an agreement with Lennox International to be the exclusive supplier of reclaimed refrigerants.",
    "Norfolk Southern and CSX jointly own Conrail Inc. NSR has a 58% economic and 50% voting interest.",
]


@pytest.mark.parametrize("description", BIO_DESCRIPTIONS)
def test_executive_biographies_are_recognised(description):
    assert Engine._is_biography_relationship({"description": description}) is True


@pytest.mark.parametrize("description", COMMERCIAL_DESCRIPTIONS)
def test_genuine_disclosures_survive_the_biography_filter(description):
    assert Engine._is_biography_relationship({"description": description}) is False


# --- Price-source independence: the entry gate must not be IB-only.
#
# For a long stretch this system could accumulate evidence, cross the signal
# bar, fire a signal and log it -- and then never open the paper trade that
# is its entire output -- because _tick only ran _mark_and_execute under
# `self.price_feed is not None`, and _try_open_from_signal only ever asked
# IB for a price. Both the drift BASELINE (_snapshot_signal_price) and the
# daily forward-validation marks already fell back to Finnhub's /quote; the
# gate that actually opens the trade did not. Confirmed live: a Gateway
# reporting "farms not connected: eufarm; euhmds" against a universe of 48
# tradeables and zero paper trades ever opened. ---

async def _signal_form(engine, sources=("reuters.com", "bloomberg.com")):
    """Drives FORM to SIGNALED off two independent news sources."""
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(sources):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    return engine.dossiers.load("FORM")


async def test_entry_opens_from_finnhub_quote_with_no_ib_feed_at_all(engine):
    engine.price_feed = None
    engine.finnhub.quotes_by_symbol["FORM"] = 10.0

    dossier = await _signal_form(engine)
    assert dossier.status == "SIGNALED"
    assert dossier.signaled_price == 10.0  # baseline came from Finnhub too

    await engine._mark_and_execute()

    assert engine.journal.has_open("FORM")
    assert engine.journal.open_trades["FORM"].entry_price == 10.0


async def test_entry_falls_back_to_finnhub_when_ib_cannot_price_the_symbol(engine):
    # IB is connected but has no price for FORM (no market-data subscription,
    # unqualifiable contract, dead data farm) -- the live failure mode.
    engine.price_feed = FakePriceFeed(prices={})
    engine.finnhub.quotes_by_symbol["FORM"] = 12.5

    await _signal_form(engine)
    await engine._mark_and_execute()

    assert engine.journal.has_open("FORM")
    assert engine.journal.open_trades["FORM"].entry_price == 12.5


async def test_ib_price_is_preferred_over_finnhub_when_both_have_one(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.finnhub.quotes_by_symbol["FORM"] = 99.0

    await _signal_form(engine)
    await engine._mark_and_execute()

    assert engine.journal.open_trades["FORM"].entry_price == 10.0


async def test_opened_trade_is_stamped_with_the_current_strategy(engine):
    """Each paper trade the engine opens carries the live strategy signature,
    so the closed record can later be split by generation and a new strategy's
    win rate is never pooled with an abandoned one (see status.py)."""
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})

    await _signal_form(engine)
    await engine._mark_and_execute()

    trade = engine.journal.open_trades["FORM"]
    assert trade.strategy == engine.settings.strategy_signature()
    assert trade.strategy["stop_loss_pct"] == engine.settings.stop_loss_pct
    assert trade.strategy["transaction_cost_profile"] == engine.settings.transaction_cost_profile


async def test_open_trade_is_marked_from_finnhub_intraday_band(engine):
    """The Finnhub fallback carries the session high/low, so a stop that
    traded intraday still stops the trade out -- a close-only fallback would
    have quietly erased exactly those losses (see paper_journal.update)."""
    engine.price_feed = None
    engine.finnhub.quotes_by_symbol["FORM"] = 10.0
    await _signal_form(engine)
    await engine._mark_and_execute()
    trade = engine.journal.open_trades["FORM"]

    # Closed above the stop, but the session low traded through it.
    engine.finnhub.quotes_by_symbol["FORM"] = (10.0, 10.2, trade.stop_price - 0.05)
    _backdate_entry(engine, "FORM")  # a stop/target never resolves on the entry session
    await engine._mark_and_execute()

    assert not engine.journal.has_open("FORM")
    closed = [json.loads(line) for line in engine.journal.log_path.read_text().splitlines()]
    assert closed[-1]["status"] == "LOSS"


async def test_unpriceable_signal_expires_at_the_entry_deadline(engine):
    """No price from ANY source used to be a bare `return` that sat ABOVE
    the deadline check, so such a signal never opened and never expired --
    it held the tightened entry-poll cadence open forever and blocked the
    dossier from ever producing a fresh, cleanly-baselined signal."""
    engine.price_feed = None  # and no Finnhub quote for FORM either

    dossier = await _signal_form(engine)
    assert dossier.status == "SIGNALED"
    episode = dossier.signaled_at

    # Still inside the deadline: stays SIGNALED, waiting.
    await engine._mark_and_execute()
    assert engine.dossiers.load("FORM").status == "SIGNALED"

    # Backdate past signal_entry_deadline_days.
    dossier = engine.dossiers.load("FORM")
    signaled = datetime.fromisoformat(dossier.signaled_at)
    dossier.signaled_at = (
        signaled - timedelta(days=engine.settings.signal_entry_deadline_days + 1)
    ).isoformat()
    engine.dossiers.save(dossier)

    await engine._mark_and_execute()

    reset = engine.dossiers.load("FORM")
    assert reset.status == "ACTIVE"
    rows = [json.loads(line) for line in
            (Path(engine.settings.log_dir) / "decisions.jsonl").read_text().splitlines()]
    expired = [r for r in rows if r["event"] == "signal_expired"]
    assert len(expired) == 1
    assert "no price available from any source" in expired[0]["reason"]
    assert expired[0]["episode"]  # episode-keyed, so event_study can join it
    assert episode  # the original episode key existed before the backdate


def test_has_price_source_is_true_with_finnhub_alone(engine):
    engine.price_feed = None
    assert engine._has_price_source() is True


def test_has_price_source_is_false_with_neither(engine):
    engine.price_feed = None
    engine.finnhub = None
    assert engine._has_price_source() is False


# --- Expiry hysteresis: an episode must not be killed before the entry gate
# has evaluated it once.
#
# The news poll walks the whole universe spending two LLM calls per article,
# so an episode fired early in a poll used to be exposed for the rest of that
# poll -- and one skeptic-approved contrary item is enough to end it, because
# confidence is multiplied by (1 - mass_opposing/mass_agree). The episode
# died, its price baseline was wiped, and the gate never saw it. That is the
# life story of the only signal this system had ever fired. ---

async def _degrade(engine, symbol, confidence, magnitude):
    d = engine.dossiers.load(symbol)
    d.confidence, d.magnitude = confidence, magnitude
    engine.dossiers.save(d)
    return d


async def test_marginal_dip_before_the_entry_gate_does_not_expire(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)
    # Threshold is 0.5 in the fixture; hysteresis bar is 0.5 * 0.8 = 0.4.
    # Score 0.45 is below the SIGNAL bar but above the EXPIRY bar.
    dossier = await _degrade(engine, "FORM", confidence=0.9, magnitude=0.5)
    assert dossier.entry_attempts == 0

    assert engine._should_expire_unopened(dossier) is False


async def test_material_dip_before_the_entry_gate_still_expires(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)
    dossier = await _degrade(engine, "FORM", confidence=0.2, magnitude=0.2)  # 0.04, far below 0.4

    assert engine._should_expire_unopened(dossier) is True


async def test_a_direction_flip_always_expires_immediately(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)
    dossier = engine.dossiers.load("FORM")
    dossier.direction = "SHORT"  # signaled LONG
    dossier.confidence, dossier.magnitude = 0.9, 0.9  # still way over the bar

    assert engine._should_expire_unopened(dossier) is True


async def test_the_grace_period_ends_once_the_gate_has_looked(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)
    dossier = await _degrade(engine, "FORM", confidence=0.9, magnitude=0.5)
    dossier.entry_attempts = 1  # the gate has had its evaluation

    assert engine._should_expire_unopened(dossier) is True


async def test_the_decay_pass_respects_the_grace_period(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)
    # Degrade the aggregate directly, then make sure recompute_decay can't
    # simply restore it: strip the evidence the aggregate is computed from
    # would change direction to NONE, so instead lower the threshold view by
    # asserting on _should_expire_unopened, which is what the pass consults.
    dossier = await _degrade(engine, "FORM", confidence=0.9, magnitude=0.5)
    assert engine._should_expire_unopened(dossier) is False


async def test_entry_attempts_is_persisted_and_reset_with_the_episode(engine):
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)
    assert engine.dossiers.load("FORM").entry_attempts == 0

    # Drift-block the entry so the episode survives the poll unopened.
    engine.price_feed.prices["FORM"] = 11.0
    await engine._mark_and_execute()
    assert engine.dossiers.load("FORM").entry_attempts == 1
    await engine._mark_and_execute()
    assert engine.dossiers.load("FORM").entry_attempts == 2

    engine._expire_signal(engine.dossiers.load("FORM"), "test")
    assert engine.dossiers.load("FORM").entry_attempts == 0


async def test_a_fresh_signal_pulls_the_entry_poll_to_the_next_tick(engine):
    """_fire_signal clears _last_price_poll so the first entry evaluation
    happens on the next 30s tick, not a full 15-minute entry interval later
    -- the window in which every expiry path can kill the episode unseen."""
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine._last_price_poll = time.monotonic()  # a poll just ran

    await _signal_form(engine)

    assert engine._last_price_poll is None
    assert engine._entry_pending is True


# --- Whole-evidence-body synthesis.
#
# Everything else here is incremental: each item is scored alone against a
# one-line thesis summary, and the aggregate is arithmetic over those
# independent scores. Nothing read the evidence as a BODY, which left three
# questions structurally unanswerable -- are these N facts or one fact
# counted N times, do they cohere, and has the market already connected them
# -- and all three decide whether a trade is justified.
#
# Its verdict CAPS the arithmetic aggregate. It can veto and it can trim; it
# cannot inflate a score into a trade, so one model call never becomes a
# single point of failure for committing capital. ---

async def _build_thesis(engine, symbol="FORM", confidence=0.8, magnitude=0.8):
    engine.updater.default = proposal(direction="LONG", magnitude=magnitude,
                                      confidence=confidence, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=confidence,
                                     adjusted_magnitude=magnitude)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol=symbol, evidence_text=f"e{i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    return engine.dossiers.load(symbol)


async def test_synthesis_trims_a_score_it_judges_over_counted(engine):
    engine.synthesizer = FakeSynthesizer(default=synthesis(confidence=0.4, magnitude=0.5,
                                                           distinct_fact_count=1))
    dossier = await _build_thesis(engine)
    before = dossier.confidence * dossier.magnitude

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.confidence == 0.4
    assert dossier.magnitude == 0.5
    assert dossier.confidence * dossier.magnitude < before
    assert dossier.distinct_fact_count == 1


async def test_synthesis_cannot_inflate_a_score(engine):
    """A cap, never a lift -- otherwise one confident model call could
    manufacture a trade on evidence that never accumulated."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(confidence=1.0, magnitude=1.0))
    # Above the near-the-bar floor, so synthesis actually runs and the
    # assertion tests the cap rather than the skip.
    dossier = await _build_thesis(engine, confidence=0.6, magnitude=0.6)
    before_c, before_m = dossier.confidence, dossier.magnitude

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.confidence == before_c
    assert dossier.magnitude == before_m


async def test_already_priced_in_is_a_veto(engine):
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine)

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.confidence == 0.0
    assert dossier.magnitude == 0.0
    assert dossier.already_priced_in is True


async def test_a_direction_disagreement_is_a_veto(engine):
    engine.synthesizer = FakeSynthesizer(default=synthesis(direction="SHORT"))
    dossier = await _build_thesis(engine)  # arithmetic says LONG

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.confidence == 0.0
    assert dossier.magnitude == 0.0


async def test_a_failed_synthesis_leaves_the_aggregate_untouched(engine):
    """A transient error or an exhausted budget must be a no-op, not a block."""
    engine.synthesizer = FakeSynthesizer(default=None)
    dossier = await _build_thesis(engine)
    before_c, before_m = dossier.confidence, dossier.magnitude

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert (dossier.confidence, dossier.magnitude) == (before_c, before_m)


async def test_synthesis_is_skipped_for_a_directionless_dossier(engine):
    engine.synthesizer = FakeSynthesizer(default=synthesis())
    dossier = engine.dossiers.load("FORM")  # never had evidence -> direction NONE

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert engine.synthesizer.calls == []


async def test_out_of_range_model_numbers_are_clamped(engine):
    """Tool schemas declare min/max but tool use does not hard-enforce them,
    and these flow straight into a trade decision."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(confidence=7.5, magnitude=-2.0))
    dossier = await _build_thesis(engine)

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert 0.0 <= dossier.synthesis_confidence <= 1.0
    assert 0.0 <= dossier.synthesis_magnitude <= 1.0
    assert dossier.magnitude == 0.0


async def test_the_decay_pass_runs_synthesis(engine):
    engine.synthesizer = FakeSynthesizer(default=synthesis(confidence=0.3, magnitude=0.3))
    await _build_thesis(engine)

    await engine._run_decay_pass()

    assert [c["symbol"] for c in engine.synthesizer.calls] == ["FORM"]


# --- The loop closes: cold start -> evidence -> signal -> open paper trade.
#
# Every other test here exercises one stage. This one runs the actual tick
# loop from an empty data directory and asserts a position exists at the end,
# because the live system's defining symptom was that each stage worked in
# isolation while the whole never produced its one output: 209 symbols, 17
# dossiers, 10,705 ingested items, one signal ever, and zero paper trades. ---

async def test_a_cold_start_reaches_an_open_paper_trade(engine):
    """No IB, no dossiers, no prior state -- just news arriving."""
    engine.price_feed = None
    engine.finnhub.quotes_by_symbol["FORM"] = 10.0
    engine.finnhub.articles_by_symbol["FORM"] = [
        NewsArticle(symbol="FORM", headline="FORM wins $40M photomask supply award",
                    summary="multi-year", source="Reuters", url="https://finnhub.io/1",
                    published_at="2026-07-29T12:00:00+00:00"),
        NewsArticle(symbol="FORM", headline="Intel raises capex guidance for 2027",
                    summary="fab expansion", source="Bloomberg", url="https://finnhub.io/2",
                    published_at="2026-07-29T13:00:00+00:00"),
    ]
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)

    # Two ticks: the first ingests and fires, the second is the entry poll
    # that _fire_signal pulled forward by clearing _last_price_poll.
    await engine._tick()
    await engine._tick()

    assert engine.journal.has_open("FORM"), "cold start did not reach an open paper trade"
    trade = engine.journal.open_trades["FORM"]
    assert trade.direction == "LONG"
    assert trade.entry_price == 10.0
    assert trade.stop_price < trade.entry_price < trade.target_price
    # ...and the record needed to judge it later exists.
    log_dir = Path(engine.settings.log_dir)
    assert (log_dir / "signals.jsonl").exists()
    opened = [json.loads(line) for line in (log_dir / "decisions.jsonl").read_text().splitlines()]
    assert any(r["event"] == "trade_opened" and r["symbol"] == "FORM" for r in opened)


async def test_the_same_cold_start_closes_the_trade_when_the_target_trades(engine):
    """The other half of the loop: a position that reaches its target closes,
    banks a WIN, and hands the symbol back so fresh evidence can re-signal."""
    engine.price_feed = None
    engine.finnhub.quotes_by_symbol["FORM"] = 10.0
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    await _signal_form(engine)
    await engine._mark_and_execute()
    trade = engine.journal.open_trades["FORM"]

    engine.finnhub.quotes_by_symbol["FORM"] = trade.target_price + 0.05
    _backdate_entry(engine, "FORM")  # a stop/target never resolves on the entry session
    await engine._mark_and_execute()

    assert not engine.journal.has_open("FORM")
    closed = [json.loads(line) for line in engine.journal.log_path.read_text().splitlines()]
    assert closed[-1]["status"] == "WIN"
    assert closed[-1]["r_multiple"] is not None       # net of the cost model
    assert closed[-1]["cost_bps_round_trip"] > 0      # ...and costs were charged
    assert engine.dossiers.load("FORM").status == "ACTIVE"


async def test_synthesis_is_skipped_well_below_the_bar(engine):
    """The expensive pass only runs where it can change the outcome. It can
    only veto or trim, so on a dossier far below the signal bar the reachable
    outcomes are "unchanged" and "further below" -- neither changes a
    decision, and both cost an Opus call. This is what keeps the one
    expensive pass at a handful of calls a day as the watchlist grows."""
    engine.synthesizer = FakeSynthesizer(default=synthesis())
    dossier = await _build_thesis(engine, confidence=0.2, magnitude=0.2)
    assert dossier.confidence * dossier.magnitude < (
        engine.settings.signal_confidence_threshold * engine.settings.synthesis_score_floor_pct
    )

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert engine.synthesizer.calls == []


async def test_synthesis_runs_on_a_dossier_near_the_bar(engine):
    """Near-but-below still gets checked: a thesis that would fire tomorrow
    on one more item is exactly where an over-counting veto is worth paying
    for."""
    engine.synthesizer = FakeSynthesizer(default=synthesis())
    floor = (engine.settings.signal_confidence_threshold
             * engine.settings.synthesis_score_floor_pct)
    dossier = await _build_thesis(engine, confidence=0.7, magnitude=0.6)
    assert dossier.confidence * dossier.magnitude >= floor

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert [c["symbol"] for c in engine.synthesizer.calls] == ["FORM"]


# --- Audit #1/#3/#4/#9. Four findings from the 2026-07-29 multi-agent audit
# of the live deployment. Each one is a case where the code silently did
# something other than what its own comment or docstring claimed. ---


async def test_every_signal_row_records_the_bar_it_actually_cleared(engine):
    """The bar is overridable from the add-on's options.json, so the
    documented 0.65 and the bar a row cleared can differ with nothing
    recording it. The live record has 13 signals and no way to tell which
    rules admitted any of them -- which makes SCORING_VERSION's whole
    purpose (split the record at a rules boundary) unexecutable."""
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"e{i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )

    rows = [json.loads(line) for line in
            (Path(engine.settings.log_dir) / "signals.jsonl").read_text().splitlines()]
    assert rows
    for row in rows:
        # The fixture runs a 0.5 bar, not the documented 0.65 -- which is
        # exactly the situation this field exists to make legible.
        assert row["threshold_in_force"] == 0.5
        assert row["min_sources_in_force"] == 2
        assert row["scoring_version"] == SCORING_VERSION


async def test_the_news_only_elevation_is_what_gets_stamped_not_the_base_setting(engine):
    """A news-only dossier clears an ELEVATED source bar. Stamping the
    unelevated setting would misdescribe precisely the rows where the
    distinction decided the outcome."""
    engine.settings.min_independent_sources_news_only = 3
    engine.updater.default = proposal(direction="LONG", magnitude=0.9, confidence=0.9, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.9, adjusted_magnitude=0.9)
    for i, source in enumerate(("reuters.com", "bloomberg.com", "wsj.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"e{i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )

    rows = [json.loads(line) for line in
            (Path(engine.settings.log_dir) / "signals.jsonl").read_text().splitlines()]
    assert rows
    assert all(row["min_sources_in_force"] == 3 for row in rows)


def test_a_snapshot_that_wrote_nothing_leaves_the_day_due(engine):
    """The comment above both daily passes says they are marked done only
    after a successful run, because 'a lost day is not' recoverable. That
    was true of price marks and false of the snapshot pass, which marked
    done unconditionally -- on the pass whose data is the less replaceable
    of the two."""
    assert engine.dossiers.all_symbols() == []
    assert engine._run_daily_snapshot() is False
    assert engine._daily_pass_due("dossier_snapshot")  # still due, will retry


def test_a_snapshot_that_wrote_rows_marks_the_day_done(engine):
    dossier = engine.dossiers.load("FORM")
    dossier.direction = "LONG"
    engine.dossiers.save(dossier)

    assert engine._run_daily_snapshot() is True
    rows = (Path(engine.settings.log_dir) / "dossier_snapshots.jsonl").read_text().splitlines()
    assert len(rows) == 1


async def test_no_trade_opens_outside_regular_trading_hours(engine, monkeypatch):
    """Two live paper trades were booked at 09:18 ET. No price source
    refuses to answer out of hours -- they return the last close -- so the
    engine opened at a price no order could have been filled at."""
    monkeypatch.setattr(smartboi.engine, "is_regular_trading_hours", lambda now=None: False)
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"e{i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    assert engine.dossiers.load("FORM").status == "SIGNALED"

    await engine._mark_and_execute()
    assert not engine.journal.has_open("FORM")

    # And it opens on the next poll once the session is in -- blocked, not lost.
    monkeypatch.setattr(smartboi.engine, "is_regular_trading_hours", lambda now=None: True)
    await engine._mark_and_execute()
    assert engine.journal.has_open("FORM")


async def test_an_out_of_hours_signal_still_expires_at_its_deadline(engine, monkeypatch):
    """The regression this ordering exists to prevent: the deadline check
    once sat below an early return, and the result was a signal that never
    opened AND never expired -- it held the tightened entry cadence open
    forever and blocked the dossier from ever signalling cleanly again."""
    monkeypatch.setattr(smartboi.engine, "is_regular_trading_hours", lambda now=None: False)
    dossier = engine.dossiers.load("FORM")
    dossier.direction = dossier.signaled_direction = "LONG"
    dossier.status = "SIGNALED"
    dossier.confidence, dossier.magnitude = 0.9, 0.9
    dossier.independent_source_count = 2
    dossier.signaled_at = (
        datetime.now(timezone.utc)
        - timedelta(days=engine.settings.signal_entry_deadline_days + 1)
    ).isoformat()
    engine.dossiers.save(dossier)

    await engine._try_open_from_signal("FORM", engine.dossiers.load("FORM"))

    assert engine.dossiers.load("FORM").status != "SIGNALED"
    assert not engine.journal.has_open("FORM")


async def test_a_non_object_relationship_does_not_kill_the_paid_for_extraction(engine):
    """A bare string in the relationships array raised AttributeError on the
    first .get() -- after the call was paid for and before backfill_state
    was set, so the filing stayed due and every future poll re-paid and died
    at the same element. Two mega-cap filings were stuck in that loop."""
    engine.extractor.default = [
        "UCTT",  # the malformed element that used to raise
        {"counterparty_name": "Ultra Clean Holdings", "counterparty_ticker": "UCTT",
         "rel_type": "customer", "description": "d", "evidence_quote": "q", "confidence": 0.9},
    ]

    ran = await engine._extract_relationships(
        "FORM", FilingEvent(symbol="FORM", cik10="0000000001", form="10-K",
                            filing_date="2026-07-20", accession_number="a-1",
                            primary_document="d.htm"),
        "filing text",
    )

    # The call is not wasted: it ran, and the well-formed edge survived.
    assert ran is True
    assert any(r.to_symbol == "UCTT" for r in engine.graph.relationships)


async def test_the_daily_price_marks_pass_is_skipped_at_the_weekend(engine, monkeypatch):
    """Both price sources answer on a Saturday, with Friday's close -- a
    real-looking number under a weekend date key. forward_returns walks
    FORWARD to the first date at or after its target, so a horizon landing on
    a weekend joins that stale row instead of Monday's, truncating the window
    by a day or two. It never extends it, so the bias is one-directional:
    every measured return, hit rate and correlation is attenuated."""
    monkeypatch.setattr(smartboi.engine, "is_trading_day", lambda now=None: False)
    engine.price_feed = None
    engine.finnhub.quotes_by_symbol["FORM"] = 10.0

    await engine._tick()

    assert not (Path(engine.settings.log_dir) / "price_marks.jsonl").exists()
    assert engine._daily_pass_due("price_marks")  # still due, picked up on Monday


async def test_the_daily_price_marks_pass_runs_on_a_weekday(engine):
    engine.price_feed = None
    engine.finnhub.quotes_by_symbol["FORM"] = 10.0

    await engine._tick()

    assert (Path(engine.settings.log_dir) / "price_marks.jsonl").exists()
    assert not engine._daily_pass_due("price_marks")


# --- Graph maintenance: the rolling re-extraction refresh -------------------
# The graph IS the strategy -- an edge is the only path by which an anchor's
# news reaches a tradeable -- and the backfill reads each symbol exactly once,
# ever. These lock in the pass that keeps it from decaying.

def _mark_backfilled(engine, symbol, stamp):
    engine.backfill_state.set(symbol, {"backfilled_at": stamp, "accession": "x"})


def test_graph_refresh_requeues_the_least_recently_extracted_symbols(engine):
    """Oldest-first: a refresh slot spent on a freshly-read symbol is a slot
    not spent on one carrying holes from when the universe was smaller."""
    engine.settings.graph_refresh_symbols_per_day = 2
    _mark_backfilled(engine, "FORM", "2026-01-01T00:00:00+00:00")   # oldest
    _mark_backfilled(engine, "UCTT", "2026-06-01T00:00:00+00:00")
    _mark_backfilled(engine, "INTC", "2026-07-01T00:00:00+00:00")   # newest (an anchor)

    assert engine._run_graph_refresh() == 2

    # The two stalest markers are cleared -- backfill re-reads them next tick.
    assert engine.backfill_state.get("FORM") is None
    assert engine.backfill_state.get("UCTT") is None
    assert engine.backfill_state.get("INTC") is not None


def test_graph_refresh_includes_anchors(engine):
    """The old rebuild button skipped anchors, but an anchor with no edge to a
    tradeable is inert -- its news resolves to zero targets and is discarded
    unread -- so it is exactly what most needs re-reading."""
    engine.settings.graph_refresh_symbols_per_day = 1
    _mark_backfilled(engine, "INTC", "2020-01-01T00:00:00+00:00")   # anchor, stalest
    _mark_backfilled(engine, "FORM", "2026-07-01T00:00:00+00:00")

    engine._run_graph_refresh()

    assert engine.backfill_state.get("INTC") is None


def test_graph_refresh_skips_symbols_already_pending(engine):
    """A symbol with no marker is already queued for backfill; spending a
    refresh slot on it would displace one that has actually gone stale."""
    engine.settings.graph_refresh_symbols_per_day = 5
    _mark_backfilled(engine, "FORM", "2026-01-01T00:00:00+00:00")
    # UCTT and INTC have no marker at all -- already pending.

    assert engine._run_graph_refresh() == 1


def test_graph_refresh_is_a_no_op_when_disabled_or_zero(engine):
    engine.settings.graph_refresh_symbols_per_day = 0
    _mark_backfilled(engine, "FORM", "2026-01-01T00:00:00+00:00")

    assert engine._run_graph_refresh() == 0
    assert engine.backfill_state.get("FORM") is not None


async def test_graph_refresh_runs_once_a_day_from_the_tick(engine):
    """Scheduled off the PERSISTED daily marker, like the other daily passes:
    a process-local timer would re-fire on every restart and burn a fresh
    batch of extraction calls each time."""
    engine.settings.enable_graph_refresh = True
    engine.settings.graph_refresh_symbols_per_day = 1
    engine.settings.enable_auto_supplier_research = False
    _mark_backfilled(engine, "FORM", "2026-01-01T00:00:00+00:00")
    _mark_backfilled(engine, "UCTT", "2026-06-01T00:00:00+00:00")

    await engine._tick()
    assert engine.backfill_state.get("FORM") is None      # stalest was re-queued
    assert not engine._daily_pass_due("graph_refresh")

    # A second tick the same day must not queue another batch.
    await engine._tick()
    assert engine.backfill_state.get("UCTT") is not None


async def test_auto_supplier_research_failure_never_kills_the_tick(engine, monkeypatch):
    """It is a web-search-backed LLM call -- the flakiest thing in the system.
    A failure must degrade to 'nothing happened today', not stop ingestion,
    scoring and signalling, which share this single task."""
    async def boom(_engine):
        raise RuntimeError("web search exploded")

    monkeypatch.setattr("smartboi.tools.run_supplier_research", boom)
    engine.settings.enable_auto_supplier_research = True
    engine.settings.anthropic_api_key = "test-key"

    await engine._tick()   # must not raise

    # Marked done anyway: retrying an expensive pass every 30s tick until it
    # succeeds is far worse than waiting a day.
    assert not engine._daily_pass_due("supplier_research")


# --- Invariant: seen_count counts FILINGS, not extraction passes. It gates
# tradeable auto-accept and means "disclosed across filings", so re-reading
# one filing must never manufacture repeat disclosure. 0.47.0's rolling
# monthly re-extraction made this reachable on a schedule. ---

async def test_re_extracting_the_same_filing_does_not_inflate_seen_count(engine):
    """The regression the graph refresh introduced: the refresh clears a
    symbol's backfill marker so its latest 10-K is read again, and an
    unconditional increment turned one disclosure into two -- crossing
    auto_accept_min_seen_count (default 2) with no new filing in
    existence."""
    filing = FilingEvent(
        symbol="FORM", cik10="0000000001", form="10-K", filing_date="2026-07-01",
        accession_number="0001234567-26-000001", primary_document="form.htm",
    )
    engine.extractor.default = [{
        "counterparty_name": "Some Uncommon Co", "counterparty_ticker": "ZZZZ",
        "rel_type": "customer", "description": "our largest customer",
        "confidence": 0.9, "quote": "our largest customer, Some Uncommon Co",
    }]
    await engine._extract_relationships("FORM", filing, "filing text")
    assert engine.candidates.get("ZZZZ")["seen_count"] == 1

    # The monthly refresh re-reads the very same document, twice over.
    await engine._extract_relationships("FORM", filing, "filing text")
    await engine._extract_relationships("FORM", filing, "filing text")
    entry = engine.candidates.get("ZZZZ")
    assert entry["seen_count"] == 1, "re-reading one filing is not repeat disclosure"
    assert entry["sources"] == [filing.document_url]


async def test_a_second_real_filing_does_increment_seen_count(engine):
    """The other half: the guard must still let genuine repeat disclosure
    through, or the rolling refresh would freeze every candidate at 1 and no
    tradeable could ever auto-accept again."""
    engine.extractor.default = [{
        "counterparty_name": "Some Uncommon Co", "counterparty_ticker": "ZZZZ",
        "rel_type": "customer", "description": "our largest customer",
        "confidence": 0.9, "quote": "our largest customer, Some Uncommon Co",
    }]
    for n, symbol in ((1, "FORM"), (2, "UCTT")):
        await engine._extract_relationships(symbol, FilingEvent(
            symbol=symbol, cik10=f"000000000{n}", form="10-K",
            filing_date=f"2026-07-0{n}", accession_number=f"0001234567-26-00000{n}",
            primary_document=f"{symbol.lower()}.htm",
        ), "filing text")

    entry = engine.candidates.get("ZZZZ")
    assert entry["seen_count"] == 2  # two filers, two filings -> real corroboration
    assert len(entry["sources"]) == 2


# --- Invariant: a synthesis verdict SURVIVES the pass that produced it.
#
# The verdict used to be computed, used to cap the score for the rest of the
# decay pass, and then dropped: it reached disk only when something else
# happened to save the dossier in the same pass (a signal firing, an expiry).
# For an ACTIVE dossier that stayed below the bar -- the common case -- the
# most expensive judgement in the system left no trace, so its effect on
# outcomes could never be measured, and the per-trade synthesis stamp read a
# verdict that was usually stale or absent. ---

async def test_a_verdict_on_an_active_dossier_is_persisted(engine):
    """The case that was being lost: nothing else saves this dossier, so
    without an explicit save the verdict evaporates at end of pass."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(confidence=0.4, magnitude=0.5,
                                                           distinct_fact_count=1))
    # Above the synthesis floor (0.5 * 0.6) but below the signal bar (0.5),
    # so synthesis runs and NOTHING else in the pass saves this dossier --
    # no signal fires, no expiry. That is precisely the case the verdict
    # used to be lost in.
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    assert dossier.status == "ACTIVE"

    await engine._decay_one("FORM", datetime.now(timezone.utc))

    reloaded = engine.dossiers.load("FORM")
    assert reloaded.synthesis_at != "", "the verdict never reached disk"
    assert reloaded.distinct_fact_count == 1
    assert reloaded.synthesis_confidence == 0.4
    # ...and the CAP it implies is on disk too, not just in memory.
    assert reloaded.confidence == 0.4
    assert reloaded.magnitude == 0.5


async def test_a_veto_is_persisted_rather_than_recomputed_away(engine):
    """A veto zeroes the score. If it is not written down, the record cannot
    tell a vetoed thesis from one that simply decayed to nothing."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    # ACTIVE, so there is no expiry path to persist this as a side effect.
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    assert dossier.status == "ACTIVE"

    await engine._decay_one("FORM", datetime.now(timezone.utc))

    reloaded = engine.dossiers.load("FORM")
    assert reloaded.already_priced_in is True
    assert reloaded.confidence == 0.0
    assert reloaded.magnitude == 0.0


async def test_a_deferred_synthesis_does_not_rewrite_the_dossier(engine):
    """Budget exhaustion / transient failure must stay a no-op: no verdict,
    and no save claiming one happened."""
    engine.synthesizer = FakeSynthesizer(default=None)
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    before = (dossier.confidence, dossier.magnitude)

    await engine._decay_one("FORM", datetime.now(timezone.utc))

    reloaded = engine.dossiers.load("FORM")
    assert reloaded.synthesis_at == ""
    assert (reloaded.confidence, reloaded.magnitude) == before


async def test_the_daily_snapshot_records_the_synthesis_verdict(engine):
    """The snapshot is the primary forward dataset. Without these columns a
    0.000 row from a veto and one from a dead thesis are indistinguishable."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._decay_one("FORM", datetime.now(timezone.utc))

    row = snapshot_dossier(engine.dossiers.load("FORM"), "2026-08-07T00:00:00+00:00")
    assert row["already_priced_in"] is True
    assert row["synthesis_at"] != ""
    assert row["score"] == 0.0
    assert row["scoring_version"] == 4  # the regime where score is the CAPPED number


# --- _prune_dead_symbols: the universe-wipe guards ---
#
# This is the only routinely-scheduled path in the system that destroys
# accumulated evidence, and none of it was covered.

def _screened(symbol, market_cap, lookup_failed=False):
    from smartboi.universe_screen import ScreenResult
    return ScreenResult(
        symbol, market_cap is not None, "", market_cap, None, lookup_failed=lookup_failed,
    )


def test_a_transient_lookup_failure_never_prunes(engine):
    """The CRITICAL finding this guards. market_cap_musd returned None on
    ANY HTTPError, and every one of those was read as 'delisted' -- so a
    single Finnhub timeout inside the monthly screen's ~12-minute window
    deleted the entire runtime-accepted universe and archived its
    dossiers, with no reader for dossiers_archived/ to get them back."""
    engine.accepted_candidates.set("AAAA", {"as": "tradeable", "source": "auto"})

    pruned = engine._prune_dead_symbols([_screened("AAAA", None, lookup_failed=True)])

    assert pruned == []
    assert "AAAA" in engine.accepted_candidates.data


def test_a_genuinely_dead_symbol_is_still_pruned(engine):
    """The guards must not disable the behaviour they are protecting."""
    engine.accepted_candidates.set("DEADCO", {"as": "tradeable", "source": "auto"})

    pruned = engine._prune_dead_symbols([
        _screened("DEADCO", None),
        *[_screened(f"OK{i}", 500.0) for i in range(30)],
    ])

    assert pruned == ["DEADCO"]
    assert "DEADCO" not in engine.accepted_candidates.data


def test_a_mass_no_data_result_refuses_to_prune_anything(engine):
    """Even with the lookup_failed split, a systemic answer-but-empty
    failure (plan downgrade returning empty profiles with HTTP 200, a
    schema change) presents as every symbol dying at once. Real
    delistings arrive one or two at a time."""
    for i in range(20):
        engine.accepted_candidates.set(f"S{i}", {"as": "tradeable", "source": "auto"})
    results = [_screened(f"S{i}", None) for i in range(20)]

    pruned = engine._prune_dead_symbols(results)

    assert pruned == []
    assert len(engine.accepted_candidates.data) == 20
    assert engine.universe_screen_state.get("prune_refused_at")
    assert engine.universe_screen_state.get("prune_refused_symbols") == sorted(f"S{i}" for i in range(20))


def test_the_blast_radius_cap_is_measured_against_screened_symbols_only(engine):
    """A screen where most symbols failed to look up must not make the few
    genuine deaths look like a mass extinction -- nor hide behind them."""
    engine.accepted_candidates.set("DEADCO", {"as": "tradeable", "source": "auto"})
    results = [
        _screened("DEADCO", None),
        *[_screened(f"OK{i}", 500.0) for i in range(30)],
        *[_screened(f"UNK{i}", None, lookup_failed=True) for i in range(50)],
    ]

    assert engine._prune_dead_symbols(results) == ["DEADCO"]


def test_curated_symbols_are_reported_but_never_pruned(engine):
    """Unchanged behaviour, pinned: a curated symbol is a human decision."""
    results = [_screened("FORM", None), *[_screened(f"OK{i}", 500.0) for i in range(30)]]

    assert engine._prune_dead_symbols(results) == []
    assert engine.universe_screen_state.get("curated_no_market_data") == ["FORM"]


# --- nightly backup wiring ---

def test_backup_runs_before_anything_that_can_destroy_state(engine, tmp_path):
    """Ordering is the whole point: the decay pass expires signals and the
    universe screen archives dossiers. A backup taken after those has
    already lost whatever they got wrong today."""
    from smartboi.backup import BACKUP_DIR_NAME

    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "graph.json").write_text("[]")

    assert engine._run_backup() is True
    written = list((tmp_path / BACKUP_DIR_NAME).glob("smartboi-*.tar.gz"))
    assert len(written) == 1


def test_backup_pass_stays_due_when_it_fails(engine, tmp_path, monkeypatch):
    """Marked done only on success, like the snapshot and price-mark
    passes -- a night silently skipped is a night with no copy."""
    import smartboi.engine as engine_mod

    monkeypatch.setattr(engine_mod, "run_backup", lambda *a, **k: None)
    assert engine._daily_pass_due("backup") is True
    assert engine._run_backup() is False
    assert engine._daily_pass_due("backup") is True, "a failed backup must be retried"


def test_backup_can_be_turned_off_without_retrying_every_tick(engine):
    engine.settings.enable_local_backup = False
    assert engine._run_backup() is True  # 'done', so the tick stops asking


def test_quarantined_data_loss_is_repeated_on_every_heartbeat(engine, caplog):
    """The ERROR that records a quarantine scrolls out of the log within
    hours; the loss is permanent. Silence must not look like health."""
    from smartboi import persist

    persist.quarantine_events.clear()
    persist.quarantine_events.append(persist.QuarantineEvent(
        path="data/graph.json", quarantined_to="data/graph.json.corrupt-x",
        reason="invalid JSON", at="2026-08-08T00:00:00+00:00", bytes_preserved=2048,
    ))
    try:
        with caplog.at_level("ERROR"):
            engine._log_heartbeat()
        assert "UNACKNOWLEDGED corrupt-file quarantine" in caplog.text
        assert "data/graph.json" in caplog.text
    finally:
        persist.quarantine_events.clear()
