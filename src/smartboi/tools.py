"""Operator tools -- the analyses that used to require a terminal, exposed
as plain awaitable/callable functions so the dashboard can run them from a
button (see webapp.py's /api/tools/*) instead of anyone needing shell access
to the Home Assistant host.

That matters more than convenience: the HA host's terminal add-ons are an
awkward and unreliable place to run anything, and the two things an operator
most needs between deployments -- "does this candidate ticker actually screen
thin enough to trade?" and "is confidence*magnitude predicting forward
returns yet?" -- are exactly the things that shouldn't require it.

Both are strictly read-only with respect to the strategy: screening performs
Finnhub lookups and returns a report, forward-return analysis reads two
append-only log files. Neither mutates a dossier, the graph, the universe, or
the paper journal, and neither can place an order (nothing in this codebase
can -- see prices.py/paper_journal.py)."""
from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartboi.news import redact_token, redact_url
from smartboi.paper_journal import cost_buckets, trade_economics
from smartboi.usage import CAT_RESEARCH, CATEGORIES
from smartboi.status import (
    gather_dossiers,
    gather_graph_health,
    gather_paper_trade_stats,
    gather_strategy_generations,
)
from smartboi.event_study import (
    OUTCOME_LABELS,
    attach_outcomes,
    collapse_episodes,
    format_event_study,
)
from smartboi.forward_returns import (
    compute_forward_return,
    dedup_snapshots,
    format_report,
    price_marks_by_symbol,
)
from smartboi.exit_analysis import format_report as format_exit_report
from smartboi.skeptic_report import analyze_skeptic_effect, format_skeptic_report
from smartboi.screen import candidates_from_file, resolve_candidates_path
from smartboi.universe import CompanySpec, spec_by_symbol
from smartboi.edgar_search import MAX_HITS_PER_QUERY, concentration_context
from smartboi.research import (
    MAX_ANCHORS_PER_RUN,
    ResearchedSupplier,
    SupplierResearcher,
    format_research_report,
    merge_into_candidates,
    researched_anchors,
)
from smartboi.universe_screen import format_screening_report, guess_ecosystem, screen_candidate

log = logging.getLogger(__name__)

DEFAULT_HORIZONS = (5, 20)
# A screening pass costs two Finnhub calls per ticker, and FinnhubClient
# spaces requests ~1.1s apart to stay inside the free tier's 60/min -- so a
# request is roughly 2.2s per ticker. Capped so one dashboard click can't
# monopolise the shared client for many minutes while the engine's own news
# polling waits behind it.
MAX_TICKERS_PER_RUN = 40


def read_jsonl(path: Path) -> list[dict]:
    """Tolerant JSONL read: a malformed line is skipped rather than aborting
    the whole analysis. These are append-only logs written by a long-running
    process, so a torn final line after an unclean shutdown is possible and
    must not make the report unavailable."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


async def run_screen(
    finnhub,
    universe: list[CompanySpec],
    tickers: list[str],
    min_cap: float,
    max_cap: float,
    max_analysts: int,
    candidates_file: str | None = None,
) -> str:
    """Screens `tickers` (or every resolved-ticker universe candidate when
    empty) against the thin-coverage bounds, returning the formatted report.

    Takes the CALLER'S FinnhubClient rather than constructing one: the
    engine's client carries the process's request-spacing state (see
    news.py's _throttled_get), so sharing it keeps a dashboard-triggered
    screen inside the same 60/min free-tier budget the engine's own polling
    is already pacing against. A second client would have its own timer and
    the two together would 429."""
    if finnhub is None:
        return ("News ingestion is disabled, so there's no Finnhub client to screen with. "
                "Set FINNHUB_API_KEY (and enable news ingestion) first.")

    specs = spec_by_symbol(universe)
    if tickers:
        candidates = [(t, []) for t in tickers]
    else:
        candidates = candidates_from_file(resolve_candidates_path(candidates_file))
        if not candidates:
            return ("No resolved-ticker candidates discovered yet -- nothing to screen. "
                    "Enter tickers explicitly to screen names the extraction pipeline hasn't surfaced.")

    truncated = len(candidates) - MAX_TICKERS_PER_RUN
    candidates = candidates[:MAX_TICKERS_PER_RUN]

    results = []
    for symbol, related_to in candidates:
        market_cap = await finnhub.market_cap_musd(symbol)
        analysts = await finnhub.analyst_count(symbol)
        results.append(
            screen_candidate(
                symbol, market_cap, analysts, guess_ecosystem(related_to, specs),
                min_cap, max_cap, max_analysts,
            )
        )
    report = format_screening_report(results)
    if truncated > 0:
        # Never silently drop work -- an operator reading a table of 40 has
        # no way to tell it was capped otherwise.
        report += (f"\n\n({truncated} further candidate(s) not screened this run -- capped at "
                   f"{MAX_TICKERS_PER_RUN} per run to stay inside Finnhub's rate limit. Re-run to continue.)")
    return report


async def run_supplier_research(engine) -> str:
    """Researches the universe's anchors for small-cap counterparties the
    filing path structurally cannot find (see research.py), writing what it
    finds into the existing universe-candidate store.

    Anchors are ordered by how INERT they are -- one with no graph edge to
    any tradeable is discarded news, so it has the most to gain -- then by
    ecosystem so a run covers ground rather than ten names from one sector.
    Already-researched anchors are skipped, so re-running continues through
    the list."""
    if engine.settings.anthropic_api_key.strip() == "":
        return "Supplier research needs ANTHROPIC_API_KEY (it is a web-search-backed Claude call)."

    universe = set(engine.symbol_list)
    tradeables = {c.symbol for c in engine.universe if not c.signal_source_only}
    already = researched_anchors(engine.candidates, engine.research_state)

    def is_inert(symbol: str) -> bool:
        return not any(linked in tradeables
                       for linked, _ in engine.graph.linked_symbols(symbol, universe))

    anchors = [c for c in engine.universe if c.signal_source_only and c.symbol not in already]
    if not anchors:
        return ("Every anchor has already been researched. Delete `last_researched_at` from "
                "data/universe_candidates.json entries to re-run, or add more anchors.")
    anchors.sort(key=lambda c: (not is_inert(c.symbol), c.ecosystem, c.symbol))
    selected, skipped = anchors[:MAX_ANCHORS_PER_RUN], [c.symbol for c in anchors[MAX_ANCHORS_PER_RUN:]]

    researcher = SupplierResearcher(
        engine.settings.anthropic_api_key, engine.settings.synthesis_model, engine.usage,
    )
    results: list[tuple[str, list]] = []
    new = updated = 0
    try:
        for spec in selected:
            found = await researcher.research(
                spec.symbol, spec.name or spec.symbol, spec.ecosystem,
                engine.settings.universe_min_market_cap_musd,
                engine.settings.universe_max_market_cap_musd,
            )
            # None means NO REQUEST WENT OUT -- the budget is gone, the
            # circuit breaker is open, or the call itself failed. Nothing was
            # spent and nothing was learned, so the anchor must stay unmarked
            # and the run must stop: the gate that refused this one refuses
            # every anchor behind it, and marking them all would retire the
            # entire list permanently in a single run. Nothing expires
            # anchor_research.json, so that is not recoverable.
            if found is None:
                # Appended to the report's not-researched list rather than
                # counted separately: an operator reading the report needs the
                # SYMBOLS that still need doing, and these are in exactly the
                # same state as the ones the per-run cap deferred.
                unattempted = [s.symbol for s in selected[len(results):]]
                skipped.extend(unattempted)
                log.info("Supplier research stopped after %d anchor(s): %s. %d anchor(s) left "
                         "unmarked for a later run.",
                         len(results),
                         engine.usage.deferral_reason(CAT_RESEARCH) or "the call failed",
                         len(unattempted))
                break
            results.append((spec.symbol, found))
            # Marked BEFORE the merge and regardless of what came back: the
            # call is what costs money, so the call is what has to be
            # recorded. Gating this on `found` meant a legitimate empty
            # result was re-billed on every subsequent run, forever. [] still
            # means exactly that -- billed, and genuinely empty.
            engine.research_state.set(
                spec.symbol,
                {"researched_at": datetime.now(timezone.utc).isoformat(), "found": len(found)},
            )
            if found:
                added, touched = merge_into_candidates(engine.candidates, found)
                new += added
                updated += touched
    finally:
        await researcher.aclose()
    return format_research_report(results, new, updated, skipped)


# Anchors per EDGAR full-text search run. Each one costs a search request
# plus up to MAX_HITS_PER_QUERY document fetches to proximity-test, all at
# EDGAR's 0.3s request spacing -- so this is bounded for the same reason
# MAX_ANCHORS_PER_RUN is: an operator clicking a button wants it to finish.
MAX_SEARCH_ANCHORS_PER_RUN = 5


async def run_edgar_supplier_search(engine) -> str:
    """Asks EDGAR which OTHER filers name each anchor, and routes what it
    finds to universe CANDIDATES (see edgar_search.py for why that is the
    only honest destination).

    Operator-run rather than part of the tick loop, for the same three
    reasons run_supplier_research is: the results need a human accept anyway,
    the right cadence is "when the anchor list changes" rather than hourly,
    and it is the kind of pass whose first real run should be watched rather
    than discovered in a log three days later.

    Anchors are ordered by how INERT they are -- one with no graph edge to
    any tradeable has its news resolved to zero targets and discarded unread,
    so it has the most to gain from a lead."""
    if engine.edgar_client is None:
        return ("EDGAR ingestion is disabled, so there is no client to search with. "
                "Set ENABLE_EDGAR_INGESTION=true and EDGAR_USER_AGENT first.")

    universe_symbols = set(engine.symbol_list)
    tradeables = {c.symbol for c in engine.universe if not c.signal_source_only}

    def is_inert(symbol: str) -> bool:
        return not any(linked in tradeables
                       for linked, _ in engine.graph.linked_symbols(symbol, universe_symbols))

    # Anchors already searched are skipped so a run CONTINUES through the
    # list. Without this the pass is idempotent-by-accident when a human
    # presses the button occasionally, but ruinous on a schedule: selection
    # is deterministic (inertness, then ecosystem, then symbol), so a daily
    # run would re-search the same first five anchors forever and never
    # reach the rest. This is the identical trap research.researched_anchors
    # documents for supplier research, and the marker is written per anchor
    # as it completes, so a crash mid-run loses only the unfinished ones.
    already = set(engine.edgar_search_state.data.keys())
    anchors = [c for c in engine.universe
               if c.signal_source_only and (c.name or "").strip() and c.symbol not in already]
    if not anchors:
        return ("Every anchor has already been searched. Delete "
                "data/anchor_edgar_search.json to re-run them, or add more anchors.")
    anchors.sort(key=lambda c: (not is_inert(c.symbol), c.ecosystem, c.symbol))
    per_run = max(1, int(getattr(engine.settings, "edgar_search_anchors_per_run",
                                 MAX_SEARCH_ANCHORS_PER_RUN)))
    selected = anchors[:per_run]
    skipped = [c.symbol for c in anchors[per_run:]]

    lines: list[str] = ["EDGAR full-text supplier search", "=" * 34, ""]
    new = updated = 0
    for spec in selected:
        hits = await engine.edgar_client.full_text_search(spec.name)
        # Marked as soon as the search returns, and regardless of what came
        # back -- the REQUEST is what was spent, so the request is what has
        # to be recorded. Gating this on a hit would re-search a legitimately
        # empty anchor on every future run, forever, which is the same bug
        # research.researched_anchors was fixed for.
        engine.edgar_search_state.set(
            spec.symbol,
            {"searched_at": datetime.now(timezone.utc).isoformat(), "hits": len(hits)},
        )
        if not hits:
            lines.append(f"{spec.symbol} ({spec.name}): no hits.")
            continue
        found = []
        checked = 0
        for hit in hits[:MAX_HITS_PER_QUERY]:
            # Already in the universe -> _poll_edgar has fetched this filing
            # and run extraction on it already. A lead about a name we hold
            # is not a lead.
            if hit.ticker and hit.ticker in engine.spec_by_symbol:
                continue
            if not hit.cik or not hit.document:
                continue  # cannot build an archive URL -> cannot proximity-test
            checked += 1
            try:
                text = await engine.edgar_client.fetch_text(
                    engine.edgar_client.filing_from_hit(hit), max_chars=200_000,
                )
            except Exception:  # noqa: BLE001 - one unreadable filing must not stop the run
                log.exception("%s: could not fetch %s for the proximity pass", spec.symbol, hit.adsh)
                continue
            context = concentration_context(text, spec.name)
            if not context:
                continue
            found.append(ResearchedSupplier(
                anchor=spec.symbol,
                name=hit.name,
                ticker=hit.ticker,
                rel_type="supplier",
                # The raw sentence, verbatim and truncated, NOT a verdict.
                # An IDIQ ceiling, a historical figure and a live
                # concentration disclosure all match the same phrases and
                # only the actual words tell them apart -- so the operator
                # reads them, not a summary of them.
                description=f"[{hit.form} {hit.filing_date}] ...{context[:320]}...",
                evidence_url=f"https://www.sec.gov/Archives/edgar/data/{hit.cik}/"
                             f"{hit.adsh.replace('-', '')}/",
                # Below DISCLOSED_LINK_CONFIDENCE on purpose. This IS a
                # primary filing disclosure, but it is being read by a regex
                # proximity heuristic rather than by the extraction pass, and
                # merge_into_candidates writes no edge from it either way.
                confidence=0.6,
            ))
        lines.append(
            f"{spec.symbol} ({spec.name}): {len(hits)} hit(s), {checked} fetched, "
            f"{len(found)} with a concentration disclosure."
        )
        if found:
            added, touched = merge_into_candidates(engine.candidates, found)
            new += added
            updated += touched

    lines += ["", f"{new} new candidate(s), {updated} updated.",
              "Candidates only -- no graph edge is written from a search hit. Accept one and "
              "its own filings are backfilled; the edge is created only if a filing discloses it."]
    if skipped:
        # Never silently drop work.
        lines.append(f"\n{len(skipped)} anchor(s) not searched this run (capped at "
                     f"{MAX_SEARCH_ANCHORS_PER_RUN}): {', '.join(skipped[:12])}"
                     f"{' ...' if len(skipped) > 12 else ''}. Re-run to continue.")
    return "\n".join(lines)


async def run_graph_maintenance(engine, apply: bool = False) -> str:
    """The one graph-maintenance pass: AUDIT what is already there, CLEAN what
    is structurally unfit, then GROW.

    WHY ONE BUTTON. Maintenance had accreted into three daily passes, two
    operator buttons and a connectivity reconcile with its own dry-run, and
    between them they only ever asked "is the graph BIG enough". Nothing asked
    "is what we have CORRECT" -- which is how seven of eleven accepted
    tradeables came to be bond funds, preferred series and delisted shells,
    each polled hourly and accruing LLM spend against a thesis that cannot
    exist. Splitting the work across buttons also made the ORDER an operator
    problem, and the order matters: growing before cleaning re-admits the
    symbol you just removed, and cleaning before the ticker recheck acts on
    stale resolutions.

    So the sequence is fixed, and it is the argument for the button:

      1. AUDIT      read-only; every structural fault, most decisive first.
      2. CLEAN      quarantine the unfit (only with apply=True). Never
                    deletes, never touches a symbol with an open paper trade.
      3. RESOLVE    retry ticker resolution and re-screen candidates, so the
                    growth step sees current recommendations.
      4. DISCOVER   EDGAR full-text search: which other filers name our
                    anchors. Free, and the only pass that is size-selected
                    toward small counterparties.
      5. CONNECT    the connectivity reconcile, last, so it can act on
                    everything the previous steps produced.

    apply=False is a full dry run: the audit and the search still run (they
    are read-only and candidate-only respectively), and the clean and the
    reconcile report exactly what they WOULD do without mutating anything.

    Deliberately NOT included: the web-search supplier research. It is the one
    pass that spends real money per press, it already runs daily on its own
    cadence, and a maintenance button an operator is meant to press freely
    must not have a variable bill attached to it."""
    lines: list[str] = [
        f"Graph maintenance -- {'APPLY' if apply else 'DRY RUN'}",
        "=" * 46,
        "",
    ]

    # --- 1. AUDIT ---
    findings = await engine.audit_universe()
    # Persisted even on a dry run: the audit is read-only, so refreshing the
    # dashboard's panel from it costs nothing and stops the panel showing
    # yesterday's count beside a report generated just now.
    summary = engine.persist_audit(findings)
    lines.append(f"1. AUDIT: {summary['total']} finding(s), {summary['actionable']} actionable.")
    if not findings:
        lines.append("   Nothing structurally wrong with the universe or the graph.")
    for kind, count in sorted(summary["by_kind"].items(), key=lambda kv: -kv[1]):
        lines.append(f"   {count:>4}  {kind}")
    lines.append("")
    for finding in findings[:MAX_LISTED_ROWS]:
        flag = "  " if finding.actionable else "!!"
        lines.append(f"   {flag} [{finding.kind}] {finding.subject}")
        lines.append(f"        {finding.detail}")
        if finding.blocked_reason:
            lines.append(f"        NOT ACTIONED: {finding.blocked_reason}")
    if len(findings) > MAX_LISTED_ROWS:
        lines.append(f"   ... and {len(findings) - MAX_LISTED_ROWS} more.")
    lines.append("")

    # --- 2. CLEAN ---
    cleaned = engine.quarantine_from_findings(findings, apply=apply)
    planned = cleaned["would_quarantine"]
    if not planned:
        lines.append("2. CLEAN: nothing to quarantine.")
    elif apply:
        lines.append(f"2. CLEAN: quarantined {len(cleaned['quarantined'])} symbol(s): "
                     f"{', '.join(cleaned['quarantined'])}.")
        lines.append("   Recoverable from data/quarantined_symbols.json -- delete a row to reconsider.")
    else:
        lines.append(f"2. CLEAN: would quarantine {len(planned)} symbol(s): "
                     f"{', '.join(row['symbol'] for row in planned)}.")
    lines.append("")

    # --- 3. RESOLVE ---
    try:
        await engine._run_candidate_ticker_recheck()
        lines.append("3. RESOLVE: ticker resolution and candidate screening refreshed.")
    except Exception as exc:  # noqa: BLE001 - one failed step must not lose the rest of the report
        log.exception("Graph maintenance: ticker recheck failed")
        lines.append(f"3. RESOLVE: FAILED ({exc}). Later steps still ran.")
    lines.append("")

    # --- 4. DISCOVER ---
    try:
        search_report = await run_edgar_supplier_search(engine)
        lines.append("4. DISCOVER (EDGAR full-text search)")
        lines += [f"   {line}" for line in search_report.splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001
        log.exception("Graph maintenance: EDGAR search failed")
        lines.append(f"4. DISCOVER: FAILED ({exc}).")
    lines.append("")

    # --- 5. CONNECT ---
    try:
        rec = await engine.reconcile_universe_connectivity(apply=apply)
        added = rec["added"] if apply else [a["symbol"] for a in rec["would_add"]]
        pruned = rec["pruned"] if apply else rec["would_prune"]
        verb = "" if apply else "would "
        lines.append(f"5. CONNECT: {verb}add {len(added or [])} connected symbol(s), "
                     f"{verb}prune {len(pruned or [])} inert accepted anchor(s).")
        if added:
            lines.append(f"   add:   {', '.join(sorted(added))}")
        if pruned:
            lines.append(f"   prune: {', '.join(sorted(pruned))}")
        if rec["inert_seed_anchors"]:
            lines.append(f"   {len(rec['inert_seed_anchors'])} curated seed anchor(s) inert -- "
                         "reported only, remove them from universe.py by hand.")
    except Exception as exc:  # noqa: BLE001
        log.exception("Graph maintenance: connectivity reconcile failed")
        lines.append(f"5. CONNECT: FAILED ({exc}).")

    lines.append("")
    if apply:
        lines.append("Applied. Nothing was deleted: quarantined symbols keep their row and their "
                     "reason, and no symbol with an open paper trade was touched.")
    else:
        lines.append("Dry run -- nothing was changed. New CANDIDATES may have been recorded by the "
                     "search step, which is candidate-only by construction and writes no edge.")
    lines.append("The web-search supplier research is not part of this button (it costs money per "
                 "run); it keeps its own daily cadence.")
    return "\n".join(lines)


def run_forward_returns(
    log_dir: str | Path,
    universe: list[CompanySpec],
    horizons: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
) -> str:
    """The "does score predict forward returns" report over every captured
    dossier snapshot, for each horizon -- the analysis half of the
    forward-validation capture (see engine.py's _run_daily_snapshot /
    _run_daily_price_marks). Pure file reads; no network, no LLM."""
    log_dir = Path(log_dir)
    raw_snapshots = read_jsonl(log_dir / "dossier_snapshots.jsonl")
    marks = read_jsonl(log_dir / "price_marks.jsonl")
    if not raw_snapshots:
        return "No dossier snapshots captured yet -- nothing to analyze. These accrue once a day."
    if not marks:
        return ("No price marks captured yet -- nothing to join against. These accrue once a day from "
                "IB when it's reachable, otherwise from Finnhub quotes (FINNHUB_API_KEY).")

    snapshots = dedup_snapshots(raw_snapshots)
    lines = []
    dropped = len(raw_snapshots) - len(snapshots)
    if dropped:
        lines.append(
            f"Dropped {dropped} duplicate snapshot row(s) (restart artifacts) -- "
            f"{len(snapshots)} distinct (symbol, date) observations remain.\n"
        )

    price_marks = price_marks_by_symbol(marks)
    ecosystem_by_symbol = {symbol: spec.ecosystem for symbol, spec in spec_by_symbol(universe).items()}
    directional = [s for s in snapshots if s.get("direction") in ("LONG", "SHORT")]
    for horizon_days in horizons:
        joined = [
            r for r in (compute_forward_return(s, price_marks, horizon_days) for s in directional)
            if r is not None
        ]
        lines.append(format_report(horizon_days, joined, price_marks, ecosystem_by_symbol,
                                   attempted=len(directional)))
        lines.append("")
    return "\n".join(lines)


def run_skeptic_report(log_dir: str | Path, dossier_store) -> str:
    """The skeptic-effect readout: refutation rate and re-scaling distribution
    over every accepted evidence record (each carries the updater's pre-skeptic
    proposed_confidence/proposed_magnitude) and the refutation log. The inputs
    that decide whether the trade-gating skeptic belongs on a cheaper or
    pricier model tier. Pure file/dossier reads; no network, no LLM."""
    refutations = read_jsonl(Path(log_dir) / "skeptic_refutations.jsonl")
    accepted = []
    for symbol in dossier_store.all_symbols():
        for e in dossier_store.load(symbol).evidence:
            accepted.append({
                "is_propagated": e.is_propagated,
                "model": e.reviewed_by_model or "unknown",
                "proposed_confidence": e.proposed_confidence,
                "proposed_magnitude": e.proposed_magnitude,
                "confidence": e.confidence,
                "magnitude": e.magnitude,
            })
    return format_skeptic_report(analyze_skeptic_effect(accepted, refutations))


def run_event_study(
    log_dir: str | Path,
    horizons: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
) -> str:
    """The signal-episode event study (see event_study.py): forward
    returns after each signal episode, split by what the engine did with
    it -- the entry-timing guards' scorecard. Pure file reads of
    signals.jsonl, decisions.jsonl, and price_marks.jsonl."""
    log_dir = Path(log_dir)
    signal_rows = read_jsonl(log_dir / "signals.jsonl")
    if not signal_rows:
        return "No signals logged yet -- the event study starts meaning something once signals fire."
    decision_rows = read_jsonl(log_dir / "decisions.jsonl")
    marks = read_jsonl(log_dir / "price_marks.jsonl")
    return format_event_study(signal_rows, decision_rows, price_marks_by_symbol(marks), horizons)


def run_exit_analysis(log_dir: str | Path) -> str:
    """Exit-quality report over the closed paper-trade ledger (see
    exit_analysis.py): how early trades closed vs their horizon, the realized
    reward:risk, whether stops gapped, the cost drag, and the hold-to-horizon
    counterfactual. Pure file reads of paper_trades.jsonl and price_marks.jsonl
    -- no network, no LLM, nothing mutated."""
    log_dir = Path(log_dir)
    trades = read_jsonl(log_dir / "paper_trades.jsonl")
    if not trades:
        return "No paper trades recorded yet -- exit analysis starts meaning something once trades close."
    marks = read_jsonl(log_dir / "price_marks.jsonl")
    return format_exit_report(trades, price_marks_by_symbol(marks))


# Settings safe to print in a diagnostics bundle. An explicit ALLOW-list, not
# a deny-list: the bundle is meant to be pasted into a chat or an issue, and a
# deny-list silently starts leaking the moment a new secret-ish setting is
# added. Everything omitted here is either a credential (anthropic_api_key,
# finnhub_api_key), personal data (edgar_user_agent carries a real name and
# email, as SEC requires), or a URL that can embed a token
# (alert_webhook_url).
_DIAGNOSTIC_SETTINGS = (
    "signal_confidence_threshold", "min_independent_sources",
    "min_independent_sources_news_only", "max_horizon_days",
    "max_favorable_drift_pct", "signal_entry_deadline_days",
    "stop_loss_pct", "take_profit_pct", "strategy_label", "transaction_cost_bps_per_side",
    "transaction_cost_profile",
    "initial_trading_capital", "trading_currency", "max_concurrent_positions",
    "max_daily_llm_calls", "max_daily_usd",
    "budget_share_extraction", "budget_share_synthesis", "budget_share_research",
    "budget_reserve_synthesis",
    "max_propagated_evidence_per_link",
    "propagated_evidence_cooldown_hours",
    "enable_ecosystem_propagation", "max_ecosystem_evidence_per_link",
    "extraction_model", "dossier_model", "skeptic_model", "synthesis_model",
    "synthesis_score_floor_pct",
    "edgar_poll_interval_sec", "news_poll_interval_sec", "price_poll_interval_sec",
    "signal_entry_poll_interval_sec",
    "enable_edgar_ingestion", "enable_news_ingestion", "edgar_forms",
    "max_6k_items_per_symbol_per_day", "enable_regsho",
    "enable_federal_register", "federal_register_lookback_days",
    "federal_register_poll_interval_sec",
    "enable_dod_contracts", "dod_lookback_days", "dod_poll_interval_sec",
    "dod_anchor_value_floor_usd",
    "edgar_lookback_days", "news_lookback_days", "enable_universe_autoscreen",
    "universe_min_market_cap_musd", "universe_max_market_cap_musd",
    "universe_max_analyst_count", "universe_screen_interval_days",
    "enable_auto_accept_candidates", "auto_accept_anchors", "auto_accept_tradeables",
    "auto_accept_min_seen_count", "auto_accept_max_per_day",
    "enable_relationship_backfill", "backfill_anchors",
    "enable_graph_refresh", "graph_refresh_symbols_per_day", "enable_auto_supplier_research",
    "enable_auto_edgar_search", "edgar_search_anchors_per_run",
    "enable_ib_price_feed", "ib_host", "ib_port",
)
MAX_LOG_LINES = 40
MAX_LISTED_ROWS = 60
# Continuation lines kept per multi-line warning. Reg SHO's per-URL report is
# six; the cap is what stops one pathological traceback from consuming the
# whole tail.
MAX_LOG_CONTINUATION_LINES = 12
# How many rotated files to read behind the live one. RotatingFileHandler is
# configured with backupCount=5 (logging_setup), so this reaches the whole
# retained history.
MAX_LOG_BACKUPS = 5
# A record starts with "2026-08-11 14:24:02 UTC | LEVEL | logger | ...".
# Anything else is a continuation of the record above it.
_LOG_RECORD_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC \| ")


def _jsonl_span(rows: list[dict], key: str) -> str:
    stamps = sorted(r.get(key, "")[:10] for r in rows if r.get(key))
    return f"{len(rows)} row(s), {stamps[0]} .. {stamps[-1]}" if stamps else f"{len(rows)} row(s)"


def _ago(stamp: str, now: datetime) -> str:
    """"3h 12m" for an ISO timestamp, or "?" for anything unparseable. Used
    wherever a bare timestamp would make the reader do the subtraction."""
    if not stamp:
        return "?"
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = int((now - then).total_seconds())
    if seconds < 0:
        return "0m"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {rem // 60}m"


def _restarts_last_24h(records: list[list[str]], now: datetime) -> int:
    """Startup banners in the last 24h, counted from the log rather than a
    process counter -- which could not survive the restarts it is counting.

    String comparison on the timestamp is safe rather than lazy: the format is
    fixed-width "%Y-%m-%d %H:%M:%S", so lexicographic order IS chronological
    order."""
    cutoff = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(
        1 for record in records
        if "=== SmartBoi version=" in record[0] and record[0][:19] >= cutoff
    )


def _log_paths(log_dir: Path) -> list[Path]:
    """The live log plus its rotations, oldest first.

    Reading only smartboi.log was self-defeating in exactly the case that
    matters: a burst big enough to matter is a burst big enough to ROTATE, so
    the bigger the incident the less likely the bundle could see it. Confirmed
    live -- 11,893 identical "credit balance is too low" failures inside two
    hours had already rotated out of the live file by the time anyone looked,
    and the bundle reported nothing unusual."""
    paths = [Path(log_dir) / f"smartboi.log.{n}" for n in range(MAX_LOG_BACKUPS, 0, -1)]
    paths.append(Path(log_dir) / "smartboi.log")
    return [p for p in paths if p.exists()]


def _read_log_records(log_dir: Path) -> list[list[str]]:
    """Every log record across the live file and its rotations, each as its
    own list of [first line, continuation...].

    Records rather than lines because a line filter silently truncates any
    message that spans lines, keeping the header and dropping the payload.
    Reg SHO's failure report is the live example: the fix that made the
    failure diagnosable logs "...Tried:\\n  <url> -> <outcome>" per URL, and
    the bundle showed nine copies of a bare "Tried:" with every URL removed --
    so the diagnostic written to explain the failure was invisible in the
    artifact that exists to carry it, and the integration was misdiagnosed as
    dead for weeks when the log said plainly that the parse, not the fetch,
    was at fault."""
    records: list[list[str]] = []
    for path in _log_paths(log_dir):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if _LOG_RECORD_START.match(line) or not records:
                records.append([line])
            else:
                records[-1].append(line)
    return records


def _recent_log_problems(records: list[list[str]], webhook_url: str = "") -> list[str]:
    """The tail of WARNING/ERROR records from the log and its rotations,
    continuation lines included.

    Run through redact_token because a logged exception can carry a Finnhub
    request URL, which has the API key in its query string -- this bundle is
    meant to be pasted somewhere."""
    problems = [rec for rec in records if "| WARNING" in rec[0] or "| ERROR" in rec[0]]
    out: list[str] = []
    for record in problems[-MAX_LOG_LINES:]:
        kept = record[:1 + MAX_LOG_CONTINUATION_LINES]
        dropped = len(record) - len(kept)
        out.extend(kept)
        if dropped > 0:
            out.append(f"  ... {dropped} more line(s) of this message")
    # Scrubbed a SECOND time here, on top of the scrub at each logging site.
    # Not redundancy for its own sake: this function's output is copied
    # verbatim into a bundle whose own heading promises credentials are
    # omitted, and pasted into chats and issue trackers. Every future
    # log.warning that happens to interpolate a URL would otherwise be one
    # edit away from leaking, in a file nobody thinks of as security-
    # sensitive. The boundary that makes the promise is the right place to
    # enforce it.
    return [redact_url(webhook_url, ln) for ln in out]


def _calendar_days(first: str, last: str) -> list[str]:
    """Every YYYY-MM-DD from `first` to `last` inclusive.

    Bounded at a year so a single stray timestamp in an old capture file
    cannot turn a table into tens of thousands of rows."""
    try:
        start = datetime.strptime(first, "%Y-%m-%d")
        end = datetime.strptime(last, "%Y-%m-%d")
    except (TypeError, ValueError):
        return sorted({first, last})
    span = min((end - start).days, 365)
    return [(start + timedelta(days=n)).strftime("%Y-%m-%d") for n in range(max(span, 0) + 1)]


def _log_span(records: list[list[str]]) -> str:
    """"2026-08-09 06:21 .. 2026-08-11 14:26" for a set of log records.

    Read off the first and last record that actually carries a timestamp, so
    a leading continuation line (a file that starts mid-record) cannot report
    a blank bound."""
    stamps = [r[0][:16] for r in records if _LOG_RECORD_START.match(r[0])]
    return f"{stamps[0]} .. {stamps[-1]}" if stamps else "no timestamped records"


def _log_problem_histogram(records: list[list[str]], webhook_url: str = "") -> list[str]:
    """WARNING/ERROR counts by logger and message shape, across the whole
    retained log history.

    The verbatim tail above answers "what just happened"; it cannot answer
    "what is happening constantly", and the two failures that mattered most in
    this system's live history were both invisible to it. A 40-line tail full
    of hourly Reg SHO and DoD repeats showed no trace of 11,893 billing
    failures in one morning, or of 243 whole-universe price lookup failures --
    the first because it had rotated away, both because a tail shows the last
    N lines and not the big N.

    The shape is the message with its variable head stripped (symbols, ids,
    numbers), so one repeated failure aggregates into one row with a count."""
    counts: dict[tuple[str, str], int] = {}
    for record in records:
        head = record[0]
        if "| WARNING" not in head and "| ERROR" not in head:
            continue
        parts = head.split(" | ", 3)
        if len(parts) < 4:
            continue
        level, logger, message = parts[1].strip(), parts[2].strip(), parts[3]
        key = (f"{level} {logger}", _message_shape(message))
        counts[key] = counts.get(key, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_LISTED_ROWS]
    return [
        redact_url(webhook_url, f"  {count:>6}  {origin:<28} {shape}")
        for (origin, shape), count in rows
    ]


def _message_shape(message: str) -> str:
    """A log message reduced to its constant part, so N occurrences of one
    failure collapse to one counted row.

    Drops a leading "SYMBOL: " subject, then replaces digit runs and SHORT
    quoted values with placeholders. All three are what vary between
    repetitions of the same underlying problem; without collapsing them,
    11,893 identical billing failures counted as 11,893 distinct shapes and
    the histogram would be no better than the tail.

    Short quoted values specifically, because of a real case this missed on
    its first outing: a malformed extraction response was being walked one
    CHARACTER at a time, and the offending character is interpolated as %r.
    That spread 7,618 occurrences of ONE bug across twenty-odd rows -- ('a'),
    ('b'), ('c') -- each looking like a minor separate annoyance. Only short
    runs are collapsed; a long quoted string is usually the message itself
    rather than a varying value."""
    # A bracketed tag is peeled off first and put back afterwards: it is
    # constant across repetitions and says which pass logged the line, so it
    # belongs in the shape -- but while it is in front, the subject strip
    # cannot see the symbol behind it. This codebase logs "[PAPER] %s: ..."
    # and "[UNIVERSE] %s dropped: ..." with the symbol as the only varying
    # part, and every one of those was getting its own histogram row.
    tag = ""
    message = message.strip()
    tag_match = _LEADING_TAG.match(message)
    if tag_match:
        tag, message = tag_match.group(0), message[tag_match.end():]
    message = tag + _LEADING_SUBJECT.sub("", message)
    message = _QUOTED_SPAN.sub(_collapse_short_quote, message)
    return _DIGIT_RUN.sub("#", message)[:MAX_SHAPE_CHARS]


def _collapse_short_quote(match: re.Match) -> str:
    """Replace a short quoted value with a placeholder, keeping long ones.

    Matching EVERY quoted span and deciding per match, rather than matching
    only short ones directly: a `'[^']{0,12}'` pattern skips a long span's
    opening quote and then pairs that span's CLOSING quote with the next
    span's opening one, so `'invalid_request_error', 'message'` came out as
    `'invalid_request_error'?'message'?` -- the delimiters eaten and the
    shape harder to read than the raw message. Consuming long spans whole
    keeps the scanner aligned to real quote pairs."""
    inner = match.group(0)[1:-1]
    return "'?'" if len(inner) <= 12 else match.group(0)


_LEADING_TAG = re.compile(r"^\[[A-Za-z][A-Za-z0-9_. -]{0,15}\] ")
_LEADING_SUBJECT = re.compile(r"^\[?[A-Z0-9][A-Z0-9.\-]{0,9}\]?: ")
_DIGIT_RUN = re.compile(r"\d+")
_QUOTED_SPAN = re.compile(r"'[^']*'")
# Long enough to keep the CAUSE of a structured API error, which is at the end
# of the message and not the start. At 110 the busiest row in a real bundle
# read "dossier update proposal failed: Error code: # - {...'invalid_request_
# error'..." and stopped -- 11,893 failures reported without ever saying that
# the credit balance was exhausted.
MAX_SHAPE_CHARS = 180


# --- The full file bundle -------------------------------------------------
#
# Everything run_diagnostics summarises, as the actual files. The text bundle
# is a summary by construction, and a summary is exactly what a novel problem
# is invisible in: diagnosing this system's last round of failures needed the
# raw logs (a storm that had rotated away), signals.jsonl (a collapse in the
# firing RATE), paper_trades.jsonl (which trades predate position sizing),
# periodic_pass_state.json (two passes drifting apart) and the dossiers (which
# verdict zeroed which thesis). Every one of those had to be fetched by hand,
# over several rounds, from a Home Assistant share.

# Files under log_dir. Logs are scrubbed line by line; the .jsonl captures are
# scrubbed wholesale (see _redact_text).
_BUNDLE_LOG_FILES = (
    "signals.jsonl",
    "decisions.jsonl",
    "paper_trades.jsonl",
    "open_paper_trades.json",
    "price_marks.jsonl",
    "dossier_snapshots.jsonl",
    "skeptic_refutations.jsonl",
    "universe_screen.jsonl",
)

# Files under data/. Deliberately an ALLOW-LIST rather than a glob: a glob
# would sweep up whatever a future version happens to drop in that directory,
# including the .corrupt-<timestamp> quarantine copies, and this archive
# leaves the machine.
_BUNDLE_DATA_FILES = (
    "graph.json",
    "graph_audit.json",
    "accepted_candidates.json",
    "universe_candidates.json",
    "quarantined_symbols.json",
    "dedup_index.json",
    "periodic_pass_state.json",
    "llm_usage.json",
    "model_provenance.json",
    "universe_screen_state.json",
    "auto_accept_state.json",
    "retry_state.json",
    "resynthesis_state.json",
    "sixk_state.json",
    "relationship_backfill.json",
    "edgar_cik_cache.json",
    "extracted_filings.json",
    "anchor_research.json",
    "anchor_edgar_search.json",
)

# Uncompressed ceiling for the whole archive. Text zips at roughly 10:1, so
# this is a few MB on the wire; the cap exists so a runaway log cannot turn a
# diagnostic click into an out-of-memory.
MAX_BUNDLE_BYTES = 120 * 1024 * 1024


def _redact_text(text: str, webhook_url: str) -> str:
    """The same scrub the text bundle's log lines get, applied to a whole
    file. This archive carries the same promise run_diagnostics makes and
    reaches the same places, so it gets the same treatment at the same
    boundary -- see _recent_log_problems for why the boundary is the right
    place rather than each logging site."""
    return redact_url(webhook_url, redact_token(text))


def collect_full_diagnostics(engine) -> bytes:
    """Every runtime file needed to diagnose this deployment from somewhere
    else, as one redacted zip.

    What is NOT in here is as deliberate as what is: no .env, no
    /data/options.json, no add-on configuration. Those hold the Anthropic and
    Finnhub keys and the webhook URL, and nothing in this archive is worth
    them. The two file lists above are allow-lists for the same reason."""
    s = engine.settings
    log_dir = Path(s.log_dir)
    # Taken from the live store rather than importing engine.DATA_DIR:
    # engine imports this module lazily, inside functions, precisely to keep
    # the dependency one-way, and a module-level import back would undo that.
    data_dir = engine.dossiers.dir_path.parent
    webhook = s.alert_webhook_url
    buf = io.BytesIO()
    manifest: list[str] = []
    budget = MAX_BUNDLE_BYTES

    def store(zf: zipfile.ZipFile, arcname: str, path: Path) -> None:
        nonlocal budget
        # Size checked from the DIRECTORY ENTRY, before the file is opened.
        # Checking after read_text() meant the cap was consulted once three
        # full copies of the file were already resident (raw, redacted, and
        # the encoded measurement), so a runaway log would have caused
        # precisely the out-of-memory the cap is here to prevent, and only
        # then been declined. st_size is bytes and the budget is bytes, so
        # this over-counts only for multi-byte characters -- in the
        # conservative direction.
        try:
            on_disk = path.stat().st_size
        except OSError as exc:
            manifest.append(f"  SKIPPED  {arcname}  ({exc})")
            return
        if on_disk > budget:
            manifest.append(
                f"  SKIPPED  {arcname}  ({on_disk:,} bytes would exceed the "
                f"{MAX_BUNDLE_BYTES // 1024 // 1024}MB bundle cap)")
            return
        try:
            raw = path.read_text(errors="replace")
        except OSError as exc:
            manifest.append(f"  SKIPPED  {arcname}  ({exc})")
            return
        text = _redact_text(raw, webhook)
        size = len(text.encode("utf-8", "replace"))
        budget -= size
        zf.writestr(arcname, text)
        manifest.append(f"  {size:>10,}  {arcname}")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # The text bundle first: it is the index to everything else, and a
        # reader should not have to reconstruct it from the raw files.
        try:
            report = run_diagnostics(engine)
        except Exception as exc:  # noqa: BLE001 - a broken summary must not cost the files
            report = f"run_diagnostics raised: {exc!r}"
            log.exception("Diagnostics summary failed while building the full bundle.")
        # Scrubbed like every other member rather than trusted. run_diagnostics
        # scrubs its own log lines, but the FAILURE path above interpolates a
        # raw exception -- and an httpx error carries the full request URL,
        # which for Finnhub has the API key in its query string. The one member
        # written outside store() must not be the one member that leaks.
        report = _redact_text(report, webhook)
        zf.writestr("diagnostics.txt", report)
        manifest.append(f"  {len(report):>10,}  diagnostics.txt")

        for path in reversed(_log_paths(log_dir)):  # newest first
            store(zf, f"logs/{path.name}", path)
        for name in _BUNDLE_LOG_FILES:
            path = log_dir / name
            if path.exists():
                store(zf, f"logs/{name}", path)
        for name in _BUNDLE_DATA_FILES:
            path = data_dir / name
            if path.exists():
                store(zf, f"data/{name}", path)
        # The dossiers carry the synthesis verdicts and the full evidence
        # bodies -- the difference between "this thesis scored 0.000" and
        # "this thesis was vetoed, on these grounds, against this evidence".
        dossier_dir = data_dir / "dossiers"
        if dossier_dir.is_dir():
            for path in sorted(dossier_dir.glob("*.json")):
                store(zf, f"data/dossiers/{path.name}", path)

        # Also scrubbed: a SKIPPED line embeds an OSError, whose text is a
        # path this system did not compose.
        zf.writestr("MANIFEST.txt", _redact_text("\n".join([
            "SmartBoi full diagnostics bundle",
            f"generated_at : {datetime.now(timezone.utc).isoformat()}",
            f"version      : {os.environ.get('SMARTBOI_VERSION', 'dev')}",
            f"commit       : {os.environ.get('SMARTBOI_COMMIT', 'unknown')}",
            "",
            "Credentials and personal data are scrubbed from every file, and no",
            "configuration file (.env, /data/options.json) is included at all.",
            "",
            "Contents (uncompressed bytes):",
            *manifest,
        ]), webhook))
    return buf.getvalue()


def run_diagnostics(engine) -> str:
    """One pasteable runtime-state bundle: what's enabled, what the universe
    and graph look like, every dossier's score, where evidence is actually
    coming from, spend, signals, trades, candidates, capture-log coverage and
    recent problems.

    Deliberately one report rather than several buttons -- diagnosing this
    system has repeatedly needed several of these AT ONCE (the evidence-source
    breakdown below is what exposed every news article being attributed to
    "finnhub.io", which made independent_source_count structurally unable to
    exceed 1 and blocked every signal). Pure reads; nothing here mutates
    state or costs an API call."""
    s = engine.settings
    out: list[str] = []
    add = out.append

    now = datetime.now(timezone.utc)
    add("=== SmartBoi diagnostics ===")
    add(f"generated_at : {now.isoformat()}")
    add(f"version      : {os.environ.get('SMARTBOI_VERSION', 'dev')}")
    add(f"commit       : {os.environ.get('SMARTBOI_COMMIT', 'unknown')}")
    # Uptime reframes every other number below, and reading a bundle without
    # it is how a two-hour-old process gets mistaken for a steady state. The
    # restart count comes from the log rather than a counter, so it survives
    # the restarts it is counting.
    add(f"started_at   : {engine.started_at} ({_ago(engine.started_at, now)} ago)")
    # Read ONCE and shared by the three consumers below (restart count, the
    # tail, the histogram). The retained history is up to six 5MB files, so
    # re-reading and re-splitting it per section was triple the work and
    # triple the peak memory for identical output.
    log_records = _read_log_records(Path(s.log_dir))
    restarts = _restarts_last_24h(log_records, now)
    add(f"restarts/24h : {restarts}"
        + ("  <-- restarting repeatedly; treat the state below as unsettled" if restarts > 8 else ""))

    add("\n--- Integrations ---")
    for label, on in (
        ("EDGAR ingestion", engine.edgar_client is not None),
        ("News ingestion", engine.finnhub is not None),
        ("Dossier engine (Claude)", engine.updater is not None),
        ("IB price feed", engine.price_feed is not None),
        ("Webhook alerts", engine.alerts.enabled),
    ):
        add(f"  {label:26} {'ENABLED' if on else 'disabled'}")
    # ENABLED is a config echo, not health. Reg SHO read ENABLED for the whole
    # life of the integration while holding zero symbols "as of never",
    # because a parser bug returned the empty set from a perfectly good file
    # -- a state this block would have shown on day one and the flag never
    # could. Everything else that is enabled-but-failing shows up in the
    # warning histogram below rather than needing per-integration bookkeeping.
    if engine.regsho is not None:
        as_of = engine.regsho.as_of or "never"
        add(f"  {'Reg SHO threshold list':26} {engine.regsho.count} symbol(s), as of {as_of}")
        if not engine.regsho.count:
            add("    ^^ EMPTY: every SHORT is falling back to the market-cap borrow proxy.")
            add("       Check the per-URL report in the warnings below -- an HTTP 200 that parses")
            add("       to zero symbols is a FORMAT problem, not a fetch problem.")

    tradeable = [c for c in engine.universe if not c.signal_source_only]
    anchors = [c for c in engine.universe if c.signal_source_only]
    add(f"\n--- Universe ({len(engine.universe)} symbols: {len(tradeable)} tradeable, {len(anchors)} anchors) ---")
    for eco in dict.fromkeys(c.ecosystem for c in engine.universe):
        t = [c.symbol for c in tradeable if c.ecosystem == eco]
        a = [c.symbol for c in anchors if c.ecosystem == eco]
        add(f"  {eco:16} tradeable({len(t)}): {' '.join(t) or '-'}")
        add(f"  {'':16} anchors({len(a)}): {' '.join(a) or '-'}")

    edges = engine.graph.relationships
    add(f"\n--- Relationship graph ({len(edges)} edges) ---")
    by_type: dict[str, int] = {}
    for e in edges:
        by_type[e.rel_type] = by_type.get(e.rel_type, 0) + 1
    add("  by type: " + (", ".join(f"{k}={v}" for k, v in sorted(by_type.items())) or "-"))
    for e in edges[:MAX_LISTED_ROWS]:
        add(f"  {e.from_symbol:6} -> {e.to_symbol:6} {e.rel_type:11} conf={e.confidence:.2f}  {e.description[:70]}")
    if len(edges) > MAX_LISTED_ROWS:
        add(f"  ... {len(edges) - MAX_LISTED_ROWS} more")

    dossiers = gather_dossiers(engine.dossiers)
    add(f"\n--- Dossiers ({len(dossiers)}) ---")
    if dossiers:
        add(f"  {'SYM':7}{'DIR':6}{'CONF':>7}{'MAG':>7}{'SCORE':>8}{'SRC':>5}{'EV':>4}  {'STATUS':9} MASS(agree/opp)")
        for d in dossiers[:MAX_LISTED_ROWS]:
            add(f"  {d['symbol']:7}{d['direction']:6}{d['confidence']:7.3f}{d['magnitude']:7.3f}"
                f"{d['confidence'] * d['magnitude']:8.3f}{d['independent_source_count']:5}{d['evidence_count']:4}"
                f"  {d['status']:9} {d.get('mass_agree', 0):.2f}/{d.get('mass_opposing', 0):.2f}")
    else:
        add("  none yet")

    # The pass that can stop every trade in the system, and which had no
    # section here at all. A veto writes confidence AND magnitude to exactly
    # 0.0, so a vetoed dossier and one whose evidence decayed away are the
    # same row in the table above -- and when 22 of 45 sat at 0.000 with no
    # position opened for four days, nothing in this bundle said which pass
    # had done it, or that a pass had done it at all.
    #
    # ARITH is the arithmetic score the aggregate proposed, RATED is what the
    # whole-body pass judged the same evidence worth, APPLIED is what
    # survived. A large and persistent ARITH/RATED gap is the finding: it
    # means the aggregate is counting one story many times, which is what
    # effective_corroboration_count now bounds.
    # Whether the per-item fact labelling is actually happening. The whole
    # per-fact independence mechanism rests on the model assigning a label and
    # REUSING it rather than paraphrasing; a model that quietly stops doing
    # either degrades scoring back to per-channel counting with no error
    # raised anywhere. Two numbers say which: coverage (are items labelled at
    # all) and the facts-to-items ratio (is it collapsing anything).
    labelled = sum(d.get("labelled_evidence_count", 0) for d in dossiers)
    items = sum(d.get("evidence_count", 0) for d in dossiers)
    facts = sum(d.get("distinct_fact_keys", 0) for d in dossiers)
    add("\n--- Fact labelling (independence is counted per fact, see SCORING_VERSION 8) ---")
    add(f"  evidence items      : {items}")
    add(f"  carrying a label    : {labelled}" + (f"  ({labelled / items * 100:.0f}%)" if items else ""))
    add(f"  distinct facts      : {facts}"
        + (f"  -- {labelled / facts:.1f} item(s) per fact" if facts else ""))
    if items and labelled / items < 0.5:
        add("  ^^ under half the evidence carries a label. Items merged before SCORING_VERSION 8")
        add("     score under the OLD per-channel rules, so the board is currently a mix of both.")
    elif facts and labelled / facts < 1.2:
        add("  ^^ almost one fact per item: the labels are not collapsing anything. Either the")
        add("     evidence really is that diverse, or the model is paraphrasing instead of")
        add("     reusing a label -- check the dossiers before trusting a high source count.")

    judged = [d for d in dossiers if d.get("synthesis_at")]
    add(f"\n--- Synthesis verdicts ({len(judged)} of {len(dossiers)} dossier(s) judged) ---")
    if judged:
        vetoed = [d for d in judged if d["already_priced_in"]]
        redundant = [d for d in judged if d["redundant_evidence"] and not d["already_priced_in"]]
        add(f"  vetoed (already priced in) : {len(vetoed)}")
        add(f"  trimmed (redundant evidence): {len(redundant)}")
        add(f"  passed through              : {len(judged) - len(vetoed) - len(redundant)}")
        add(f"  {'SYM':7}{'ARITH':>7}{'RATED':>7}{'APPLIED':>9}{'FACTS':>7}{'SRC':>5}  {'VERDICT':11} JUDGED")
        for d in judged[:MAX_LISTED_ROWS]:
            rated = d["synthesis_confidence"] * d["synthesis_magnitude"]
            verdict = ("priced-in" if d["already_priced_in"]
                       else "redundant" if d["redundant_evidence"] else "-")
            add(f"  {d['symbol']:7}{d['pre_synthesis_score']:7.3f}{rated:7.3f}"
                f"{d['confidence'] * d['magnitude']:9.3f}{d['distinct_fact_count']:7}"
                f"{d['independent_source_count']:5}  {verdict:11} {_ago(d['synthesis_at'], now)} ago")
        gaps = [d["pre_synthesis_score"] / (d["synthesis_confidence"] * d["synthesis_magnitude"])
                for d in judged
                if d["synthesis_confidence"] * d["synthesis_magnitude"] > 0
                and d["pre_synthesis_score"] > 0]
        if gaps:
            gaps.sort()
            add(f"  median ARITH/RATED gap: {gaps[len(gaps) // 2]:.1f}x")
            if gaps[len(gaps) // 2] >= 3.0:
                add("  ^^ the arithmetic is running well hot against the only pass that reads the")
                add("     evidence as a body. That is an aggregate problem, not a synthesis one --")
                add("     check FACTS against SRC above: a large gap between them is one story")
                add("     counted many times (see dossier.effective_corroboration_count).")
        if len(judged) and len(vetoed) == len(judged):
            add("  ^^ EVERY judged dossier was vetoed. No thesis can reach a trade while this holds.")
    else:
        add("  none yet -- a dossier is only judged once it reaches "
            f"{s.signal_confidence_threshold * s.synthesis_score_floor_pct:.3f} "
            "(signal_confidence_threshold * synthesis_score_floor_pct)")

    # The single most diagnostic table here: independent_source_count counts
    # DISTINCT source names, so if this collapses to one or two entries then
    # corroboration is structurally impossible no matter how much news lands.
    add("\n--- Evidence sources seen (dedup index) ---")
    counts: dict[str, int] = {}
    for value in engine.dedup._seen.values():
        name = value[0] if isinstance(value, list) else value
        counts[name] = counts.get(name, 0) + 1
    add(f"  {len(engine.dedup._seen)} fingerprint(s) across {len(counts)} distinct source name(s)")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_LISTED_ROWS]:
        add(f"  {n:6}  {name}")
    if len(counts) <= 2:
        add("  ^^ WARNING: near-single source identity means independent_source_count")
        add("     can never exceed 1, so no dossier can ever reach the signal bar.")

    u = engine.usage.snapshot()
    add("\n--- LLM usage today ---")
    budget = f"/${u.daily_usd_budget:.2f}" if u.daily_usd_budget else " (no cap)"
    add(f"  {u.date}: {u.calls}/{u.daily_call_budget} calls, {u.input_tokens:,} in / {u.output_tokens:,} out tokens")
    add(f"  {' ' * len(u.date)}  ${u.usd_spent:.2f}{budget} estimated spend")
    # Stated, never inferred. A halted system and an idle one both read as a
    # low spend, and the difference is the whole diagnosis.
    if u.breaker_reason:
        add(f"  !! LLM CIRCUIT BREAKER OPEN since {u.breaker_tripped_at}: {u.breaker_reason}.")
        add("     Every Claude call is refused until UTC midnight. Ingestion continues and")
        add("     evidence keeps accruing, but nothing is being scored. See usage.note_failure.")
    # Per category, because the total alone hid the failure that mattered:
    # the budget was being consumed before the US market opened, by the one
    # pass whose output is not time-sensitive, leaving nothing for the pass
    # that turns news into a position. A single number cannot show that.
    shares = engine.usage.category_shares
    for cat in CATEGORIES:
        spent_usd, spent_calls = u.by_category.get(cat, (0.0, 0))
        share = shares.get(cat)
        if share is None or share >= 1.0:
            cap = "uncapped"
        elif share <= 0:
            cap = "OFF"
        elif u.daily_usd_budget:
            cap = f"cap ${u.daily_usd_budget * share:.2f} ({share * 100:.0f}%)"
        else:
            cap = f"cap {share * 100:.0f}%"
        exhausted = "" if engine.usage.budget_remaining(cat) else "  <-- EXHAUSTED, deferred to UTC midnight"
        add(f"    {cat:<11} ${spent_usd:>6.2f}  {spent_calls:>5} calls  {cap}{exhausted}")
    add("  ^^ 'dossier' (updater+skeptic) is uncapped by design: it gets whatever the others")
    add("     cannot reach, and the whole day when they are idle. See usage.py.")

    log_dir = Path(s.log_dir)
    signals = read_jsonl(log_dir / "signals.jsonl")
    decisions = read_jsonl(log_dir / "decisions.jsonl")
    # Episodes, not raw rows: evaluation is status-blind, so one signal
    # re-logs a row per accepted evidence item and a raw list overstates how
    # often signals actually fire. Joined against the decisions ledger so
    # each episode carries WHAT HAPPENED TO IT. Without this the bundle
    # showed a signal firing and zero trades with nothing in between, and
    # "why did this signal not become a trade" -- the single most important
    # question this system can be asked -- needed shell access to answer.
    episodes = attach_outcomes(collapse_episodes(signals), decisions)
    add(f"\n--- Signal episodes ({len(episodes)} from {len(signals)} logged row(s)) ---")
    if episodes:
        add(f"  {'FIRED':17}{'SYM':7}{'DIR':6}{'CONF':>6}{'MAG':>6}{'SCORE':>7}  {'OUTCOME':<24} WHY")
        for e in episodes[-MAX_LISTED_ROWS:]:
            score = (e.get("confidence") or 0.0) * (e.get("magnitude") or 0.0)
            add(f"  {e['fired_at'][:16]:17}{e['symbol']:7}{(e.get('direction') or '-'):6}"
                f"{e.get('confidence') or 0.0:6.2f}{e.get('magnitude') or 0.0:6.2f}{score:7.3f}"
                f"  {OUTCOME_LABELS[e['outcome']]:<24} {e['decision_reason'][:100]}")
        untracked = [e for e in episodes if e["outcome"] == "untracked"]
        if untracked:
            add(f"  ^^ {len(untracked)} episode(s) carry no ledger row -- either they fired before")
            add("     decisions.jsonl existed, or they are still SIGNALED awaiting an entry decision")
            add("     (cross-check STATUS in the dossier table above; an episode whose dossier is")
            add("     back to ACTIVE with no ledger row was expired by a pre-ledger build).")
        # The rate, not just the roster. A list of episodes cannot show a
        # collapse, and the collapse is the thing worth seeing: live, this
        # went 113 -> 41 -> 30 -> 1 -> 0 over five days while every other
        # number in the bundle looked ordinary, and reading it off required
        # loading signals.jsonl by hand.
        add("\n  Per day (last 14):  fired / opened / expired")
        # Keyed on the outcome KEYS ("trade_opened"/"signal_expired"), not on
        # the display strings OUTCOME_LABELS maps them to. Comparing against
        # the labels made both columns permanently zero and fired the alarm
        # below on every bundle that had any signal at all -- on a deployment
        # with fifteen closed trades, which is the precise opposite of what
        # this table is for.
        by_day: dict[str, list[int]] = {}
        for e in episodes:
            row = by_day.setdefault(e["fired_at"][:10], [0, 0, 0])
            row[0] += 1
            if e["outcome"] == "trade_opened":
                row[1] += 1
            elif e["outcome"] == "signal_expired":
                row[2] += 1
        # Every CALENDAR day in the window, not only the days that happened to
        # produce a signal. A day with zero signals is the single most
        # important row here -- skipping it is how a collapse from 113/day to
        # 0/day renders as an unbroken column of numbers.
        for day in _calendar_days(min(by_day), max(by_day))[-14:]:
            fired, opened, expired = by_day.get(day, (0, 0, 0))
            add(f"    {day}  {fired:>4} / {opened:>3} / {expired:>3}")
        recent = [by_day.get(d, (0, 0, 0))[1] for d in _calendar_days(min(by_day), max(by_day))[-5:]]
        if not any(recent):
            add("    ^^ nothing has OPENED in the last 5 days. Check the synthesis verdict table")
            add("       above before touching the signal bar.")
    else:
        add("  none yet")

    # Recomputed from the dossier rows above rather than read off
    # engine._entry_pending: that flag is deliberately optimistic (it starts
    # True and self-corrects on the first poll), which would read as "an
    # entry is waiting" on a deployment with no dossiers at all.
    waiting = [d["symbol"] for d in dossiers
               if d["status"] == "SIGNALED" and d["symbol"] not in engine.journal.open_trades]
    add("\n--- Entry pipeline ---")
    add(f"  price feed              : {'IB' if engine.price_feed is not None else 'DISABLED -- no entry can ever be confirmed'}")
    add(f"  waiting for an entry    : {' '.join(waiting) or 'nothing'}")
    add(f"  price poll (idle)       : {s.price_poll_interval_sec}s")
    add(f"  price poll (entry due)  : {s.signal_entry_poll_interval_sec}s")
    add(f"  entry deadline          : {s.signal_entry_deadline_days}d, max favorable drift {s.max_favorable_drift_pct}%")

    stats, closed = gather_paper_trade_stats(
        log_dir / "paper_trades.jsonl", s.initial_trading_capital, s.trading_currency,
    )
    # The graph IS the strategy -- an edge is the only path by which an
    # anchor's news reaches a tradeable, so a missing edge is a trade that
    # never happens. The two lines that matter most are the disconnected
    # tradeables carrying a thesis (single-stock signals wearing this
    # system's clothes) and how far behind the rolling re-extraction is.
    gh = gather_graph_health(
        engine.graph, engine.universe, engine.dossiers,
        backfill_state=engine.backfill_state.data,
        last_refresh=engine.periodic_state.get("graph_refresh", "") or "",
        last_research=engine.periodic_state.get("supplier_research", "") or "",
        researched_anchor_count=len(researched_anchors(engine.candidates, engine.research_state)),
        refresh_per_day=(s.graph_refresh_symbols_per_day if s.enable_graph_refresh else 0),
    )
    add("\n--- Graph health (the mechanism the whole strategy runs on) ---")
    add(f"  edges: {gh['edges']} ({', '.join(f'{k} {v}' for k, v in gh['edges_by_type'].items()) or '-'})")
    add(f"  tradeables connected: {gh['tradeables_connected']}/{gh['tradeables']} "
        f"({gh['tradeables_disconnected']} disconnected)")
    if gh["disconnected_with_thesis"]:
        add(f"  !! {gh['disconnected_with_thesis']} tradeable(s) carry a THESIS with no graph edge at all: "
            f"{' '.join(gh['disconnected_with_thesis_symbols'])}")
        add("     ^^ their dossier came only from their own filings -- the cross-company mechanism "
            "never fired for them.")
    add(f"  anchors linked to a tradeable: {gh['anchors_live']}/{gh['anchors']} "
        f"({gh['anchors_inert']} inert -- their news reaches nothing)")
    stalest = gh["stalest_days"]
    add(f"  extraction age: median {gh['median_extraction_age_days']}d, stalest "
        f"{'-' if stalest is None else f'{stalest:.0f}d'}, never extracted {gh['never_extracted']}")
    refresh_age = gh["last_refresh_days"]
    research_age = gh["last_research_days"]
    if gh["refresh_per_day"]:
        add(f"  rolling refresh: {gh['refresh_per_day']}/day -> full universe every ~{gh['cycle_days']:.0f}d; "
            f"last run {'never' if refresh_age is None else f'{refresh_age:.1f}d ago'}")
    else:
        add("  rolling refresh: DISABLED -- the graph only grows from new filings and the manual button.")
    add(f"  anchors researched for suppliers: {gh['researched_anchors']}/{gh['anchors']}, "
        f"last run {'never' if research_age is None else f'{research_age:.1f}d ago'}")

    add("\n--- Paper trades ---")
    # Currency overlay: each trade is one slot of the account, so the record
    # reads in real money as well as R. Equity is realized only; open positions'
    # unrealized P&L is marked live on the dashboard, not here.
    open_unreal = sum(v for v in (t.unrealized_currency() for t in engine.journal.open_trades.values()) if v is not None)
    add(f"  account: {stats.currency} {stats.initial_capital:.0f} start, "
        f"realized {stats.realized_pnl:+.2f} -> equity {stats.equity:.2f} "
        f"(open unreal. {open_unreal:+.2f}); "
        f"{s.max_concurrent_positions} slots @ {stats.currency} {s.initial_trading_capital / max(1, s.max_concurrent_positions):.0f} each")
    # Position sizing (PaperTrade.position_value) arrived after most of this
    # record was written, and currency_pnl is None for every trade opened
    # before it -- so "realized +0.00 -> equity 5000.00" reads as break-even
    # on a record that is actually well underwater in R. Say which trades the
    # money column can even describe, rather than letting a reader infer that
    # a flat equity means a flat result.
    closed_rows = [r for r in read_jsonl(log_dir / "paper_trades.jsonl") if r.get("closed_at")]
    priced = sum(1 for r in closed_rows if r.get("currency_pnl") is not None)
    if priced < stats.closed:
        add(f"  ^^ {stats.closed - priced} of {stats.closed} closed trade(s) predate position "
            "sizing and carry no currency result, so the equity above is NOT the record.")
        add("     Read avg R below; the money column becomes meaningful as new trades close.")
    add(f"  open: {len(engine.journal.open_trades)} ({', '.join(engine.journal.open_trades) or '-'})")
    # The win rate carries its 95% Wilson interval: at a dozen-odd closed
    # trades the point estimate alone reads as fact when it is noise, and the
    # interval width is exactly what says whether the record can be told apart
    # from the break-even hit rate below.
    add(f"  closed (all generations): {stats.closed} ({stats.wins}W/{stats.losses}L net of cost, "
        f"{stats.timeouts} held to horizon), win rate {stats.win_rate * 100:.0f}% "
        f"(95% CI {stats.win_rate_ci_low * 100:.0f}-{stats.win_rate_ci_high * 100:.0f}%), "
        f"avg R {stats.avg_r:.2f}")
    # By direction: a win is net-of-cost R>0, so a SHORT is no longer penalised
    # by a 100%-take-profit target it could only reach at price 0.
    if stats.closed_long or stats.closed_short:
        add(f"    by direction: long {stats.win_rate_long * 100:.0f}% (n={stats.closed_long}) / "
            f"short {stats.win_rate_short * 100:.0f}% (n={stats.closed_short})")
    # The all-time line above pools every strategy the record has ever run.
    # This splits it by generation so the CURRENT strategy is measured on its
    # own trades, not dragged by an old, abandoned config (see status.py).
    gens = gather_strategy_generations(log_dir / "paper_trades.jsonl", s.strategy_signature())
    if gens:
        add("  strategy record (win/loss by generation):")
        for g in gens:
            ver = ""
            if not g.legacy and g.version_from:
                ver = f" v{g.version_from}"
                if g.version_to and g.version_to != g.version_from:
                    ver += f"-v{g.version_to}"
            tag = "  <- current" if g.is_current else ""
            if g.closed:
                add(f"    {g.label}{ver}: {g.wins}W/{g.losses}L net of cost, win {g.win_rate * 100:.0f}% "
                    f"(CI {g.win_rate_ci_low * 100:.0f}-{g.win_rate_ci_high * 100:.0f}%), avg R {g.avg_r:+.2f}{tag}")
            else:
                add(f"    {g.label}{ver}: no closed trades yet{tag}")
    for symbol, trade in engine.journal.open_trades.items():
        econ = trade_economics(
            s.stop_loss_pct, s.take_profit_pct, trade.cost_bps_round_trip, trade.direction
        )
        cap = f"${trade.market_cap_musd:.0f}M" if trade.market_cap_musd else "cap unknown"
        add(f"    {symbol:6} {trade.direction:5} {cap:>12}  {trade.cost_bps_round_trip:.0f}bp round trip"
            f"  -> win {econ.r_win:+.2f}R / loss {econ.r_loss:+.2f}R")

    # The 8%/16% grid LOOKS like 2:1 and is not: cost is charged on notional
    # while R is measured against the stop distance, so it lands on both
    # legs and eats a large share of a tight stop. Printed per bucket
    # because it is the difference between a strategy that needs a 42% hit
    # rate and one that needs 59%, and nothing else in this output shows it.
    add(f"\n--- Cost drag on the {s.stop_loss_pct:.0f}%/{s.take_profit_pct:.0f}% grid "
        f"(profile: {s.transaction_cost_profile}) ---")
    buckets = cost_buckets(s.transaction_cost_profile)
    for index, (cap_floor, bps_per_side) in enumerate(buckets):
        charged = max(bps_per_side, s.transaction_cost_bps_per_side)
        econ = trade_economics(s.stop_loss_pct, s.take_profit_pct, charged * 2)
        # Buckets run high cap to low, so the previous entry's floor is this
        # one's ceiling -- printing only the floor labelled the last bucket
        # "any cap", which reads as if it applied to every trade.
        if index == 0:
            band = f">${cap_floor:.0f}M"
        elif cap_floor <= 0:
            band = f"<${buckets[index - 1][0]:.0f}M"
        else:
            band = f"${cap_floor:.0f}M-${buckets[index - 1][0]:.0f}M"
        add(f"  {band:>14}: {charged * 2:.0f}bp round trip -> win {econ.r_win:+.2f}R, "
            f"loss {econ.r_loss:+.2f}R, break-even win rate "
            f"{econ.breakeven_win_rate * 100:.0f}%, cost = {econ.cost_share_of_risk * 100:.0f}% of 1R")
    add("  ^^ break-even is the hit rate at which this grid nets zero AFTER costs, not 33%.")
    add("     (it assumes a win is the +target and a loss the -stop; the win rate above counts ANY "
        "net-of-cost profit as a win, so the two ratios are not directly comparable.)")
    if any(
        trade_economics(s.stop_loss_pct, s.take_profit_pct,
                        max(bps, s.transaction_cost_bps_per_side) * 2).breakeven_win_rate >= 0.55
        for _, bps in cost_buckets(s.transaction_cost_profile)
    ):
        add("  ^^ a bucket needs >=55% to break even: widen take_profit_pct, raise the tradeable")
        add("     market-cap floor, or -- if the intended position size genuinely cannot move the")
        add("     book -- set transaction_cost_profile to 'retail'. Do NOT do the last one to make")
        add("     the record look better; r_multiple_gross is already stored for that comparison.")

    cands = engine.candidates.data
    with_ticker = [c for c in cands.values() if c.get("ticker")]
    add(f"\n--- Universe candidates ({len(cands)}) ---")
    add(f"  with a resolved ticker : {len(with_ticker)}")
    add(f"  recommended tradeable  : {sum(1 for c in with_ticker if c.get('recommended_as') == 'tradeable')}")
    add(f"  recommended anchor     : {sum(1 for c in with_ticker if c.get('recommended_as') == 'anchor')}")
    add(f"  no recommendation yet  : {sum(1 for c in with_ticker if not c.get('recommended_as'))}")
    add(f"  blocked from auto-add  : {sum(1 for c in cands.values() if c.get('auto_accept_blocked'))}")
    auto = sum(1 for v in engine.accepted_candidates.data.values() if isinstance(v, dict) and v.get("source") == "auto")
    add(f"  accepted: {len(engine.accepted_candidates.data)} ({auto} auto, {len(engine.accepted_candidates.data) - auto} manual)")
    # Curated symbols the last screen found no market data for. Runtime-
    # accepted dead symbols are pruned automatically (see
    # Engine._prune_dead_symbols); these need a human to edit the list, so
    # they have to be visible somewhere other than a log line.
    curated_dead = engine.universe_screen_state.get("curated_no_market_data") or []
    curated_unknown = engine.universe_screen_state.get("curated_unknown_to_edgar") or []
    add(f"  last screened          : {engine.universe_screen_state.get('last_screened_at') or 'never'}")
    if curated_dead:
        add(f"  CURATED, no market data: {' '.join(curated_dead)}")
        add("    ^^ delisted/acquired/uncovered -- remove from universe.py or SYMBOLS; polling them costs and returns nothing")
    if curated_unknown:
        add(f"  CURATED, unknown to SEC: {' '.join(curated_unknown)}")
        add("    ^^ no CIK in EDGAR's ticker map -- can never produce filing evidence; remove from universe.py or SYMBOLS")

    add("\n--- Forward-validation capture ---")
    add(f"  dossier_snapshots.jsonl : {_jsonl_span(read_jsonl(log_dir / 'dossier_snapshots.jsonl'), 'snapshotted_at')}")
    add(f"  price_marks.jsonl       : {_jsonl_span(read_jsonl(log_dir / 'price_marks.jsonl'), 'marked_at')}")
    add(f"  decisions.jsonl         : {_jsonl_span(read_jsonl(log_dir / 'decisions.jsonl'), 'at')}")

    # WHEN each daily pass last ran, not just what it produced. Each is gated
    # on "24h since its own last run", so the passes drift independently and
    # the two halves of the forward-return join drift apart from each other:
    # live, the snapshot was landing at 16:00 UTC (midday ET, mid-session)
    # while the marks landed at 04:11 UTC (prior close) -- twelve hours apart,
    # visible nowhere except by reading periodic_pass_state.json by hand.
    add("\n--- Daily pass schedule ---")
    pass_state = engine.periodic_state.data
    if pass_state:
        for name in sorted(pass_state):
            stamp = str(pass_state.get(name) or "")
            add(f"  {name:20} last {stamp[:19] or 'never':19} ({_ago(stamp, now)} ago)")
        stamps = [str(v)[11:16] for v in pass_state.values() if isinstance(v, str) and len(str(v)) > 16]
        if len(set(stamps)) > 1:
            add("  ^^ these drift apart independently (each is gated on 24h since ITS last run).")
            add("     dossier_snapshot and price_marks are the two halves of one join -- a wide")
            add("     gap between them means scores and prices are captured at different")
            add("     points of the session.")
    else:
        add("  no pass has run yet")

    problems = _recent_log_problems(log_records, s.alert_webhook_url)
    add(f"\n--- Recent warnings/errors (last {MAX_LOG_LINES} messages) ---")
    for line in problems:
        add(f"  {line}")
    if not problems:
        add("  none")

    # The tail answers "what just happened"; it cannot answer "what is
    # happening constantly", and both failures that mattered most here were
    # invisible to it -- 11,893 billing failures in one morning (rotated out
    # of the live file entirely) and 243 whole-universe price failures, while
    # the visible 40 lines were hourly Reg SHO and DoD repeats.
    histogram = _log_problem_histogram(log_records, s.alert_webhook_url)
    # The span the counts are over, following the same convention the
    # forward-validation section uses. A bare "11893" cannot distinguish a
    # two-hour hard failure loop from six months of slow accrual, and that
    # distinction is most of why this section exists.
    add(f"\n--- Warning/error counts across the whole retained log ({_log_span(log_records)}) ---")
    for line in histogram:
        add(line)
    if not histogram:
        add("  none")

    add("\n--- Key settings (credentials and personal data omitted) ---")
    for name in _DIAGNOSTIC_SETTINGS:
        add(f"  {name:38} {getattr(s, name, '?')}")

    return "\n".join(out)
