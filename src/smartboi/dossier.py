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

from smartboi.usage import UsageTracker

log = logging.getLogger(__name__)

DIRECTIONS = ("LONG", "SHORT", "NONE")


@dataclass
class EvidenceRecord:
    evidence_id: str
    source_type: str  # "news" | "8-K" | "10-K" | "10-Q" | "4"
    source_name: str  # publisher/domain, or "SEC EDGAR (<form>)" e.g. "SEC EDGAR (8-K)"
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
    relationship_confidence: float | None = None  # the graph edge's own extracted confidence; None when not propagated
    # The exact model snapshots that produced this record. Forward-only
    # testing is this system's core validity claim (README point 7), and it
    # has one fragile edge: an LLM whose training corpus covers the period
    # being "predicted" can recall how the story ended, and this is
    # demonstrated, large, and NOT fixable by prompting -- models asked to
    # forecast from Sept-Nov 2019 earnings calls mention COVID-19 in over a
    # quarter of cases even when explicitly told not to use future
    # knowledge. So a track record is only meaningful RELATIVE TO A MODEL
    # SNAPSHOT: swapping the backbone silently resets the out-of-sample
    # clock, because the new model's cutoff may postdate part of the record.
    # Stamped per record so a model change is visible in the data rather
    # than being something you have to remember.
    scored_by_model: str = ""   # the dossier model that proposed this update
    reviewed_by_model: str = ""  # the skeptic model that adjudicated it
    # When this record was merged into the dossier -- the decay fallback
    # anchor for records whose published_at is empty/unparseable (see
    # _age_days), so a bad upstream date can't make evidence immortal.
    merged_at: str = ""
    # The updater's PRE-skeptic numbers (confidence/magnitude above are
    # post-skeptic) -- kept so the skeptic pass's actual effect on outcomes
    # is measurable instead of overwritten and lost.
    proposed_confidence: float | None = None
    proposed_magnitude: float | None = None


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
    # How many times the entry gate has actually evaluated THIS episode (see
    # engine._try_open_from_signal). Reset with the rest of the episode
    # state. Exists so the pre-gate expiry paths -- newly merged evidence and
    # the daily decay pass -- can tell "this thesis degraded after it had its
    # chance to enter" from "this thesis was killed before the gate ever saw
    # it", and only apply the strict bar to the former. See
    # engine._should_expire_unopened.
    entry_attempts: int = 0
    # Decay-weighted evidence mass for/against the resolved direction, as
    # of the last _aggregate call (see dossier.py's _side_mass) -- exposed
    # so the dashboard can show WHY a confidence is low: a small agreeing
    # mass with a large opposing mass is a genuinely contested thesis, not
    # a data problem. mass_opposing == 0 means confidence is undiscounted.
    mass_agree: float = 0.0
    mass_opposing: float = 0.0
    # Whether any non-stale agreeing evidence comes from an SEC filing
    # (source_type != "news"). News-only corroboration is softer than it
    # looks -- two outlets rewording one wire story can slip past dedup as
    # two "independent" sources -- so signals.evaluate holds news-only
    # dossiers to a higher independent-source bar. A filing is a primary
    # disclosure and can't be a syndicated rewording of a news article.
    has_filing_evidence: bool = False
    # Whether any non-stale agreeing evidence reached this dossier over a
    # STRONGLY DISCLOSED relationship edge (see DISCLOSED_LINK_CONFIDENCE)
    # -- a customer/supplier link a 10-K states outright, usually with a
    # quantified share of revenue ("GM accounted for 25% of revenues").
    #
    # This satisfies the same bar a filing does, for the same reason. The
    # elevated news-only bar defends against two outlets rewording one wire
    # story into two apparent "sources"; it was never meant to ask whether
    # the CAUSAL LINK is real. When the link comes from a primary filing
    # disclosure, that question is already answered by a primary source,
    # and demanding a third journalist instead means waiting until the
    # market has made the connection itself -- which is precisely when the
    # move is over. Confirmed live: DCO sat at 17 agreeing evidence items,
    # mass 8.88, zero opposing, over 0.85-0.95 confidence disclosed links
    # to RTX/LMT/NOC, and could not act for want of a third publisher.
    has_disclosed_link_evidence: bool = False

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

# The graph-edge confidence at which a relationship counts as DISCLOSED
# rather than inferred -- the extractor assigns this band to links a filing
# states outright, typically with a quantified share of revenue. Calibrated
# against the live graph, where the quantified concentration disclosures
# ("GM accounted for 25% of revenues", "RTX missile programs are a major
# customer relationship") sit at 0.85-0.98, while speculative or
# passing-mention links ("Google Drive is integrated with FedEx Office",
# an indirect JV competitor) sit at 0.30-0.65.
DISCLOSED_LINK_CONFIDENCE = 0.85

# Per-extra-independent-source multiplier on magnitude (see _aggregate).
# Deliberately larger than confidence's +0.1 additive step: confidence is a
# probability and saturates at 1.0 by the third source anyway, whereas
# magnitude is the size of the expected move and is where accumulated
# corroboration actually has somewhere to go.
MAGNITUDE_CORROBORATION_STEP = 0.25

# Bumped whenever a change alters how confidence/magnitude are computed from
# the same evidence. Stamped onto every daily dossier snapshot so the
# forward-validation record can be split at the boundary instead of silently
# mixing scores that mean different things -- forward data cannot be
# backfilled, and re-scoring old rows with new logic would be look-ahead.
SCORING_VERSION = 2
# Weight an evidence item keeps right at its stale cutoff, before being
# excluded entirely -- never fully zero a moment before exclusion, since
# aged corroboration is still weak signal that a persistent theme existed.
_DECAY_FLOOR = 0.15


def _age_days(record: EvidenceRecord, now: datetime) -> float:
    # published_at first, then merged_at: a record with an empty or
    # unparseable published date must still AGE (from when it was merged)
    # -- treating it as perpetually fresh made such an item immortal, so it
    # propped up the dossier's confidence forever and the decay pass could
    # never fade or expire it.
    for raw in (record.published_at, record.merged_at):
        try:
            anchor = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        return max(0.0, (now - anchor).total_seconds() / 86400)
    return 0.0  # no parseable date at all (legacy record) -- treat as fresh


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


def _side_mass(dossier: Dossier, direction: str, now: datetime) -> float:
    """Decay-weighted evidence mass for one side (LONG or SHORT): the sum
    of confidence*weight over all non-stale evidence on that side, across
    the dossier's ENTIRE history -- not just evidence agreeing with
    whatever the dossier's direction currently happens to be. This is the
    basis for both direction resolution and the contestedness discount in
    _aggregate; NONE-direction evidence never contributes to either side."""
    return sum(
        e.confidence * evidence_weight(e, now)
        for e in dossier.evidence
        if e.direction == direction and not evidence_is_stale(e, now)
    )


def _aggregate(dossier: Dossier, now: datetime) -> None:
    """Recomputes direction/confidence/magnitude/horizon_days/
    independent_source_count from dossier.evidence as of `now`, applying
    time-decay -- called both right after a new evidence item is merged
    AND periodically with no new evidence (engine.py's daily decay pass),
    so a dormant dossier's confidence keeps fading even when nothing new
    happens to land on it.

    Direction and confidence are both resolved from ACCUMULATED evidence
    mass (see _side_mass), not decided by comparing one new record against
    the current aggregate: W_long/W_short are the decay-weighted sum of
    confidence over all non-stale LONG/SHORT evidence respectively.
    Direction is whichever side has the larger mass -- so a single strong
    contrary item can no longer instantly flip direction and erase an
    accumulated majority side the way a single-item comparison would; a
    new side only wins once its own accumulated mass actually exceeds the
    old side's. On an exact tie with nonzero mass on both sides, direction
    is left unchanged (avoids churn from a razor-thin new balance); a tie
    at zero mass (no evidence, or all of it decayed to stale) resolves to
    NONE.

    Once direction is resolved, confidence starts from the STRONGEST
    agreeing item's decay-scaled confidence (max of confidence_i *
    weight_i), plus a corroboration bonus per additional independent
    agreeing source, capped at 1.0, then multiplied by
    max(0, 1 - W_opposing / W_agreeing): a contested dossier is discounted
    proportionally to how much opposing mass exists, a 50/50 split zeroes
    confidence entirely (the honest read of a genuinely contested thesis),
    and no opposition at all leaves it unchanged (factor = 1).

    Max-plus-bonus rather than an average, deliberately: an average
    (weighted or not) makes a weaker AGREEING item drag the aggregate DOWN
    -- a dossier at 0.9 that gained a 0.2-confidence corroborating item
    fell to ~0.65 under the old mean, i.e. supporting evidence moved the
    dossier AWAY from the signal threshold, inverting the system's core
    premise that accumulated corroboration is what crosses the bar. With
    max-plus-bonus, agreeing evidence can only corroborate (bonus) or be
    neutral, never penalize; decay still works because the strongest
    item's own weight fades with age (a lone 0.9 item at the decay floor
    contributes 0.9 * 0.15), and fresher items take over the max as older
    ones fade. `independent_source_count` only counts winning-side
    evidence that isn't stale. Distinct source_name values are genuinely
    independent evidence, never the same fact double-counted: for news,
    dedup.py guarantees distinct names are different publisher domains,
    not syndicated republishes of one wire story; for EDGAR, engine.py
    differentiates by filing form ("SEC EDGAR (8-K)" vs "(Form 4)" vs
    "(10-Q)") since a material event, an insider transaction, and a
    quarterly filing are independent disclosures, not restatements of each
    other. Magnitude takes the max of each agreeing item's magnitude
    scaled by its decay weight, and horizon_days their plain mean."""
    w_long = _side_mass(dossier, "LONG", now)
    w_short = _side_mass(dossier, "SHORT", now)

    if w_long > w_short:
        dossier.direction = "LONG"
    elif w_short > w_long:
        dossier.direction = "SHORT"
    elif w_long == 0.0:
        dossier.direction = "NONE"
    # else: a genuine tie with nonzero mass on both sides -- keep whatever
    # direction the dossier already had (falls through, no assignment).

    if dossier.direction not in ("LONG", "SHORT"):
        dossier.independent_source_count = 0
        dossier.confidence = 0.0
        dossier.magnitude = 0.0
        dossier.mass_agree = 0.0
        dossier.mass_opposing = 0.0
        dossier.has_filing_evidence = False
        dossier.has_disclosed_link_evidence = False
        return

    agreeing = [
        e for e in dossier.evidence
        if e.direction == dossier.direction and not evidence_is_stale(e, now)
    ]
    weighted = [(e, evidence_weight(e, now)) for e in agreeing]
    dossier.independent_source_count = len({e.source_name for e in agreeing})
    dossier.has_filing_evidence = any(e.source_type != "news" for e in agreeing)
    dossier.has_disclosed_link_evidence = any(
        (e.relationship_confidence or 0.0) >= DISCLOSED_LINK_CONFIDENCE for e in agreeing
    )
    base_confidence = max(e.confidence * w for e, w in weighted)
    corroboration_bonus = 0.1 * max(0, dossier.independent_source_count - 1)
    raw_confidence = min(1.0, base_confidence + corroboration_bonus)

    w_agree = w_long if dossier.direction == "LONG" else w_short
    w_opposing = w_short if dossier.direction == "LONG" else w_long
    contest_factor = max(0.0, 1.0 - w_opposing / w_agree)  # w_agree > 0 is guaranteed here
    dossier.confidence = raw_confidence * contest_factor
    dossier.mass_agree = w_agree
    dossier.mass_opposing = w_opposing

    # Magnitude accumulates with corroboration, exactly as confidence does.
    #
    # This used to be a bare max() -- the one aggregator provably invariant
    # to N -- while confidence got a +0.1-per-source term. That is
    # structurally incompatible with a strategy defined as accumulating
    # individually-small second-order effects: ten independent items each
    # implying a 2% move returned exactly one item's 2%. Worse, because the
    # max decays toward _DECAY_FLOOR as its strongest item ages, a dossier's
    # magnitude FELL while fresh corroboration kept landing.
    #
    # The consequence was a hard, silent ceiling. At a 0.20 signal bar, a
    # best-item magnitude of 0.25 demanded confidence 0.80; below 0.20 the
    # bar was unreachable at any confidence, forever. The live board's
    # magnitudes ran 0.10-0.40, so most of it could never signal no matter
    # how much agreeing evidence arrived. DCO carried 17 agreeing items with
    # zero opposing and 0.85-0.95 disclosed links; items 4 through 17
    # contributed nothing at all.
    #
    # Keyed to independent_source_count (distinct source_name), not raw item
    # count: dedup already collapses syndicated republishes onto one name,
    # so N restatements of a single wire story cannot inflate this.
    base_magnitude = max(e.magnitude * w for e, w in weighted)
    magnitude_bonus = 1.0 + MAGNITUDE_CORROBORATION_STEP * max(0, dossier.independent_source_count - 1)
    dossier.magnitude = min(1.0, base_magnitude * magnitude_bonus)
    dossier.horizon_days = round(sum(e.horizon_days for e in agreeing) / len(agreeing))
    # The last few agreeing items' reasoning, not just the single latest --
    # a one-sentence blurb from only the newest item is a thin "current
    # thesis" to feed back into the updater's prompt (see
    # DossierUpdater.propose_update) when several items have actually
    # accumulated; this gives it more of the accumulated picture at
    # basically no extra cost (no new LLM call, just joining text already
    # on hand).
    dossier.thesis_summary = " | ".join(e.reasoning for e in agreeing[-3:])


def recompute_decay(dossier: Dossier, now: datetime | None = None) -> None:
    """Re-scores a dossier's aggregate from its EXISTING evidence with no
    new item added -- the periodic half of time-decay (see _aggregate);
    merge_evidence is the other half, run whenever fresh evidence lands."""
    now = now or datetime.now(timezone.utc)
    _aggregate(dossier, now)
    dossier.updated_at = now.isoformat()


def merge_evidence(dossier: Dossier, record: EvidenceRecord, now: datetime | None = None) -> None:
    """Folds one accepted (post-skeptic) evidence record into the dossier's
    aggregate state. Direction and confidence are both fully resolved
    inside _aggregate from the dossier's ACCUMULATED evidence mass, not
    decided here by comparing this one new record against the current
    aggregate -- see _aggregate's docstring for why that matters."""
    now = now or datetime.now(timezone.utc)
    if not record.merged_at:
        record.merged_at = now.isoformat()
    dossier.evidence.append(record)
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
    def __init__(self, api_key: str, model: str, usage: UsageTracker):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._usage = usage

    async def propose_update(
        self, dossier: Dossier, evidence_text: str, origin_symbol: str, relationship_note: str,
        relationship_confidence: float | None = None,
    ) -> dict | None:
        if not self._usage.budget_remaining():
            log.info("%s: daily LLM call budget reached -- deferring dossier update.", dossier.symbol)
            return None
        current = (
            f"Direction={dossier.direction}, magnitude={dossier.magnitude:.2f}, "
            f"confidence={dossier.confidence:.2f}, thesis: {dossier.thesis_summary or '(none yet)'}"
            if dossier.evidence
            else "No existing thesis -- this is the first evidence item for this company."
        )
        relationship_confidence_note = (
            f" (the relationship itself is disclosed with confidence {relationship_confidence:.2f}, "
            "extracted from a filing -- weigh how directly it connects the two companies)"
            if relationship_confidence is not None
            else ""
        )
        propagation = (
            f"This evidence is about a LINKED company ({origin_symbol}), not {dossier.symbol} "
            f"directly. Relationship: {relationship_note}{relationship_confidence_note}"
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
                # Pinned to 0: this call's confidence/magnitude directly
                # gate trades at a hard threshold, and the API default of
                # 1.0 made every score a single high-temperature sample --
                # some threshold crossings were sampling noise, not evidence.
                temperature=0,
                system=_SYSTEM_PROMPT,
                tools=[_UPDATE_TOOL],
                tool_choice={"type": "tool", "name": "update_thesis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad API call kill the ingestion loop
            log.warning("%s: dossier update proposal failed: %s", dossier.symbol, exc)
            return None
        self._usage.record(response.usage.input_tokens, response.usage.output_tokens)
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        return None

    async def aclose(self) -> None:
        await self._client.close()
