"""Engine-level integration tests using scripted fakes (see fakes.py) --
covers the documented invariants pure-module unit tests can't reach: the
retry/registration semantics around a deferred LLM call, the propagation
cooldown's definitive-only recording, and the full signal -> snapshot ->
open -> close -> reset lifecycle."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartboi.config import Settings
from smartboi.edgar import FilingEvent
import smartboi.engine
from smartboi.dossier import (
    ECOSYSTEM_ASSOCIATION_CONFIDENCE,
    SCORING_VERSION,
    Dossier,
    EvidenceRecord,
    merge_evidence,
    recompute_decay,
)
from smartboi.engine import ECOSYSTEM_LINK_CONFIDENCE, Engine, is_common_equity
from smartboi.universe import CompanySpec
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

# --- Fixture dates are computed from the real clock, never hardcoded. ---
# Engine paths score evidence against datetime.now() internally, so a literal
# calendar date in a fixture re-ages every day the suite runs: it drifts down
# the decay curve and eventually across evidence_is_stale's cutoff, flipping
# test outcomes weeks after the test was written. 29 tests in this file went
# red exactly that way between 2026-08-13 and 2026-08-18 with no code change.
# The only literals still allowed (enforced by
# test_no_hardcoded_fresh_dates_in_this_file) are the deliberately-old
# backfill/candidate ordering marks, whose intent -- "in the distant past" --
# is the one thing aging cannot break.
_TODAY = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _days_ago(n: int, hour: int = 0) -> str:
    """Full ISO timestamp n days back (00:00 UTC unless an hour is given)."""
    return (_TODAY - timedelta(days=n)).replace(hour=hour).isoformat()


def _day_ago(n: int) -> str:
    """Date-only form (YYYY-MM-DD) n days back, for filing_date/published_at."""
    return (_TODAY - timedelta(days=n)).strftime("%Y-%m-%d")


FRESH_DAY = _day_ago(1)   # yesterday: evidence that must score at full weight
FRESH_TS = _days_ago(1)   # the same day as a full timestamp
RECENT_FILING_DAY = _day_ago(3)  # a filing a few days back, well inside freshness



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
                    url="https://finnhub.io/api/news/1", published_at=FRESH_TS),
        NewsArticle(symbol="FORM", headline="Headline B", summary="s", source="Bloomberg",
                    url="https://finnhub.io/api/news/2", published_at=FRESH_TS),
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
                    url="https://finnhub.io/api/news/1", published_at=FRESH_TS),
        # A different publisher syndicating the exact same story, same day --
        # the dedup FINGERPRINT (symbol:normalized_headline:date) is what
        # collapses this, deliberately independent of source identity.
        NewsArticle(symbol="FORM", headline="Same Headline", summary="s", source="Yahoo",
                    url="https://finnhub.io/api/news/2", published_at=FRESH_TS),
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
        symbol="FORM", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="FORM", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="FORM", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="UCTT", cik10="0000000002", form="10-K", filing_date=_day_ago(2),
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
        "seen_count": 3, "first_seen_at": _days_ago(30),
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


# --- Connectivity reconcile: grow only with anchors that land connected to a
# tradeable, prune runtime-accepted anchors that reach none (see
# Engine.reconcile_universe_connectivity). ---

def _connected_candidate(engine, ticker, from_symbol, rel="customer", conf=0.9, **extra):
    """A discovered candidate whose pending edge is disclosed BY `from_symbol`,
    so accepting it replays that edge into the graph (see _promote_pending_edges).
    Disclosed by a tradeable => lands connected; by an anchor => still inert."""
    entry = {
        "name": f"{ticker} Inc", "ticker": ticker,
        "related_to": [from_symbol], "rel_types": [rel],
        "description": "", "sources": [], "seen_count": 1,
        "pending_edges": [{"from_symbol": from_symbol, "rel_type": rel,
                           "description": "disclosed", "confidence": conf, "source": "u"}],
    }
    entry.update(extra)
    engine.candidates.set(ticker, entry)
    return entry


async def test_reconcile_grows_connected_anchor_and_promotes_its_edge(engine):
    """A candidate disclosed by a tradeable (FORM) is accepted as an anchor and
    lands CONNECTED: its edge enters the graph and it is classified into the
    discovering tradeable's ecosystem, not the inert 'accepted' bucket."""
    _connected_candidate(engine, "NEWCO", from_symbol="FORM")

    result = await engine.reconcile_universe_connectivity(apply=True)

    assert "NEWCO" in result["added"]
    assert engine.spec_by_symbol["NEWCO"].signal_source_only is True  # an anchor
    assert engine.spec_by_symbol["NEWCO"].ecosystem == engine.spec_by_symbol["FORM"].ecosystem
    assert any(r.from_symbol == "FORM" and r.to_symbol == "NEWCO"
               for r in engine.graph.relationships)


# --- Graph maintenance: quarantine is removal from the live universe, never
# deletion, and never of something holding an open position. ---

def _finding(kind, subject, actionable=True, blocked=""):
    from smartboi.graph_audit import Finding

    return Finding(kind, subject, "because", actionable, blocked)


async def test_quarantine_dry_run_reports_without_mutating(engine):
    from smartboi.graph_audit import KIND_DEAD_LISTING

    engine.accept_candidate("DEADCO", "anchor", source="auto")
    findings = [_finding(KIND_DEAD_LISTING, "DEADCO")]

    result = engine.quarantine_from_findings(findings, apply=False)

    assert result["applied"] is False
    assert [r["symbol"] for r in result["would_quarantine"]] == ["DEADCO"]
    assert "DEADCO" in engine.spec_by_symbol
    assert not engine.quarantine.data


async def test_quarantine_removes_from_the_universe_but_keeps_the_row(engine):
    from smartboi.graph_audit import KIND_DEAD_LISTING

    engine.accept_candidate("DEADCO", "anchor", source="auto")
    findings = [_finding(KIND_DEAD_LISTING, "DEADCO")]

    result = engine.quarantine_from_findings(findings, apply=True)

    assert result["quarantined"] == ["DEADCO"]
    assert "DEADCO" not in engine.spec_by_symbol          # out of the live universe
    assert "DEADCO" not in engine.accepted_candidates.data
    row = engine.quarantine.get("DEADCO")                 # but recoverable, with the reason
    assert row["was"]["as"] == "anchor"
    assert any(KIND_DEAD_LISTING in r for r in row["reasons"])


async def test_quarantine_never_acts_on_a_non_actionable_finding(engine):
    from smartboi.graph_audit import KIND_DEAD_LISTING

    engine.accept_candidate("DEADCO", "anchor", source="auto")
    findings = [_finding(KIND_DEAD_LISTING, "DEADCO", actionable=False,
                         blocked="has an OPEN paper trade")]

    result = engine.quarantine_from_findings(findings, apply=True)

    assert result["would_quarantine"] == []
    assert "DEADCO" in engine.spec_by_symbol


async def test_a_quarantined_symbol_is_not_re_accepted(engine):
    """Without this the next auto-accept re-adds it from the same candidate row
    and the clean undoes itself on a schedule."""
    from smartboi.graph_audit import KIND_DEAD_LISTING

    engine.settings.enable_auto_accept_candidates = True
    engine.settings.auto_accept_anchors = True
    engine.candidates.set("ZOMBI", {
        "name": "Zombi Inc", "ticker": "ZOMBI", "related_to": ["FORM"],
        "rel_types": ["customer"], "description": "", "sources": [], "seen_count": 3,
        "recommended_as": "anchor", "recommendation_reason": "big",
        "pending_edges": [{"from_symbol": "FORM", "rel_type": "customer",
                           "description": "d", "confidence": 0.9, "source": "u"}],
    })
    engine.accept_candidate("ZOMBI", "anchor", source="auto")
    engine.quarantine_from_findings([_finding(KIND_DEAD_LISTING, "ZOMBI")], apply=True)
    assert "ZOMBI" not in engine.spec_by_symbol

    await engine._auto_accept_candidates()

    assert "ZOMBI" not in engine.spec_by_symbol
    result = await engine.reconcile_universe_connectivity(apply=True)
    assert "ZOMBI" not in (result["added"] or [])


async def test_audit_skips_the_liveness_check_when_sec_is_unreachable(engine, monkeypatch):
    """An unreachable SEC must never read as "the whole universe is delisted"."""
    from smartboi.graph_audit import KIND_DEAD_LISTING

    async def no_map():
        return None

    monkeypatch.setattr(engine.edgar_client, "live_tickers", no_map, raising=False)
    engine.accept_candidate("DEADCO", "anchor", source="auto")

    findings = await engine.audit_universe()

    assert KIND_DEAD_LISTING not in {f.kind for f in findings}


async def test_graph_maintenance_dry_run_changes_nothing(engine, monkeypatch):
    from smartboi.tools import run_graph_maintenance

    async def no_map():
        return None

    monkeypatch.setattr(engine.edgar_client, "live_tickers", no_map, raising=False)
    engine.accept_candidate("KEEPME", "anchor", source="auto")
    before = set(engine.spec_by_symbol)

    report = await run_graph_maintenance(engine, apply=False)

    assert "DRY RUN" in report
    assert set(engine.spec_by_symbol) == before
    assert not engine.quarantine.data
    # The five steps must all be present and in order -- growing before
    # cleaning would re-admit what was just removed.
    for step in ("1. AUDIT", "2. CLEAN", "3. RESOLVE", "4. DISCOVER", "5. CONNECT"):
        assert step in report


async def test_reconcile_grows_a_tradeable_candidate_as_tradeable(engine):
    """The GROW arm used to hardcode "anchor", which permanently converted a
    candidate that screens TRADEABLE -- _auto_accept_candidates skips anything
    already accepted, and no promote-to-tradeable path exists anywhere. A
    connectivity tool must not spend the scarcest thing in the universe."""
    _connected_candidate(engine, "SMALLCO", from_symbol="FORM",
                         recommended_as="tradeable",
                         recommendation_reason="market cap $200M fits the profile")

    result = await engine.reconcile_universe_connectivity(apply=True)

    assert "SMALLCO" in result["added"]
    assert engine.spec_by_symbol["SMALLCO"].signal_source_only is False  # a tradeable
    assert engine.accepted_candidates.get("SMALLCO")["as"] == "tradeable"


async def test_reconcile_grows_an_otc_adr_as_an_anchor_even_if_screened_tradeable(engine):
    """The arm's own filter (_anchor_equity_ok) is LOOSER than is_common_equity:
    it admits OTC ADRs, which accept_candidate rejects as tradeables by raising.
    Passing one through as "tradeable" would abort the apply loop partway and
    leave the reconcile half-applied, so it degrades to anchor instead."""
    _connected_candidate(engine, "ADRXY", from_symbol="FORM",
                         recommended_as="tradeable",
                         recommendation_reason="market cap $200M fits the profile")

    result = await engine.reconcile_universe_connectivity(apply=True)

    assert "ADRXY" in result["added"]
    assert engine.spec_by_symbol["ADRXY"].signal_source_only is True  # anchor, not tradeable


async def test_reconcile_still_grows_an_unscreened_candidate_as_an_anchor(engine):
    """No recommendation is the normal state for a fresh candidate. Unvetted
    must stay anchor -- the conservative direction."""
    _connected_candidate(engine, "NOREC", from_symbol="FORM")

    result = await engine.reconcile_universe_connectivity(apply=True)

    assert "NOREC" in result["added"]
    assert engine.spec_by_symbol["NOREC"].signal_source_only is True


async def test_reconcile_skips_a_candidate_disclosed_only_by_an_anchor(engine):
    """INTC is an anchor here, so a candidate it alone discloses would make an
    anchor->anchor edge -- still inert. It must not be grown."""
    _connected_candidate(engine, "ANCHORONLY", from_symbol="INTC", rel="competitor")

    result = await engine.reconcile_universe_connectivity(apply=True)

    assert "ANCHORONLY" not in (result["added"] or [])
    assert "ANCHORONLY" not in engine.spec_by_symbol


async def test_reconcile_skips_preferred_and_name_mismatch(engine):
    """A preferred/share-class ticker is not anchorable equity; a ticker whose
    SEC name doesn't verify is a misresolution. Both are reported, not grown."""
    _connected_candidate(engine, "SCE-PM", from_symbol="FORM")
    _connected_candidate(engine, "WRONG", from_symbol="FORM", name="Totally Different Co")
    engine.edgar_client.name_matches = False  # every name-match check fails

    result = await engine.reconcile_universe_connectivity(apply=True)

    assert result["added"] == []
    skipped = {s["symbol"] for s in result["add_skipped"]}
    assert "SCE-PM" in skipped and "WRONG" in skipped


async def test_reconcile_prunes_inert_accepted_anchor_but_keeps_curated_seed(engine):
    """An inert runtime-accepted anchor is removed; an inert curated seed anchor
    (INTC, from anchor_symbols) is reported but never deleted."""
    engine.accept_candidate("DEADWT", "anchor", source="auto")  # no edges -> inert
    assert "DEADWT" in engine.spec_by_symbol

    result = await engine.reconcile_universe_connectivity(apply=True)

    assert "DEADWT" in result["pruned"]
    assert "DEADWT" not in engine.spec_by_symbol
    assert "DEADWT" not in engine.accepted_candidates.data
    assert "INTC" in result["inert_seed_anchors"]
    assert "INTC" in engine.spec_by_symbol  # curated seed is kept


async def test_reconcile_dry_run_reports_without_mutating(engine):
    _connected_candidate(engine, "NEWCO", from_symbol="FORM")
    before = set(engine.spec_by_symbol)

    result = await engine.reconcile_universe_connectivity(apply=False)

    assert result["applied"] is False
    assert [a["symbol"] for a in result["would_add"]] == ["NEWCO"]
    assert set(engine.spec_by_symbol) == before
    assert "NEWCO" not in engine.accepted_candidates.data
    assert not any(r.to_symbol == "NEWCO" for r in engine.graph.relationships)


async def test_auto_accept_anchor_requires_a_tradeable_connection(engine):
    """The safe gate: with anchor auto-accept ON, only a candidate that lands
    connected to a tradeable is accepted; one disclosed solely by an anchor is
    left pending -- the flood that built the 221-inert board can't recur."""
    engine.settings.enable_auto_accept_candidates = True
    engine.settings.auto_accept_anchors = True
    _connected_candidate(engine, "REAL", from_symbol="FORM", recommended_as="anchor")
    _connected_candidate(engine, "FANOUT", from_symbol="INTC", rel="competitor",
                         recommended_as="anchor")

    await engine._auto_accept_candidates()

    assert "REAL" in engine.spec_by_symbol
    assert "FANOUT" not in engine.spec_by_symbol


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


async def test_monthly_screen_demotes_a_graduated_tradeable(engine):
    """A runtime-accepted tradeable that outgrows the cap ceiling on its own is
    demoted to anchor by the monthly screen, WITHOUT the operator ever changing
    the bounds -- the drift the reconcile pass's bounds-gated recheck could
    never catch (AUDIT-2026-08-FOLLOWUP HIGH-2)."""
    engine.accepted_candidates.set(
        "GRAD", {"as": "tradeable", "source": "auto", "ecosystem": "semi_equipment"})
    engine.universe = engine.universe + [CompanySpec("GRAD", "Graduated Co", "semi_equipment")]
    engine.spec_by_symbol = {c.symbol: c for c in engine.universe}
    engine.finnhub.market_cap_by_symbol["GRAD"] = 500_000.0  # $500B -- far past the ceiling

    await engine._run_universe_screen()

    assert engine.accepted_candidates.get("GRAD")["as"] == "anchor"
    assert engine.spec_by_symbol["GRAD"].signal_source_only is True


async def test_monthly_screen_leaves_a_subfloor_tradeable_alone(engine):
    """A tradeable that dips below the floor is still a valid (just smaller)
    trade target -- recommend_candidate_type keeps it 'tradeable' -- so the
    screen must NOT demote it, only graduated (over-ceiling) names."""
    engine.accepted_candidates.set(
        "SMALL", {"as": "tradeable", "source": "auto", "ecosystem": "semi_equipment"})
    engine.universe = engine.universe + [CompanySpec("SMALL", "Small Co", "semi_equipment")]
    engine.spec_by_symbol = {c.symbol: c for c in engine.universe}
    engine.finnhub.market_cap_by_symbol["SMALL"] = 10.0  # $10M -- below the floor, still tradeable

    await engine._run_universe_screen()

    assert engine.accepted_candidates.get("SMALL")["as"] == "tradeable"


async def test_monthly_screen_does_not_demote_a_curated_tradeable(engine):
    """Only RUNTIME-ACCEPTED symbols are the screen's to overrule; a curated
    symbol (universe.py / SYMBOLS) that outgrows the bounds is reported loudly
    but left in place, exactly as _prune_dead_symbols treats curated dead ones."""
    engine.universe = engine.universe + [CompanySpec("CURATED", "Curated Co", "semi_equipment")]
    engine.spec_by_symbol = {c.symbol: c for c in engine.universe}
    engine.finnhub.market_cap_by_symbol["CURATED"] = 500_000.0  # graduated, but curated

    await engine._run_universe_screen()

    assert "CURATED" not in engine.accepted_candidates.data
    assert engine.spec_by_symbol["CURATED"].signal_source_only is False  # untouched


# --- Invariant: evidence is registered only on definitive handling ---

async def test_deferred_updater_is_not_definitive(engine):
    engine.updater.default = None  # simulates budget exhaustion / a transient failure
    handled = await engine._process_evidence(
        origin_symbol="FORM", evidence_text="some news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
    )
    assert handled is False


async def test_refuted_evidence_is_definitive_but_does_not_merge(engine):
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=True)
    handled = await engine._process_evidence(
        origin_symbol="FORM", evidence_text="some news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
    )
    assert handled is True
    dossier = engine.dossiers.load("FORM")
    assert dossier.evidence == []  # refuted: handled, but nothing merged


async def test_accepted_evidence_is_definitive_and_merges(engine):
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=False)
    handled = await engine._process_evidence(
        origin_symbol="FORM", evidence_text="some news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
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
        "https://x/1", "h1", FRESH_DAY,
    )
    assert first == "deferred"
    assert len(engine.updater.calls) == 1

    engine.skeptic.queue(verdict(refuted=False))  # retry: skeptic now answers
    second = await engine._update_dossier(
        "FORM", "evidence text", "FORM", "", None, "news", "reuters.com",
        "https://x/1", "h1", FRESH_DAY,
    )
    assert second == "handled"
    # The cached proposal from the first attempt was reused -- propose_update
    # was never called a second time for the same evidence.
    assert len(engine.updater.calls) == 1


# --- Invariant: the propagation cooldown slot is only consumed once a
# target's evidence is DEFINITIVELY handled, never on a deferred attempt ---

async def test_propagation_slot_not_consumed_on_deferred_attempt(engine):
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, FRESH_DAY))
    engine.updater.default = proposal()
    engine.skeptic.default = None  # every attempt against FORM is deferred

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="Intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
    )
    now = time.monotonic()
    assert engine._propagation_limiter.would_allow("INTC->FORM", now)


async def test_propagation_slot_consumed_once_definitively_handled(engine):
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, FRESH_DAY))
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=True)  # definitively refused -- still "handled"

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="Intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
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
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
    )
    dossier = engine.dossiers.load("FORM")
    assert dossier.status == "ACTIVE"  # only 1 independent source so far -- not enough to signal

    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="evidence 2", source_type="news",
        source_name="bloomberg.com", url="https://x/2", headline="h2", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
        symbol="AMPX", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="UFPT", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="TAYD", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="EPAC", cik10="0000000001", form="8-K", filing_date=RECENT_FILING_DAY,
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
        symbol="UFPT", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="FORM", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
        symbol="FORM", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
        )
    assert engine.dossiers.load("FORM").status == "SIGNALED"

    # Strong opposing evidence arrives before any trade opened: direction
    # flips/collapses below threshold -- the signal must expire, not linger.
    engine.updater.default = proposal(direction="SHORT", magnitude=0.9, confidence=0.9)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.9, adjusted_magnitude=0.9)
    for i in range(3):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"bad news {i}", source_type="news",
            source_name=f"src{i}.com", url=f"https://y/{i}", headline=f"bad{i}", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
        )
    assert engine.dossiers.load("FORM").status == "ACTIVE"

    # A third, genuinely distinct publisher clears the news-only bar.
    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="evidence 2", source_type="news",
        source_name="wsj.com", url="https://x/2", headline="h2", published_at=FRESH_DAY,
    )
    assert engine.dossiers.load("FORM").status == "SIGNALED"


async def test_filing_evidence_restores_the_normal_bar(engine):
    engine.settings.min_independent_sources_news_only = 3
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)

    await engine._process_evidence(
        origin_symbol="UCTT", evidence_text="news evidence", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="n1", published_at=FRESH_DAY,
    )
    await engine._process_evidence(
        origin_symbol="UCTT", evidence_text="8-K material event", source_type="8-K",
        source_name="SEC EDGAR (8-K)", url="https://sec.gov/1", headline="8-K", published_at=FRESH_DAY,
    )
    # Two sources, one of them a filing: normal bar (2) applies -> signals.
    assert engine.dossiers.load("UCTT").status == "SIGNALED"


# --- Regression: retry of a partially-deferred item must not re-pay LLM
# calls for sibling targets already definitively handled (not-new/refuted),
# and must not burn extra propagation-cooldown slots. ---

async def test_retry_does_not_repay_for_already_refuted_target(engine):
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, FRESH_DAY))
    engine.graph.add(Relationship("INTC", "UCTT", "customer", "Intel is a customer of UCTT", "test", 0.9, FRESH_DAY))

    # Pass 1: FORM's evidence is refuted (propose + skeptic paid), UCTT's
    # skeptic call is deferred -- the item as a whole stays unregistered.
    engine.updater.default = proposal()
    engine.skeptic.queue(verdict(refuted=True))   # FORM: refuted, definitive
    engine.skeptic.queue(None)                    # UCTT: deferred
    scored = await engine._process_evidence(
        origin_symbol="INTC", evidence_text="intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
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
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
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
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "test", 0.9, FRESH_DAY))
    engine.graph.add(Relationship("INTC", "UCTT", "customer", "Intel is a customer of UCTT", "test", 0.9, FRESH_DAY))
    engine.updater.default = proposal()
    engine.skeptic.queue(verdict(refuted=False))  # FORM: merged
    engine.skeptic.queue(None)                    # UCTT: deferred

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
    )
    # FORM consumed its 1 slot; window for INTC->FORM is now full.
    now = time.monotonic()
    assert not engine._propagation_limiter.would_allow("INTC->FORM", now)
    events_after_first = len(engine._propagation_limiter._events["INTC->FORM"])

    engine.skeptic.default = verdict(refuted=False)
    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="intel news", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h1", published_at=FRESH_DAY,
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
        "https://x/1", "h1", FRESH_DAY,
    )
    assert outcome == "handled"
    assert engine.dossiers.load("FORM").evidence == []


# --- Regression: the fact key the model returns must survive the whole way
# to the merged EvidenceRecord. dossier.py's per-fact independence was tested
# on records built directly, and _UPDATE_TOOL declared the field, but nothing
# covered the path BETWEEN them -- and _validated_proposal, a whitelist, was
# quietly dropping it. Live effect: 0 of 970 evidence items carried a label,
# so independence silently counted per publisher/origin symbol (the exact
# over-count fact keys replace) with no error raised anywhere. ---

async def _merge_one(engine, prop):
    engine.updater = FakeUpdater(default=prop)
    engine.skeptic = FakeSkeptic(default=verdict(refuted=False))
    await engine._update_dossier(
        "FORM", "evidence text", "FORM", "", None, "news", "reuters.com",
        "https://x/1", "h1", FRESH_DAY,
    )
    return engine.dossiers.load("FORM").evidence


async def test_fact_key_from_the_proposal_reaches_the_merged_evidence(engine):
    merged = await _merge_one(engine, proposal(fact_key="ai datacenter capex q2 2026"))
    assert len(merged) == 1
    assert merged[0].fact_key == "ai datacenter capex q2 2026"


async def test_missing_fact_key_still_merges_under_pre_fact_key_rules(engine):
    """A model that omits the label must not cost the evidence: it merges
    with an empty key and scores the old way (see dossier.independence_key)."""
    merged = await _merge_one(engine, proposal())  # no fact_key at all
    assert len(merged) == 1
    assert merged[0].fact_key == ""


async def test_overlong_fact_key_is_truncated_not_rejected(engine):
    from smartboi.dossier import MAX_FACT_KEY_CHARS

    merged = await _merge_one(engine, proposal(fact_key="x" * (MAX_FACT_KEY_CHARS + 40)))
    assert len(merged) == 1
    assert merged[0].fact_key == "x" * MAX_FACT_KEY_CHARS


async def test_non_string_fact_key_is_coerced_not_crashing(engine):
    """A non-string here would poison the independence key rather than
    merely be missing -- coerce, don't propagate."""
    merged = await _merge_one(engine, proposal(fact_key=12345))
    assert len(merged) == 1
    assert isinstance(merged[0].fact_key, str)


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
            published_at=FRESH_TS, origin_symbol="FORM", is_propagated=False,
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
            published_at=FRESH_TS, origin_symbol="FORM", is_propagated=False,
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
    engine.graph.add(Relationship("INTC", "FORM", "customer", "Intel is a customer of FORM", "t", 0.9, FRESH_DAY))
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=False)

    # max_propagated_evidence_per_link=1 in the fixture: the first item
    # consumes INTC->FORM's only slot.
    assert await engine._process_evidence(
        origin_symbol="INTC", evidence_text="first", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h1", published_at=FRESH_DAY) is True

    # The second finds every link throttled -> zero targets. It must report
    # NOT-definitive so its fingerprint stays unregistered and it is retried.
    assert await engine._process_evidence(
        origin_symbol="INTC", evidence_text="second", source_type="news", source_name="reuters.com",
        url="https://x/2", headline="h2", published_at=FRESH_DAY) is False


async def test_evidence_for_an_unconnected_anchor_is_definitively_done(engine):
    """Empty because there is no target at all is genuinely finished --
    retrying it forever would re-fetch the same article on every poll for
    nothing. Ecosystem fallback is off here so this isolates the
    no-targets path; its own behaviour is covered below."""
    engine.settings.enable_ecosystem_propagation = False
    assert await engine._process_evidence(
        origin_symbol="INTC", evidence_text="x", source_type="news", source_name="reuters.com",
        url="https://x/3", headline="h3", published_at=FRESH_DAY) is True


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
            published_at=FRESH_TS, origin_symbol="FORM", is_propagated=False,
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
        source_name="reuters.com", url="https://x/1", headline="h", published_at=FRESH_DAY)

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
        url="https://x/1", headline="h", published_at=FRESH_DAY)

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
        url="https://x/1", headline="h", published_at=FRESH_DAY)

    assert engine.dossiers.load("FORM").has_disclosed_link_evidence is False


async def test_a_disclosed_edge_suppresses_the_ecosystem_fallback(engine):
    """A disclosed contract is strictly better evidence -- the fallback must
    not run alongside it or double up on the same target."""
    engine.graph.add(Relationship("FORM", "INTC", "customer", "Intel is a customer of FORM", "t", 0.95, FRESH_DAY))
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="x", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h", published_at=FRESH_DAY)

    notes = [c["relationship_note"] for c in engine.updater.calls]
    assert all("ecosystem" not in n for n in notes)
    # UCTT shares the ecosystem but has no disclosed link -- it must NOT be
    # reached, because FORM's disclosed edge suppressed the fallback wholesale.
    assert not engine.dossiers.load("UCTT").evidence


async def test_a_throttled_disclosed_link_does_not_fall_back(engine):
    """Otherwise the fallback routes around the very cooldown it just hit,
    reaching the same target by a weaker route."""
    engine.graph.add(Relationship("FORM", "INTC", "customer", "Intel is a customer of FORM", "t", 0.95, FRESH_DAY))
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)

    # max_propagated_evidence_per_link=1 in the fixture: this consumes it.
    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="first", source_type="news", source_name="reuters.com",
        url="https://x/1", headline="h1", published_at=FRESH_DAY)
    before = len(engine.updater.calls)

    await engine._process_evidence(
        origin_symbol="INTC", evidence_text="second", source_type="news", source_name="bloomberg.com",
        url="https://x/2", headline="h2", published_at=FRESH_DAY)

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
            published_at=FRESH_DAY)

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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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


async def test_a_flip_refire_resets_the_grace_period(engine):
    """A direction flip that still qualifies re-fires a fresh episode on the
    merge path (_fire_signal). It must reset entry_attempts so the new episode
    is born WITH its grace period -- the merge path used to leave the prior
    episode's count, so the flipped episode could be expired unseen (the exact
    failure the grace period exists to prevent). The decay path already reset
    via _reset_to_active; the merge path did not."""
    from smartboi.signals import evaluate

    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    dossier = Dossier(
        symbol="FORM", direction="SHORT", confidence=0.9, magnitude=0.9,
        independent_source_count=3, has_filing_evidence=True, status="SIGNALED",
        signaled_at=_days_ago(7), signaled_direction="LONG", entry_attempts=2,
    )
    signal = evaluate(dossier, engine.settings.signal_confidence_threshold,
                      engine.settings.min_independent_sources,
                      engine.settings.min_independent_sources_news_only)
    assert signal is not None  # the flipped thesis still clears the bar -> re-fires

    await engine._fire_signal(dossier, signal)

    assert dossier.entry_attempts == 0            # fresh episode -> grace period restored
    assert dossier.signaled_direction == "SHORT"  # re-baselined to the new direction


async def test_the_entry_gate_expires_a_thesis_that_flipped(engine):
    """The entry gate's OWN flip branch: if merged evidence flipped the
    direction while the status stayed SIGNALED, the gate must refuse to open
    and expire, not take a position opposite the one that signaled -- distinct
    from the merge/decay _should_expire_unopened path above."""
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)  # SIGNALED LONG
    dossier = engine.dossiers.load("FORM")
    dossier.direction = "SHORT"  # flipped; status still SIGNALED as LONG
    dossier.confidence, dossier.magnitude = 0.9, 0.9  # still well over the bar
    engine.dossiers.save(dossier)

    await engine._try_open_from_signal("FORM", dossier)

    assert not engine.journal.has_open("FORM")               # nothing opened
    assert engine.dossiers.load("FORM").status == "ACTIVE"   # expired back to active


async def test_a_runaway_horizon_is_clamped_to_the_max_hold(engine):
    """horizon_days is clamped to max_horizon_days, so a mis-scored 900 can't
    keep a stale item at full decay weight for years (staleness scales off it)."""
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=900)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)

    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="e", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h", published_at=FRESH_DAY,
    )

    rec = engine.dossiers.load("FORM").evidence[-1]
    assert rec.horizon_days == engine.settings.max_horizon_days  # 900 clamped to 21


async def test_the_skeptic_adjustment_replaces_the_proposed_values(engine):
    """The stored evidence uses the skeptic's adjusted_* (its contrarian /
    link-strength rescaling), not the updater's proposed numbers -- and keeps
    the pre-skeptic values so the pass's effect stays measurable."""
    engine.updater.default = proposal(direction="LONG", magnitude=0.9, confidence=0.9, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.4, adjusted_magnitude=0.3)

    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="e", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h", published_at=FRESH_DAY,
    )

    rec = engine.dossiers.load("FORM").evidence[-1]
    assert (rec.confidence, rec.magnitude) == (0.4, 0.3)  # skeptic's, not the proposed 0.9/0.9
    assert (rec.proposed_confidence, rec.proposed_magnitude) == (0.9, 0.9)  # pre-skeptic kept


async def test_inception_price_is_captured_when_the_thesis_first_turns_directional(engine):
    """The pre-signal baseline is snapped the moment the thesis first points
    somewhere tradeable -- well before it fires -- so the entry gate can later
    measure the move that happened WHILE corroboration accumulated."""
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    engine.updater.default = proposal(direction="LONG", magnitude=0.6, confidence=0.7, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.7, adjusted_magnitude=0.6)

    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="e", source_type="news",
        source_name="reuters.com", url="https://x/1", headline="h", published_at=FRESH_DAY,
    )

    d = engine.dossiers.load("FORM")
    assert d.direction == "LONG" and d.status == "ACTIVE"   # directional but not yet signaled
    assert d.inception_price == 10.0 and d.inception_direction == "LONG"


async def test_the_drift_guard_measures_from_inception_not_just_fire(engine):
    """If the price already ran up while the thesis accumulated, the entry gate
    skips -- even when nothing drifted between fire and entry. The fire-time
    baseline alone would have opened into a move that was already gone."""
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)                    # SIGNALED LONG
    dossier = engine.dossiers.load("FORM")
    # The move happened during accumulation: inception low, fired after the run-up.
    dossier.inception_price, dossier.inception_direction = 10.0, "LONG"
    dossier.signaled_price = 11.5                 # fire -> now will be flat
    engine.dossiers.save(dossier)
    engine.price_feed.prices["FORM"] = 11.5       # 0% drift from the fire baseline...

    await engine._try_open_from_signal("FORM", dossier)

    # ...but +15% from inception (>5% fixture bar) -> skipped, not opened.
    assert not engine.journal.has_open("FORM")


async def test_inception_is_cleared_when_a_signal_expires(engine):
    """Each fresh episode measures its own accumulation window, so an expiry
    clears the inception baseline for the next thesis to re-capture."""
    engine.price_feed = FakePriceFeed(prices={"FORM": 10.0})
    await _signal_form(engine)
    dossier = engine.dossiers.load("FORM")
    assert dossier.inception_price is not None     # captured during accumulation

    engine._reset_to_active(dossier)

    assert dossier.inception_price is None and dossier.inception_at == ""
    assert dossier.inception_direction == ""


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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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


async def test_an_open_position_is_still_judged_by_synthesis(engine):
    """The synthesis bypass: converting to a trade used to make a symbol
    exempt from the only pass that reads its evidence as a body, for exactly
    as long as capital was committed to it. Measured on the 2026-08-15 board,
    three of four live positions had never been synthesised even once."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(confidence=0.4, magnitude=0.5,
                                                           distinct_fact_count=1))
    dossier = await _build_thesis(engine, confidence=0.9, magnitude=0.9)
    engine.journal.open(
        symbol="FORM", direction="LONG", entry_price=10.0, stop_loss_pct=50.0,
        take_profit_pct=100.0, horizon_days=21, thesis_summary="t", confidence=0.9,
        independent_source_count=2, citations=[],
    )
    assert engine.journal.has_open("FORM")
    calls_before = len(engine.synthesizer.calls)

    await engine._decay_one("FORM", datetime.now(timezone.utc))

    assert len(engine.synthesizer.calls) > calls_before, "the open position was never judged"
    assert engine.dossiers.load("FORM").synthesis_at != "", "the verdict never reached disk"


async def test_a_verdict_on_an_open_position_never_acts_on_the_trade(engine):
    """...but it is RECORDED, never acted on. An entry is a committed
    decision: the trade resolves on its own stop, target and horizon, and a
    mid-flight re-judgement must not contradict it."""
    engine.synthesizer = FakeSynthesizer(
        default=synthesis(confidence=0.0, magnitude=0.0, distinct_fact_count=1,
                          already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.9, magnitude=0.9)
    engine.journal.open(
        symbol="FORM", direction="LONG", entry_price=10.0, stop_loss_pct=50.0,
        take_profit_pct=100.0, horizon_days=21, thesis_summary="t", confidence=0.9,
        independent_source_count=2, citations=[],
    )
    trade_before = engine.journal.open_trades["FORM"]
    status_before = engine.dossiers.load("FORM").status
    signals_log = Path(engine.settings.log_dir) / "signals.jsonl"
    rows_before = len(signals_log.read_text().splitlines()) if signals_log.exists() else 0

    await engine._decay_one("FORM", datetime.now(timezone.utc))

    trade_after = engine.journal.open_trades["FORM"]
    assert engine.journal.has_open("FORM"), "a veto closed a committed position"
    assert trade_after.stop_price == trade_before.stop_price
    assert trade_after.target_price == trade_before.target_price
    assert trade_after.horizon_days == trade_before.horizon_days
    assert engine.dossiers.load("FORM").status == status_before, "a veto expired a live signal"
    rows_after = len(signals_log.read_text().splitlines()) if signals_log.exists() else 0
    assert rows_after == rows_before, "the decay pass signalled on a symbol already in a position"


def test_ecosystem_confidence_constants_match():
    """dossier._aggregate excludes ecosystem-association evidence from the
    independent-source count by comparing against its OWN copy of the ecosystem
    link confidence (it cannot import engine). If engine's value ever drifts,
    the containment silently stops matching the value engine actually stamps
    on the evidence -- so pin them equal."""
    assert ECOSYSTEM_ASSOCIATION_CONFIDENCE == ECOSYSTEM_LINK_CONFIDENCE


def test_merge_cap_applies_a_fresh_synthesis_trim(engine):
    now = datetime.now(timezone.utc)
    dossier = Dossier(symbol="FORM", direction="LONG", confidence=0.8, magnitude=0.8,
                      synthesis_at=now.isoformat(),
                      synthesis_confidence=0.4, synthesis_magnitude=0.5)
    engine._cap_with_synthesis(dossier, now)
    assert dossier.confidence == 0.4
    assert dossier.magnitude == 0.5


def test_merge_cap_honours_an_already_priced_in_veto(engine):
    now = datetime.now(timezone.utc)
    dossier = Dossier(symbol="FORM", direction="LONG", confidence=0.9, magnitude=0.9,
                      already_priced_in=True, synthesis_at=now.isoformat())
    engine._cap_with_synthesis(dossier, now)
    assert dossier.confidence == 0.0
    assert dossier.magnitude == 0.0


def test_merge_cap_never_lifts_a_score(engine):
    """A cap, never a lift -- a synthesis value ABOVE the arithmetic must not
    raise it (the whole reason synthesis is min(), not assignment)."""
    now = datetime.now(timezone.utc)
    dossier = Dossier(symbol="FORM", direction="LONG", confidence=0.3, magnitude=0.3,
                      synthesis_at=now.isoformat(),
                      synthesis_confidence=0.9, synthesis_magnitude=0.9)
    engine._cap_with_synthesis(dossier, now)
    assert dossier.confidence == 0.3
    assert dossier.magnitude == 0.3


def test_merge_cap_ignores_a_stale_verdict(engine):
    """Once the daily pass stops refreshing a dossier, the cap lapses rather
    than suppressing the thesis forever -- so a merge past the freshness window
    sees the raw arithmetic, and a genuine direction flip is re-judged by the
    next decay pass instead of being pinned to a days-old veto."""
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=48)).isoformat()
    dossier = Dossier(symbol="FORM", direction="LONG", confidence=0.8, magnitude=0.8,
                      already_priced_in=True, synthesis_at=stale)
    engine._cap_with_synthesis(dossier, now)
    assert dossier.confidence == 0.8
    assert dossier.magnitude == 0.8


async def test_a_synthesis_veto_survives_a_later_evidence_merge(engine):
    """The load-bearing 2.1.1 fix: a dossier the daily synthesis vetoed as
    already-priced-in must STAY capped when fresh evidence merges, instead of
    re-firing on the raw arithmetic _aggregate rebuilds. Before this, the merge
    path never consulted synthesis and the veto was erased by the next item."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    now = datetime.now(timezone.utc)
    await _build_thesis(engine, confidence=0.8, magnitude=0.8)
    await engine._decay_one("FORM", now)
    vetoed = engine.dossiers.load("FORM")
    assert vetoed.already_priced_in is True
    assert vetoed.confidence == 0.0 and vetoed.magnitude == 0.0

    # A brand-new evidence item merges (distinct id -> not deduped as handled).
    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="e2", source_type="news",
        source_name="ft.com", url="https://x/2", headline="h2", published_at=FRESH_DAY,
    )

    after = engine.dossiers.load("FORM")
    assert after.confidence == 0.0 and after.magnitude == 0.0  # veto held, no re-fire


async def test_a_refuted_outcome_survives_a_restart_and_is_not_re_judged(engine):
    """The 2.5 fix: a skeptic-refuted marker is persisted, so after one of the
    several-daily restarts the same (still-unregistered) evidence is NOT
    re-proposed and re-run through a nondeterministic skeptic that could accept
    what the first run refuted."""
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=True)
    args = dict(target_symbol="FORM", evidence_text="e", origin_symbol="FORM",
                relationship_note="", relationship_confidence=None, source_type="news",
                source_name="reuters.com", url="https://x/9", headline="h9",
                published_at=FRESH_DAY)
    assert await engine._update_dossier(**args) == "handled"
    key = f"FORM:news:https://x/9:{FRESH_DAY}"
    assert key in engine._handled_outcomes

    # "Restart": a fresh Engine in the same working dir loads the persisted
    # retry state rather than starting with an empty _handled_outcomes.
    restarted = Engine(engine.settings)
    assert key in restarted._handled_outcomes

    # Re-processing the same evidence is now a no-op, and the skeptic is not
    # re-run -- so it can never accept what the first pass refuted.
    restarted.updater, restarted.skeptic = engine.updater, engine.skeptic
    skeptic_calls_before = len(engine.skeptic.calls)
    assert await restarted._update_dossier(**args) == "already"
    assert len(engine.skeptic.calls) == skeptic_calls_before


async def test_reset_runtime_state_clears_signals_and_trades_but_keeps_evidence(engine):
    """The clean-measurement-window reset: open (previous-regime) paper trades
    are archived, dossiers reset to ACTIVE with their synthesis episode cleared,
    but the accumulated evidence is kept (it re-aggregates under the new rules)."""
    d = Dossier(symbol="FORM", status="SIGNALED", signaled_at=_days_ago(7),
                signaled_price=10.0, already_priced_in=True, synthesis_at=_days_ago(7))
    merge_evidence(d, EvidenceRecord(
        evidence_id="e1", source_type="news", source_name="reuters.com", url="u", headline="h",
        published_at=FRESH_DAY, origin_symbol="FORM", is_propagated=False, relationship_note="",
        direction="LONG", magnitude=0.5, confidence=0.5, horizon_days=20, reasoning="r", skeptic_note=""))
    d.status = "SIGNALED"  # merge_evidence doesn't touch status; keep it signaled for the test
    engine.dossiers.save(d)
    engine.journal.open("FORM", "LONG", 100.0, 8.0, 16.0, 21, "t", 0.9, 3, [])

    result = engine.reset_runtime_state()

    assert result["archived_open_trades"] == ["FORM"]
    assert engine.journal.open_trades == {}
    reloaded = engine.dossiers.load("FORM")
    assert reloaded.status == "ACTIVE"
    assert reloaded.signaled_at == "" and reloaded.already_priced_in is False
    assert reloaded.synthesis_at == ""
    assert len(reloaded.evidence) == 1  # evidence KEPT -- re-aggregates under the new rules


async def test_a_refutation_is_logged_for_the_skeptic_readout(engine):
    """A skeptic refutation left no stored record, so the refutation rate was
    unmeasurable. It now appends to logs/skeptic_refutations.jsonl (see
    skeptic_report / run_skeptic_report)."""
    engine.updater.default = proposal()
    engine.skeptic.default = verdict(refuted=True, reasoning="generic sector filler")
    await engine._update_dossier(
        target_symbol="FORM", evidence_text="e", origin_symbol="INTC",  # propagated: origin != target
        relationship_note="INTC is a customer", relationship_confidence=0.9, source_type="news",
        source_name="reuters.com", url="https://x/ref", headline="h", published_at=FRESH_DAY)

    rows = [json.loads(line) for line in
            (Path(engine.settings.log_dir) / "skeptic_refutations.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "FORM"
    assert rows[0]["is_propagated"] is True
    assert rows[0]["model"] == engine.settings.skeptic_model


async def test_extraction_is_not_re_billed_when_scoring_defers(engine):
    """2.2: relationship extraction (a paid ~150k-char call) runs before
    dossier scoring, and the filing is dedup-registered only once scoring
    completes. When scoring defers on an exhausted budget the filing stays
    unregistered, so the next poll must RETRY SCORING but must NOT re-run the
    already-completed extraction."""
    filing = FilingEvent("FORM", "0000000001", "10-K", RECENT_FILING_DAY, "acc-1", "d10k.htm")
    engine.edgar_client.text_by_accession["acc-1"] = "some 10-K text with a customer"
    engine.extractor.default = []      # extraction runs and returns no edges (not None -> True)
    engine.updater.default = None      # dossier scoring defers (budget-exhausted shape)

    await engine._process_filing("FORM", filing)
    assert len(engine.extractor.calls) == 1                       # extracted once
    assert engine.extracted_filings.get("filing:FORM:acc-1")     # marked extracted
    assert not engine.dedup.is_duplicate("filing:FORM:acc-1")    # scoring deferred -> unregistered

    updater_calls_after_first = len(engine.updater.calls)
    await engine._process_filing("FORM", filing)                 # same filing, next poll

    assert len(engine.extractor.calls) == 1                       # NOT re-extracted
    assert len(engine.updater.calls) > updater_calls_after_first  # but scoring was retried


async def test_already_priced_in_is_a_veto(engine):
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine)

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.confidence == 0.0
    assert dossier.magnitude == 0.0
    assert dossier.already_priced_in is True


async def test_redundant_evidence_trims_rather_than_vetoes(engine):
    """Overlap is a claim about the EVIDENCE, not the price. Live, the model
    answered the overlap question with the price flag on all 77 vetoes in
    three days -- zeroing theses whose actual finding was 'one fact repeated
    across seven counterparties', and arming a price-based falsification test
    against a claim it had never made."""
    engine.synthesizer = FakeSynthesizer(
        default=synthesis(confidence=0.5, magnitude=0.35, redundant_evidence=True))
    dossier = await _build_thesis(engine)

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.redundant_evidence is True
    assert dossier.already_priced_in is False   # NOT the price claim
    # Trimmed to what synthesis rated it, not zeroed.
    assert dossier.confidence == pytest.approx(0.5)
    assert dossier.magnitude == pytest.approx(0.35)


def _returns_bar(close: float):
    """An engine._price_bar stand-in that always prices a symbol at `close`."""
    from smartboi.prices import PriceBar

    async def _bar(_symbol):
        return PriceBar(close=close, high=close, low=close)
    return _bar


async def test_redundant_evidence_does_not_arm_the_price_falsification(engine, monkeypatch):
    """_veto_refuted_by_price watches the tape for a move disproving 'the
    market has absorbed this'. A duplication finding makes no such claim, so
    it must not be put to that test.

    Every OTHER early return is disarmed first -- a baseline price is set and
    the tape is moved far enough to refute -- so already_priced_in is the only
    thing left that can make this False. Without that, the test passed on a
    missing price baseline and would have kept passing with the flag check
    deleted."""
    engine.synthesizer = FakeSynthesizer(
        default=synthesis(redundant_evidence=True, confidence=0.9, magnitude=0.9))
    dossier = await _build_thesis(engine)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    dossier.synthesis_price = 10.0
    monkeypatch.setattr(engine, "_price_bar", _returns_bar(20.0))  # +100%, far past the 8% bar

    assert dossier.redundant_evidence is True
    assert dossier.already_priced_in is False
    assert await engine._veto_refuted_by_price(dossier) is False

    # Positive control: the same tape DOES refute the claim that actually
    # asserts something about price, so the assertion above is about the flag
    # and not about an unreachable code path.
    dossier.already_priced_in = True
    assert await engine._veto_refuted_by_price(dossier) is True


async def test_both_findings_at_once_still_vetoes(engine):
    """Splitting the fields must not weaken the veto: evidence that is BOTH
    redundant and already absorbed is still a thesis with nothing to trade."""
    engine.synthesizer = FakeSynthesizer(
        default=synthesis(already_priced_in=True, redundant_evidence=True))
    dossier = await _build_thesis(engine)

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.confidence == 0.0
    assert dossier.magnitude == 0.0


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
                    published_at=_days_ago(1, hour=12)),
        NewsArticle(symbol="FORM", headline="Intel raises capex guidance for 2027",
                    summary="fab expansion", source="Bloomberg", url="https://finnhub.io/2",
                    published_at=_days_ago(1, hour=13)),
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at=FRESH_DAY,
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
                            filing_date=_day_ago(5), accession_number="a-1",
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
    _mark_backfilled(engine, "INTC", _days_ago(30))   # newest (an anchor)

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
    _mark_backfilled(engine, "FORM", _days_ago(30))

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


# --- The EDGAR full-text search on a schedule. It was written, wired to a
# dashboard button, and then never scheduled -- the same omission that had
# left supplier research at $0.00 spent. It is the cheapest mechanism in the
# system that is size-selected toward small counterparties. ---

async def test_auto_edgar_search_runs_daily_from_the_tick(engine, monkeypatch):
    calls = []

    async def fake_search(_engine):
        calls.append(1)
        return "EDGAR full-text supplier search\n"

    monkeypatch.setattr("smartboi.tools.run_edgar_supplier_search", fake_search)
    engine.settings.enable_auto_edgar_search = True
    engine.settings.enable_auto_supplier_research = False

    await engine._tick()
    assert calls == [1]
    assert not engine._daily_pass_due("edgar_search")

    # A second tick the same day must not re-run it.
    await engine._tick()
    assert calls == [1]


async def test_auto_edgar_search_failure_never_kills_the_tick(engine, monkeypatch):
    async def boom(_engine):
        raise RuntimeError("EFTS exploded")

    monkeypatch.setattr("smartboi.tools.run_edgar_supplier_search", boom)
    engine.settings.enable_auto_edgar_search = True
    engine.settings.enable_auto_supplier_research = False

    await engine._tick()   # must not raise

    assert not engine._daily_pass_due("edgar_search")


async def test_auto_edgar_search_is_skipped_when_disabled(engine, monkeypatch):
    async def fail(_engine):
        raise AssertionError("must not run when the flag is off")

    monkeypatch.setattr("smartboi.tools.run_edgar_supplier_search", fail)
    engine.settings.enable_auto_edgar_search = False
    engine.settings.enable_auto_supplier_research = False

    await engine._tick()   # must not raise


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
        symbol="FORM", cik10="0000000001", form="10-K", filing_date=RECENT_FILING_DAY,
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

    row = snapshot_dossier(engine.dossiers.load("FORM"), _days_ago(6))
    assert row["already_priced_in"] is True
    assert row["synthesis_at"] != ""
    assert row["score"] == 0.0
    # Pinned to the constant, not a literal: every regime from 4 onward
    # records the CAPPED number, so what this asserts is that the snapshot
    # stamps whatever regime produced the row -- not that it is any one of
    # them. Pinning the literal meant a version bump failed this test for a
    # reason unrelated to what it tests.
    assert row["scoring_version"] == SCORING_VERSION


# --- A synthesis verdict is a claim about a moment -- a price and a body of
# evidence. Both move. Until SCORING_VERSION 6 neither could contradict it,
# so `already_priced_in` was re-asserted daily against whatever arrived and
# the only thing that could overturn it was another copy of itself. ---

async def test_a_veto_records_what_synthesis_actually_thought(engine):
    """The record used to carry 0.0/0.0 for every vetoed dossier, so a thesis
    the pass rated 0.9-but-priced-in was indistinguishable from one it rated
    0.05 -- permanently, for the most expensive pass in the system."""
    engine.synthesizer = FakeSynthesizer(
        default=synthesis(confidence=0.9, magnitude=0.8, already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    before = dossier.confidence * dossier.magnitude

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    # The VETO's effect is unchanged -- the score is still zeroed...
    assert dossier.confidence == 0.0
    assert dossier.magnitude == 0.0
    # ...but what the pass thought is no longer destroyed.
    assert dossier.synthesis_confidence == 0.9
    assert dossier.synthesis_magnitude == 0.8
    assert dossier.pre_synthesis_score == pytest.approx(before)


async def test_a_verdict_records_the_price_and_evidence_it_judged(engine):
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)

    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    assert dossier.synthesis_price == 10.0
    assert dossier.synthesis_price_at != ""
    assert len(dossier.synthesis_keys) == 2  # the two publishers behind the thesis


async def test_a_price_that_refutes_the_veto_triggers_a_re_judgement(engine):
    """The veto asserts the market has absorbed this. A move IN THE THESIS
    DIRECTION after the verdict says otherwise -- so it is judged again."""
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    assert dossier.synthesis_price == 10.0
    calls_before = len(engine.synthesizer.calls)

    engine.price_feed.prices["FORM"] = 11.0  # +10% on a LONG, past the 8% bar
    # The merge path recomputes the arithmetic before re-judging; do the same
    # here so the synthesis floor gate sees a live score rather than the
    # veto's zeroes.
    recompute_decay(dossier, datetime.now(timezone.utc))
    await engine._maybe_resynthesize(dossier, datetime.now(timezone.utc))

    assert len(engine.synthesizer.calls) == calls_before + 1, "a fresh verdict was requested"
    assert dossier.veto_falsified_by_price is True, "attributed to the price falsification"


async def test_a_price_move_the_wrong_way_leaves_the_veto_standing(engine):
    """A LONG whose price FELL has not refuted 'already priced in' -- and the
    entry is not one this system wants to take on that basis either."""
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    calls_before = len(engine.synthesizer.calls)

    engine.price_feed.prices["FORM"] = 8.0  # -20% on a LONG
    await engine._maybe_resynthesize(dossier, datetime.now(timezone.utc))

    assert len(engine.synthesizer.calls) == calls_before


async def test_no_price_leaves_the_veto_standing(engine):
    """The fail-safe direction: an unfalsifiable veto suppresses a thesis, it
    never opens a trade."""
    engine.price_feed = FakePriceFeed({})   # nothing priceable
    engine.finnhub = None
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    assert dossier.synthesis_price is None
    calls_before = len(engine.synthesizer.calls)

    await engine._maybe_resynthesize(dossier, datetime.now(timezone.utc))

    assert len(engine.synthesizer.calls) == calls_before


async def test_the_cap_is_replaced_by_a_fresh_verdict_never_merely_skipped(engine):
    """Skipping the cap on a stale verdict would fail OPEN -- the thesis
    re-fires on the raw arithmetic _cap_with_synthesis exists to correct. The
    veto stays in force until a real verdict replaces it."""
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    engine.dossiers.save(dossier)

    # Budget spent, so no fresh verdict can be produced.
    engine.resynthesis_state.set("date", datetime.now(timezone.utc).date().isoformat())
    engine.resynthesis_state.set("count", 99)
    engine.price_feed.prices["FORM"] = 11.0

    reloaded = engine.dossiers.load("FORM")
    recompute_decay(reloaded, datetime.now(timezone.utc))   # arithmetic restored
    assert reloaded.confidence > 0
    await engine._maybe_resynthesize(reloaded, datetime.now(timezone.utc))
    engine._cap_with_synthesis(reloaded, datetime.now(timezone.utc))

    assert reloaded.confidence == 0.0, "the veto must stand when it cannot be re-judged"


async def test_two_new_independent_sources_invalidate_a_verdict(engine):
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    engine.dossiers.save(dossier)
    calls_before = len(engine.synthesizer.calls)

    for i, source in enumerate(("ft.com", "wsj.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"n{i}", source_type="news",
            source_name=source, url=f"https://y/{i}", headline=f"n{i}",
            published_at=FRESH_DAY,
        )

    assert len(engine.synthesizer.calls) > calls_before


async def test_one_new_source_is_ordinary_accumulation_not_invalidation(engine):
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    engine.dossiers.save(dossier)
    calls_before = len(engine.synthesizer.calls)

    await engine._process_evidence(
        origin_symbol="FORM", evidence_text="n", source_type="news",
        source_name="ft.com", url="https://y/1", headline="n", published_at=FRESH_DAY,
    )

    assert len(engine.synthesizer.calls) == calls_before


async def test_off_schedule_re_synthesis_is_capped_per_day(engine):
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)

    engine.price_feed.prices["FORM"] = 11.0
    for _ in range(12):
        dossier.already_priced_in = True
        dossier.synthesis_price = 10.0
        await engine._maybe_resynthesize(dossier, now)

    spent = int(engine.resynthesis_state.get("count", 0))
    assert spent <= 5, f"re-synthesis ran {spent} times against a cap of 5"


# --- 6-K is the one added form that arrives at a cadence capable of
# manufacturing corroboration: direct EDGAR evidence keys independence on
# form AND filing day, so a cross-filer pushing routine announcements would
# mint a fresh independent-source slot with each one. ---

def _filing(form, accession, filing_date=_day_ago(6)):
    from smartboi.edgar import FilingEvent

    return FilingEvent(symbol="FORM", cik10="0000000001", form=form,
                       filing_date=filing_date, accession_number=accession,
                       primary_document="d.htm")


async def test_a_second_6k_on_the_same_day_is_skipped(engine):
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)
    for n in ("a1", "a2"):
        engine.edgar_client.text_by_accession[n] = "material news"

    await engine._process_filing("FORM", _filing("6-K", "a1"))
    await engine._process_filing("FORM", _filing("6-K", "a2"))

    dossier = engine.dossiers.load("FORM")
    assert len(dossier.evidence) == 1, "the second 6-K must not mint a second slot"


async def test_a_6k_on_a_new_filing_date_gets_a_fresh_allowance(engine):
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)
    for n in ("b1", "b2"):
        engine.edgar_client.text_by_accession[n] = "material news"

    await engine._process_filing("FORM", _filing("6-K", "b1", _day_ago(6)))
    await engine._process_filing("FORM", _filing("6-K", "b2", _day_ago(3)))

    assert len(engine.dossiers.load("FORM").evidence) == 2


async def test_the_cap_never_applies_to_other_forms(engine):
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)
    for n in ("c1", "c2"):
        engine.edgar_client.text_by_accession[n] = "material news"

    await engine._process_filing("FORM", _filing("8-K", "c1"))
    await engine._process_filing("FORM", _filing("10-Q", "c2"))

    assert len(engine.dossiers.load("FORM").evidence) == 2


async def test_a_failed_fetch_does_not_burn_the_6k_allowance(engine):
    """The slot is spent only once the text is actually in hand. Consuming it
    on a failed fetch would let the next poll -- which exists to retry exactly
    that case -- find the day's allowance already gone."""
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)
    engine.edgar_client.text_by_accession["d1"] = ""      # fetch fails
    engine.edgar_client.text_by_accession["d2"] = "real"

    await engine._process_filing("FORM", _filing("6-K", "d1"))
    await engine._process_filing("FORM", _filing("6-K", "d2"))

    assert len(engine.dossiers.load("FORM").evidence) == 1


async def test_a_verdict_predating_the_key_record_is_not_read_as_fully_stale(engine):
    """A verdict written before synthesis_keys existed carries an empty set.
    Reading that as 'every current source is new' would invalidate every
    pre-existing verdict on the first merge after deploying and spend the
    whole daily allowance on a migration artifact."""
    engine.price_feed = FakePriceFeed({"FORM": 10.0})
    engine.synthesizer = FakeSynthesizer(default=synthesis(already_priced_in=True))
    dossier = await _build_thesis(engine, confidence=0.5, magnitude=0.5)
    await engine._apply_synthesis(dossier, datetime.now(timezone.utc))

    dossier.synthesis_keys = []          # as an old dossier file deserializes
    recompute_decay(dossier, datetime.now(timezone.utc))
    calls_before = len(engine.synthesizer.calls)

    await engine._maybe_resynthesize(dossier, datetime.now(timezone.utc))

    assert len(engine.synthesizer.calls) == calls_before


# --- EDGAR full-text search: the same safety-critical NEGATIVE property web
# research has, for a stricter reason. A full-text hit is not a disclosure
# ABOUT the anchor -- it is a third party's filing that happens to mention
# it. Routed to evidence it would score one company's 10-K against another
# company's dossier. Candidates only. ---

def _hit(adsh="0001050915-25-000012", company="ICHOR HOLDINGS, LTD. (ICHR)"):
    from smartboi.edgar_search import SearchHit

    return SearchHit(adsh=adsh, cik="0001050915", company=company, form="10-K",
                     filing_date="2025-02-14", document="ichr-20250101.htm")


def _anchor_spec(engine):
    """The anchor run_edgar_supplier_search will pick first."""
    return next(c for c in engine.universe if c.signal_source_only and (c.name or "").strip())


async def test_edgar_search_writes_candidates_and_never_an_edge(engine):
    from smartboi.tools import run_edgar_supplier_search

    anchor = _anchor_spec(engine)
    edges_before = len(engine.graph.relationships)
    engine.edgar_client.search_hits_by_anchor[anchor.name] = [_hit()]
    engine.edgar_client.text_by_accession["0001050915-25-000012"] = (
        f"{anchor.name}, our largest customer, accounted for 22% of net sales."
    )

    report = await run_edgar_supplier_search(engine)

    assert len(engine.graph.relationships) == edges_before, "a hit must never mint an edge"
    assert engine.candidates.data, "but it must produce a candidate"
    entry = next(iter(engine.candidates.data.values()))
    assert entry.get("researched_only") is True
    assert not entry.get("pending_edges")
    assert "candidate" in report.lower()


async def test_edgar_search_does_not_inflate_seen_count(engine):
    """seen_count gates auto-accept as a TRADE TARGET at
    auto_accept_min_seen_count. It counts independent filing DISCLOSURES of a
    relationship, not sightings of a name in a search index."""
    from smartboi.tools import run_edgar_supplier_search

    anchor = _anchor_spec(engine)
    engine.edgar_client.search_hits_by_anchor[anchor.name] = [_hit()]
    engine.edgar_client.text_by_accession["0001050915-25-000012"] = (
        f"{anchor.name} accounted for 22% of net sales."
    )

    await run_edgar_supplier_search(engine)
    first = next(iter(engine.candidates.data.values())).get("seen_count")
    await run_edgar_supplier_search(engine)
    second = next(iter(engine.candidates.data.values())).get("seen_count")

    assert first == second == 1


async def test_edgar_search_rotates_through_anchors_instead_of_repeating(engine):
    """Selection is deterministic (inertness, ecosystem, symbol), so on a daily
    schedule an unrotated pass would re-search the same first anchors forever
    and never reach the rest -- the trap research.researched_anchors documents.
    The marker is written even when a search returns nothing, because the
    REQUEST is what was spent."""
    from smartboi.tools import run_edgar_supplier_search

    first = _anchor_spec(engine)
    named_anchors = {c.symbol for c in engine.universe
                     if c.signal_source_only and (c.name or "").strip()}

    await run_edgar_supplier_search(engine)
    searched = set(engine.edgar_search_state.data)
    assert first.symbol in searched, "a no-hit anchor must still be marked"

    # Everything reachable in one run is now marked, so the next run has
    # nothing left rather than starting the same list over.
    report = await run_edgar_supplier_search(engine)
    assert searched == named_anchors
    assert "already been searched" in report


async def test_a_hit_without_a_concentration_disclosure_is_dropped(engine):
    """Document-level AND over-matches by construction; the local proximity
    pass is what makes a hit a lead rather than a coincidence."""
    from smartboi.tools import run_edgar_supplier_search

    anchor = _anchor_spec(engine)
    engine.edgar_client.search_hits_by_anchor[anchor.name] = [_hit()]
    engine.edgar_client.text_by_accession["0001050915-25-000012"] = (
        f"We compete with {anchor.name} in several markets." + " filler." * 500
    )

    await run_edgar_supplier_search(engine)

    assert not engine.candidates.data


async def test_a_hit_on_a_symbol_already_held_is_skipped(engine):
    """If the filer is already in the universe, _poll_edgar has fetched that
    filing and run extraction on it already -- the hit adds nothing and the
    fetch is pure waste."""
    from smartboi.tools import run_edgar_supplier_search

    anchor = _anchor_spec(engine)
    engine.edgar_client.search_hits_by_anchor[anchor.name] = [
        _hit(company="FORMFACTOR INC (FORM)")
    ]
    engine.edgar_client.text_by_accession["0001050915-25-000012"] = (
        f"{anchor.name} accounted for 22% of net sales."
    )

    await run_edgar_supplier_search(engine)

    assert engine.edgar_client.fetched == [], "a name already in the universe must not be fetched"
    assert not engine.candidates.data


async def test_no_hits_is_reported_not_raised(engine):
    from smartboi.tools import run_edgar_supplier_search

    report = await run_edgar_supplier_search(engine)   # must not raise

    assert "no hits" in report.lower()


# --- Federal Register: a rule is not a company, so propagation runs from a
# synthetic regulator origin. The contract that matters is that the regulator
# is an ORIGIN ONLY -- it must never gain a dossier, never be polled, never
# be screened, and never buy the corroboration discount. ---

def _fedreg_engine(tmp_path, monkeypatch, **overrides):
    from smartboi.universe import CompanySpec

    settings = Settings(
        _env_file=None, symbols="BWEN,HDSN", anchor_symbols="",
        enable_relationship_backfill=False, enable_universe_autoscreen=False,
        enable_dashboard=False, enable_federal_register=True, **overrides,
    )
    monkeypatch.chdir(tmp_path)
    e = Engine(settings)
    e.universe = [
        CompanySpec("BWEN", "Broadwind", "grid_datacenter"),
        CompanySpec("HDSN", "Hudson Technologies", "industrial_machinery"),
    ]
    e.spec_by_symbol = {c.symbol: c for c in e.universe}
    return e


def test_a_regulator_is_never_a_universe_member(tmp_path, monkeypatch):
    """Registering BIS/EPA as universe members would put them into the EDGAR
    poll, the news poll, the screen and every count -- they are not filers and
    have no market in them."""
    from smartboi.federal_register import REGULATOR_SYMBOLS

    engine = _fedreg_engine(tmp_path, monkeypatch)

    for regulator in REGULATOR_SYMBOLS:
        assert regulator not in engine.symbol_list
        assert regulator not in engine.spec_by_symbol


def test_regulator_edges_are_seeded_below_the_disclosed_link_bar(tmp_path, monkeypatch):
    from smartboi.dossier import DISCLOSED_LINK_CONFIDENCE

    engine = _fedreg_engine(tmp_path, monkeypatch)
    engine._seed_regulator_edges()

    seeded = [r for r in engine.graph.relationships if r.source == "regulator seed"]
    assert seeded, "no regulator edges were seeded"
    assert all(r.rel_type == "regulator" for r in seeded)
    assert all(r.confidence < DISCLOSED_LINK_CONFIDENCE for r in seeded), (
        "a sector-wide rule must never buy the corroboration discount"
    )


def test_a_regulator_edge_points_at_the_regulator(tmp_path, monkeypatch):
    """rel_type describes what to_symbol IS to from_symbol, so the regulator
    is the TARGET. Backwards it reads as 'BWEN is a regulator of ITC', and
    that text goes to the dossier updater and the skeptic."""
    engine = _fedreg_engine(tmp_path, monkeypatch)
    engine._seed_regulator_edges()

    edge = next(r for r in engine.graph.relationships
                if r.source == "regulator seed" and r.from_symbol == "BWEN")
    assert edge.to_symbol == "ITC"
    assert "is a regulator of BWEN" in edge.description


async def test_a_regulatory_document_reaches_the_company_not_the_regulator(tmp_path, monkeypatch):
    engine = _fedreg_engine(tmp_path, monkeypatch)
    engine.updater = FakeUpdater(default=proposal(direction="SHORT"))
    engine.skeptic = FakeSkeptic(default=verdict(refuted=False))
    engine._seed_regulator_edges()

    await engine._process_evidence(
        origin_symbol="ITC", evidence_text="AD/CVD final results on wind towers.",
        source_type="regulatory", source_name="Federal Register (ITC/wind-towers-adcvd)",
        url="https://x/1", headline="Wind towers AD/CVD", published_at=_day_ago(6),
    )

    assert engine.dossiers.load("BWEN").evidence, "the rule must reach the company"
    assert not engine.dossiers.load("ITC").evidence, "there is no thesis about a regulator"


async def test_two_proceedings_by_one_agency_are_two_sources(tmp_path, monkeypatch):
    """The search key is part of the source name, so one agency running two
    unrelated rulemakings is two facts rather than one restated. Collapsing
    them would cap a regulatory thesis at one source per agency forever."""
    from smartboi.dossier import independence_key

    from smartboi.universe import CompanySpec

    engine = _fedreg_engine(tmp_path, monkeypatch)
    # AOSL is what the BIS searches actually reach -- entity-list names it
    # directly and semi-export-controls reaches its ecosystem.
    engine.universe.append(CompanySpec("AOSL", "Alpha and Omega", "semi_equipment"))
    engine.spec_by_symbol = {c.symbol: c for c in engine.universe}
    engine.updater = FakeUpdater(default=proposal(direction="SHORT"))
    engine.skeptic = FakeSkeptic(default=verdict(refuted=False))
    engine._seed_regulator_edges()

    for key in ("entity-list", "semi-export-controls"):
        await engine._process_evidence(
            origin_symbol="BIS", evidence_text=f"rule {key}", source_type="regulatory",
            source_name=f"Federal Register (BIS/{key})", url=f"https://x/{key}",
            headline=key, published_at=_day_ago(6),
        )

    records = engine.dossiers.load("AOSL").evidence
    assert len(records) == 2, "both proceedings must reach the company"
    keys = {independence_key(r) for r in records}
    assert len(keys) == 2, "and count as two independent sources, not one agency"



# --- Startup warnings must be true ---------------------------------------


class _FakePriceFeed:
    """Stands in for ReadOnlyPriceFeed so start() makes no IB connection."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def ensure_connected(self):
        return True


async def test_the_ib_disabled_warning_does_not_fire_when_ib_is_enabled(monkeypatch, caplog, tmp_path):
    """_has_price_source() is true for IB *or* Finnhub, so a bare `else` on it
    fired on every startup with IB enabled and connected -- printing 'IB price
    feed disabled ... until ENABLE_IB_PRICE_FEED=true' on a deployment where
    it was already true, 15 startups out of 15, one line after CONNECTED. A
    warning that prescribes a setting already in force sends whoever reads it
    to check the one thing that was never wrong."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("smartboi.engine.ReadOnlyPriceFeed", _FakePriceFeed)
    engine = Engine(Settings(_env_file=None, enable_dashboard=False,
                             enable_universe_autoscreen=False,
                             enable_ib_price_feed=True, finnhub_api_key="k"))

    with caplog.at_level("WARNING"):
        await engine.start()

    assert "IB price feed disabled" not in caplog.text


async def test_the_ib_disabled_warning_still_fires_when_ib_is_off(monkeypatch, caplog, tmp_path):
    """Still worth saying when it is TRUE: a Finnhub-only deployment is a real
    and supported configuration, and this is how an operator knows entries are
    priced off quotes rather than IB."""
    monkeypatch.chdir(tmp_path)
    engine = Engine(Settings(_env_file=None, enable_dashboard=False,
                             enable_universe_autoscreen=False,
                             enable_ib_price_feed=False, finnhub_api_key="k"))

    with caplog.at_level("WARNING"):
        await engine.start()

    assert "IB price feed disabled" in caplog.text
    assert "ENABLE_IB_PRICE_FEED=true" in caplog.text


async def test_the_daily_snapshot_is_skipped_on_a_weekend(engine, monkeypatch):
    """A snapshot row is only useful joined to a price mark for the same
    symbol and DATE, and the mark pass writes nothing on a Saturday. The
    guard was present on the mark side and missing here, so the fix had been
    applied to exactly one side of a two-sided join: 44-48 snapshot rows on
    each of four consecutive weekend days with nothing to join."""
    monkeypatch.setattr("smartboi.engine.is_trading_day", lambda: False)
    calls = []
    monkeypatch.setattr(engine, "_run_daily_snapshot", lambda: calls.append(1) or True)

    await engine._tick()

    assert calls == []
    # And the pass stays DUE, so Monday still captures rather than the
    # weekend silently consuming the day's slot.
    assert engine._daily_pass_due("dossier_snapshot")


async def test_the_daily_snapshot_still_runs_on_a_trading_day(engine, monkeypatch):
    """The guard has to let the ordinary case through, or it would be a
    capture outage rather than a weekend skip."""
    monkeypatch.setattr("smartboi.engine.is_trading_day", lambda: True)
    calls = []
    monkeypatch.setattr(engine, "_run_daily_snapshot", lambda: calls.append(1) or True)

    await engine._tick()

    assert calls == [1]


async def test_a_free_refusal_does_not_spend_the_resynthesis_slot(engine):
    """_apply_synthesis declines for free at three points before it reaches
    the API -- no synthesizer, a non-directional dossier, and the floor gate.
    The slot used to be spent before that call, so a dossier sitting below
    the floor with permanently-changed premises burned the whole day's
    off-schedule allowance without a single Opus call. The cap bounds SPEND,
    so an attempt that costs nothing must not count against it."""
    engine.synthesizer = None  # the cheapest of the three free refusals
    dossier = await _build_thesis(engine)
    dossier.already_priced_in = True
    dossier.synthesis_at = datetime.now(timezone.utc).isoformat()
    dossier.synthesis_price = 10.0
    engine._price_bar = _returns_bar(20.0)  # refutes the veto, so premises HAVE changed

    before = int(engine.resynthesis_state.get("count", 0) or 0)
    await engine._maybe_resynthesize(dossier, datetime.now(timezone.utc))

    assert int(engine.resynthesis_state.get("count", 0) or 0) == before


async def test_a_real_re_judgement_does_spend_the_slot(engine):
    """The cap has to still bind, or removing the false spend would just have
    removed the bound."""
    engine.synthesizer = FakeSynthesizer(default=synthesis(confidence=0.9, magnitude=0.9))
    dossier = await _build_thesis(engine)
    dossier.already_priced_in = True
    dossier.synthesis_at = datetime.now(timezone.utc).isoformat()
    dossier.synthesis_price = 10.0
    engine._price_bar = _returns_bar(20.0)

    before = int(engine.resynthesis_state.get("count", 0) or 0)
    await engine._maybe_resynthesize(dossier, datetime.now(timezone.utc))

    assert int(engine.resynthesis_state.get("count", 0) or 0) == before + 1


async def test_the_synthesis_floor_gate_reads_the_uncapped_arithmetic(engine):
    """The central fix of the corroboration-cap change, and it survived full
    reversion with a green suite until this existed.

    A dossier whose CAPPED score sits below the synthesis floor but whose
    arithmetic is above it must still be judged. Gating on the capped score
    is circular -- the verdict suppresses the re-judgement that would renew
    it -- and the circle fails open: the verdict goes stale at 36h, the cap
    lapses, and the score springs back above the signal bar."""
    from smartboi.dossier import Dossier, merge_evidence, recompute_decay

    now = datetime.now(timezone.utc)
    dossier = Dossier(symbol="BWEN")
    for n in range(8):
        merge_evidence(dossier, EvidenceRecord(
            f"e{n}", "news", f"outlet{n}.com", "u", "h", now.isoformat(), "BWEN", False, "",
            "LONG", 0.45, 0.60, 20, "reason", "skeptic",
        ), now=now)
    dossier.synthesis_at = now.isoformat()
    dossier.distinct_fact_count = 1
    recompute_decay(dossier, now)

    floor = (engine.settings.signal_confidence_threshold
             * engine.settings.synthesis_score_floor_pct)
    assert dossier.confidence * dossier.magnitude < floor, "precondition: capped, under the floor"
    assert dossier.arithmetic_score > floor, "precondition: the arithmetic is above it"

    engine.synthesizer = FakeSynthesizer(default=synthesis())
    await engine._apply_synthesis(dossier, now)

    assert engine.synthesizer.calls, "the pass that set the cap must still be reachable under it"


# --- Counterparties that can never be a propagation channel ---------------


@pytest.mark.parametrize("symbol,name", [
    # Live: all ten counterparties extraction found for HURC were its own
    # subsidiaries or its auditor, which is why it carries a thesis and no
    # graph edge -- the extraction worked and produced nothing usable.
    ("HURC", "HURCO AUTOMATION, LTD."),
    ("HURC", "HURCO MANUFACTURING LIMITED"),
    ("HURC", "DELOITTE & TOUCHE LLP"),
    ("PLPC", "ERNST & YOUNG LLP"),
    ("PLPC", "BAKER & HOSTETLER LLP"),
    ("PLPC", "PNC EQUIPMENT FINANCE LLC"),
])
def test_noise_counterparties_are_dropped(symbol, name, tmp_path, monkeypatch):
    """An auditor, a law firm, a financing arm and the company's own
    subsidiary are all disclosed relationships, and none is a channel news
    travels down. The existing lender filter reads the DESCRIPTION, so an
    audit engagement -- disclosed without a word like 'credit facility' --
    went straight through it."""
    monkeypatch.chdir(tmp_path)
    engine = Engine(Settings(_env_file=None, enable_dashboard=False,
                             enable_universe_autoscreen=False))
    rel = {"counterparty_name": name, "rel_type": "supplier", "description": ""}

    assert engine._is_professional_services(rel) or engine._is_self_reference(symbol, rel)


@pytest.mark.parametrize("symbol,name", [
    # Real supply-chain counterparties, including the ones that are foreign
    # or private. Those cannot become edges either, but for a different
    # reason -- and this filter must never be what removes them, or it would
    # be hiding the finding instead of cleaning it up.
    ("MVST", "IVECO"),
    ("MVST", "HIGER BUS"),
    ("PLPC", "J.A.P. INDUSTRIA DE MATERIAIS PARA TELEFONIA LTD"),
    ("CVLG", "TRANSPORT ENTERPRISE LEASING, LLC"),
    ("DCO", "RTX CORPORATION"),
    ("UCTT", "APPLIED MATERIALS INC"),
    ("THRM", "LEAR CORPORATION"),
    # An operating company whose name merely contains a financial word.
    ("DCO", "CAPITAL SENIOR LIVING CORP"),
])
def test_real_counterparties_survive_the_filters(symbol, name, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = Engine(Settings(_env_file=None, enable_dashboard=False,
                             enable_universe_autoscreen=False))
    rel = {"counterparty_name": name, "rel_type": "customer", "description": ""}

    assert not engine._is_professional_services(rel)
    assert not engine._is_self_reference(symbol, rel)


# --- The tape must reach the pass that judges already_priced_in ------------
#
# already_priced_in ZEROES a thesis, and its own tool description calls it "a
# claim about the PRICE" -- yet the synthesis prompt carried no price of any
# kind. The model was inferring "the market has absorbed this" from how old
# and how widely covered the story was, which is the wrong proxy for this
# strategy: a heavily covered story about an ANCHOR is precisely the setup
# where the thinly-covered supplier has not moved yet.

async def test_synthesis_is_shown_the_price_around_its_earliest_evidence(engine):
    from smartboi.dossier import Dossier, merge_evidence, recompute_decay

    now = datetime.now(timezone.utc)
    marks = [
        (_day_ago(9), 32.10), (_day_ago(8), 32.44), (_day_ago(7), 32.29),
        (_day_ago(6), 33.33), (_day_ago(3), 34.90), (_day_ago(2), 36.28),
    ]
    path = Path(engine.settings.log_dir)
    path.mkdir(parents=True, exist_ok=True)
    path = path / "price_marks.jsonl"
    path.write_text("".join(
        __import__("json").dumps({"symbol": "BWEN", "marked_at": d + "T04:00:00+00:00",
                                  "price": p}) + "\n"
        for d, p in marks
    ))

    dossier = Dossier(symbol="BWEN")
    for n in range(8):
        merge_evidence(dossier, EvidenceRecord(
            f"e{n}", "news", f"outlet{n}.com", "u", "h", _days_ago(6, hour=13),
            "BWEN", False, "", "LONG", 0.45, 0.60, 20, "reason", "skeptic",
        ), now=now)
    recompute_decay(dossier, now)

    context = engine._price_context_for(dossier, now)
    assert "PRICE" in context, "the synthesis prompt carries no price block"
    # Starts BEFORE the news, or the reaction it exists to reveal is invisible.
    assert _day_ago(9) in context
    assert "first evidence in this dossier is dated here" in context
    # ...and states the move, which is the number the judgement turns on.
    assert "+8.9% since the first evidence is dated" in context


async def test_price_context_is_absent_rather_than_wrong_when_unpriceable(engine):
    """A symbol no price source could mark must leave the pass judging exactly
    as it did before -- never a fabricated or empty-looking series."""
    from smartboi.dossier import Dossier, merge_evidence, recompute_decay

    now = datetime.now(timezone.utc)
    Path(engine.settings.log_dir).mkdir(parents=True, exist_ok=True)
    (Path(engine.settings.log_dir) / "price_marks.jsonl").write_text("")
    dossier = Dossier(symbol="BWEN")
    merge_evidence(dossier, EvidenceRecord(
        "e0", "news", "outlet.com", "u", "h", now.isoformat(), "BWEN", False, "",
        "LONG", 0.45, 0.60, 20, "reason", "skeptic",
    ), now=now)
    recompute_decay(dossier, now)

    assert engine._price_context_for(dossier, now) == ""


async def test_the_price_block_actually_reaches_the_synthesizer(engine):
    """End-to-end: built, and passed through _apply_synthesis. The builder
    working while the call site drops it is exactly the shape of bug that put
    fact_key at 0% for a whole scoring version."""
    from smartboi.dossier import Dossier, merge_evidence, recompute_decay

    now = datetime.now(timezone.utc)
    path = Path(engine.settings.log_dir)
    path.mkdir(parents=True, exist_ok=True)
    path = path / "price_marks.jsonl"
    path.write_text("".join(
        __import__("json").dumps({"symbol": "BWEN", "marked_at": d + "T04:00:00+00:00",
                                  "price": p}) + "\n"
        for d, p in [(_day_ago(9), 30.0), (_day_ago(6), 33.0), (_day_ago(2), 36.0)]
    ))
    dossier = Dossier(symbol="BWEN")
    for n in range(8):
        merge_evidence(dossier, EvidenceRecord(
            f"e{n}", "news", f"outlet{n}.com", "u", "h", _days_ago(6, hour=13),
            "BWEN", False, "", "LONG", 0.9, 0.9, 20, "reason", "skeptic",
        ), now=now)
    recompute_decay(dossier, now)
    engine.synthesizer = FakeSynthesizer(default=None)  # verdict irrelevant here

    await engine._apply_synthesis(dossier, now)

    assert engine.synthesizer.calls, "synthesis never ran"
    assert "PRICE" in engine.synthesizer.calls[0]["price_context"]


# --- A failed lookup is not a fact about the filer -------------------------
#
# The backfill wrote one permanent "nothing to extract, and never will be"
# marker for all three ways latest_filing can come back empty. A mis-cached
# CIK therefore became "this company has no 10-K" forever: XOM's cached CIK
# (0002115436) is not Exxon's (0000034088), so Exxon's own 10-K had never
# been read while the state file said there was nothing to read.

async def test_a_filer_with_no_10k_is_settled_permanently(engine):
    engine.edgar_client.latest_filing_outcomes[("FORM", "10-K")] = "absent"
    await engine._run_relationship_backfill()
    marker = engine.backfill_state.get("FORM")
    assert marker["backfilled_at"] and marker["accession"] is None
    assert engine._backfill_due("FORM") is False, "a real 'no 10-K' must retire the symbol"


async def test_a_failed_lookup_does_not_retire_the_symbol(engine):
    engine.edgar_client.latest_filing_outcomes[("FORM", "10-K")] = "no_cik"
    await engine._run_relationship_backfill()
    marker = engine.backfill_state.get("FORM")
    assert marker.get("error") == "no_cik"
    assert "backfilled_at" not in marker, "a lookup failure was recorded as a completed read"


async def test_a_failed_lookup_backs_off_instead_of_retrying_every_tick(engine):
    """The backfill runs on EVERY tick. Simply leaving a permanently broken
    symbol pending would re-ask EDGAR every 30s -- ~2,880 requests a day for
    one bad ticker, against a rate-limited endpoint."""
    from smartboi.engine import _backfill_retry_hours

    engine.edgar_client.latest_filing_outcomes[("FORM", "10-K")] = "fetch_error"
    await engine._run_relationship_backfill()
    assert engine._backfill_due("FORM") is False, "retried immediately on the next tick"

    # ...but it IS due again once the backoff has elapsed.
    marker = dict(engine.backfill_state.get("FORM"))
    hours = _backfill_retry_hours(marker["attempts"])
    marker["last_attempt_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=hours + 1)
    ).isoformat()
    engine.backfill_state.set("FORM", marker)
    assert engine._backfill_due("FORM") is True
    assert _backfill_retry_hours(99) == 24, "backoff must stay bounded"


# --- seen_count is split across spellings of one company ------------------

async def test_auto_accept_counts_filing_sightings_across_name_variants(engine):
    """"TENNECO INC." and "TEN" are one company disclosed twice, but the count
    splits into two 1s and neither clears auto_accept_min_seen_count."""
    engine.candidates.set("TENNECO INC.", {"name": "Tenneco Inc.", "seen_count": 1})
    engine.candidates.set("TEN", {"name": "Tenneco", "seen_count": 1, "ticker": "TEN"})

    assert engine._filing_seen_count(engine.candidates.get("TEN")) == 2


async def test_a_web_sighting_never_helps_clear_the_auto_accept_bar(engine):
    """merge_into_candidates refuses to increment seen_count for a research
    sighting on purpose -- letting web sourcing admit a trade target. Summing
    the group blindly would reinstate that through the back door."""
    engine.candidates.set("SERVOTRONICS, INC.", {"name": "Servotronics, Inc.", "seen_count": 1})
    engine.candidates.set("SVT", {"name": "Servotronics, Inc.", "seen_count": 1,
                                  "ticker": "SVT", "researched_only": True})

    assert engine._filing_seen_count(engine.candidates.get("SVT")) == 1


# --- Guard: this file must never grow another hardcoded fixture date ---

def test_no_hardcoded_fresh_dates_in_this_file():
    """Engine paths score against the real wall clock, so a literal calendar
    date used for evidence or filing freshness in THIS file re-ages every day
    and eventually crosses the staleness cutoff -- 29 tests flipped red that
    way in August 2026 without a line of code changing. Fixture dates go
    through _days_ago()/_day_ago(). The allowlist below is the full set of
    deliberately-old literals (backfill ordering marks and prior-year filing
    dates in extraction fixtures), whose "distant past" intent is the one
    thing further aging cannot break."""
    allowed = {"2020-01-01", "2025-02-14", "2025-09-01", "2026-01-01", "2026-06-01"}
    found = set(re.findall(r'"(20\d\d-\d\d-\d\d)', Path(__file__).read_text()))
    assert found <= allowed, (
        f"hardcoded date literal(s) {sorted(found - allowed)} in test_engine.py "
        "-- use _days_ago()/_day_ago() so the fixture cannot age across the "
        "staleness cutoff"
    )
