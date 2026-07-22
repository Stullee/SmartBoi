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
    source_type: str  # "news" | "8-K" | "10-K" | "10-Q" | "4"
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
    # Snapshot taken the moment this dossier last flipped to SIGNALED --
    # used at entry time to check whether the price has already moved
    # ("are we too late") and to expire a signal that never got a chance to
    # enter (see engine.py's _try_open_from_signal / _expire_signal).
    # Blank/None whenever status == ACTIVE.
    signaled_at: str = ""
    signaled_price: float | None = None
    signaled_direction: str = ""
    drift_alert_sent: bool = False

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


# Evidence stops contributing once its age exceeds this many multiples of
# its OWN predicted horizon_days -- by then either the market already
# reacted (thesis proved out, so the item is priced in and no longer a
# reason to enter fresh) or the predicted horizon passed with nothing
# happening (thesis didn't pan out). Floored so a short-horizon item (e.g.
# 1 day) isn't discarded almost immediately.
_STALE_HORIZON_MULTIPLE = 2
_MIN_STALE_DAYS = 14
# Weight an evidence item keeps right at its stale cutoff, before being
# excluded entirely -- never fully zero a moment before exclusion, since
# aged corroboration is still weak signal that a persistent theme existed.
_DECAY_FLOOR = 0.15


def _age_days(record: EvidenceRecord, now: datetime) -> float:
    try:
        published = datetime.fromisoformat(record.published_at)
    except ValueError:
        return 0.0  # unparseable published_at -- treat as fresh rather than discard the item
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return max(0.0, (now - published).total_seconds() / 86400)


def _stale_cutoff_days(record: EvidenceRecord) -> float:
    return max(record.horizon_days * _STALE_HORIZON_MULTIPLE, _MIN_STALE_DAYS)


def evidence_is_stale(record: EvidenceRecord, now: datetime) -> bool:
    """True once an item has aged well past its own predicted horizon
    without the dossier's thesis ever being confirmed or invalidated by a
    price move -- see the module-level constants above. Stale evidence is
    excluded from the aggregate entirely, not merely down-weighted."""
    return _age_days(record, now) > _stale_cutoff_days(record)


def evidence_weight(record: EvidenceRecord, now: datetime) -> float:
    """1.0 while an item is still within its own predicted horizon (it
    hasn't had a chance to prove out yet); decays linearly to
    `_DECAY_FLOOR` by the point it's considered stale (see
    evidence_is_stale) -- this is what keeps an old article from propping
    up a dossier's confidence forever."""
    age = _age_days(record, now)
    horizon = max(record.horizon_days, 1)
    if age <= horizon:
        return 1.0
    cutoff = _stale_cutoff_days(record)
    if age >= cutoff:
        return _DECAY_FLOOR
    fraction = (age - horizon) / (cutoff - horizon)
    return 1.0 - fraction * (1.0 - _DECAY_FLOOR)


def _aggregate(dossier: Dossier, now: datetime) -> None:
    """Recomputes confidence/magnitude/horizon_days/independent_source_count
    from dossier.evidence as of `now`, applying time-decay -- called both
    right after a new evidence item is merged AND periodically with no new
    evidence (engine.py's daily decay pass), so a dormant dossier's
    confidence keeps fading even when nothing new happens to land on it.

    `independent_source_count` only counts evidence agreeing with the
    dossier's RESOLVED direction that ISN'T stale (dedup.py guarantees
    distinct source names are genuinely different domains, never
    syndicated republishes). Confidence is the mean of each agreeing item's
    OWN confidence scaled by its own decay weight -- scaling before
    averaging, not weighting the average itself, matters: a weighted mean
    is scale-invariant when there's a single item (the weight cancels), so
    a lone aging item would never fade if the weight were only applied as
    an averaging factor. Boosted for corroboration from distinct sources
    and capped at 1.0. Magnitude takes the max of each agreeing item's
    magnitude scaled the same way, and horizon_days their plain mean."""
    agreeing = [
        e for e in dossier.evidence
        if e.direction == dossier.direction and not evidence_is_stale(e, now)
    ]
    if dossier.direction == "NONE" or not agreeing:
        dossier.independent_source_count = 0
        dossier.confidence = 0.0
        dossier.magnitude = 0.0
        return

    weighted = [(e, evidence_weight(e, now)) for e in agreeing]
    dossier.independent_source_count = len({e.source_name for e in agreeing})
    base_confidence = sum(e.confidence * w for e, w in weighted) / len(weighted)
    corroboration_bonus = 0.1 * max(0, dossier.independent_source_count - 1)
    dossier.confidence = min(1.0, base_confidence + corroboration_bonus)
    dossier.magnitude = max(e.magnitude * w for e, w in weighted)
    dossier.horizon_days = round(sum(e.horizon_days for e in agreeing) / len(agreeing))
    dossier.thesis_summary = agreeing[-1].reasoning


def recompute_decay(dossier: Dossier, now: datetime | None = None) -> None:
    """Re-scores a dossier's aggregate from its EXISTING evidence with no
    new item added -- the periodic half of time-decay (see _aggregate);
    merge_evidence is the other half, run whenever fresh evidence lands."""
    now = now or datetime.now(timezone.utc)
    _aggregate(dossier, now)
    dossier.updated_at = now.isoformat()


def merge_evidence(dossier: Dossier, record: EvidenceRecord, now: datetime | None = None) -> None:
    """Folds one accepted (post-skeptic) evidence record into the dossier's
    aggregate state (see _aggregate for the actual aggregation/decay math).

    Direction only changes when the new record's confidence beats the
    existing aggregate -- a single weak item can't flip a thesis built on
    stronger evidence."""
    now = now or datetime.now(timezone.utc)
    dossier.evidence.append(record)

    if record.direction != "NONE" and (
        dossier.direction == "NONE" or record.confidence >= dossier.confidence
    ):
        dossier.direction = record.direction

    _aggregate(dossier, now)
    dossier.updated_at = now.isoformat()


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
