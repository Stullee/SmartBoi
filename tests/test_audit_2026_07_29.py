"""Regression tests for the 2026-07-29 audit fixes (see
docs/AUDIT-2026-07-29.md): bar-session entry honesty (no fills from a bar
whose session predates the signal; no entry-day stop-outs from pre-entry
price action), the per-accession extraction marker (no double-paid
extraction, no lost extraction), backfill surviving transient EDGAR
failures, the horizon-days clamp, archive overwrite protection, and
accepted candidates joining a real ecosystem instead of the "accepted"
pseudo-ecosystem."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from smartboi.config import Settings
from smartboi.edgar import FilingEvent
from smartboi.engine import Engine, _ET, _bar_postdates_signal, _et_session_date
from smartboi.paper_journal import PaperTrade

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
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, symbols="FORM,UCTT", anchor_symbols="INTC",
        signal_confidence_threshold=0.5, min_independent_sources=2,
        min_independent_sources_news_only=2,
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


def _et_iso(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=_ET).isoformat()


async def _signal_form(engine, price=10.0):
    """Drives FORM to SIGNALED via two independent news sources (the same
    recipe as test_engine's lifecycle tests)."""
    engine.price_feed = FakePriceFeed(prices={"FORM": price})
    engine.updater.default = proposal(direction="LONG", magnitude=0.8, confidence=0.8, horizon_days=20)
    engine.skeptic.default = verdict(refuted=False, adjusted_confidence=0.8, adjusted_magnitude=0.8)
    for i, source in enumerate(("reuters.com", "bloomberg.com")):
        await engine._process_evidence(
            origin_symbol="FORM", evidence_text=f"evidence {i}", source_type="news",
            source_name=source, url=f"https://x/{i}", headline=f"h{i}", published_at="2026-07-23",
        )
    dossier = engine.dossiers.load("FORM")
    assert dossier.status == "SIGNALED"
    return dossier


# --- Bar-session honesty: the pure predicate ---

def test_bar_postdates_signal_rejects_afterhours_same_day():
    # 8-K at 17:30 ET Monday; Monday's bar closed 16:00 -- every price in it
    # predates the news. Filling from it books Tuesday's gap as P&L.
    assert _bar_postdates_signal("2026-07-27", _et_iso(2026, 7, 27, 17, 30)) is False


def test_bar_postdates_signal_accepts_intrasession_signal():
    assert _bar_postdates_signal("2026-07-27", _et_iso(2026, 7, 27, 10, 0)) is True


def test_bar_postdates_signal_accepts_next_session():
    assert _bar_postdates_signal("2026-07-28", _et_iso(2026, 7, 27, 17, 30)) is True


def test_bar_postdates_signal_rejects_weekend_signal_on_friday_bar():
    # Saturday-noon news against Friday's bar: stale.
    assert _bar_postdates_signal("2026-07-24", _et_iso(2026, 7, 25, 12, 0)) is False


def test_bar_postdates_signal_degrades_open_on_missing_date():
    # A price source that can't supply session dates must not silently
    # disable trade opening -- it degrades to the old behavior instead.
    assert _bar_postdates_signal("", _et_iso(2026, 7, 27, 17, 30)) is True
    assert _bar_postdates_signal("2026-07-27", "not-a-timestamp") is True


def test_et_session_date_converts_utc_evening_to_same_et_day():
    # 01:00 UTC on the 28th is 21:00 ET on the 27th.
    assert _et_session_date("2026-07-28T01:00:00+00:00") == "2026-07-27"


# --- Bar-session honesty: engine entry behavior ---

async def test_entry_deferred_when_last_bar_predates_signal(engine):
    await _signal_form(engine)
    yesterday_et = (datetime.now(_ET).date() - timedelta(days=1)).isoformat()
    # (close, high, low, open, session_date): yesterday's completed bar.
    engine.price_feed.prices["FORM"] = (10.0, 10.1, 9.9, 10.0, yesterday_et)

    await engine._mark_and_execute()

    assert not engine.journal.has_open("FORM")
    assert engine.dossiers.load("FORM").status == "SIGNALED"  # deferred, not expired


async def test_afterhours_signal_fills_at_next_session_open(engine):
    dossier = await _signal_form(engine)
    yesterday = datetime.now(_ET).date() - timedelta(days=1)
    # Re-stamp the episode as fired AFTER yesterday's close.
    dossier.signaled_at = datetime(yesterday.year, yesterday.month, yesterday.day,
                                   17, 0, tzinfo=_ET).isoformat()
    engine.dossiers.save(dossier)
    today_et = datetime.now(_ET).date().isoformat()
    # Today's bar: open 10.2 (2% above the 10.0 baseline -- under the 5%
    # drift bar), close 10.3.
    engine.price_feed.prices["FORM"] = (10.3, 10.4, 10.1, 10.2, today_et)

    await engine._mark_and_execute()

    assert engine.journal.has_open("FORM")
    trade = engine.journal.open_trades["FORM"]
    assert trade.entry_price == 10.2  # next-session OPEN, not the stale close
    assert trade.entry_fill == "open"


async def test_intrasession_signal_still_fills_at_close(engine):
    await _signal_form(engine)  # signaled_at = now, i.e. during today's session date
    today_et = datetime.now(_ET).date().isoformat()
    engine.price_feed.prices["FORM"] = (10.1, 10.2, 9.9, 9.95, today_et)

    await engine._mark_and_execute()

    trade = engine.journal.open_trades.get("FORM")
    assert trade is not None
    assert trade.entry_price == 10.1
    assert trade.entry_fill == "close"


# --- Entry-day stop/target contamination ---

async def test_close_fill_not_stopped_by_pre_entry_intraday_low(engine):
    today_et = datetime.now(_ET).date().isoformat()
    engine.journal.open("FORM", "LONG", 10.0, 8.0, 16.0, 20, "t", 0.8, 2, [], entry_fill="close")
    # Today's bar dipped to 8.9 (below the 9.2 stop) hours BEFORE the trade
    # existed, then recovered: must NOT stop out a position opened at 10.0
    # at/near the close.
    engine.price_feed.prices["FORM"] = (10.0, 10.4, 8.9, 9.5, today_et)

    await engine._mark_and_execute()

    assert engine.journal.has_open("FORM")


async def test_next_session_bar_evaluates_full_range(engine):
    today_et = datetime.now(_ET).date().isoformat()
    tomorrow_et = (datetime.now(_ET).date() + timedelta(days=1)).isoformat()
    engine.journal.open("FORM", "LONG", 10.0, 8.0, 16.0, 20, "t", 0.8, 2, [], entry_fill="close")
    engine.price_feed.prices["FORM"] = (10.0, 10.4, 8.9, 9.5, tomorrow_et)

    await engine._mark_and_execute()

    # From the next session on, the whole bar is post-entry: the 8.9 low is
    # a real stop-out.
    assert not engine.journal.has_open("FORM")
    assert today_et != tomorrow_et  # sanity


async def test_open_fill_owns_its_entry_session(engine):
    today_et = datetime.now(_ET).date().isoformat()
    engine.journal.open("FORM", "LONG", 10.0, 8.0, 16.0, 20, "t", 0.8, 2, [], entry_fill="open")
    engine.price_feed.prices["FORM"] = (10.0, 10.4, 8.9, 9.5, today_et)

    await engine._mark_and_execute()

    # An open-convention fill entered at the session open -- the day's low
    # happened while the position existed, so the stop is real.
    assert not engine.journal.has_open("FORM")


# --- Per-accession extraction marker ---

async def test_extraction_deferral_retries_without_rescoring(engine):
    filing = FilingEvent("FORM", "0000000001", "10-K", "2026-07-20", "acc-1", "doc.htm")
    engine.edgar_client.text_by_accession["acc-1"] = "filing text"
    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)
    engine.extractor.default = None  # extraction deferred (budget/API failure)

    await engine._process_filing("FORM", filing)
    assert len(engine.updater.calls) == 1        # evidence was scored...
    assert len(engine.extractor.calls) == 1      # ...but extraction deferred

    engine.extractor.default = []                # extraction now succeeds
    await engine._process_filing("FORM", filing)
    assert len(engine.extractor.calls) == 2      # extraction retried
    assert len(engine.updater.calls) == 1        # WITHOUT re-scoring (no double pay)

    await engine._process_filing("FORM", filing)
    assert len(engine.extractor.calls) == 2      # fully done -- nothing re-runs
    assert len(engine.updater.calls) == 1


async def test_scoring_deferral_does_not_repay_extraction(engine):
    filing = FilingEvent("FORM", "0000000001", "10-Q", "2026-07-21", "acc-2", "doc.htm")
    engine.edgar_client.text_by_accession["acc-2"] = "filing text"
    engine.extractor.default = []                # extraction succeeds immediately
    engine.updater.default = None                # scoring deferred

    await engine._process_filing("FORM", filing)
    assert len(engine.extractor.calls) == 1

    engine.updater.default = proposal(direction="LONG")
    engine.skeptic.default = verdict(refuted=False)
    await engine._process_filing("FORM", filing)

    assert len(engine.extractor.calls) == 1      # the ~40k-token call is NOT re-paid
    assert len(engine.updater.calls) == 2        # scoring retried and completed


# --- Backfill vs transient EDGAR failures ---

class _RaisingEdgarClient(FakeEdgarClient):
    async def latest_filing(self, symbol, form):
        raise httpx.ConnectError("EDGAR 503")


async def test_backfill_stays_pending_on_transient_edgar_failure(engine):
    engine.edgar_client = _RaisingEdgarClient()

    await engine._run_relationship_backfill()

    # A 503 must not be recorded as "no 10-K exists": both tradeables stay
    # pending for the next tick instead of being skipped forever.
    assert engine.backfill_state.get("FORM") is None
    assert engine.backfill_state.get("UCTT") is None


# --- Horizon clamp ---

def test_validated_proposal_clamps_runaway_horizon():
    raw = {"is_new_information": True, "direction": "LONG", "magnitude": 0.5,
           "confidence": 0.5, "horizon_days": 3650, "reasoning": "r"}
    assert Engine._validated_proposal("X", dict(raw))["horizon_days"] == 60
    assert Engine._validated_proposal("X", dict(raw), max_horizon_days=21)["horizon_days"] == 21
    ok = {**raw, "horizon_days": 15}
    assert Engine._validated_proposal("X", ok, max_horizon_days=21)["horizon_days"] == 15


# --- PaperTrade rows written before entry_fill existed keep loading ---

def test_paper_trade_backcompat_without_entry_fill():
    row = {"symbol": "FORM", "direction": "LONG", "entry_price": 10.0,
           "stop_price": 9.2, "target_price": 11.6, "opened_at": "2026-07-01T00:00:00+00:00",
           "horizon_days": 20, "thesis_summary": "t", "confidence": 0.8,
           "independent_source_count": 2}
    assert PaperTrade(**row).entry_fill == "close"


# --- Archive overwrite protection + close-path resurrection guard ---

async def test_closing_trade_on_archived_symbol_does_not_resurrect_dossier(engine):
    # GONE is not in the universe -> not tradeable. Give it a live dossier,
    # archive it, then close a trade on it.
    dossier = engine.dossiers.load("GONE")
    dossier.thesis_summary = "real accumulated history"
    engine.dossiers.save(dossier)
    engine._archive_orphaned_dossiers()
    archived = Path("data/dossiers_archived/GONE.json")
    assert archived.exists()
    before = archived.read_text()

    engine.journal.open("GONE", "LONG", 10.0, 8.0, 16.0, 20, "t", 0.8, 2, [])
    engine.price_feed = FakePriceFeed(prices={"GONE": 5.0})  # deep through the stop
    await engine._mark_and_execute()

    assert not engine.journal.has_open("GONE")
    # The close must not recreate a live dossier file for the archived
    # symbol -- that file is what the next archive pass would clobber the
    # real history with.
    assert not (engine.dossiers.dir_path / "GONE.json").exists()
    engine._archive_orphaned_dossiers()
    assert archived.read_text() == before


def test_archive_never_overwrites_existing_history(engine):
    dossier = engine.dossiers.load("GONE")
    dossier.thesis_summary = "original history"
    engine.dossiers.save(dossier)
    engine._archive_orphaned_dossiers()
    archived = Path("data/dossiers_archived/GONE.json")
    original = archived.read_text()

    # A later (empty) live file appears for the same symbol.
    empty = engine.dossiers.load("GONE")
    engine.dossiers.save(empty)
    engine._archive_orphaned_dossiers()

    # The original archive is untouched; the new file landed elsewhere.
    assert archived.read_text() == original
    siblings = list(Path("data/dossiers_archived").glob("GONE.*.json"))
    assert len(siblings) == 1


# --- Accepted candidates join a real ecosystem ---

def test_accepted_candidate_inherits_discloser_ecosystem(engine):
    engine.candidates.set("ACME", {"name": "Acme Corp", "ticker": "ACME",
                                   "related_to": ["FORM"], "rel_types": ["customer"],
                                   "description": "", "sources": [], "seen_count": 3,
                                   "first_seen_at": "2026-07-01T00:00:00+00:00"})
    spec = engine.accept_candidate("ACME", "anchor")
    expected = engine.spec_by_symbol["FORM"].ecosystem
    assert spec.ecosystem == expected
    assert spec.ecosystem != "accepted"


def test_accepted_candidate_without_candidate_entry_falls_back(engine):
    spec = engine.accept_candidate("MYST", "anchor")
    assert spec.ecosystem == "accepted"
