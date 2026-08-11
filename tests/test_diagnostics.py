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


# --- Signal episodes and their outcomes: the bundle used to show a signal
# firing and zero trades with nothing in between, so "why did this signal not
# become a trade" -- the single most important question this system can be
# asked -- still needed shell access to answer. ---

def test_a_signal_episode_reports_what_happened_to_it(engine, tmp_path):
    import json
    from pathlib import Path

    log_dir = Path(engine.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    episode = "2026-07-28T19:15:00+00:00"
    (log_dir / "signals.jsonl").write_text(json.dumps({
        "symbol": "DCO", "direction": "LONG", "confidence": 0.62, "magnitude": 0.40,
        "horizon_days": 20, "independent_source_count": 2, "thesis_summary": "t",
        "generated_at": episode, "episode": episode,
    }) + "\n")
    (log_dir / "decisions.jsonl").write_text(json.dumps({
        "event": "signal_expired", "symbol": "DCO", "direction": "LONG",
        "episode": episode, "price": None,
        "reason": "thesis fell below the signal bar on the daily decay pass (score 0.240 < 0.250)",
        "at": "2026-07-29T04:00:00+00:00",
    }) + "\n")

    report = run_diagnostics(engine)

    assert "Signal episodes (1" in report
    assert "DCO" in report
    assert "expired unopened" in report
    assert "decay pass" in report


def test_an_episode_with_no_ledger_row_is_called_out(engine, tmp_path):
    import json
    from pathlib import Path

    log_dir = Path(engine.settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "signals.jsonl").write_text(json.dumps({
        "symbol": "DCO", "direction": "LONG", "confidence": 0.62, "magnitude": 0.40,
        "horizon_days": 20, "independent_source_count": 2, "thesis_summary": "t",
        "generated_at": "2026-07-28T19:15:00+00:00", "episode": "2026-07-28T19:15:00+00:00",
    }) + "\n")

    report = run_diagnostics(engine)

    assert "carry no ledger row" in report


# --- The allow-list is the only thing keeping a credential out of a pasted
# bundle, so it can't be a deny-list -- but that same design silently drops
# NEW settings from the report. transaction_cost_bps_per_side and
# min_independent_sources_news_only were both added and both invisible.

def test_every_non_secret_setting_is_either_reported_or_deliberately_omitted():
    """Fails when a setting is added to Settings without a decision about
    whether the diagnostics bundle should show it. Add it to
    _DIAGNOSTIC_SETTINGS, or to the omitted set here with a reason."""
    from smartboi.tools import _DIAGNOSTIC_SETTINGS

    # Secrets and personal data (never printable), plus fields whose value
    # is either already shown elsewhere in the report or is pure plumbing.
    DELIBERATELY_OMITTED = {
        "anthropic_api_key", "finnhub_api_key", "edgar_user_agent", "alert_webhook_url",
        "symbols", "anchor_symbols",          # shown in full by the Universe section
        "ib_client_id", "log_level", "log_dir",
        "enable_dashboard", "dashboard_port",  # self-evident to anyone reading a dashboard
    }
    reported = set(_DIAGNOSTIC_SETTINGS)
    fields = set(Settings.model_fields)
    undecided = fields - reported - DELIBERATELY_OMITTED
    assert not undecided, f"settings neither reported nor deliberately omitted: {sorted(undecided)}"
    # And nothing in the allow-list that no longer exists.
    assert not reported - fields, f"_DIAGNOSTIC_SETTINGS names non-existent settings: {sorted(reported - fields)}"


def test_runs_on_a_completely_fresh_deployment(engine):
    """Nothing captured, no dossiers, no graph -- must still produce a
    report rather than raising on an empty everything."""
    report = run_diagnostics(engine)
    assert "=== SmartBoi diagnostics ===" in report
    assert "none yet" in report


# --- Audit round 2: the diagnostics bundle promised "credentials omitted"
# and leaked the alert webhook URL. The webhook id IS the credential -- and
# the bundle is what an operator pastes into a chat or an issue. ---


def test_a_failed_webhook_post_never_logs_the_url():
    from smartboi.news import redact_url

    secret = "http://homeassistant.local:8123/api/webhook/abc-SECRET-id"
    # What httpx's HTTPStatusError actually stringifies to after
    # raise_for_status(), which is the path AlertSender takes.
    exc = f"Client error '404 Not Found' for url '{secret}'\nFor more information check: ..."

    scrubbed = redact_url(secret, exc)
    assert "abc-SECRET-id" not in scrubbed
    assert "<webhook-url-redacted>" in scrubbed
    assert "404 Not Found" in scrubbed  # the diagnostic value survives


def test_the_finnhub_token_scrub_still_applies():
    from smartboi.news import redact_url

    line = "quote fetch failed: GET https://finnhub.io/api/v1/quote?symbol=X&token=SECRETKEY"
    scrubbed = redact_url("", line)
    assert "SECRETKEY" not in scrubbed
    assert "token=REDACTED" in scrubbed


def test_an_unconfigured_webhook_does_not_scrub_the_empty_string():
    from smartboi.news import redact_url

    line = "some ordinary warning line"
    assert redact_url("", line) == line
    assert redact_url("   ", line) == line


def test_the_bundle_reports_graph_health_and_maintenance(engine):
    """The graph IS the strategy -- an edge is the only path by which an
    anchor's news reaches a tradeable -- so the pasteable bundle has to say
    whether the edge map is being kept alive, not just what the dossiers say.
    Also guards the section's own formatting: it is the one block built from
    live engine state (backfill markers, periodic timestamps) rather than
    settings, so a shape change here fails loudly instead of at 3am on the
    operator's dashboard."""
    report = run_diagnostics(engine)

    assert "--- Graph health" in report
    assert "tradeables connected:" in report
    assert "anchors linked to a tradeable:" in report
    assert "rolling refresh:" in report
    assert "anchors researched for suppliers:" in report


# --- The bundle's own blind spots, each one a real misdiagnosis -----------


def _write_log(tmp_path, name, text):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / name).write_text(text)


REGSHO_FAILURE = (
    "2026-08-11 14:24:02 UTC | WARNING | smartboi.regsho | [REGSHO] No threshold list found "
    "in the last 6 day(s) -- keeping the previous list (0 symbol(s), as of never). Borrow "
    "flags fall back to the market-cap proxy. Tried:\n"
    "  https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth20260810.txt -> HTTP 200 "
    "but 0 symbols parsed; body starts: 'Symbol|Security Name|Market Category'\n"
)


def test_a_multi_line_warning_keeps_its_payload(engine, tmp_path):
    """The line filter kept the header and dropped every continuation, so the
    bundle showed a bare 'Tried:' with all the URLs removed -- the diagnostic
    written to explain a failure was invisible in the artifact that exists to
    carry it, and Reg SHO was misdiagnosed as a dead integration for weeks
    when the log said plainly that the PARSE, not the fetch, was at fault."""
    _write_log(tmp_path, "smartboi.log", REGSHO_FAILURE)
    report = run_diagnostics(engine)

    assert "Tried:" in report
    assert "nasdaqth20260810.txt" in report
    assert "0 symbols parsed" in report


def test_rotated_logs_are_read(engine, tmp_path):
    """Reading only smartboi.log was self-defeating: a burst big enough to
    matter is a burst big enough to ROTATE. 11,893 billing failures in two
    hours had already rotated out by the time anyone looked, and the bundle
    reported nothing unusual."""
    _write_log(tmp_path, "smartboi.log.1",
               "2026-08-09 07:00:00 UTC | WARNING | smartboi.dossier | AAA: dossier update "
               "proposal failed: credit balance is too low\n")
    _write_log(tmp_path, "smartboi.log",
               "2026-08-11 14:00:00 UTC | WARNING | smartboi.regsho | [REGSHO] routine\n")
    report = run_diagnostics(engine)

    assert "credit balance is too low" in report


def test_repeated_failures_are_counted_not_just_tailed(engine, tmp_path):
    """A 40-line tail cannot show 11,893 of anything. The histogram collapses
    a repeated failure to one counted row, so a storm is legible as a storm."""
    _write_log(tmp_path, "smartboi.log", "".join(
        f"2026-08-09 07:{n % 60:02d}:00 UTC | WARNING | smartboi.dossier | SYM{n}: dossier "
        f"update proposal failed: credit balance is too low\n"
        for n in range(500)
    ))
    report = run_diagnostics(engine)

    assert "Warning/error counts across the whole retained log" in report
    # Per-symbol subjects and digits are stripped, so all 500 collapse to ONE
    # counted row rather than 500 shapes -- without that they would be no more
    # legible than the tail they are meant to summarise.
    assert "   500  WARNING smartboi.dossier" in report
    assert report.count("credit balance is too low") < 50


def test_uptime_and_restart_count_are_reported(engine, tmp_path):
    """A two-hour-old build gets mistaken for a steady state without this."""
    _write_log(tmp_path, "smartboi.log",
               "2026-08-11 12:20:46 UTC | INFO    | smartboi.main | "
               "=== SmartBoi version=0.57.0 commit=abc123 ===\n")
    report = run_diagnostics(engine)

    assert "started_at" in report
    assert "restarts/24h" in report


def test_the_daily_pass_schedule_is_reported(engine):
    """The 12-hour drift between the two halves of the forward-return join
    was only ever visible by reading periodic_pass_state.json by hand."""
    engine.periodic_state.set("dossier_snapshot", "2026-08-10T16:00:51+00:00")
    engine.periodic_state.set("price_marks", "2026-08-11T04:11:46+00:00")
    report = run_diagnostics(engine)

    assert "Daily pass schedule" in report
    assert "dossier_snapshot" in report and "price_marks" in report


def test_an_open_circuit_breaker_is_stated_not_inferred(engine):
    """A halted system and an idle one both read as a low spend."""
    engine.usage.note_failure(Exception("your credit balance is too low"))
    report = run_diagnostics(engine)

    assert "LLM CIRCUIT BREAKER OPEN" in report


def test_an_empty_regsho_list_is_called_out(engine):
    """ENABLED is a config echo. Reg SHO read ENABLED for the whole life of
    the integration while holding zero symbols."""
    assert engine.regsho is not None, "fixture must have Reg SHO wired for this to mean anything"
    report = run_diagnostics(engine)
    assert "0 symbol(s), as of never" in report
    assert "market-cap borrow proxy" in report


# --- The full file bundle -------------------------------------------------


def _bundle(engine):
    import io
    import zipfile

    from smartboi.tools import collect_full_diagnostics
    return zipfile.ZipFile(io.BytesIO(collect_full_diagnostics(engine)))


def test_the_full_bundle_carries_the_files_a_summary_cannot(engine, tmp_path):
    """Each of these had to be fetched by hand, over several rounds, to
    diagnose the last round of failures: the raw log (a storm that had
    rotated away), signals.jsonl (a collapse in the firing RATE),
    paper_trades.jsonl (which trades predate position sizing) and
    periodic_pass_state.json (two passes drifting apart)."""
    _write_log(tmp_path, "smartboi.log", "2026-08-11 14:00:00 UTC | WARNING | x | y\n")
    _write_log(tmp_path, "smartboi.log.1", "2026-08-09 07:00:00 UTC | WARNING | x | old\n")
    (tmp_path / "logs" / "signals.jsonl").write_text('{"symbol":"DCO"}\n')
    engine.periodic_state.set("price_marks", "2026-08-11T04:11:46+00:00")

    names = _bundle(engine).namelist()

    assert "diagnostics.txt" in names
    assert "MANIFEST.txt" in names
    assert "logs/smartboi.log" in names
    assert "logs/smartboi.log.1" in names          # rotations too
    assert "logs/signals.jsonl" in names
    assert "data/periodic_pass_state.json" in names


def test_the_full_bundle_never_carries_a_credential(engine, tmp_path):
    """It reaches the same places the pasteable bundle does and makes the
    same promise, so it gets the same scrub -- at the same boundary."""
    _write_log(tmp_path, "smartboi.log",
               "2026-08-11 14:00:00 UTC | ERROR | smartboi.news | failed: "
               "https://finnhub.io/api/v1/x?token=FINNHUB-LEAKED-KEY\n")
    zf = _bundle(engine)

    blob = b"".join(zf.read(n) for n in zf.namelist())
    for fragment in (b"FINNHUB-LEAKED-KEY", b"sk-ant-", b"LEAKED-WEBHOOK-ID", b"real.person@"):
        assert fragment not in blob
    assert b"token=REDACTED" in blob


def test_the_full_bundle_excludes_configuration_entirely(engine, tmp_path):
    """No .env, no /data/options.json. Those hold the Anthropic and Finnhub
    keys and the webhook URL, and nothing in this archive is worth them --
    which is why both file lists are allow-lists rather than globs."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-NOPE\n")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "options.json").write_text('{"anthropic_api_key":"sk-ant-NOPE"}')
    (tmp_path / "data" / "graph.json.corrupt-20260809T060000Z").write_text('{"secret":"NOPE"}')

    zf = _bundle(engine)
    names = zf.namelist()

    assert not any(".env" in n or "options.json" in n or "corrupt" in n for n in names)
    assert b"sk-ant-NOPE" not in b"".join(zf.read(n) for n in names)


def test_the_full_bundle_survives_a_broken_summary(engine, monkeypatch):
    """The files are the point. A summary that raises must cost the summary,
    not the archive -- the bundle is most needed exactly when something is
    broken enough to break the report."""
    import smartboi.tools as tools_module
    monkeypatch.setattr(tools_module, "run_diagnostics",
                        lambda _engine: (_ for _ in ()).throw(RuntimeError("boom")))

    zf = _bundle(engine)
    assert "MANIFEST.txt" in zf.namelist()
    assert b"boom" in zf.read("diagnostics.txt")
