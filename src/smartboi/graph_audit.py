"""Faults in the universe and the relationship graph that nothing else looks for.

WHY THIS EXISTS. Every existing maintenance pass answers "is the graph BIG
enough": the refresh re-extracts, the research and full-text passes hunt for
new candidates, and reconcile_universe_connectivity grows and prunes on
CONNECTIVITY. None of them asks "is what we already have CORRECT", and an
audit of the live board says that is the question that was going unasked.
All eleven accepted tradeables were inspected; only four were sound:

    GHY     "PGIM, Inc."                  a closed-end BOND FUND. Resolved by
                                          an over-loose name prefix, off a
                                          Note Purchase Agreement -- i.e. a
                                          LENDER, the exact class the
                                          extraction filters exist to drop.
    TCPA    "TransCanada Corporation"     a junior subordinated note due 2085
    SCE-PN  "Southern California Edison"  a preferred series, not equity
    SPWR    "SunPower Corporation"        delisted
    RJET    "Republic Airways Holdings"   taken private

Each of those is a symbol the engine polls hourly, writes dossiers for, and
spends LLM budget on, in service of a thesis that cannot exist. The graph is
the mechanism the whole strategy runs on, so a wrong edge is worse than a
missing one: a missing edge is a trade that never happens, a wrong edge is a
trade fired against an unrelated company.

WHAT THIS IS NOT. It is not a screen and it does not judge whether a company
is a GOOD holding -- universe_screen.py does that. Every check here is
structural and mechanical: does this ticker still exist, is it the kind of
security the system can hold, does its name still verify against SEC's filer
list, is this "relationship" actually a loan. A finding is a statement of
fact with the evidence attached, not an opinion.

DELIBERATELY PURE. No I/O, no engine import, no network. Everything the
checks need is passed in -- including the two async results (SEC's live
ticker map and the per-symbol name verification) which the caller resolves
first. That keeps the rules testable against hand-built inputs, and it is why
the phrase tuples are parameters rather than an import from engine.py, which
imports this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from smartboi.federal_register import REGULATOR_SYMBOLS

# A finding is one of these kinds. Ordered by how decisively it disqualifies
# a symbol, because that is the order an operator should read them in.
KIND_DEAD_LISTING = "dead_listing"
KIND_NOT_COMMON_EQUITY = "not_common_equity"
KIND_NAME_MISMATCH = "name_mismatch"
KIND_JUNK_RELATIONSHIP = "junk_relationship"
KIND_SELF_EDGE = "self_edge"
KIND_DANGLING_EDGE = "dangling_edge"
KIND_STALE_EDGE = "stale_edge"
KIND_DUPLICATE_NAME = "duplicate_name"
# An edge left pointing at a symbol a maintenance pass REMOVED. Split from
# dangling_edge because the advice inverts: a dangling edge invites accepting
# the endpoint, and for these that means re-admitting exactly what was just
# quarantined -- which the audit was telling the operator to do for 106 edges.
KIND_ORPHANED_EDGE = "orphaned_edge"

# Symbol-level kinds are the ones a quarantine can act on; edge-level and
# store-level kinds are reported for a human, since dropping an edge is a
# different and more destructive act than removing a symbol from the universe
# (the edge cannot be recreated without re-reading the filing that produced
# it, which may be a year away).
SYMBOL_KINDS = (
    KIND_DEAD_LISTING, KIND_NOT_COMMON_EQUITY, KIND_NAME_MISMATCH, KIND_JUNK_RELATIONSHIP,
)


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str            # a ticker, or "FROM->TO (rel_type)" for an edge
    detail: str             # human-readable, carrying the evidence verbatim
    actionable: bool        # whether a quarantine pass may act on this
    blocked_reason: str = ""  # why it may not, when actionable is False

    @property
    def is_symbol_fault(self) -> bool:
        return self.kind in SYMBOL_KINDS


def _days_since(stamp: str, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def _matches_any(text: str, phrases) -> str:
    lowered = (text or "").lower()
    for phrase in phrases:
        if phrase in lowered:
            return phrase
    return ""


def audit(
    *,
    accepted: dict,
    candidates: dict,
    relationships,
    universe_symbols: set[str],
    curated_symbols: set[str],
    open_positions: set[str],
    live_tickers: set[str] | None,
    name_verified: dict,
    is_common_equity,
    lender_phrases=(),
    normalize_name=None,
    stale_edge_days: int = 120,
    # Symbols a maintenance pass has already removed. Their edges are
    # ORPHANED, not merely dangling -- see KIND_ORPHANED_EDGE.
    quarantined: set[str] = frozenset(),
    now: datetime | None = None,
) -> list[Finding]:
    """Every structural fault found, most decisive first.

    `accepted` is the runtime-accepted set only (data/accepted_candidates.json)
    -- a curated DEFAULT_UNIVERSE symbol is never a quarantine target, because
    a runtime pass cannot durably remove a code-seeded symbol (it returns on
    the next restart) and dropping a curated giant is an editorial call. Those
    are still REPORTED, with the reason recorded as blocked, so a genuine fault
    in the seed list is visible rather than silently skipped.

    `live_tickers` may be None, meaning SEC's map could not be fetched. The
    dead-listing check is then SKIPPED rather than guessed -- an unreachable
    endpoint must never be read as "every symbol is delisted", which would
    propose quarantining the entire universe.

    `name_verified` maps ticker -> bool for the symbols the caller was able to
    check. A ticker ABSENT from the mapping is not checked, not failed, for the
    same reason.
    """
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []

    def block_reason(symbol: str) -> tuple[bool, str]:
        """Whether a quarantine may act, and why not when it may not. An open
        paper trade is the hard one: removing the symbol would strand a
        position that can never be marked out, so the position has to close on
        its own terms first."""
        # Curated FIRST. A symbol can be in both stores -- code-seeded in
        # universe.py and also sitting in accepted_candidates from an earlier
        # discovery -- and the old order let that case pass as actionable.
        # Quarantine then deleted the accepted row and rebuilt the universe
        # from settings.universe, which put the symbol straight back: TM was
        # recorded as quarantined on 2026-08-11 and is still live and still
        # polled. Worse, its fault became unreportable -- name verification
        # only walks accepted_candidates, which it had just been deleted from,
        # so the very finding that triggered the quarantine can no longer fire.
        if symbol in curated_symbols:
            return False, "curated universe symbol -- edit universe.py, a runtime pass cannot remove it"
        if symbol in open_positions:
            return False, "has an OPEN paper trade -- close or expire it first"
        if symbol not in accepted:
            return False, "not in the accepted store -- nothing for a runtime pass to remove"
        return True, ""

    # --- Symbol faults, over the accepted set AND the curated seeds ---
    for symbol in sorted(set(accepted) | curated_symbols):
        entry = accepted.get(symbol) or {}
        as_type = entry.get("as") if isinstance(entry, dict) else entry
        candidate = candidates.get(symbol) or {}
        disclosed_name = candidate.get("name") or ""
        actionable, blocked = block_reason(symbol)

        if live_tickers is not None and symbol not in live_tickers:
            findings.append(Finding(
                KIND_DEAD_LISTING, symbol,
                f"not present in SEC's current registered-filer map -- delisted, renamed or taken "
                f"private. Polled hourly for filings that can no longer exist.",
                actionable, blocked,
            ))
            # A dead ticker makes every other check on it moot and its
            # evidence unreadable, so nothing further is reported for it.
            continue

        # Only TRADEABLES are held, so only tradeables need to be common
        # equity. An OTC ADR or a preferred series is a perfectly good ANCHOR
        # -- it exists to propagate news, never to be bought.
        if as_type == "tradeable" and not is_common_equity(symbol):
            findings.append(Finding(
                KIND_NOT_COMMON_EQUITY, symbol,
                "accepted as a TRADE TARGET but is not common equity (preferred series, share "
                "class, or OTC ADR). The thesis this system builds is about operating-company "
                "equity; as an anchor it would still propagate.",
                actionable, blocked,
            ))

        if disclosed_name and name_verified.get(symbol) is False:
            findings.append(Finding(
                KIND_NAME_MISMATCH, symbol,
                f"disclosed as {disclosed_name!r}, which no longer verifies against {symbol}'s "
                f"registered name in SEC's filer list -- the misresolution signature.",
                actionable, blocked,
            ))

        # The lender filter runs at extraction time, so anything accepted
        # before it shipped kept its place. GHY is the live proof: its only
        # edge is a Note Purchase Agreement.
        if lender_phrases:
            haystack = f"{candidate.get('description', '')} {_pending_text(candidate)}"
            hit = _matches_any(haystack, lender_phrases)
            if hit:
                findings.append(Finding(
                    KIND_JUNK_RELATIONSHIP, symbol,
                    f"the disclosure that discovered it reads as a financing relationship "
                    f"(matched {hit!r}), not a supply-chain one. A lender's news has no path to "
                    f"the borrower's fundamentals, which is the only thing the graph is used for.",
                    actionable, blocked,
                ))

    # --- Edge faults ---
    for rel in relationships:
        pair = f"{rel.from_symbol}->{rel.to_symbol} ({rel.rel_type})"
        if rel.from_symbol == rel.to_symbol:
            findings.append(Finding(
                KIND_SELF_EDGE, pair,
                "an edge from a symbol to itself carries no propagation and is a resolution bug "
                "upstream (usually a subsidiary name resolving to its own parent).",
                False, "edge removal is a manual call -- see rebuild_relationship_graph",
            ))
            continue
        missing = [s for s in (rel.from_symbol, rel.to_symbol) if s not in universe_symbols]
        # Agency pseudo-symbols are endpoints BY DESIGN -- federal_register.py
        # writes BIS/EPA/ITC/NHTSA into to_symbol deliberately. They are not
        # tickers, can never be accepted, and so produced findings that could
        # never be cleared and were re-emitted on every daily pass (13 of them
        # live, 16 of the graph's 17 regulator edges). Keyed on the symbol, not
        # on rel_type == "regulator": three dangling regulator edges are
        # ticker-to-ticker and are real findings.
        missing = [s for s in missing if s not in REGULATOR_SYMBOLS]
        if missing:
            orphaned = [s for s in missing if s in quarantined]
            if orphaned:
                findings.append(Finding(
                    KIND_ORPHANED_EDGE, pair,
                    f"endpoint(s) quarantined by a maintenance pass: {', '.join(orphaned)}. "
                    f"This edge was orphaned BY that removal, not by a discovery gap.",
                    False,
                    "do NOT re-accept -- the endpoint was removed on purpose; drop the edge instead",
                ))
                continue
            findings.append(Finding(
                KIND_DANGLING_EDGE, pair,
                f"endpoint(s) not in the universe: {', '.join(missing)}. The edge cannot "
                f"propagate anything until they are accepted.",
                False, "not a fault in itself -- accept the endpoint, or leave it as a lead",
            ))
            continue
        age = _days_since(getattr(rel, "extracted_at", ""), now)
        if age is not None and age > stale_edge_days:
            findings.append(Finding(
                KIND_STALE_EDGE, pair,
                f"not re-confirmed by any filing in {age:.0f} days. graph.add refreshes the stamp "
                f"on every re-confirmation, so this means no filing has mentioned the "
                f"relationship in months -- a lost customer is itself a tradeable event.",
                False, "reported only -- a stale edge may still be true",
            ))

    # --- Candidate-store faults ---
    if normalize_name is not None:
        by_normal: dict[str, list[str]] = {}
        for key, entry in candidates.items():
            name = (entry or {}).get("name") or key
            normalized = normalize_name(name)
            if normalized:
                by_normal.setdefault(normalized, []).append(key)
        for normalized, keys in sorted(by_normal.items()):
            if len(keys) > 1:
                findings.append(Finding(
                    KIND_DUPLICATE_NAME, normalized,
                    f"{len(keys)} candidate rows collapse to one company: {', '.join(sorted(keys)[:6])}"
                    + ("..." if len(keys) > 6 else "")
                    + ". seen_count is split across the spellings, and seen_count is what gates "
                      "tradeable auto-accept -- so repeat disclosures are being under-counted.",
                    False, "merging rewrites discovery history -- reported for review",
                ))

    order = {
        KIND_DEAD_LISTING: 0, KIND_NOT_COMMON_EQUITY: 1, KIND_NAME_MISMATCH: 2,
        KIND_JUNK_RELATIONSHIP: 3, KIND_SELF_EDGE: 4, KIND_DANGLING_EDGE: 5,
        KIND_STALE_EDGE: 6, KIND_DUPLICATE_NAME: 7,
    }
    findings.sort(key=lambda f: (order.get(f.kind, 99), f.subject))
    return findings


def _pending_text(candidate: dict) -> str:
    return " ".join(str(p.get("description", "")) for p in candidate.get("pending_edges") or [])


def summarize(findings: list[Finding]) -> dict:
    """Counts by kind, plus how many are actionable -- the shape the dashboard
    and the daily log line both want."""
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    return {
        "total": len(findings),
        "by_kind": by_kind,
        "actionable": sum(1 for f in findings if f.actionable),
        "symbols_at_fault": sorted({f.subject for f in findings if f.is_symbol_fault}),
    }
