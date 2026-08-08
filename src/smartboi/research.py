"""Operator-run web research: find small-cap US-listed suppliers of the
universe's anchors, so the relationship graph can grow from something other
than what a filing happens to name.

WHY THIS EXISTS. Every edge in the graph comes from SEC filing text, and
filing text is one-directional in a way that structurally starves this
strategy. A small company's 10-K names its big CUSTOMERS, because customer
concentration is a disclosable risk -- that is where edges like
"DCO -> LMT (customer, 95%)" come from, and it works. But a giant's 10-K
does not enumerate its small suppliers: no rule requires it, and no
supplier is material enough to the giant to mention. So the direction this
strategy actually needs -- "who are the thinly-covered names that move when
this anchor moves" -- is exactly the direction filings do not disclose.
Measured live: 98-134 of 161 anchors had no graph edge to any tradeable at
all, which makes them inert (their news resolves to zero targets and is
discarded unread).

The supply relationships are public. They are in trade press, teardowns,
supplier-award announcements, conference coverage and the suppliers' own
marketing -- just not in the anchor's filings. This reads that.

WHAT IT DELIBERATELY DOES NOT DO. It never writes a graph edge. A
web-sourced relationship is not a disclosed one, and the graph's confidence
band is load-bearing: an edge at or above dossier.DISCLOSED_LINK_CONFIDENCE
satisfies the corroboration bar that fires trades (see signals.evaluate).
Letting research mint those would mean a blog post, a stale article or a
plain hallucination could clear the bar a 10-K disclosure was supposed to.

So research produces CANDIDATES ONLY, into the same
universe_candidates.json the filing path writes to -- where they get ticker
resolution, the market-cap/analyst screen, a tradeable-vs-anchor
recommendation, and the dashboard's Accept button, exactly like any other
candidate. Once accepted, the symbol's OWN 10-K is backfilled, and if the
relationship is real that filing discloses it and the edge is created from
a primary source. Research decides where to look; EDGAR still decides what
is true.

Operator-run rather than part of the tick loop: it is the only thing here
that spends on web search, the results need a human accept anyway, and the
right cadence is "when the anchor list changes", not hourly."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from smartboi.llm import cacheable_system, make_client, request_kwargs
from smartboi.usage import CAT_RESEARCH, UsageTracker

log = logging.getLogger(__name__)

# Anchors per run. Bounded because each one costs a handful of web searches
# plus a long-context call, and because an operator reading a report wants
# it to finish. Re-run to continue; already-researched anchors are skipped.
MAX_ANCHORS_PER_RUN = 10
# Web searches the model may run per anchor. Enough to check a few framings
# (supplier lists, teardowns, award announcements) without open-ended
# crawling.
MAX_SEARCHES_PER_ANCHOR = 6
# Server-tool turns can stop with `pause_turn` when the search loop hits its
# own iteration limit; the turn is resumed by re-sending. Bounded so a
# pathological loop cannot run forever.
MAX_RESUMES = 3

_REPORT_TOOL = {
    "name": "report_suppliers",
    "description": (
        "Report the small-cap, US-listed public companies you found that have a supplier, "
        "customer or partner relationship with this company. Call this exactly once, after "
        "searching."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "suppliers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Company legal or common name."},
                        "ticker": {
                            "type": "string",
                            "description": (
                                "US exchange ticker if you are confident of it, else empty. An "
                                "empty ticker is fine and far better than a guessed one -- a "
                                "wrong ticker points this system at an unrelated company."
                            ),
                        },
                        "rel_type": {
                            "type": "string",
                            "enum": ["supplier", "customer", "competitor", "regulator"],
                            "description": "The smaller company's role RELATIVE TO the anchor.",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "What the relationship is, in one or two sentences, with whatever "
                                "was actually stated: the component or service supplied, the "
                                "programme or platform, any revenue share or contract value."
                            ),
                        },
                        "evidence_url": {
                            "type": "string",
                            "description": "The single best source URL you actually read for this.",
                        },
                        "confidence": {
                            "type": "number", "minimum": 0, "maximum": 1,
                            "description": (
                                "How sure you are the relationship is real AND current. Use a low "
                                "value for a single old article or a marketing page; high only "
                                "where a primary or contemporaneous source states it plainly."
                            ),
                        },
                    },
                    "required": ["name", "ticker", "rel_type", "description",
                                 "evidence_url", "confidence"],
                },
            },
            "notes": {
                "type": "string",
                "description": "One or two sentences on what you searched and what you could not establish.",
            },
        },
        "required": ["suppliers", "notes"],
    },
}

_SYSTEM_PROMPT = (
    "You research public-company supply chains. Given one large, well-covered company, you "
    "find the SMALL, thinly-covered, US-listed public companies that supply it, buy from it, "
    "or compete with it.\n\n"
    "Why the size filter matters: this is for a strategy that trades the lag between a large "
    "company's news and the market repricing a small second-order name. A large supplier is "
    "useless to it -- that name is already efficiently priced, and its news is already "
    "connected to the anchor's by every analyst covering both. Target roughly $75M-$5B in "
    "market capitalisation with thin analyst coverage. If a relationship's counterparty is "
    "another mega-cap, do not report it.\n\n"
    "Hard requirements, in order of importance:\n"
    "1. PUBLICLY LISTED IN THE US. A private supplier, a foreign issuer that does not file "
    "with the SEC, a subsidiary of a larger listed parent, or a division is unusable -- there "
    "is no security to hold. Do not report them.\n"
    "2. DO NOT GUESS TICKERS. An empty ticker is fine; this system resolves tickers itself "
    "from SEC's registered-filer list and verifies the name matches before acting. A confident "
    "wrong ticker points a research pipeline at an unrelated company, which is the single "
    "worst outcome here -- worse than finding nothing.\n"
    "3. CITE WHAT YOU ACTUALLY READ. evidence_url must be a page you retrieved in this "
    "session, not one you believe exists. If you cannot find a source for a relationship you "
    "are fairly sure of, either omit it or report it with a low confidence and say so.\n"
    "4. PREFER CURRENT AND SPECIFIC. A named component on a named programme with a date beats "
    "'is a supplier to the automotive industry'. A relationship that ended is worse than no "
    "relationship, because it will be acted on as if live -- if the sourcing looks historical, "
    "say so in the description and score confidence low.\n\n"
    "Report nothing rather than padding. An empty list with an honest note is a good answer "
    "for a company whose supply chain is genuinely private or entirely large-cap; a list of "
    "plausible-sounding names with no sources is not. Every name you return costs downstream "
    "verification work, and a wrong one costs more than a missing one."
)


@dataclass(frozen=True)
class ResearchedSupplier:
    anchor: str
    name: str
    ticker: str
    rel_type: str
    description: str
    evidence_url: str
    confidence: float


class SupplierResearcher:
    """One web-search-backed call per anchor. Uses the Anthropic server-side
    web search tool, so there is no additional vendor, API key or scraper to
    maintain -- and the search runs on Anthropic's side, meaning this works
    from a deployment with no general outbound network access."""

    def __init__(self, api_key: str, model: str, usage: UsageTracker):
        self._client = make_client(api_key)
        self._model = model
        self._usage = usage

    async def research(self, anchor: str, anchor_name: str, ecosystem: str,
                       min_cap_musd: float, max_cap_musd: float) -> list[ResearchedSupplier]:
        if not self._usage.budget_remaining(CAT_RESEARCH):
            log.info("%s: daily LLM budget reached -- skipping supplier research.", anchor)
            return []
        messages = [{
            "role": "user",
            "content": (
                f"Company: {anchor_name} ({anchor})\n"
                f"Sector context: {ecosystem}\n"
                f"Target size band: ${min_cap_musd:,.0f}M-${max_cap_musd:,.0f}M market cap, "
                "thin analyst coverage.\n\n"
                f"Search the web for small, US-listed public companies with a disclosed or "
                f"credibly reported supplier / customer / competitor relationship to "
                f"{anchor_name}. Then call report_suppliers exactly once with what you found."
            ),
        }]
        tools = [
            {"type": "web_search_20260209", "name": "web_search",
             "max_uses": MAX_SEARCHES_PER_ANCHOR},
            _REPORT_TOOL,
        ]
        for _ in range(MAX_RESUMES + 1):
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    **request_kwargs(self._model, max_tokens=12000, effort="medium"),
                    system=cacheable_system(_SYSTEM_PROMPT),
                    tools=tools,
                    messages=messages,
                )
            except Exception as exc:  # noqa: BLE001 - one bad anchor must not stop the run
                log.warning("%s: supplier research call failed: %s", anchor, exc)
                return []
            self._usage.record(response.usage.input_tokens, response.usage.output_tokens,
                               model=self._model, category=CAT_RESEARCH)
            payload = _report_payload(response)
            if payload is not None:
                return _to_suppliers(anchor, payload)
            if response.stop_reason != "pause_turn":
                log.info("%s: research returned no report_suppliers call (stop_reason=%s).",
                         anchor, response.stop_reason)
                return []
            # The server-side search loop hit its iteration limit mid-turn.
            # Re-sending the assistant turn resumes it where it stopped; do
            # NOT append a "continue" message, which the API does not expect
            # and which would derail the search.
            messages = messages[:1] + [{"role": "assistant", "content": response.content}]
        log.warning("%s: research still paused after %d resume(s) -- giving up on this anchor.",
                    anchor, MAX_RESUMES)
        return []

    async def aclose(self) -> None:
        await self._client.close()


def _report_payload(response) -> dict | None:
    """The report_suppliers input, by NAME. Not `first_tool_use`: a
    web-search turn's content is full of server_tool_use / result blocks,
    and on a resumed turn more than one tool block can appear -- picking the
    first tool call would pick a search, not the report."""
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "report_suppliers":
            return block.input
    return None


def _to_suppliers(anchor: str, payload: dict) -> list[ResearchedSupplier]:
    found = []
    for row in payload.get("suppliers") or []:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0
        found.append(ResearchedSupplier(
            anchor=anchor,
            name=name,
            # Uppercased and stripped, but never invented: an empty ticker
            # is resolved downstream against SEC's registered-filer list and
            # name-verified before anything acts on it.
            ticker=str(row.get("ticker") or "").strip().upper(),
            rel_type=str(row.get("rel_type") or "supplier"),
            description=str(row.get("description") or "")[:400],
            evidence_url=str(row.get("evidence_url") or ""),
            confidence=confidence,
        ))
    return found


def merge_into_candidates(candidates, suppliers: list[ResearchedSupplier]) -> tuple[int, int]:
    """Folds researched suppliers into the universe-candidate store the
    filing path already writes to (a JsonState), so they inherit ticker
    resolution, the market-cap screen, the tradeable/anchor recommendation
    and the dashboard's Accept button rather than needing a parallel UI.

    Returns (new, updated).

    Two deliberate asymmetries against a filing-sourced candidate:

    - NO `pending_edges`. That key is what makes an acceptance write a graph
      edge (see engine._promote_pending_edges), and a web-sourced
      relationship must never become one: an edge at or above
      DISCLOSED_LINK_CONFIDENCE satisfies the corroboration bar that fires
      trades, and this is not a disclosure. Once accepted, the symbol's own
      10-K is backfilled and a real edge is created from a primary source if
      the relationship is genuine.
    - `researched_only` is set, and `seen_count` is NOT incremented for a
      repeat sighting from research. seen_count gates auto-accept as
      TRADEABLE (auto_accept_min_seen_count), and it is meant to count
      independent FILING disclosures. Letting research inflate it would let
      a name be auto-added as a trade target on nothing but web sourcing.
    """
    new = updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for supplier in suppliers:
        key = supplier.ticker or supplier.name
        entry = dict(candidates.get(key) or {})
        is_new = not entry
        entry.setdefault("name", supplier.name)
        entry.setdefault("first_seen_at", now)
        entry.setdefault("seen_count", 1)
        if supplier.ticker and not entry.get("ticker"):
            entry["ticker"] = supplier.ticker
        related = list(entry.get("related_to") or [])
        if supplier.anchor not in related:
            related.append(supplier.anchor)
        entry["related_to"] = related
        rel_types = list(entry.get("rel_types") or [])
        if supplier.rel_type not in rel_types:
            rel_types.append(supplier.rel_type)
        entry["rel_types"] = rel_types
        if not entry.get("description"):
            entry["description"] = supplier.description
        sources = list(entry.get("sources") or [])
        if supplier.evidence_url and supplier.evidence_url not in sources:
            sources.append(supplier.evidence_url)
        entry["sources"] = sources[-8:]
        # Marks provenance for the dashboard AND for the accept guards: a
        # candidate no filing has ever named is a lead, not a disclosure.
        entry["researched_only"] = not entry.get("pending_edges")
        entry["research_confidence"] = supplier.confidence
        entry["last_researched_at"] = now
        candidates.set(key, entry)
        new, updated = (new + 1, updated) if is_new else (new, updated + 1)
    return new, updated


def format_research_report(anchor_results: list[tuple[str, list[ResearchedSupplier]]],
                           new: int, updated: int, skipped: list[str]) -> str:
    lines = ["Anchor supplier research (web) -- candidates only, no graph edges written", ""]
    for anchor, suppliers in anchor_results:
        if not suppliers:
            lines.append(f"{anchor}: nothing usable found")
            continue
        lines.append(f"{anchor}: {len(suppliers)} candidate(s)")
        for s in sorted(suppliers, key=lambda x: -x.confidence):
            lines.append(f"    {(s.ticker or '?'):<6} {s.name[:38]:<38} {s.rel_type:<10} "
                         f"conf={s.confidence:.2f}")
            lines.append(f"           {s.description[:150]}")
            if s.evidence_url:
                lines.append(f"           {s.evidence_url[:150]}")
        lines.append("")
    lines.append(f"{new} new candidate(s), {updated} updated.")
    if skipped:
        lines.append(f"{len(skipped)} anchor(s) not researched this run (capped at "
                     f"{MAX_ANCHORS_PER_RUN}): {', '.join(skipped[:20])}. Re-run to continue.")
    lines.append("")
    lines.append("These are LEADS, not disclosures. Nothing here writes a relationship edge: "
                 "accept a candidate and its own 10-K is backfilled, and the edge is created "
                 "only if a filing actually discloses the relationship.")
    return "\n".join(lines)


def researched_anchors(candidates, research_state=None) -> set[str]:
    """Anchors already covered by a previous run, so re-running continues
    through the list instead of redoing the first ten.

    Reads BOTH sources. Derived-from-candidates was the original mechanism
    and is kept so existing markers keep counting, but it can only ever see
    an anchor that produced at least one candidate: `last_researched_at` is
    written onto the SUPPLIER entries, so "searched, found nothing" -- which
    research.py's own prompt explicitly encourages as an answer -- left no
    trace anywhere. That anchor was reselected and re-billed for a paid
    web-search call on every future run, and since selection is deterministic
    (sorted by inertness, then ecosystem, then symbol) a batch of
    unproductive anchors could sit at the front of the queue forever,
    permanently starving the rest of the list. research_state records the
    attempt itself, which is the thing that actually cost money."""
    seen: set[str] = set()
    for entry in candidates.data.values():
        if entry.get("last_researched_at"):
            seen.update(entry.get("related_to") or [])
    if research_state is not None:
        seen.update(research_state.data.keys())
    return seen


def _dump(obj) -> str:  # pragma: no cover - debugging aid
    return json.dumps(obj, indent=2, default=str)
