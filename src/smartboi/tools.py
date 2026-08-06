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

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from smartboi.news import redact_token, redact_url
from smartboi.paper_journal import cost_buckets, trade_economics
from smartboi.usage import CATEGORIES
from smartboi.status import gather_dossiers, gather_paper_trade_stats
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
from smartboi.screen import candidates_from_file, resolve_candidates_path
from smartboi.universe import CompanySpec, spec_by_symbol
from smartboi.research import (
    MAX_ANCHORS_PER_RUN,
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
            results.append((spec.symbol, found))
            # Marked BEFORE the merge and regardless of what came back: the
            # call is what costs money, so the call is what has to be
            # recorded. Gating this on `found` meant a legitimate empty
            # result was re-billed on every subsequent run, forever.
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
    "stop_loss_pct", "take_profit_pct", "transaction_cost_bps_per_side",
    "transaction_cost_profile",
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
    "edgar_lookback_days", "news_lookback_days", "enable_universe_autoscreen",
    "universe_min_market_cap_musd", "universe_max_market_cap_musd",
    "universe_max_analyst_count", "universe_screen_interval_days",
    "enable_auto_accept_candidates", "auto_accept_anchors", "auto_accept_tradeables",
    "auto_accept_min_seen_count", "auto_accept_max_per_day",
    "enable_relationship_backfill", "backfill_anchors", "enable_ib_price_feed", "ib_host", "ib_port",
)
MAX_LOG_LINES = 40
MAX_LISTED_ROWS = 60


def _jsonl_span(rows: list[dict], key: str) -> str:
    stamps = sorted(r.get(key, "")[:10] for r in rows if r.get(key))
    return f"{len(rows)} row(s), {stamps[0]} .. {stamps[-1]}" if stamps else f"{len(rows)} row(s)"


def _recent_log_problems(log_dir: Path, webhook_url: str = "") -> list[str]:
    """The tail of WARNING/ERROR lines from smartboi.log. Run through
    redact_token because a logged exception can carry a Finnhub request URL,
    which has the API key in its query string -- this bundle is meant to be
    pasted somewhere."""
    path = Path(log_dir) / "smartboi.log"
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    problems = [ln for ln in lines if "| WARNING" in ln or "| ERROR" in ln]
    # Scrubbed a SECOND time here, on top of the scrub at each logging site.
    # Not redundancy for its own sake: this function's output is copied
    # verbatim into a bundle whose own heading promises credentials are
    # omitted, and pasted into chats and issue trackers. Every future
    # log.warning that happens to interpolate a URL would otherwise be one
    # edit away from leaking, in a file nobody thinks of as security-
    # sensitive. The boundary that makes the promise is the right place to
    # enforce it.
    return [redact_url(webhook_url, ln) for ln in problems[-MAX_LOG_LINES:]]


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

    add("=== SmartBoi diagnostics ===")
    add(f"generated_at : {datetime.now(timezone.utc).isoformat()}")
    add(f"version      : {os.environ.get('SMARTBOI_VERSION', 'dev')}")
    add(f"commit       : {os.environ.get('SMARTBOI_COMMIT', 'unknown')}")

    add("\n--- Integrations ---")
    for label, on in (
        ("EDGAR ingestion", engine.edgar_client is not None),
        ("News ingestion", engine.finnhub is not None),
        ("Dossier engine (Claude)", engine.updater is not None),
        ("IB price feed", engine.price_feed is not None),
        ("Webhook alerts", engine.alerts.enabled),
    ):
        add(f"  {label:26} {'ENABLED' if on else 'disabled'}")

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

    stats, closed = gather_paper_trade_stats(log_dir / "paper_trades.jsonl")
    add("\n--- Paper trades ---")
    add(f"  open: {len(engine.journal.open_trades)} ({', '.join(engine.journal.open_trades) or '-'})")
    # The win rate carries its 95% Wilson interval: at a dozen-odd closed
    # trades the point estimate alone reads as fact when it is noise, and the
    # interval width is exactly what says whether the record can be told apart
    # from the break-even hit rate below.
    add(f"  closed: {stats.closed} (W{stats.wins}/L{stats.losses}/T{stats.timeouts}), "
        f"win rate {stats.win_rate * 100:.0f}% "
        f"(95% CI {stats.win_rate_ci_low * 100:.0f}-{stats.win_rate_ci_high * 100:.0f}%), "
        f"avg R {stats.avg_r:.2f}")
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

    problems = _recent_log_problems(log_dir, s.alert_webhook_url)
    add(f"\n--- Recent warnings/errors (last {MAX_LOG_LINES}) ---")
    for line in problems:
        add(f"  {line}")
    if not problems:
        add("  none")

    add("\n--- Key settings (credentials and personal data omitted) ---")
    for name in _DIAGNOSTIC_SETTINGS:
        add(f"  {name:38} {getattr(s, name, '?')}")

    return "\n".join(out)
