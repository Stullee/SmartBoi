"""The structural-fault checks behind the graph-maintenance button.

Every case here is drawn from a symbol that was actually live in the accepted
universe -- the audit exists because seven of eleven accepted tradeables were
unfit to hold, and each check is named after the one that got through.
"""
from datetime import datetime, timedelta, timezone

from smartboi.edgar import normalize_company_name
from smartboi.engine import is_common_equity
from smartboi.graph import Relationship
from smartboi.graph_audit import (
    KIND_DANGLING_EDGE,
    KIND_DEAD_LISTING,
    KIND_DUPLICATE_NAME,
    KIND_JUNK_RELATIONSHIP,
    KIND_NAME_MISMATCH,
    KIND_NOT_COMMON_EQUITY,
    KIND_SELF_EDGE,
    KIND_STALE_EDGE,
    audit,
    summarize,
)

_LENDERS = ("note purchase", "credit facility", "promissory note")
NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _audit(**over):
    kwargs = dict(
        accepted={},
        candidates={},
        relationships=[],
        universe_symbols=set(),
        curated_symbols=set(),
        open_positions=set(),
        live_tickers=None,
        name_verified={},
        is_common_equity=is_common_equity,
        lender_phrases=_LENDERS,
        normalize_name=normalize_company_name,
        stale_edge_days=120,
        now=NOW,
    )
    kwargs.update(over)
    return audit(**kwargs)


def _kinds(findings):
    return [f.kind for f in findings]


# --- Dead listings: SPWR (bankrupt) and RJET (taken private) ---

def test_a_symbol_absent_from_secs_map_is_a_dead_listing():
    findings = _audit(
        accepted={"SPWR": {"as": "tradeable"}},
        live_tickers={"AAPL", "MSFT"},
    )
    assert _kinds(findings) == [KIND_DEAD_LISTING]
    assert findings[0].actionable is True


def test_an_unreachable_ticker_map_skips_the_liveness_check_entirely():
    """The failure that must never happen: an unreachable SEC read as evidence
    that every symbol is delisted, proposing to empty the universe."""
    findings = _audit(accepted={"SPWR": {"as": "tradeable"}}, live_tickers=None)
    assert findings == []


def test_a_dead_symbol_reports_only_the_dead_listing():
    """Once the ticker is gone, every other check on it is moot noise."""
    findings = _audit(
        accepted={"TCPA": {"as": "tradeable"}},
        candidates={"TCPA": {"name": "TransCanada Corporation"}},
        live_tickers={"AAPL"},
        name_verified={"TCPA": False},
    )
    assert _kinds(findings) == [KIND_DEAD_LISTING]


# --- Not common equity: TCPA (a 2085 note), SCE-PN (preferred), SCRNY (ADR) ---

def test_a_tradeable_that_is_not_common_equity_is_a_fault():
    findings = _audit(
        accepted={"SCE-PN": {"as": "tradeable"}},
        live_tickers={"SCE-PN"},
    )
    assert _kinds(findings) == [KIND_NOT_COMMON_EQUITY]


def test_the_same_security_is_fine_as_an_anchor():
    """An anchor is never bought -- it exists to propagate news, so a preferred
    series or an ADR is a perfectly good one."""
    findings = _audit(
        accepted={"SCE-PN": {"as": "anchor"}, "SCRNY": {"as": "anchor"}},
        live_tickers={"SCE-PN", "SCRNY"},
    )
    assert findings == []


# --- Name mismatch: GHY <- "PGIM, Inc." ---

def test_a_name_that_no_longer_verifies_is_a_misresolution():
    findings = _audit(
        accepted={"GHY": {"as": "tradeable"}},
        candidates={"GHY": {"name": "PGIM, Inc."}},
        live_tickers={"GHY"},
        name_verified={"GHY": False},
    )
    assert KIND_NAME_MISMATCH in _kinds(findings)
    assert "PGIM" in findings[0].detail


def test_a_symbol_never_checked_is_not_reported_as_mismatched():
    """Absent from name_verified means "not checked", not "failed" -- same
    reason live_tickers=None skips the liveness check."""
    findings = _audit(
        accepted={"GHY": {"as": "anchor"}},
        candidates={"GHY": {"name": "PGIM, Inc."}},
        live_tickers={"GHY"},
        name_verified={},
    )
    assert KIND_NAME_MISMATCH not in _kinds(findings)


# --- Junk relationship: GHY's only edge is a Note Purchase Agreement ---

def test_a_financing_relationship_is_flagged_on_an_accepted_symbol():
    findings = _audit(
        accepted={"GHY": {"as": "tradeable"}},
        candidates={"GHY": {
            "name": "PGIM, Inc.",
            "description": "party to the Note Purchase and Private Shelf Agreement dated 2017",
        }},
        live_tickers={"GHY"},
    )
    assert KIND_JUNK_RELATIONSHIP in _kinds(findings)


def test_a_financing_phrase_in_a_pending_edge_counts_too():
    findings = _audit(
        accepted={"X": {"as": "anchor"}},
        candidates={"X": {"name": "Some Bank", "description": "",
                          "pending_edges": [{"description": "provides a credit facility"}]}},
        live_tickers={"X"},
    )
    assert KIND_JUNK_RELATIONSHIP in _kinds(findings)


# --- What a quarantine may NOT touch ---

def test_an_open_paper_trade_blocks_the_quarantine():
    """Removing the symbol would strand a position that could never be marked
    out, so the trade has to close on its own terms first."""
    findings = _audit(
        accepted={"SPWR": {"as": "tradeable"}},
        live_tickers={"AAPL"},
        open_positions={"SPWR"},
    )
    assert findings[0].actionable is False
    assert "OPEN paper trade" in findings[0].blocked_reason


def test_a_curated_seed_symbol_is_reported_but_never_actionable():
    """A runtime pass cannot durably remove a code-seeded symbol -- it returns
    on the next restart -- so it is surfaced for a human to edit universe.py."""
    findings = _audit(
        accepted={},
        curated_symbols={"DEADCO"},
        live_tickers={"AAPL"},
    )
    assert _kinds(findings) == [KIND_DEAD_LISTING]
    assert findings[0].actionable is False
    assert "universe.py" in findings[0].blocked_reason


# --- Edge faults ---

def _rel(frm, to, rel_type="customer", extracted_at=""):
    return Relationship(frm, to, rel_type, "d", "s", 0.9, extracted_at)


def test_a_self_edge_is_reported_and_never_actionable():
    findings = _audit(relationships=[_rel("AAPL", "AAPL")], universe_symbols={"AAPL"})
    assert _kinds(findings) == [KIND_SELF_EDGE]
    assert findings[0].actionable is False


def test_an_edge_with_an_endpoint_outside_the_universe_is_dangling():
    findings = _audit(relationships=[_rel("AAPL", "NOTHERE")], universe_symbols={"AAPL"})
    assert _kinds(findings) == [KIND_DANGLING_EDGE]
    assert "NOTHERE" in findings[0].detail


def test_an_edge_not_reconfirmed_in_months_is_stale():
    old = (NOW - timedelta(days=200)).isoformat()
    findings = _audit(
        relationships=[_rel("AAPL", "MSFT", extracted_at=old)],
        universe_symbols={"AAPL", "MSFT"},
    )
    assert _kinds(findings) == [KIND_STALE_EDGE]


def test_a_recently_confirmed_edge_is_not_stale():
    fresh = (NOW - timedelta(days=3)).isoformat()
    findings = _audit(
        relationships=[_rel("AAPL", "MSFT", extracted_at=fresh)],
        universe_symbols={"AAPL", "MSFT"},
    )
    assert findings == []


# --- Candidate store ---

def test_rows_that_collapse_to_one_company_are_reported():
    """seen_count gates tradeable auto-accept and is split across spellings, so
    repeat disclosures are under-counted. Measured live: VOLKSWAGEN x5."""
    findings = _audit(candidates={
        "VOLKSWAGEN AG": {"name": "Volkswagen AG"},
        "VOLKSWAGEN GROUP": {"name": "Volkswagen Group"},
        "VWAGY": {"name": "Volkswagen"},
    })
    assert _kinds(findings) == [KIND_DUPLICATE_NAME]
    assert "3 candidate rows" in findings[0].detail


# --- Ordering and summary ---

def test_findings_are_ordered_most_decisive_first():
    findings = _audit(
        accepted={"DEAD": {"as": "tradeable"}, "PREF-A": {"as": "tradeable"}},
        relationships=[_rel("AAPL", "AAPL")],
        universe_symbols={"AAPL"},
        live_tickers={"PREF-A", "AAPL"},
    )
    assert _kinds(findings) == [KIND_DEAD_LISTING, KIND_NOT_COMMON_EQUITY, KIND_SELF_EDGE]


def test_summarize_counts_actionable_and_names_the_symbols():
    findings = _audit(
        accepted={"SPWR": {"as": "tradeable"}},
        relationships=[_rel("AAPL", "AAPL")],
        universe_symbols={"AAPL"},
        live_tickers={"AAPL"},
    )
    summary = summarize(findings)
    assert summary["total"] == 2
    assert summary["actionable"] == 1          # the self-edge is reported, not actioned
    assert summary["symbols_at_fault"] == ["SPWR"]
