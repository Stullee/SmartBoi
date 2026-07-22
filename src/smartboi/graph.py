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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from anthropic import AsyncAnthropic

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
                self.relationships = [Relationship(**r) for r in raw]
            except (json.JSONDecodeError, OSError, TypeError):
                log.warning("Could not read %s, starting with an empty graph.", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in self.relationships], indent=2))
        tmp.replace(self.path)

    def add(self, rel: Relationship) -> bool:
        """Returns False (no-op) if an equivalent edge already exists,
        checked on (from, to, rel_type) only -- a re-extraction from a newer
        filing just skips instead of duplicating. It doesn't refresh an
        existing edge's description/confidence; graph.json is plain JSON a
        human can review and hand-edit if an extracted edge is low quality."""
        for existing in self.relationships:
            if (
                existing.from_symbol == rel.from_symbol
                and existing.to_symbol == rel.to_symbol
                and existing.rel_type == rel.rel_type
            ):
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
    "relationships with clear business/financial significance."
)


class RelationshipExtractor:
    def __init__(self, api_key: str, model: str):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def extract(
        self, filing_symbol: str, filing_form: str, filing_text: str, known_tickers: list[str]
    ) -> list[dict]:
        if not filing_text.strip():
            return []
        prompt = (
            f"Filing company: {filing_symbol} ({filing_form})\n"
            f"Known tickers you may match counterparties to: {', '.join(sorted(known_tickers))}\n\n"
            f"Filing text (truncated):\n{filing_text}"
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2000,
                system=_SYSTEM_PROMPT,
                tools=[_EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "report_relationships"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad API call kill the ingestion loop
            log.warning("%s: relationship extraction failed: %s", filing_symbol, exc)
            return []
        for block in response.content:
            if block.type == "tool_use":
                return block.input.get("relationships", [])
        return []

    async def aclose(self) -> None:
        await self._client.close()
