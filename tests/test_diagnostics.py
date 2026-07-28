"""The diagnostics bundle (smartboi.tools.run_diagnostics).

Its whole purpose is to be pasted somewhere -- a chat, an issue -- so the
non-negotiable property is that it never carries a credential, personal data,
or a tokenised URL out with it. The rest covers the sections that have
actually caught real problems in this system."""
from __future__ import annotations

import pytest

from smartboi.config import Settings
from smartboi.dossier import Dossier, EvidenceRecord, merge_evidence
from smartboi.engine import Engine
from smartboi.tools import run_diagnostics

from tests.fakes import FakeEdgarClient, FakeFinnhub

SECRETS = {
    "anthropic_api_key": "sk-ant-LEAKED-KEY",
    "finnhub_api_key": "FINNHUB-LEAKED-KEY",
    # SEC requires a real name and email in the user agent -- personal data,
    # not a credential, but equally not something to paste into a chat.
    "edgar_user_agent": "Real Person real.person@example.com",
    "alert_webhook_url": "http://homeassistant.local:8123/api/webhook/LEAKED-WEBHOOK-ID",
}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    e = Engine(Settings(_env_file=None, enable_dashboard=False,
                        enable_universe_autoscreen=False, **SECRETS))
    e.edgar_client = FakeEdgarClient()
    e.finnhub = FakeFinnhub()
    return e


def test_no_credentials_or_personal_data_are_ever_included(engine):
    report = run_diagnostics(engine)
    for value in SECRETS.values():
        assert value not in report
    # The whole token, and the distinctive part of the email/webhook id.
    for fragment in ("sk-ant-", "LEAKED-KEY", "real.person@", "LEAKED-WEBHOOK-ID"):
        assert fragment not in report


def test_log_lines_are_scrubbed_of_query_string_tokens(engine, tmp_path):
    """httpx-style exception text carries the full request URL, and Finnhub
    puts its API key in the query string -- confirmed-live leak vector."""
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "smartboi.log").write_text(
        "2026-07-28 | ERROR | smartboi.news | failed: "
        "https://finnhub.io/api/v1/company-news?symbol=X&token=FINNHUB-LEAKED-KEY\n"
    )
    report = run_diagnostics(engine)
    assert "FINNHUB-LEAKED-KEY" not in report
    assert "token=REDACTED" in report


def test_reports_universe_and_integration_state(engine):
    report = run_diagnostics(engine)
    assert "EDGAR ingestion            ENABLED" in report
    assert "Dossier engine (Claude)    disabled" in report  # no updater wired
    assert "tradeable" in report and "anchors" in report


def test_reports_dossier_scores(engine):
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, EvidenceRecord(
        "e1", "news", "Reuters", "u", "h", "2026-07-28", "DCO", False, "",
        "LONG", 0.5, 0.6, 20, "reason", "skeptic",
    ))
    engine.dossiers.save(dossier)
    report = run_diagnostics(engine)
    assert "DCO" in report and "LONG" in report


def test_flags_a_collapsed_evidence_source_identity(engine):
    """The check that would have caught the live bug where every news article
    was attributed to "finnhub.io", making independent_source_count unable to
    exceed 1 and blocking every signal."""
    engine.dedup.register("fp1", "finnhub.io")
    engine.dedup.register("fp2", "finnhub.io")
    report = run_diagnostics(engine)
    assert "distinct source name" in report
    assert "WARNING: near-single source identity" in report


def test_no_warning_when_sources_are_genuinely_diverse(engine):
    for i, publisher in enumerate(["Reuters", "Bloomberg", "MarketWatch", "SEC EDGAR (8-K)"]):
        engine.dedup.register(f"fp{i}", publisher)
    report = run_diagnostics(engine)
    assert "WARNING: near-single source identity" not in report


def test_runs_on_a_completely_fresh_deployment(engine):
    """Nothing captured, no dossiers, no graph -- must still produce a
    report rather than raising on an empty everything."""
    report = run_diagnostics(engine)
    assert "=== SmartBoi diagnostics ===" in report
    assert "none yet" in report
