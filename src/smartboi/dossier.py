"""Per-company accumulated thesis ("dossier"): the trading signal is a
change in accumulated, corroborated evidence crossing a threshold, not any
single article -- see README point 3. Each new evidence item (direct, or
propagated across the relationship graph -- see graph.py) proposes an
update to the company's dossier; skeptic.py then tries to refute it before
it's allowed to move the aggregate confidence (see merge_evidence)."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

DIRECTIONS = ("LONG", "SHORT", "NONE")


@dataclass
class EvidenceRecord:
    evidence_id: str
    source_type: str  # "news" | "8-K" | "10-K" | "4"
    source_name: str  # publisher/domain, or "SEC EDGAR"
    url: str
    headline: str
    published_at: str
    origin_symbol: str  # the company the evidence item is literally about
    is_propagated: bool  # True if origin_symbol != this dossier's symbol (arrived via the graph)
    relationship_note: str  # e.g. "AMAT is a customer of UCTT" -- empty when not propagated
    direction: str
    magnitude: float
    confidence: float  # post-skeptic
    horizon_days: int
    reasoning: str
    skeptic_note: str


@dataclass
class Dossier:
    symbol: str
    direction: str = "NONE"
    magnitude: float = 0.0
    confidence: float = 0.0
    horizon_days: int = 0
    thesis_summary: str = ""
    evidence: list[EvidenceRecord] = field(default_factory=list)
    independent_source_count: int = 0
    status: str = "ACTIVE"  # ACTIVE | SIGNALED
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class DossierStore:
    def __init__(self, dir_path: Path):
        self.dir_path = dir_path
        self.dir_path.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.dir_path / f"{symbol}.json"

    def load(self, symbol: str) -> Dossier:
        path = self._path(symbol)
        if not path.exists():
            return Dossier(symbol=symbol)
        try:
            raw = json.loads(path.read_text())
            raw["evidence"] = [EvidenceRecord(**e) for e in raw.get("evidence", [])]
            return Dossier(**raw)
        except (json.JSONDecodeError, OSError, TypeError):
            log.warning("Could not read dossier for %s, starting fresh.", symbol)
            return Dossier(symbol=symbol)

    def save(self, dossier: Dossier) -> None:
        path = self._path(dossier.symbol)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dossier.to_dict(), indent=2))
        tmp.replace(path)

    def all_symbols(self) -> list[str]:
        return sorted(p.stem for p in self.dir_path.glob("*.json"))


def has_evidence(dossier: Dossier, evidence_id: str) -> bool:
    """Whether this evidence item was already merged -- makes reprocessing
    idempotent when an ingestion pass is retried after a partial failure
    (see engine.py, which only marks an item done once every affected
    dossier has definitively handled it)."""
    return any(e.evidence_id == evidence_id for e in dossier.evidence)


def merge_evidence(dossier: Dossier, record: EvidenceRecord) -> None:
    """Folds one accepted (post-skeptic) evidence record into the dossier's
    aggregate state.

    Direction only changes when the new record's confidence beats the
    existing aggregate -- a single weak item can't flip a thesis built on
    stronger evidence. `independent_source_count` is recomputed here from
    the evidence agreeing with the dossier's RESOLVED direction (never the
    new record's, which may disagree and must not corrupt the count the
    signal gate reads; dedup.py guarantees distinct source names are
    genuinely different domains/stories, never syndicated republishes).
    Aggregate confidence is the mean confidence of agreeing evidence,
    boosted for corroboration from distinct sources and capped at 1.0.
    Magnitude takes the max of agreeing evidence (the biggest single
    implied impact, not diluted by weaker corroborating items) and
    horizon_days their mean."""
    dossier.evidence.append(record)

    if record.direction != "NONE" and (
        dossier.direction == "NONE" or record.confidence >= dossier.confidence
    ):
        dossier.direction = record.direction

    agreeing = [e for e in dossier.evidence if e.direction == dossier.direction]
    if dossier.direction != "NONE" and agreeing:
        dossier.independent_source_count = len({e.source_name for e in agreeing})
        base_confidence = sum(e.confidence for e in agreeing) / len(agreeing)
        corroboration_bonus = 0.1 * max(0, dossier.independent_source_count - 1)
        dossier.confidence = min(1.0, base_confidence + corroboration_bonus)
        dossier.magnitude = max(e.magnitude for e in agreeing)
        dossier.horizon_days = round(sum(e.horizon_days for e in agreeing) / len(agreeing))
        dossier.thesis_summary = record.reasoning
    dossier.updated_at = datetime.now(timezone.utc).isoformat()


_UPDATE_TOOL = {
    "name": "update_thesis",
    "description": "Propose how this new evidence item should update the company's trading thesis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_new_information": {
                "type": "boolean",
                "description": "False if this is stale/rehashed news or immaterial to the thesis -- if false, all other fields are ignored.",
            },
            "direction": {"type": "string", "enum": list(DIRECTIONS)},
            "magnitude": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "How large a price impact this evidence plausibly implies (0=negligible, 1=major re-rating).",
            },
            "confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "How confident you are in this specific evidence item's read, in isolation.",
            },
            "horizon_days": {
                "type": "integer", "minimum": 1, "maximum": 60,
                "description": "Plausible number of days for this to be reflected in price, given how directly/indirectly it affects this company.",
            },
            "reasoning": {"type": "string", "description": "One or two sentences."},
        },
        "required": ["is_new_information", "direction", "magnitude", "confidence", "horizon_days", "reasoning"],
    },
}

_SYSTEM_PROMPT = (
    "You maintain a trading thesis for one company, built up from many small pieces of "
    "evidence over time rather than reacting to any single headline. You will be given "
    "the company's CURRENT thesis (if any) and ONE new piece of evidence -- which may be "
    "about the company directly, or about a linked company (its customer, supplier, "
    "competitor, or regulator; you'll be told which). Second-order evidence about a "
    "linked company is often the more interesting case: the market reprices the linked "
    "company's news within minutes but rarely connects it to this one for days or weeks "
    "-- that lag is the opportunity this thesis exists to capture. Weigh how directly the "
    "evidence bears on THIS company: direct news usually implies a shorter horizon and "
    "higher confidence than propagated news. Be conservative -- most news is noise, most "
    "single articles should not flip an established thesis, and vague or promotional "
    "language deserves low confidence."
)


class DossierUpdater:
    def __init__(self, api_key: str, model: str):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def propose_update(
        self, dossier: Dossier, evidence_text: str, origin_symbol: str, relationship_note: str
    ) -> dict | None:
        current = (
            f"Direction={dossier.direction}, magnitude={dossier.magnitude:.2f}, "
            f"confidence={dossier.confidence:.2f}, thesis: {dossier.thesis_summary or '(none yet)'}"
            if dossier.evidence
            else "No existing thesis -- this is the first evidence item for this company."
        )
        propagation = (
            f"This evidence is about a LINKED company ({origin_symbol}), not {dossier.symbol} "
            f"directly. Relationship: {relationship_note}"
            if relationship_note
            else f"This evidence is about {dossier.symbol} directly."
        )
        prompt = (
            f"Company: {dossier.symbol}\n"
            f"Current thesis: {current}\n"
            f"{propagation}\n\n"
            f"New evidence:\n{evidence_text}"
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system=_SYSTEM_PROMPT,
                tools=[_UPDATE_TOOL],
                tool_choice={"type": "tool", "name": "update_thesis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad API call kill the ingestion loop
            log.warning("%s: dossier update proposal failed: %s", dossier.symbol, exc)
            return None
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        return None

    async def aclose(self) -> None:
        await self._client.close()
