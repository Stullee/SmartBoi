"""Cross-company relationship graph: customer/supplier/competitor/regulator
edges between companies in the universe (including the "anchor" companies
from universe.py -- large names that are never trade targets but whose news
is exactly the kind of headline that should propagate). This is the
mechanism for README point 2 ("trade second-order effects, not headlines"):
a piece of news about company A gets propagated to every company B with an
edge to/from A, so the dossier engine asks "who else does this affect" on
every new item instead of only reacting to news that names the trade target
directly."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from anthropic import AsyncAnthropic

from smartboi.llm import cacheable_system, first_tool_use, request_kwargs
from smartboi.state import atomic_write_json, quarantine_corrupt_file
from smartboi.usage import CAT_EXTRACTION, UsageTracker

log = logging.getLogger(__name__)

REL_TYPES = ("customer", "supplier", "competitor", "regulator")


@dataclass(frozen=True)
class Relationship:
    from_symbol: str  # the company the evidence is about
    to_symbol: str  # the company the relationship links it to
    rel_type: str  # one of REL_TYPES, describing what to_symbol IS to from_symbol
    description: str
    source: str  # "manual seed" or a filing citation/URL
    confidence: float
    extracted_at: str = ""


@dataclass
class RelationshipGraph:
    path: Path
    relationships: list[Relationship] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                loaded = [Relationship(**r) for r in raw]
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                # Quarantined rather than silently discarded: the next _save()
                # would overwrite the original, and the graph is expensive to
                # rebuild (a full-universe re-extraction). See state.py.
                quarantine_corrupt_file(self.path, exc)
                return
            # Self-healing cleanup: an invalid rel_type could only have
            # gotten in from before engine.py's _extract_relationships
            # started guarding against it (the LLM's tool schema declares
            # an enum, but Anthropic tool use doesn't hard-enforce it, so a
            # stray value could slip through). Dropped on load rather than
            # left to silently mismatch REL_TYPES-based logic elsewhere.
            self.relationships = [r for r in loaded if r.rel_type in REL_TYPES]
            dropped = len(loaded) - len(self.relationships)
            if dropped:
                log.warning(
                    "Dropped %d relationship(s) with an invalid rel_type on load: %s",
                    dropped,
                    ", ".join(f"{r.from_symbol}->{r.to_symbol} ({r.rel_type!r})" for r in loaded if r.rel_type not in REL_TYPES),
                )
                self._save()

    def _save(self) -> None:
        atomic_write_json(self.path, [asdict(r) for r in self.relationships], indent=2)

    def add(self, rel: Relationship) -> bool:
        """Adds a new (from, to, rel_type) edge, or UPGRADES an existing one
        when the new extraction is stronger. Returns True when a new edge was
        added or an existing one raised to higher confidence; False when the
        edge already existed at >= this confidence.

        Upgrade-on-stronger fixes a real trap: a weak passing-mention edge
        (say 0.55) extracted first would otherwise PERMANENTLY block a later
        quantified-concentration disclosure ("GM accounted for 25% of net
        sales", 0.95) from ever raising it -- and DISCLOSED_LINK_CONFIDENCE
        (0.85, see dossier.py) gates has_disclosed_link_evidence, which decides
        the corroboration bar signals.evaluate applies. The strongest
        structural edge in the system could be held below its own gate forever
        by whichever extraction happened to run first.

        extracted_at is refreshed on EVERY re-confirmation (even a weaker one),
        without downgrading the stored confidence/description/source, so a
        relationship re-seen in a newer filing ages from the newer date and an
        edge that STOPS appearing in filings can be surfaced as stale rather
        than looking freshly confirmed forever (see status.gather_graph_health).
        graph.json stays plain JSON a human can review and hand-edit."""
        for i, existing in enumerate(self.relationships):
            if (
                existing.from_symbol == rel.from_symbol
                and existing.to_symbol == rel.to_symbol
                and existing.rel_type == rel.rel_type
            ):
                if rel.confidence > existing.confidence:
                    # Stronger disclosure supersedes the weaker one wholesale
                    # (confidence, description, source and the fresh stamp).
                    self.relationships[i] = rel
                    self._save()
                    return True
                # Equal-or-weaker: keep the stronger substance, but record that
                # the relationship was re-confirmed now -- the aging anchor.
                if rel.extracted_at and rel.extracted_at != existing.extracted_at:
                    self.relationships[i] = replace(existing, extracted_at=rel.extracted_at)
                    self._save()
                return False
        self.relationships.append(rel)
        self._save()
        return True

    def linked_symbols(self, symbol: str, universe: set[str]) -> list[tuple[str, Relationship]]:
        """Every other in-universe company with an edge to/from `symbol`,
        paired with the relationship connecting them -- both directions,
        since a supplier's news matters to its customer just as much as the
        other way around."""
        out = []
        for rel in self.relationships:
            if rel.from_symbol == symbol and rel.to_symbol in universe:
                out.append((rel.to_symbol, rel))
            elif rel.to_symbol == symbol and rel.from_symbol in universe:
                out.append((rel.from_symbol, rel))
        return out


_EXTRACTION_TOOL = {
    "name": "report_relationships",
    "description": (
        "Report business relationships (customer, supplier, competitor, regulator) "
        "between the filing company and other named companies, found in this filing text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "relationships": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "counterparty_name": {"type": "string"},
                        "counterparty_ticker": {
                            "type": ["string", "null"],
                            "description": (
                                "Stock ticker if you recognize the counterparty from the provided list, "
                                "or its public ticker if you know it confidently even though it's not on "
                                "the list (used to propose new watchlist candidates). Null for private "
                                "companies or when unsure -- never guess a ticker."
                            ),
                        },
                        "rel_type": {"type": "string", "enum": list(REL_TYPES)},
                        "description": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "quote": {"type": "string", "description": "Verbatim snippet from the filing supporting this."},
                    },
                    "required": [
                        "counterparty_name", "counterparty_ticker", "rel_type",
                        "description", "confidence", "quote",
                    ],
                },
            }
        },
        "required": ["relationships"],
    },
}

_SYSTEM_PROMPT = (
    "You extract business relationships from SEC filing text: which named companies "
    "are customers, suppliers, competitors, or regulators of the filing company. Only "
    "report relationships explicitly stated or clearly implied by the text (e.g. "
    "'our largest customer, X Corp, accounted for 22% of revenue'), never speculation. "
    "Match counterparty names to the provided ticker list only when you're confident; "
    "otherwise leave counterparty_ticker null. Skip vague or immaterial mentions -- only "
    "relationships with clear business/financial significance. Do NOT report a company's "
    "LENDERS, underwriters or auditors as suppliers -- a credit agreement is a real "
    "disclosure, but a bank's news has no path to the borrower's fundamentals, which is "
    "the only thing this graph is used for. A financial institution should only appear when "
    "it is a genuine CUSTOMER or competitor of the filing company.\n\n"
    # `regulator` was being used as a catch-all for any legal or structural
    # relationship the model could not fit into the other three. Measured on
    # the live graph, ALL THREE regulator edges were corporate structure, not
    # regulation: a spinoff parent (CARR->RTX, 0.95) and two separation-
    # agreement indemnification links (CTVA->DD, CTVA->DOW). None is a
    # regulator. That matters beyond tidiness -- a relationship's TYPE now
    # decides whether it can satisfy the disclosed-link corroboration bar
    # (see dossier._link_type_corroborates), so a mislabeled type is a
    # mislabeled corroboration, and it makes the type unusable for the thing
    # it is named after.
    "The four types are business relationships, not a catch-all for any legal connection. "
    "`regulator` means a GOVERNMENT OR STATUTORY BODY whose rules, approvals or enforcement "
    "bear on the filing company (FDA, EPA, FAA, BIS, FERC, a state utility commission). "
    "It is NOT for corporate structure: a former parent, a spinoff, a joint-venture partner, "
    "a predecessor entity, or a counterparty to a separation, indemnification, merger or "
    "transition-services agreement is NONE of these four types -- omit it entirely rather "
    "than forcing it into the closest-looking one. An omitted relationship costs this system "
    "nothing; a wrong one is propagated as if it were a causal channel."
)


class RelationshipExtractor:
    def __init__(self, api_key: str, model: str, usage: UsageTracker):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._usage = usage

    async def extract(
        self, filing_symbol: str, filing_form: str, filing_text: str, known_tickers: list[str]
    ) -> list[dict] | None:
        """Returns None (retry later, same as engine.py's other transient-
        failure paths) on an API error OR when the daily LLM call budget
        (usage.py) is exhausted -- distinct from returning [] (genuinely no
        relationships found), which must not be treated as a reason to
        retry."""
        if not filing_text.strip():
            return []
        if not self._usage.budget_remaining(CAT_EXTRACTION):
            log.info("%s: daily LLM call budget reached -- deferring relationship extraction.", filing_symbol)
            return None
        prompt = (
            f"Filing company: {filing_symbol} ({filing_form})\n"
            f"Known tickers you may match counterparties to: {', '.join(sorted(known_tickers))}\n\n"
            f"Filing text (truncated):\n{filing_text}"
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                # Model-appropriate thinking/effort/temperature (see llm.py).
                # Effort is "medium" rather than "high" here: extraction is
                # by far the largest input-token consumer in the system (a
                # 150k-char filing per call) and is a reading task, not a
                # judgement one -- it does not gate a trade the way the
                # dossier and skeptic passes do.
                **request_kwargs(self._model, max_tokens=8000, effort="medium"),
                system=cacheable_system(_SYSTEM_PROMPT),
                tools=[_EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "report_relationships"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad API call kill the ingestion loop
            self._usage.note_failure(exc)
            log.warning("%s: relationship extraction failed: %s", filing_symbol, exc)
            return None
        self._usage.record(response.usage.input_tokens, response.usage.output_tokens,
                           model=self._model, category=CAT_EXTRACTION)
        payload = first_tool_use(response)
        if payload is None:
            return []
        relationships = payload.get("relationships", [])
        # The tool schema says this is an array; the model does not always
        # agree, and when it hands back a STRING the caller's `for rel in
        # relationships` walks it one character at a time. Every character is
        # a non-dict, so every character logs its own "non-object entry"
        # warning: measured live, 7,618 warnings from three filings, which is
        # both a log storm and a completely illegible way to say "this call
        # produced nothing". The caller's per-element guard is still right for
        # a list holding one bad entry -- this is the container being wrong,
        # which is a different failure and belongs where the contract is
        # declared.
        if not isinstance(relationships, list):
            log.warning(
                "%s: relationship extraction returned %s for 'relationships', not a list -- "
                "discarding this response. The call is paid for either way; the filing is "
                "retried on the next poll.",
                filing_symbol, type(relationships).__name__,
            )
            return []
        return relationships

    async def aclose(self) -> None:
        await self._client.close()
