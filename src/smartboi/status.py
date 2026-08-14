"""Status/analytics gathering for the dashboard -- pure reads of persisted
state (dossiers, graph, paper trade journal, signal log). No live IB/LLM
calls here, so the dashboard stays fast and never risks blocking on a slow
upstream API."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from smartboi.config import strategy_key
from smartboi.dossier import SCORING_VERSION, Dossier, DossierStore, normalized_fact_key
from smartboi.graph import RelationshipGraph


@dataclass
class PaperTradeStats:
    closed: int = 0
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    win_rate: float = 0.0
    # 95% Wilson interval around win_rate. A bare rate on a dozen trades
    # reads as fact when it is noise: at 13 closed the honest interval is
    # wide enough to still straddle the ~59% break-even, i.e. the record
    # cannot yet be called winning OR losing. Surfaced so the point estimate
    # is never presented alone (see _wilson_interval, and the dashboard card).
    win_rate_ci_low: float = 0.0
    win_rate_ci_high: float = 0.0
    # win_rate is the net-of-cost PROFITABLE rate: a closed trade is a win iff
    # its realized R after costs is > 0, NOT iff it hit the take-profit target.
    # That is the only direction-symmetric honest headline -- the shipped
    # 100%/50% grid puts a SHORT's target at price 0 (unreachable), so a
    # target-hit definition books every profitable short as a timeout and
    # biases the rate against an entire, deliberately-included direction.
    # `wins`/`losses` above follow the same net-of-cost sign; `timeouts` stays
    # an EXIT-REASON overlay (held to horizon), which can itself be a win or a
    # loss. Split by direction so the (now-fixed) asymmetry stays visible.
    closed_long: int = 0
    closed_short: int = 0
    win_rate_long: float = 0.0
    win_rate_short: float = 0.0
    avg_r: float = 0.0
    # Reported alongside net so the cost drag is visible rather than
    # implicit -- the gap between these two is the whole question of whether
    # a thin-edge strategy survives contact with real spreads.
    avg_r_gross: float = 0.0
    # Closed SHORTs whose borrow was never verifiable (small/unknown-cap
    # names are routinely hard-to-borrow -- see paper_journal.assumes_borrow)
    # and the avg R of everything else. borrow-assumed trades commingled
    # into one headline number would overstate what was actually executable.
    borrow_assumed: int = 0
    avg_r_clean: float = 0.0
    # Currency P&L overlay (see config's account model). `currency` is the ISO
    # code, `realized_pnl` sums the closed trades' currency_pnl, and `equity`
    # is the starting capital plus that realized P&L. Open positions' unrealized
    # P&L is marked live from the journal, not here, so equity stays "realized".
    currency: str = ""
    initial_capital: float = 0.0
    realized_pnl: float = 0.0
    equity: float = 0.0
    # Leverage disclosure. Each trade is sized initial_capital /
    # max_concurrent_positions, but the entry path enforces no total-count cap
    # (a hard cap belongs with real order placement, not signal validation --
    # see config), so more than `max_concurrent_positions` can be open at once.
    # When they are, the deployed book exceeds initial_capital and the currency
    # equity above reflects a return on leverage the account model never
    # claimed. peak_concurrent is the max simultaneously-open in the CLOSED
    # record; peak_concurrent > max_concurrent_positions means the equity line
    # is levered. Surfaced so the currency figure is never read as an
    # achievable single-account return without that caveat.
    max_concurrent_positions: int = 0
    peak_concurrent: int = 0


@dataclass
class StrategyGeneration:
    """One strategy "generation" in the closed record -- a set of trades that
    share the same trade-governing config (see config.strategy_key). Kept
    apart so a new strategy's win rate is never pooled with an old, abandoned
    regime's, which would measure two strategies as one number. `legacy` marks
    trades opened before generation stamping existed (config unknown), grouped
    together rather than each masquerading as its own strategy; `is_current`
    marks the generation matching the live config -- the number that actually
    describes what the bot is doing now."""

    key: str
    label: str
    version_from: str
    version_to: str
    is_current: bool
    legacy: bool
    closed: int
    wins: int
    losses: int
    timeouts: int
    win_rate: float
    win_rate_ci_low: float
    win_rate_ci_high: float
    avg_r: float
    avg_r_gross: float
    realized_pnl: float


def _dossier_row(d: Dossier) -> dict:
    """The summary fields every dossier readout shares -- the ladder, the
    all-dossiers table and the per-symbol detail below all start here, so a
    field added for one is present in the others rather than in whichever
    view happened to need it first."""
    return {
        "symbol": d.symbol,
        "direction": d.direction,
        "confidence": round(d.confidence, 3),
        "magnitude": round(d.magnitude, 3),
        "horizon_days": d.horizon_days,
        "independent_source_count": d.independent_source_count,
        "status": d.status,
        "thesis_summary": d.thesis_summary,
        "evidence_count": len(d.evidence),
        "updated_at": d.updated_at,
        "signaled_at": d.signaled_at,
        "signaled_price": d.signaled_price,
        "mass_agree": round(d.mass_agree, 3),
        "mass_opposing": round(d.mass_opposing, 3),
        # How many of this dossier's items carry a fact label, and how
        # many distinct labels that is. The whole per-fact
        # independence mechanism rests on the model reusing a label
        # rather than paraphrasing it, and a model that quietly stops
        # doing so degrades scoring back to per-channel counting with
        # no error anywhere -- so it has to be visible.
        "labelled_evidence_count": sum(1 for e in d.evidence if e.fact_key),
        "distinct_fact_keys": len({
            normalized_fact_key(e.fact_key) for e in d.evidence if e.fact_key
        }),
        # --- What the whole-body pass did to this row. Carried here
        # because the dossier table showed a vetoed 0.000 and a
        # decayed-to-zero 0.000 as the same thing, which made the one
        # pass capable of stopping every trade in the system
        # invisible in the only artifact an operator reads. Live, 22
        # of 45 dossiers sat at exactly 0.000 with no way to tell
        # from here which pass had put them there.
        "synthesis_at": d.synthesis_at,
        "pre_synthesis_score": round(d.pre_synthesis_score, 3),
        "synthesis_confidence": round(d.synthesis_confidence, 3),
        "synthesis_magnitude": round(d.synthesis_magnitude, 3),
        "distinct_fact_count": d.distinct_fact_count,
        "already_priced_in": d.already_priced_in,
        "redundant_evidence": d.redundant_evidence,
    }


def gather_dossiers(store: DossierStore) -> list[dict]:
    rows = [_dossier_row(store.load(symbol)) for symbol in store.all_symbols()]
    rows.sort(key=lambda r: (r["confidence"] * r["magnitude"]), reverse=True)
    return rows


# The evidence a single dossier can accumulate is unbounded, and this payload
# is fetched on a click rather than on the 10s refresh -- but a name that has
# been in the universe for a year should still not ship a megabyte to a phone.
# Newest first, so what is cut is the tail nobody scrolls to.
_DETAIL_EVIDENCE_LIMIT = 60


def gather_dossier_detail(
    store: DossierStore, symbol: str, evidence_limit: int = _DETAIL_EVIDENCE_LIMIT
) -> dict | None:
    """One dossier, with the evidence rows behind its score.

    The refresh payload deliberately carries only `evidence_count` -- the
    records themselves are the largest thing in the system and would be
    re-sent every 10 seconds for every dossier to render a panel nobody has
    opened. This is the on-demand half: same summary fields, plus the
    individual items, so "why is this at 0.34" has an answer in the UI
    instead of only in the JSON on disk.

    Returns None for a symbol with no dossier on disk. Callers are expected
    to have validated the symbol already -- `all_symbols` membership is
    checked here too, because DossierStore builds a path from the string.
    """
    if symbol not in set(store.all_symbols()):
        return None
    d = store.load(symbol)
    evidence = sorted(
        d.evidence, key=lambda e: (e.published_at or "", e.evidence_id), reverse=True
    )
    row = _dossier_row(d)
    row["evidence"] = [
        {
            "evidence_id": e.evidence_id,
            "source_type": e.source_type,
            "source_name": e.source_name,
            "url": e.url,
            "headline": e.headline,
            "published_at": e.published_at,
            "origin_symbol": e.origin_symbol,
            "is_propagated": e.is_propagated,
            "relationship_note": e.relationship_note,
            "relationship_confidence": e.relationship_confidence,
            "direction": e.direction,
            "magnitude": round(e.magnitude, 3),
            "confidence": round(e.confidence, 3),
            "horizon_days": e.horizon_days,
            "reasoning": e.reasoning,
            "skeptic_note": e.skeptic_note,
            "fact_key": e.fact_key,
        }
        for e in evidence[:evidence_limit]
    ]
    # Stated rather than inferred from len(evidence): a panel showing 60 rows
    # under a header saying 214 is honest; one silently showing 60 is not.
    row["evidence_shown"] = len(row["evidence"])
    return row


def gather_coverage(universe, graph: RelationshipGraph, store: DossierStore) -> dict:
    """How much of the tradeable universe is actually being covered -- the
    "are we there yet" readout.

    Three numbers, each answering a different question:

    - dossiers vs tradeables: how many trade targets have any accumulated
      thesis at all. This is the headline: a dossier count far below the
      tradeable count means most of the universe is dark, not that the
      market is quiet.
    - CONNECTED tradeables: how many have at least one graph edge, i.e. can
      ever receive propagated evidence from an anchor. An unconnected
      tradeable can only ever build a dossier from its own direct news --
      and these are selected for thin coverage, so in practice it will not.
    - LIVE anchors: how many anchors have an edge to a tradeable. An anchor
      is never its own analysis target, so one without such an edge is
      inert by construction: its news resolves to zero targets and is
      discarded unread, no matter how much of it there is.

    Measured live 2026-07-29, before the edge-promotion fix: 16 dossiers
    against 48 tradeables, 20 of 48 tradeables connected, and 26 of 130
    anchors live. Those three numbers together explain the whole gap
    between ingestion volume and signal output, which is why they belong on
    the dashboard rather than in a log line."""
    tradeables = [c.symbol for c in universe if not c.signal_source_only]
    anchors = [c.symbol for c in universe if c.signal_source_only]
    tradeable_set = set(tradeables)

    linked: dict[str, set[str]] = {}
    for r in graph.relationships:
        linked.setdefault(r.from_symbol, set()).add(r.to_symbol)
        linked.setdefault(r.to_symbol, set()).add(r.from_symbol)

    with_dossier = {s for s in store.all_symbols() if s in tradeable_set}
    connected = [s for s in tradeables if linked.get(s)]
    # An anchor-to-anchor edge yields no analysis target either, so "live"
    # means specifically "linked to something tradeable".
    live_anchors = [a for a in anchors if linked.get(a, set()) & tradeable_set]

    return {
        "tradeables": len(tradeables),
        "anchors": len(anchors),
        "tradeables_with_dossier": len(with_dossier),
        "tradeables_connected": len(connected),
        "tradeables_unconnected": sorted(tradeable_set - set(connected)),
        "anchors_live": len(live_anchors),
        "anchors_inert": sorted(set(anchors) - set(live_anchors)),
    }


def gather_graph_stats(graph: RelationshipGraph, universe=None, store=None) -> dict:
    """Relationships grouped by the filer (`from_symbol` -- "the company the
    evidence is about", see graph.py's Relationship) instead of one flat
    row per edge -- a company with several disclosed counterparties reads
    as one group ("FORM: customer of X, supplier to Y, ...") rather than
    being scattered across a table sorted by insertion order. Each group's
    own relationships are strongest-confidence first.

    Also returns flat `nodes`/`edges` for the interactive graph panel. A
    node's `kind` comes from the universe (anchors are signal_source_only),
    and its `dir`/`score` from any dossier it has -- so the viz can colour a
    tradeable by its live thesis and size it by conviction, exactly like the
    approved mockup. Both optional: called without them (as several tests do)
    it still returns the tables, just with empty node/edge lists."""
    by_symbol: dict[str, list[dict]] = {}
    for r in graph.relationships:
        by_symbol.setdefault(r.from_symbol, []).append(
            {
                "counterparty": r.to_symbol,
                "type": r.rel_type,
                "confidence": r.confidence,
                "description": r.description,
            }
        )
    groups = [
        {"symbol": symbol, "relationships": sorted(rels, key=lambda x: x["confidence"], reverse=True)}
        for symbol, rels in sorted(by_symbol.items())
    ]

    kind_by: dict[str, str] = {}
    info_by: dict[str, tuple[str, str]] = {}
    if universe is not None:
        for c in universe:
            kind_by[c.symbol] = "anchor" if c.signal_source_only else "tradeable"
            info_by[c.symbol] = (c.name, c.ecosystem)
    dossier_syms = set(store.all_symbols()) if store is not None else set()

    symbols: set[str] = set()
    edges: list[list] = []
    for r in graph.relationships:
        symbols.add(r.from_symbol)
        symbols.add(r.to_symbol)
        edges.append([r.from_symbol, r.to_symbol, r.rel_type, round(r.confidence, 3)])

    nodes = []
    for s in sorted(symbols):
        direction = score = None
        if s in dossier_syms:
            d = store.load(s)
            direction = d.direction
            score = round(d.confidence * d.magnitude, 3)
        name, sector = info_by.get(s, ("", ""))
        nodes.append({"id": s, "kind": kind_by.get(s, "external"), "dir": direction, "score": score,
                      "name": name, "sector": sector})

    return {
        "edge_count": len(graph.relationships),
        "by_symbol": groups,
        "nodes": nodes,
        "edges": edges,
    }


# An edge not re-confirmed by any filing in this many days is "stale": the
# rolling re-extraction re-reads the whole universe every ~40 days, so ~120
# days is roughly three missed re-reads -- long enough that silence is a real
# signal (the relationship may have ended) rather than refresh lag.
_STALE_EDGE_DAYS = 120


def _days_since(stamp: str) -> float | None:
    """Whole-ish days since an ISO-8601 UTC stamp; None if absent/unparseable."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _disconnected_reasons(symbols: set[str], candidates: dict | None) -> dict[str, dict]:
    """For each edge-less tradeable, what extraction actually found for it.

    Three states, and only one of them is a hole the rolling refresh can
    close:
      - `found` 0        : its filings have not produced a counterparty yet.
                           A refresh may genuinely help.
      - `found` > 0, `resolvable` 0 : extraction worked and every counterparty
                           was unlisted, foreign or the company's own
                           subsidiary. A refresh finds the same names again.
      - `resolvable` > 0 : the counterparties exist and have tickers; they are
                           waiting to be ACCEPTED into the universe, which is
                           a click, not a re-read.
    """
    out: dict[str, dict] = {}
    for symbol in sorted(symbols):
        rows = [
            (name, row) for name, row in (candidates or {}).items()
            if symbol in (row.get("related_to") or [])
        ]
        resolvable = [n for n, r in rows if (r.get("ticker") or "").strip()]
        out[symbol] = {
            "found": len(rows),
            "resolvable": len(resolvable),
            # Named, because "0 resolvable" is a fact an operator can only act
            # on by seeing WHICH companies -- a foreign listing is a different
            # decision from a law firm.
            "examples": [n[:48] for n, _ in rows[:4]],
            "resolvable_examples": resolvable[:4],
        }
    return out


def gather_graph_health(
    graph: RelationshipGraph,
    universe,
    store: DossierStore,
    backfill_state: dict | None = None,
    last_refresh: str = "",
    last_research: str = "",
    researched_anchor_count: int = 0,
    refresh_per_day: int = 0,
    audit: dict | None = None,
    candidates: dict | None = None,
) -> dict:
    """Health and maintenance state of the relationship graph -- the numbers
    that say whether the mechanism the whole strategy runs on is actually
    being kept alive.

    The graph IS the strategy: an edge is the only path by which an anchor's
    news reaches a tradeable, so a missing edge is a trade that never happens.
    Two numbers here matter more than the totals:

    - `disconnected_with_thesis`: tradeables carrying a real thesis while
      having NO graph edge at all. Their dossier came entirely from their own
      filings, so they never used the cross-company mechanism -- they are
      effectively single-stock news signals wearing this system's clothes.
      A non-zero count is the honest measure of how much of the edge map is
      still missing.
    - `stalest_days` / `never_extracted`: how far behind the rolling
      re-extraction is. Extraction only writes an edge when the counterparty
      is ALREADY in the universe, so a symbol last read when the universe was
      smaller is carrying holes that a re-read would fill.

    Every argument after `store` is optional so the tables still render on a
    caller that has no engine state to hand (several tests do exactly that).
    """
    tradeables = [c.symbol for c in universe if not c.signal_source_only]
    anchors = [c.symbol for c in universe if c.signal_source_only]
    tradeable_set = set(tradeables)

    linked: dict[str, set[str]] = {}
    by_type: dict[str, int] = {}
    edge_ages: list[float] = []
    stale_edges = 0
    for r in graph.relationships:
        linked.setdefault(r.from_symbol, set()).add(r.to_symbol)
        linked.setdefault(r.to_symbol, set()).add(r.from_symbol)
        by_type[r.rel_type] = by_type.get(r.rel_type, 0) + 1
        age = _days_since(getattr(r, "extracted_at", ""))
        if age is not None:
            edge_ages.append(age)
            # Not re-confirmed by any filing in _STALE_EDGE_DAYS -- graph.add
            # now refreshes extracted_at on every re-confirmation, so a stale
            # edge genuinely means "no filing has mentioned this relationship
            # in months" (a lost customer is itself a tradeable event), not
            # merely "first extracted long ago".
            if age > _STALE_EDGE_DAYS:
                stale_edges += 1

    connected = [s for s in tradeables if linked.get(s)]
    disconnected = sorted(tradeable_set - set(connected))
    # An anchor is never its own analysis target, so "live" means specifically
    # linked to something TRADEABLE -- an anchor-to-anchor edge reaches nothing.
    live_anchors = [a for a in anchors if linked.get(a, set()) & tradeable_set]

    # The headline: a thesis with no supply-chain path behind it.
    disconnected_set = set(disconnected)
    with_thesis = []
    for symbol in store.all_symbols():
        if symbol not in disconnected_set:
            continue
        d = store.load(symbol)
        if d.direction in ("LONG", "SHORT") and (d.confidence * d.magnitude) > 0:
            with_thesis.append(symbol)

    # Symbol-level extraction age: when each symbol's filing was last read.
    # Three states, not two. A marker is not proof a filing was READ: the
    # backfill stamps one when the filer genuinely has no 10-K, and the badge
    # rendered that as "all read" -- 24 live symbols on this deployment,
    # including two tradeables, whose filings had never been fetched. They are
    # counted separately and excluded from the ages, because an age measured
    # off "the day we established there is nothing to read" is not an
    # extraction age. A marker recording a failed LOOKUP is not settled at
    # all (see engine._backfill_due) and counts as never-extracted.
    state = backfill_state or {}
    ages: list[float] = []
    never = 0
    no_filing = 0
    for symbol in tradeables + anchors:
        marker = state.get(symbol) if isinstance(state.get(symbol), dict) else None
        if marker is not None and marker.get("error"):
            never += 1
            continue
        # `accession` present and explicitly None is the backfill saying it
        # looked and there is no filing. A marker with no `accession` KEY is
        # simply an older format and was read -- conflating the two would
        # reclassify every pre-existing marker as unread.
        if marker is not None and marker.get("backfilled_at") and (
                marker.get("reason") == "no_10k"
                or ("accession" in marker and marker["accession"] is None)):
            no_filing += 1
            continue
        age = _days_since(marker.get("backfilled_at", "") if marker else "")
        if age is None:
            never += 1
        else:
            ages.append(age)
    ages.sort()

    universe_size = len(tradeables) + len(anchors)
    return {
        "edges": len(graph.relationships),
        "edges_by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "edge_age_median_days": round(edge_ages[len(edge_ages) // 2], 1) if edge_ages else None,
        "stale_edges": stale_edges,
        "tradeables": len(tradeables),
        "tradeables_connected": len(connected),
        "tradeables_disconnected": len(disconnected),
        "disconnected": disconnected[:40],
        "disconnected_with_thesis": len(with_thesis),
        "disconnected_with_thesis_symbols": sorted(with_thesis)[:40],
        # WHY each of those has no edge, which decides whether anything can
        # be done about it. Both the dashboard and the bundle used to assert
        # that the rolling refresh "closes exactly these holes", and for the
        # five names carrying this flag live that was simply false: extraction
        # had already run on all five and found 42 counterparties between
        # them, of which ZERO resolved to a ticker -- own subsidiaries
        # (Hurco Automation Ltd), auditors and law firms (now filtered at
        # extraction), and foreign or private customers (IVECO, Higer Bus,
        # J.A.P. Industria) that EDGAR will never know. Re-reading those
        # filings finds the same names again, forever. A warning that
        # promises a fix that cannot arrive is worse than no warning.
        "disconnected_reasons": _disconnected_reasons(with_thesis, candidates),
        "anchors": len(anchors),
        "anchors_live": len(live_anchors),
        "anchors_inert": len(anchors) - len(live_anchors),
        "stalest_days": round(ages[-1], 1) if ages else None,
        "median_extraction_age_days": round(ages[len(ages) // 2], 1) if ages else None,
        "never_extracted": never,
        # Has a marker, but the marker says there was no filing to read.
        "no_filing_available": no_filing,
        "last_refresh": last_refresh,
        "last_refresh_days": _days_since(last_refresh),
        "last_research": last_research,
        "last_research_days": _days_since(last_research),
        "researched_anchors": researched_anchor_count,
        "refresh_per_day": refresh_per_day,
        # How long a full pass over the universe takes at the current rate --
        # the number that says whether "monthly" is really monthly.
        "cycle_days": round(universe_size / refresh_per_day, 1) if refresh_per_day else None,
        # The correctness half. Everything above measures whether the graph is
        # BIG enough; this is the daily read-only audit's verdict on whether
        # what is already there is RIGHT -- delisted symbols, securities that
        # are not common equity, misresolved names, financing relationships
        # wearing a supply-chain label. See graph_audit.py. None until the
        # first daily pass has run.
        "audit": audit or None,
        "audit_actionable": (audit or {}).get("actionable", 0),
        "audit_at": (audit or {}).get("at", ""),
        "audit_age_days": _days_since((audit or {}).get("at", "")),
    }


def _read_jsonl(path: Path) -> list[dict]:
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


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion (default z=1.96).

    Chosen over the textbook Wald (p +/- z*sqrt(p(1-p)/n)) interval because
    Wald degenerates exactly where this record lives: it collapses to zero
    width at 0 or 100% wins and can run past [0, 1] at small n. Wilson stays
    inside [0, 1] and stays honest at the dozen-trade counts the paper
    journal actually has. n <= 0 -> (0, 0)."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _peak_concurrent(rows: list[dict]) -> int:
    """Max number of paper trades open simultaneously in the closed record --
    a sweep over [opened_at, closed_at] intervals. Ties (an open and a close at
    the same instant) count as overlapping, so this is the conservative (upper)
    concurrency, which is the right side to err on for a LEVERAGE disclosure.
    Only closed trades are visible here; currently-open positions would raise it
    further, so this is a lower bound on the true live peak."""
    events: list[tuple[str, int]] = []
    for r in rows:
        opened, closed = r.get("opened_at"), r.get("closed_at")
        if not opened or not closed:
            continue
        events.append((opened, 1))
        events.append((closed, -1))
    # +1 (open) before -1 (close) at an equal timestamp -> counts as overlap.
    events.sort(key=lambda e: (e[0], -e[1]))
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def gather_paper_trade_stats(
    log_path: Path, initial_capital: float = 0.0, currency: str = "",
    max_concurrent_positions: int = 0,
) -> tuple[PaperTradeStats, list[dict]]:
    rows = _read_jsonl(log_path)
    stats = PaperTradeStats(closed=len(rows), currency=currency, initial_capital=initial_capital,
                            max_concurrent_positions=max_concurrent_positions,
                            peak_concurrent=_peak_concurrent(rows))
    if rows:
        # A win is net-of-cost profit (realized R > 0), not a target hit -- so
        # a profitable SHORT that can only ever TIMEOUT (its 100%-take-profit
        # target sits at price 0) is correctly a win. losses is the complement;
        # timeouts stays the exit-reason count (held to horizon), which now
        # overlaps both (a timeout can be a win or a loss).
        stats.wins = sum(1 for r in rows if (r.get("r_multiple") or 0.0) > 0)
        stats.losses = stats.closed - stats.wins
        stats.timeouts = sum(1 for r in rows if r.get("status") == "TIMEOUT")
        gross = [r.get("r_multiple_gross") for r in rows if r.get("r_multiple_gross") is not None]
        stats.avg_r_gross = round(sum(gross) / len(gross), 3) if gross else 0.0
        stats.win_rate = stats.wins / stats.closed
        # The interval is over the same successes/trials as win_rate (profitable
        # trades out of all closed).
        low, high = _wilson_interval(stats.wins, stats.closed)
        stats.win_rate_ci_low = round(low, 4)
        stats.win_rate_ci_high = round(high, 4)
        longs = [r for r in rows if r.get("direction") == "LONG"]
        shorts = [r for r in rows if r.get("direction") == "SHORT"]
        stats.closed_long, stats.closed_short = len(longs), len(shorts)
        stats.win_rate_long = round(
            sum(1 for r in longs if (r.get("r_multiple") or 0.0) > 0) / len(longs), 4) if longs else 0.0
        stats.win_rate_short = round(
            sum(1 for r in shorts if (r.get("r_multiple") or 0.0) > 0) / len(shorts), 4) if shorts else 0.0
        stats.avg_r = sum(r.get("r_multiple") or 0.0 for r in rows) / stats.closed
        stats.borrow_assumed = sum(1 for r in rows if r.get("assumes_borrow"))
        clean = [r.get("r_multiple") or 0.0 for r in rows if not r.get("assumes_borrow")]
        stats.avg_r_clean = round(sum(clean) / len(clean), 3) if clean else 0.0
        pnls = [r.get("currency_pnl") for r in rows if r.get("currency_pnl") is not None]
        stats.realized_pnl = round(sum(pnls), 2) if pnls else 0.0
    stats.equity = round(initial_capital + stats.realized_pnl, 2)
    return stats, rows[-20:]


def _generation_stats(
    key: str, rows: list[dict], current_key: str | None, current_signature: dict | None
) -> StrategyGeneration:
    legacy = key == ""
    is_current = bool(current_key) and key == current_key and not legacy
    closed = len(rows)
    # Net-of-cost basis, matching gather_paper_trade_stats: a win is realized
    # R > 0, losses the complement, timeouts the exit-reason overlay.
    wins = sum(1 for r in rows if (r.get("r_multiple") or 0.0) > 0)
    losses = closed - wins
    timeouts = sum(1 for r in rows if r.get("status") == "TIMEOUT")
    win_rate = wins / closed if closed else 0.0
    low, high = _wilson_interval(wins, closed)
    avg_r = (sum(r.get("r_multiple") or 0.0 for r in rows) / closed) if closed else 0.0
    gross_vals = [r.get("r_multiple_gross") for r in rows if r.get("r_multiple_gross") is not None]
    avg_r_gross = round(sum(gross_vals) / len(gross_vals), 3) if gross_vals else 0.0
    pnls = [r.get("currency_pnl") for r in rows if r.get("currency_pnl") is not None]
    realized = round(sum(pnls), 2) if pnls else 0.0

    # Label + version range: for the LIVE generation prefer the current
    # signature, so it reads correctly ("hold-to-horizon, since v0.43") even
    # with zero closed trades yet; otherwise read them off the stamped rows.
    stamped = [r.get("strategy") for r in rows if r.get("strategy")]
    versions = sorted(s.get("version", "") for s in stamped if s.get("version"))
    if legacy:
        label = "legacy (pre-tracking)"
    elif is_current and current_signature:
        label = current_signature.get("label") or "current strategy"
    elif stamped:
        label = stamped[-1].get("label") or "strategy"
    else:
        label = "strategy"
    version_from = versions[0] if versions else (
        (current_signature or {}).get("version", "") if is_current else ""
    )
    version_to = versions[-1] if versions else version_from

    return StrategyGeneration(
        key=key, label=label, version_from=version_from, version_to=version_to,
        is_current=is_current, legacy=legacy, closed=closed, wins=wins, losses=losses,
        timeouts=timeouts, win_rate=round(win_rate, 4),
        win_rate_ci_low=round(low, 4), win_rate_ci_high=round(high, 4),
        avg_r=round(avg_r, 3), avg_r_gross=avg_r_gross, realized_pnl=realized,
    )


def gather_strategy_generations(
    log_path: Path, current_signature: dict | None = None
) -> list[StrategyGeneration]:
    """Segments the closed paper-trade record by strategy generation, so the
    dashboard can show the CURRENT strategy's forward performance apart from
    the trades taken under an old config (institutional costs, tight stops,
    etc.). The current generation is always included even with zero closed
    trades yet -- that "0W-0L, still measuring" state is the honest headline
    right after a strategy change, and the whole point of the split. Ordered
    current first, then other generations by trade count, legacy last."""
    rows = _read_jsonl(log_path)
    current_key = strategy_key(current_signature) if current_signature else None

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(strategy_key(r.get("strategy")), []).append(r)
    # The live strategy always gets a row, even before it has closed a trade.
    if current_key:
        groups.setdefault(current_key, [])

    gens = [
        _generation_stats(key, grp, current_key, current_signature)
        for key, grp in groups.items()
    ]
    gens.sort(key=lambda g: (not g.is_current, g.legacy, -g.closed))
    return gens


def gather_recent_signals(log_path: Path, limit: int = 25) -> list[dict]:
    return _read_jsonl(log_path)[-limit:]


def _is_addable(c: dict) -> bool:
    return bool(c.get("ticker")) and not c.get("accepted_as")


def gather_universe_candidates(candidates: dict, accepted: dict) -> list[dict]:
    """Companies outside the universe that filings disclosed relationships
    to (see engine._record_universe_candidate), for human review.
    Annotated with `accepted_as` when the dashboard's Accept button (or
    SYMBOLS/ANCHOR_SYMBOLS) has already added this ticker, so the UI can
    show its state instead of an Accept button. Sorted addable candidates
    first (a resolved ticker, not yet accepted -- there's actually
    something to click), then everything else -- both groups
    most-corroborated first -- so the entries that need a decision aren't
    buried under a long tail of unresolved/already-added ones."""
    rows = []
    for key, c in candidates.items():
        row = dict(c)
        entry = accepted.get(row.get("ticker") or key)
        # Entries were originally a bare "tradeable"/"anchor" string and are
        # now a {"as", "source"} dict recording whether a human or the engine
        # accepted it (see engine.accept_candidate). Both shapes are
        # flattened here so the UI renders a string either way rather than
        # "[object Object]" for anything written since the upgrade.
        if isinstance(entry, dict):
            row["accepted_as"] = entry.get("as")
            row["accepted_source"] = entry.get("source", "manual")
        else:
            row["accepted_as"] = entry
            row["accepted_source"] = "manual" if entry else None
        rows.append(row)
    rows.sort(key=lambda c: (0 if _is_addable(c) else 1, -c.get("seen_count", 0)))
    return rows


def snapshot_dossier(d: Dossier, snapshotted_at: str, min_sources_required: int = 0) -> dict:
    """One row for the daily dossier-snapshot log (logs/dossier_snapshots.jsonl,
    see engine.py's _run_daily_snapshot) -- the raw material for eventually
    validating whether confidence*magnitude predicts forward returns,
    joined by symbol/date against logs/price_marks.jsonl. Captures every
    dossier once a day regardless of whether anything actually changed
    that day, so the resulting time series has no gaps to explain away --
    forward data can't be backfilled once a day is missed, so this is
    deliberately unconditional rather than only logging on a change.

    `min_sources_required` is passed in rather than re-derived here: the bar
    depends on runtime settings this module has no access to, and computing it
    from a second copy of the rule is how a row comes to report a bar the gate
    never applied (see signals.required_sources). 0 means "not supplied"."""
    return {
        "snapshotted_at": snapshotted_at,
        # Which scoring logic produced these numbers (see
        # dossier.SCORING_VERSION). Without it a change to how magnitude or
        # confidence is aggregated silently mixes incomparable rows into one
        # forward-return series; with it the analysis can split at the
        # boundary. Forward data can't be backfilled and old rows must never
        # be re-scored with new logic, so the split is the only honest option.
        "scoring_version": SCORING_VERSION,
        "symbol": d.symbol,
        "direction": d.direction,
        "confidence": round(d.confidence, 4),
        "magnitude": round(d.magnitude, 4),
        "score": round(d.confidence * d.magnitude, 4),
        "independent_source_count": d.independent_source_count,
        "status": d.status,
        # The thesis-inception price baseline (see engine._capture_inception):
        # snapped when the thesis first turned directional, so joined against
        # price_marks it shows how much of the favourable move happened BEFORE
        # the signal fired -- the pre-signal drift the entry guard now acts on.
        "inception_price": d.inception_price,
        "inception_at": d.inception_at,
        # The synthesis verdict behind the score above.
        #
        # `score` is the CAPPED number once synthesis has run (see engine's
        # _apply_synthesis: a veto zeroes it, a trim lowers it), and until
        # these columns existed the record could not tell a decayed score
        # from a vetoed one -- a 0.000 row and a genuinely dead thesis were
        # indistinguishable. That made the one pass built to answer
        # overlap/coherence/priced-in the only pass whose effect on the
        # forward record could not be measured at all.
        #
        # synthesis_at is a timestamp rather than a flag so staleness stays
        # visible: a verdict from days ago capping today's score is a fact
        # about the record, not a detail to hide.
        "synthesis_at": d.synthesis_at,
        "synthesis_confidence": round(d.synthesis_confidence, 4),
        "synthesis_magnitude": round(d.synthesis_magnitude, 4),
        "distinct_fact_count": d.distinct_fact_count,
        "already_priced_in": d.already_priced_in,
        # Recorded separately from already_priced_in because SCORING_VERSION 7
        # exists to let forward rows be bucketed by WHICH mechanism touched
        # them, and splitting the veto from the trim is one of its two
        # changes. Omitting it would repeat exactly the mistake v6 was bumped
        # to stop: shipping a scoring change whose effect cannot afterwards be
        # attributed to it.
        "redundant_evidence": d.redundant_evidence,
        # The arithmetic score BEFORE synthesis capped or vetoed it. Without
        # this a vetoed row records 0.000 for both numbers, so "0.9 confidence
        # but priced in" and "0.05 and priced in" are the same row forever --
        # and the veto's actual selectivity cannot be measured at all.
        "pre_synthesis_score": round(d.pre_synthesis_score, 4),
        "synthesis_price": d.synthesis_price,
        # Which bar this dossier was actually held to, and the two flags that
        # decide it (see signals.evaluate). A row that failed on sources reads
        # as an ordinary low score without them.
        "has_filing_evidence": d.has_filing_evidence,
        "has_disclosed_link_evidence": d.has_disclosed_link_evidence,
        "min_sources_required": min_sources_required,
        # --- Per-mechanism attribution for SCORING_VERSION 6. Three changes
        # in this version move scores in the same region and direction;
        # version 5 bundled three behind one boundary and can no longer
        # attribute an outcome to any of them. These make the bucketing a
        # filter over the row rather than an inference about the release. ---
        "veto_falsified_by_price": d.veto_falsified_by_price,
        "synthesis_stale_evidence": d.synthesis_stale_evidence,
        "ecosystem_slot_counted": d.ecosystem_slot_counted,
    }


def gather_usage(snapshot) -> dict:
    # Per-category spend breakdown, so the dashboard can show WHERE the day's
    # budget went (extraction / dossier / synthesis / research), not just the
    # total -- the split is the thing that reveals a category starving another
    # (the reason budget_reserve_synthesis exists; see config.py / usage.py).
    # Every category is present, zeros included, so the bar segments stay
    # stable across refreshes instead of appearing and disappearing.
    from smartboi.usage import CATEGORIES

    by_category = {}
    for cat in CATEGORIES:
        usd, calls = snapshot.by_category.get(cat, (0.0, 0))
        by_category[cat] = {"usd": round(usd, 4), "calls": calls}
    return {
        "date": snapshot.date,
        "calls": snapshot.calls,
        "input_tokens": snapshot.input_tokens,
        "output_tokens": snapshot.output_tokens,
        "daily_call_budget": snapshot.daily_call_budget,
        # Estimated spend, not just call volume: per-call cost now spans
        # more than an order of magnitude across the configurable models,
        # so a call count on its own no longer tells an operator what the
        # day is costing (see usage.py).
        "usd_spent": round(snapshot.usd_spent, 2),
        "daily_usd_budget": snapshot.daily_usd_budget,
        "by_category": by_category,
    }
