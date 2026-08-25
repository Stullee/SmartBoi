import json
from datetime import datetime, timedelta, timezone

from smartboi.dossier import Dossier
from smartboi.graph import Relationship, RelationshipGraph
from smartboi.status import (
    _peak_concurrent,
    gather_dossier_detail,
    gather_graph_stats,
    gather_paper_trade_stats,
    gather_strategy_generations,
    gather_universe_candidates,
    snapshot_dossier,
)


def _write_trades(path, statuses):
    """A paper_trades.jsonl of closed trades with the given WIN/LOSS/TIMEOUT
    statuses (r_multiple filled in only so avg_r has something to read)."""
    lines = [json.dumps({"status": s, "r_multiple": 1.0 if s == "WIN" else -1.0})
             for s in statuses]
    path.write_text("\n".join(lines) + "\n")


# --- Win rate carries a confidence interval. A bare rate on a dozen trades
# reads as fact when it is noise; the interval width is the honest signal. ---

def test_win_rate_reports_a_wilson_confidence_interval(tmp_path):
    """5 wins in 13 closed is a 38% point estimate whose 95% interval is wide
    enough to still contain the ~59% break-even -- i.e. the record cannot yet
    be called losing. The interval brackets the point estimate and stays in
    [0, 1]."""
    path = tmp_path / "paper_trades.jsonl"
    _write_trades(path, ["WIN"] * 5 + ["LOSS"] * 8)

    stats, _ = gather_paper_trade_stats(path)

    assert stats.closed == 13 and stats.wins == 5
    assert round(stats.win_rate, 2) == 0.38
    assert 0.0 <= stats.win_rate_ci_low < stats.win_rate < stats.win_rate_ci_high <= 1.0
    # Wide at n=13: the upper bound clears break-even, which is the point of
    # showing the interval rather than the bare 38%.
    assert stats.win_rate_ci_high > 0.59


def test_win_rate_interval_stays_inside_zero_one_at_the_extremes(tmp_path):
    """The reason for Wilson over Wald: an all-wins (or all-losses) record
    would give Wald a zero-width interval or one that runs past [0, 1].
    Wilson stays strictly inside and non-degenerate."""
    path = tmp_path / "paper_trades.jsonl"
    _write_trades(path, ["WIN"] * 4)

    stats, _ = gather_paper_trade_stats(path)

    assert stats.win_rate == 1.0
    assert stats.win_rate_ci_low > 0.0        # not the degenerate (1, 1)
    assert stats.win_rate_ci_high <= 1.0


def test_win_rate_interval_is_zero_with_no_closed_trades(tmp_path):
    """Nothing closed yet is the normal fresh-deploy state -- no division, no
    NaN, just a (0, 0) interval the dashboard renders as absent."""
    stats, _ = gather_paper_trade_stats(tmp_path / "does_not_exist.jsonl")

    assert stats.closed == 0
    assert stats.win_rate_ci_low == 0.0 and stats.win_rate_ci_high == 0.0


def test_win_rate_counts_any_net_of_cost_profit_as_a_win(tmp_path):
    """A win is net-of-cost R>0, not a target hit -- so a profitable SHORT that
    can only ever TIMEOUT (its 100%-take-profit target sits at price 0) still
    counts, and the record is no longer biased against an entire direction."""
    path = tmp_path / "paper_trades.jsonl"
    _write_rows(path, [
        {"status": "TIMEOUT", "r_multiple": 0.6, "direction": "SHORT"},   # profitable short, timed out
        {"status": "TIMEOUT", "r_multiple": -0.3, "direction": "SHORT"},  # losing short
        {"status": "WIN", "r_multiple": 1.9, "direction": "LONG"},        # target hit
        {"status": "LOSS", "r_multiple": -1.0, "direction": "LONG"},      # stop hit
    ])

    stats, _ = gather_paper_trade_stats(path)

    assert (stats.wins, stats.losses) == (2, 2)   # by net-of-cost R sign, not exit reason
    assert stats.timeouts == 2                     # exit-reason overlay, unchanged
    assert stats.win_rate == 0.5
    # The profitable short is a win; the per-direction split makes the fix visible.
    assert (stats.closed_short, stats.win_rate_short) == (2, 0.5)
    assert (stats.closed_long, stats.win_rate_long) == (2, 0.5)


# --- Currency overlay: equity = starting capital + realized currency P&L. ---

def test_currency_equity_is_capital_plus_realized(tmp_path):
    path = tmp_path / "paper_trades.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"status": "WIN", "r_multiple": 1.0, "currency_pnl": 150.0},
        {"status": "LOSS", "r_multiple": -1.0, "currency_pnl": -80.0},
    ]) + "\n")

    stats, _ = gather_paper_trade_stats(path, initial_capital=5000.0, currency="EUR")

    assert stats.currency == "EUR"
    assert stats.realized_pnl == 70.0
    assert stats.equity == 5070.0


def test_currency_equity_with_no_trades_is_just_the_capital(tmp_path):
    stats, _ = gather_paper_trade_stats(tmp_path / "none.jsonl", initial_capital=5000.0, currency="EUR")
    assert stats.realized_pnl == 0.0 and stats.equity == 5000.0


# --- Strategy generations: the closed record split by the config each trade
# was opened under, so a new strategy's win rate is never pooled with the old
# regime that produced the current headline 5W-8L. ---

_HTH_SIG = {
    "stop_loss_pct": 50.0, "take_profit_pct": 100.0, "signal_confidence_threshold": 0.5,
    "transaction_cost_profile": "retail", "max_favorable_drift_pct": 12.0, "max_horizon_days": 21,
    "label": "hold-to-horizon", "version": "0.43.0",
}


def test_peak_concurrent_counts_overlapping_intervals():
    rows = [
        {"opened_at": "2026-07-01T00:00:00", "closed_at": "2026-07-05T00:00:00"},
        {"opened_at": "2026-07-02T00:00:00", "closed_at": "2026-07-03T00:00:00"},  # overlaps the first -> 2
        {"opened_at": "2026-07-06T00:00:00", "closed_at": "2026-07-07T00:00:00"},  # after the first closed -> 1
    ]
    assert _peak_concurrent(rows) == 2


def test_peak_concurrent_ignores_trades_missing_timestamps():
    assert _peak_concurrent([{"status": "WIN"}, {"opened_at": "2026-07-01T00:00:00"}]) == 0


def test_paper_stats_surface_leverage_when_peak_exceeds_slots(tmp_path):
    """A5/MED-3: with no entry-time position cap, more than max_concurrent
    positions can be open at once, so the currency equity reflects leverage.
    The stats surface peak-vs-slots so that caveat is visible."""
    path = tmp_path / "paper_trades.jsonl"
    rows = [  # three trades open simultaneously, only two slots modelled
        {"status": "WIN", "r_multiple": 1.0, "opened_at": "2026-07-01T00:00:00", "closed_at": "2026-07-10T00:00:00"},
        {"status": "WIN", "r_multiple": 1.0, "opened_at": "2026-07-02T00:00:00", "closed_at": "2026-07-10T00:00:00"},
        {"status": "LOSS", "r_multiple": -1.0, "opened_at": "2026-07-03T00:00:00", "closed_at": "2026-07-10T00:00:00"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    stats, _ = gather_paper_trade_stats(path, initial_capital=5000.0, currency="EUR",
                                        max_concurrent_positions=2)

    assert stats.peak_concurrent == 3
    assert stats.max_concurrent_positions == 2  # peak > slots -> equity is levered


def _trade(status, r, strategy=None):
    row = {"status": status, "r_multiple": r}
    if strategy is not None:
        row["strategy"] = strategy
    return row


def _write_rows(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_strategy_generations_split_current_from_legacy(tmp_path):
    """The whole point: the current strategy is measured on ONLY its own
    trades, and the old unstamped record (the contaminating 5W-8L) is kept in
    a separate, labelled legacy bucket -- not pooled into the headline."""
    path = tmp_path / "paper_trades.jsonl"
    _write_rows(path,
                [_trade("WIN", 1.0) for _ in range(5)] + [_trade("LOSS", -1.0) for _ in range(8)]
                + [_trade("WIN", 1.5, _HTH_SIG) for _ in range(2)] + [_trade("LOSS", -1.0, _HTH_SIG)])

    gens = gather_strategy_generations(path, _HTH_SIG)

    current = next(g for g in gens if g.is_current)
    assert current.label == "hold-to-horizon"
    assert (current.closed, current.wins, current.losses) == (3, 2, 1)  # NOT the legacy 13
    legacy = next(g for g in gens if g.legacy)
    assert (legacy.closed, legacy.wins, legacy.losses) == (13, 5, 8)
    assert legacy.label.startswith("legacy")
    # Current first, legacy last.
    assert gens[0].is_current and gens[-1].legacy


def test_current_generation_is_present_even_with_no_closed_trades(tmp_path):
    """Right after a strategy change the current record is 0W-0L -- that empty
    'still measuring' state is the honest headline and must appear, labelled
    with the version it began at, rather than being absent."""
    path = tmp_path / "paper_trades.jsonl"
    _write_rows(path, [_trade("WIN", 1.0) for _ in range(3)])  # only legacy trades exist

    gens = gather_strategy_generations(path, _HTH_SIG)

    current = next(g for g in gens if g.is_current)
    assert current.closed == 0
    assert current.label == "hold-to-horizon"
    assert current.version_from == "0.43.0"


def test_generations_group_by_config_not_by_version(tmp_path):
    """A cosmetic version bump must not fork a strategy's record; only a rule
    change does. Two trades that differ solely in version pool together; a
    third with a changed stop is its own generation."""
    path = tmp_path / "paper_trades.jsonl"
    v1 = dict(_HTH_SIG, version="0.43.0")
    v2 = dict(_HTH_SIG, version="0.44.0")               # cosmetic bump, same rules
    changed = dict(_HTH_SIG, stop_loss_pct=25.0, version="0.45.0")  # a real rule change
    _write_rows(path, [_trade("WIN", 1.0, v1), _trade("LOSS", -1.0, v2), _trade("WIN", 1.0, changed)])

    gens = gather_strategy_generations(path, _HTH_SIG)

    non_legacy = [g for g in gens if not g.legacy]
    assert len(non_legacy) == 2                          # v1+v2 collapsed, changed separate
    current = next(g for g in gens if g.is_current)
    assert current.closed == 2                           # v1 and v2 pooled into one generation
    assert current.version_from == "0.43.0" and current.version_to == "0.44.0"


def test_generation_carries_its_own_win_rate_and_interval(tmp_path):
    """Each generation's win rate stands alone with its own Wilson interval,
    so a young strategy's noise reads as noise rather than borrowing the
    legacy record's larger n."""
    path = tmp_path / "paper_trades.jsonl"
    _write_rows(path, [_trade("WIN", 1.0, _HTH_SIG) for _ in range(5)]
                + [_trade("LOSS", -1.0, _HTH_SIG) for _ in range(8)])

    current = next(g for g in gather_strategy_generations(path, _HTH_SIG) if g.is_current)

    assert (current.closed, current.wins) == (13, 5)
    assert round(current.win_rate, 2) == 0.38
    assert 0.0 <= current.win_rate_ci_low < current.win_rate < current.win_rate_ci_high <= 1.0


def test_snapshot_dossier_includes_computed_score():
    dossier = Dossier(symbol="UCTT", direction="LONG", confidence=0.8, magnitude=0.5,
                       independent_source_count=2, status="SIGNALED")
    row = snapshot_dossier(dossier, "2026-07-23T00:00:00+00:00")
    assert row["symbol"] == "UCTT"
    assert row["direction"] == "LONG"
    assert row["confidence"] == 0.8
    assert row["magnitude"] == 0.5
    assert row["score"] == 0.4  # confidence * magnitude
    assert row["independent_source_count"] == 2
    assert row["status"] == "SIGNALED"
    assert row["snapshotted_at"] == "2026-07-23T00:00:00+00:00"


def test_snapshot_dossier_covers_empty_dossiers_too():
    # A dossier with no evidence still gets a real (score=0) data point --
    # the daily snapshot is unconditional, not gated on anything changing.
    dossier = Dossier(symbol="COHU")
    row = snapshot_dossier(dossier, "2026-07-23T00:00:00+00:00")
    assert row["direction"] == "NONE"
    assert row["score"] == 0.0


def test_gather_universe_candidates_puts_addable_ones_first():
    # "no ticker" has the highest seen_count but nothing to click; the
    # unresolved-name entry shouldn't outrank a resolved-but-lower-count one.
    candidates = {
        "no ticker": {"name": "No Ticker Co", "ticker": "", "seen_count": 50},
        "AAA": {"name": "Low Count Co", "ticker": "AAA", "seen_count": 1},
        "BBB": {"name": "High Count Co", "ticker": "BBB", "seen_count": 10},
    }
    rows = gather_universe_candidates(candidates, accepted={})
    assert [r["ticker"] for r in rows] == ["BBB", "AAA", ""]


def test_gather_universe_candidates_demotes_already_accepted():
    # Already-accepted candidates have nothing left to click either --
    # they should sort with the unresolved tail, not the addable head.
    candidates = {
        "AAA": {"name": "Already Added", "ticker": "AAA", "seen_count": 100},
        "BBB": {"name": "Still Pending", "ticker": "BBB", "seen_count": 1},
    }
    rows = gather_universe_candidates(candidates, accepted={"AAA": "AAA"})
    assert [r["ticker"] for r in rows] == ["BBB", "AAA"]


def test_gather_graph_stats_groups_by_filer_symbol(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(Relationship("FORM", "AMAT", "customer", "desc 1", "manual", 0.6, "2026-07-01"))
    graph.add(Relationship("FORM", "INTC", "supplier", "desc 2", "manual", 0.9, "2026-07-02"))
    graph.add(Relationship("UCTT", "AMAT", "customer", "desc 3", "manual", 0.7, "2026-07-03"))

    stats = gather_graph_stats(graph)
    assert stats["edge_count"] == 3
    assert [g["symbol"] for g in stats["by_symbol"]] == ["FORM", "UCTT"]  # sorted, one group per filer

    form_group = stats["by_symbol"][0]
    assert len(form_group["relationships"]) == 2
    # Strongest confidence first within a group.
    assert [r["counterparty"] for r in form_group["relationships"]] == ["INTC", "AMAT"]


def test_gather_graph_stats_empty_graph(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    stats = gather_graph_stats(graph)
    assert stats == {"edge_count": 0, "by_symbol": [], "nodes": [], "edges": []}


def test_gather_graph_stats_builds_typed_nodes_and_edges(tmp_path):
    """The interactive graph panel needs typed nodes: kind from the universe
    (anchor vs tradeable), direction/score from any dossier, plus a flat edge
    list. Without a universe/store it degrades to empty node/edge lists."""
    from smartboi.dossier import Dossier, DossierStore
    from smartboi.universe import CompanySpec

    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(Relationship("UCTT", "AMAT", "customer", "concentration", "manual", 0.85, "2026-07-01"))
    universe = [CompanySpec("UCTT", "Ultra Clean", "semi"),
                CompanySpec("AMAT", "Applied Materials", "semi", signal_source_only=True)]
    store = DossierStore(tmp_path / "d")
    store.save(Dossier(symbol="UCTT", direction="LONG", confidence=0.8, magnitude=0.5))

    stats = gather_graph_stats(graph, universe, store)

    nodes = {n["id"]: n for n in stats["nodes"]}
    assert nodes["UCTT"]["kind"] == "tradeable" and nodes["UCTT"]["dir"] == "LONG"
    assert nodes["UCTT"]["score"] == 0.4                 # confidence * magnitude
    assert nodes["UCTT"]["name"] == "Ultra Clean"        # carried for the graph tooltip
    assert nodes["AMAT"]["kind"] == "anchor" and nodes["AMAT"]["dir"] is None
    assert nodes["AMAT"]["name"] == "Applied Materials"
    assert stats["edges"] == [["UCTT", "AMAT", "customer", 0.85]]


# --- Per-dossier detail: the evidence the refresh payload deliberately does
# NOT carry, fetched when a ladder row is clicked. ---


def _ev(evidence_id, published_at, **kw):
    from smartboi.dossier import EvidenceRecord

    fields = {
        "evidence_id": evidence_id, "source_type": "news", "source_name": "Reuters",
        "url": "https://example.com/" + evidence_id, "headline": "H " + evidence_id,
        "published_at": published_at, "origin_symbol": "UCTT", "is_propagated": False,
        "relationship_note": "", "direction": "LONG", "magnitude": 0.5,
        "confidence": 0.5, "horizon_days": 20, "reasoning": "r", "skeptic_note": "",
    }
    fields.update(kw)
    return EvidenceRecord(**fields)


def test_dossier_detail_carries_the_evidence_and_the_summary_fields(tmp_path):
    from smartboi.dossier import Dossier, DossierStore

    store = DossierStore(tmp_path / "d")
    store.save(Dossier(
        symbol="UCTT", direction="LONG", confidence=0.8, magnitude=0.5,
        thesis_summary="Tool orders recovering",
        evidence=[_ev("a", "2026-07-01T00:00:00+00:00",
                      is_propagated=True, origin_symbol="AMAT",
                      relationship_note="AMAT is a customer of UCTT",
                      relationship_confidence=0.7)],
    ))

    detail = gather_dossier_detail(store, "UCTT")

    # Every field the summary row has, so a view can render either payload.
    assert detail["thesis_summary"] == "Tool orders recovering"
    assert detail["confidence"] == 0.8 and detail["evidence_count"] == 1
    assert detail["evidence"][0]["relationship_note"] == "AMAT is a customer of UCTT"
    assert detail["evidence"][0]["relationship_confidence"] == 0.7


def test_dossier_detail_is_none_for_a_symbol_with_no_dossier(tmp_path):
    """DossierStore.load invents an empty Dossier for an unknown symbol, which
    would render as a real dossier scoring 0.00 rather than as 'no such
    thing'. The membership check is also what keeps a crafted symbol from
    reaching outside the dossier directory."""
    from smartboi.dossier import DossierStore

    store = DossierStore(tmp_path / "d")
    assert gather_dossier_detail(store, "NOPE") is None


def test_dossier_detail_caps_evidence_newest_first_and_says_so(tmp_path):
    """A name that has been in the universe a year can carry hundreds of
    items; the panel must not become a megabyte. What is cut is the oldest,
    and the header can still say 3 of 5 because both numbers are reported."""
    from smartboi.dossier import Dossier, DossierStore

    store = DossierStore(tmp_path / "d")
    store.save(Dossier(
        symbol="UCTT", direction="LONG", confidence=0.8, magnitude=0.5,
        evidence=[_ev(chr(ord("a") + i), f"2026-07-0{i + 1}T00:00:00+00:00") for i in range(5)],
    ))

    detail = gather_dossier_detail(store, "UCTT", evidence_limit=3)

    assert detail["evidence_count"] == 5          # what exists
    assert detail["evidence_shown"] == 3          # what was sent
    assert [e["evidence_id"] for e in detail["evidence"]] == ["e", "d", "c"]


# --- Coverage: how much of the TRADEABLE universe is actually live. A
# dossier count far below the tradeable count means most of the universe is
# dark, not that the market is quiet -- and the connectivity rows say why. ---

def _spec(symbol, anchor=False):
    from smartboi.universe import CompanySpec

    return CompanySpec(symbol, symbol, "test", signal_source_only=anchor)


def _rel(a, b):
    from smartboi.graph import Relationship

    return Relationship(a, b, "customer", "d", "s", 0.9, "2026-07-29")


def test_coverage_counts_tradeables_anchors_and_dossiers(tmp_path):
    from smartboi.dossier import Dossier, DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    store = DossierStore(tmp_path / "d")
    store.save(Dossier(symbol="ULH"))
    graph = RelationshipGraph(tmp_path / "g.json")
    universe = [_spec("ULH"), _spec("THRM"), _spec("GM", anchor=True)]

    c = gather_coverage(universe, graph, store)

    assert c["tradeables"] == 2
    assert c["anchors"] == 1
    assert c["tradeables_with_dossier"] == 1


def test_coverage_ignores_a_dossier_for_a_non_tradeable(tmp_path):
    """Dossier FILES outlive universe membership (a demoted or dropped
    symbol keeps its file), so counting files would overstate coverage."""
    from smartboi.dossier import Dossier, DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    store = DossierStore(tmp_path / "d")
    store.save(Dossier(symbol="GM"))       # an anchor
    store.save(Dossier(symbol="GONE"))     # no longer in the universe at all
    graph = RelationshipGraph(tmp_path / "g.json")

    c = gather_coverage([_spec("ULH"), _spec("GM", anchor=True)], graph, store)

    assert c["tradeables_with_dossier"] == 0


def test_coverage_reports_connectivity_in_both_directions(tmp_path):
    """Edges are stored filer -> counterparty, but propagation traverses
    both ways (graph.linked_symbols), so connectivity must too."""
    from smartboi.dossier import DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    graph = RelationshipGraph(tmp_path / "g.json")
    graph.add(_rel("ULH", "GM"))
    universe = [_spec("ULH"), _spec("THRM"), _spec("GM", anchor=True)]

    c = gather_coverage(universe, graph, DossierStore(tmp_path / "d"))

    assert c["tradeables_connected"] == 1
    assert c["tradeables_unconnected"] == ["THRM"]
    assert c["anchors_live"] == 1
    assert c["anchors_inert"] == []


def test_an_anchor_linked_only_to_another_anchor_counts_as_inert(tmp_path):
    """An anchor is never its own analysis target, so an anchor-to-anchor
    edge still resolves to zero targets -- 'live' means linked to something
    tradeable, not merely linked."""
    from smartboi.dossier import DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    graph = RelationshipGraph(tmp_path / "g.json")
    graph.add(_rel("GM", "F"))
    universe = [_spec("ULH"), _spec("GM", anchor=True), _spec("F", anchor=True)]

    c = gather_coverage(universe, graph, DossierStore(tmp_path / "d"))

    assert c["anchors_live"] == 0
    assert c["anchors_inert"] == ["F", "GM"]


def test_coverage_on_an_empty_universe_does_not_divide_by_zero(tmp_path):
    from smartboi.dossier import DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    c = gather_coverage([], RelationshipGraph(tmp_path / "g.json"), DossierStore(tmp_path / "d"))

    assert c["tradeables"] == 0 and c["anchors"] == 0
    assert c["tradeables_with_dossier"] == 0


def test_snapshot_records_which_scoring_logic_produced_it():
    """A change to how magnitude or confidence is aggregated makes old and
    new rows incomparable. Stamping the version lets the forward-return
    analysis split at the boundary instead of silently mixing them --
    forward data can't be backfilled and old rows must never be re-scored."""
    from smartboi.dossier import SCORING_VERSION, Dossier
    from smartboi.status import snapshot_dossier

    row = snapshot_dossier(Dossier(symbol="DCO", direction="LONG", confidence=0.7,
                                   magnitude=0.4, independent_source_count=2),
                           "2026-07-29T00:00:00+00:00")

    assert row["scoring_version"] == SCORING_VERSION


# --- Graph health: is the mechanism the strategy runs on being kept alive? ---

def test_graph_health_flags_a_thesis_with_no_supply_chain_path(tmp_path):
    """The headline number. A tradeable carrying a real thesis with NO graph
    edge built that thesis entirely from its own filings -- it never used the
    cross-company mechanism this system exists for, so it is a single-stock
    news signal wearing the system's clothes. Seen live as green nodes
    floating unconnected on the dashboard graph."""
    from smartboi.dossier import Dossier, DossierStore
    from smartboi.status import gather_graph_health
    from smartboi.universe import CompanySpec

    graph = RelationshipGraph(tmp_path / "g.json")
    graph.add(Relationship("UCTT", "AMAT", "customer", "d", "s", 0.85, "2026-07-01"))
    universe = [CompanySpec("UCTT", "Ultra Clean", "semi"),
                CompanySpec("LONE", "Lonely Co", "semi"),          # thesis, no edge
                CompanySpec("QUIET", "Quiet Co", "semi"),          # no edge, no thesis
                CompanySpec("AMAT", "Applied Materials", "semi", signal_source_only=True)]
    store = DossierStore(tmp_path / "d")
    store.save(Dossier(symbol="UCTT", direction="LONG", confidence=0.8, magnitude=0.5))
    store.save(Dossier(symbol="LONE", direction="LONG", confidence=0.9, magnitude=0.7))
    store.save(Dossier(symbol="QUIET"))                            # no direction

    h = gather_graph_health(graph, universe, store)

    assert h["tradeables_connected"] == 1 and h["tradeables_disconnected"] == 2
    # Only the disconnected name that actually carries a thesis is flagged.
    assert h["disconnected_with_thesis"] == 1
    assert h["disconnected_with_thesis_symbols"] == ["LONE"]


def test_graph_health_counts_edges_by_type_and_anchor_liveness(tmp_path):
    """An anchor is never its own analysis target, so 'live' means linked to
    something TRADEABLE -- an anchor-to-anchor edge reaches nothing."""
    from smartboi.dossier import DossierStore
    from smartboi.status import gather_graph_health
    from smartboi.universe import CompanySpec

    graph = RelationshipGraph(tmp_path / "g.json")
    graph.add(Relationship("UCTT", "AMAT", "customer", "d", "s", 0.85, "2026-07-01"))
    graph.add(Relationship("UCTT", "LRCX", "competitor", "d", "s", 0.5, "2026-07-01"))
    graph.add(Relationship("AMAT", "DEAD", "customer", "d", "s", 0.5, "2026-07-01"))  # anchor->anchor
    universe = [CompanySpec("UCTT", "Ultra Clean", "semi"),
                CompanySpec("AMAT", "Applied", "semi", signal_source_only=True),
                CompanySpec("LRCX", "Lam", "semi", signal_source_only=True),
                CompanySpec("DEAD", "Inert Co", "semi", signal_source_only=True)]

    h = gather_graph_health(graph, universe, DossierStore(tmp_path / "d"))

    assert h["edges"] == 3
    assert h["edges_by_type"] == {"customer": 2, "competitor": 1}
    assert h["anchors_live"] == 2 and h["anchors_inert"] == 1   # DEAD reaches no tradeable


def test_graph_health_reports_extraction_staleness_and_cadence(tmp_path):
    """How far behind the rolling re-extraction is. A symbol last read when
    the universe was smaller carries holes a re-read would fill, and one never
    read at all is the stalest case of the lot."""
    from smartboi.dossier import DossierStore
    from smartboi.status import gather_graph_health
    from smartboi.universe import CompanySpec

    graph = RelationshipGraph(tmp_path / "g.json")
    universe = [CompanySpec("A", "A", "x"), CompanySpec("B", "B", "x"), CompanySpec("C", "C", "x")]
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    state = {"A": {"backfilled_at": old}, "B": {"backfilled_at": recent}}   # C never read

    h = gather_graph_health(graph, universe, DossierStore(tmp_path / "d"),
                            backfill_state=state, refresh_per_day=10)

    assert 39 < h["stalest_days"] < 41
    assert h["never_extracted"] == 1
    assert h["cycle_days"] == 0.3          # 3 symbols at 10/day
    assert h["last_refresh_days"] is None  # never run


def test_graph_health_works_with_no_engine_state(tmp_path):
    """Callers without engine state (several tests, and any pre-upgrade
    deploy) must still get the structural numbers rather than an exception."""
    from smartboi.dossier import DossierStore
    from smartboi.status import gather_graph_health

    h = gather_graph_health(RelationshipGraph(tmp_path / "g.json"), [], DossierStore(tmp_path / "d"))

    assert h["edges"] == 0 and h["tradeables"] == 0
    assert h["stalest_days"] is None and h["cycle_days"] is None


def test_the_snapshot_carries_per_mechanism_attribution():
    """SCORING_VERSION 6 lands three changes that all push scores up in the
    same region. Version 5 bundled three behind one boundary and the resulting
    series can no longer attribute an outcome to any of them; these columns
    make the bucketing a filter over the row rather than an inference about
    the release."""
    from smartboi.dossier import Dossier

    row = snapshot_dossier(
        Dossier(symbol="INTT", direction="LONG", confidence=0.0, magnitude=0.0,
                already_priced_in=True, synthesis_at="2026-08-09T00:00:00+00:00",
                synthesis_confidence=0.9, synthesis_magnitude=0.8,
                pre_synthesis_score=0.42, synthesis_price=12.5,
                veto_falsified_by_price=True, ecosystem_slot_counted=True,
                has_filing_evidence=False, has_disclosed_link_evidence=False),
        "2026-08-10T00:00:00+00:00", min_sources_required=3,
    )

    # A vetoed row is no longer 0.000-and-nothing-else: what the pass thought,
    # and what it capped, are both on the record.
    assert row["score"] == 0.0
    assert row["synthesis_confidence"] == 0.9
    assert row["pre_synthesis_score"] == 0.42
    assert row["synthesis_price"] == 12.5
    # Which bar it was held to, and the flags that decided it.
    assert row["min_sources_required"] == 3
    assert row["has_filing_evidence"] is False
    assert row["has_disclosed_link_evidence"] is False
    # Which mechanism touched this row.
    assert row["veto_falsified_by_price"] is True
    assert row["ecosystem_slot_counted"] is True
    assert row["synthesis_stale_evidence"] is False


def test_archived_and_flipped_rows_do_not_enter_the_win_rate(tmp_path):
    """They are in the ledger so the exclusion is visible, not so they can
    be scored. Counting an ARCHIVED row as a loss because its mark happened
    to be red would invent an outcome the trade never reached."""
    log = tmp_path / "paper_trades.jsonl"
    rows = [
        {"symbol": "AAA", "direction": "LONG", "status": "WIN", "r_multiple": 1.9,
         "opened_at": "2026-08-03T14:00:00+00:00", "closed_at": "2026-08-05T14:00:00+00:00"},
        {"symbol": "BBB", "direction": "LONG", "status": "LOSS", "r_multiple": -1.0,
         "opened_at": "2026-08-03T14:00:00+00:00", "closed_at": "2026-08-04T14:00:00+00:00"},
        {"symbol": "CCC", "direction": "LONG", "status": "ARCHIVED", "r_multiple": -0.4,
         "opened_at": "2026-08-03T14:00:00+00:00", "closed_at": "2026-08-09T08:00:00+00:00"},
        {"symbol": "DDD", "direction": "SHORT", "status": "THESIS_FLIPPED", "r_multiple": -0.2,
         "opened_at": "2026-08-03T14:00:00+00:00", "closed_at": "2026-08-06T14:00:00+00:00"},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    stats, _ = gather_paper_trade_stats(log)

    assert stats.closed == 2                     # not 4
    assert (stats.wins, stats.losses) == (1, 1)
    assert stats.win_rate == 0.5                 # 25% if the unscored rows leaked in
    assert stats.archived == 1
    assert stats.thesis_flipped == 1


def test_a_ledger_of_only_archived_rows_reports_no_record_rather_than_zero_percent(tmp_path):
    """The state right after a reset. A 0% win rate over 0 trades reads as
    "the strategy lost"; it means "nothing has resolved yet"."""
    log = tmp_path / "paper_trades.jsonl"
    log.write_text(json.dumps({
        "symbol": "AAA", "direction": "LONG", "status": "ARCHIVED", "r_multiple": -0.4,
        "opened_at": "2026-08-03T14:00:00+00:00", "closed_at": "2026-08-09T08:00:00+00:00"}) + "\n")

    stats, _ = gather_paper_trade_stats(log)

    assert stats.closed == 0
    assert stats.win_rate == 0.0
    assert stats.archived == 1


def test_a_requeued_symbol_still_reports_its_real_extraction_age(tmp_path):
    """The rolling refresh used to DELETE the marker to re-queue a symbol,
    which also erased the record that its filing had ever been read. While
    extraction was broken the marker never came back, so 36 recently-read
    symbols reported as "never extracted" and the graph looked far emptier
    than it was. The queue flag now rides alongside the stamp."""
    from smartboi.dossier import DossierStore
    from smartboi.status import gather_graph_health
    from smartboi.universe import CompanySpec

    graph = RelationshipGraph(tmp_path / "g.json")
    universe = [CompanySpec("A", "A", "x"), CompanySpec("B", "B", "x")]
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    state = {
        "A": {"backfilled_at": recent, "refresh_requested": True},  # queued, but read 2d ago
        "B": {},                                                    # genuinely never read
    }

    h = gather_graph_health(graph, universe, DossierStore(tmp_path / "d"),
                            backfill_state=state, refresh_per_day=10)

    assert h["never_extracted"] == 1               # B only
    assert 1 < h["stalest_days"] < 3               # A's real age, not "unknown"
