"""Per-company accumulated thesis ("dossier"): the trading signal is a
change in accumulated, corroborated evidence crossing a threshold, not any
single article -- see README point 3. Each new evidence item (direct, or
propagated across the relationship graph -- see graph.py) proposes an
update to the company's dossier; skeptic.py then tries to refute it before
it's allowed to move the aggregate confidence (see merge_evidence)."""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from anthropic import AsyncAnthropic

from smartboi.llm import LLMTrace, cacheable_system, first_tool_use, request_kwargs
from smartboi.state import atomic_write_json, quarantine_corrupt_file
from smartboi.usage import CAT_DOSSIER, CAT_SYNTHESIS, UsageTracker

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
    # A short canonical label for the UNDERLYING EVENT this item reports --
    # the thing that happened, not the article about it. Assigned per item by
    # the updater, which is shown the labels already on the dossier so it
    # reuses one when the event is the same.
    #
    # This exists because no metadata field can tell corroboration from
    # repetition, and the difference is the whole strategy. Measured on the
    # live board: DCO's evidence arrived from RTX, LMT and NOC -- three
    # defense_tier2 anchors, one window, which the whole-body pass read as
    # SEVEN OR EIGHT distinct facts. BWEN's arrived from MSFT, META and EQIX
    # -- three grid_datacenter anchors, one window, which the same pass read
    # as essentially ONE. Publisher, origin symbol, ecosystem, date and edge
    # type are identical in shape across those two cases. Only the fact
    # separates them, so the fact has to be recorded.
    fact_key: str = ""
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
    # Which KIND of graph edge this item arrived over ("customer" /
    # "supplier" / "competitor" / "regulator"), empty for direct evidence and
    # for records written before this field existed. Carried because
    # relationship_confidence alone cannot answer whether a link is a causal
    # TRANSMISSION CHANNEL: the live graph is 42% competitor edges (448 of
    # 1066) with 84% of them at or above DISCLOSED_LINK_CONFIDENCE -- a
    # higher clearance rate than customer (75%) or supplier (76%) -- so the
    # most numerous and most sign-ambiguous class was also the one most
    # likely to relax the corroboration bar. See has_disclosed_link_evidence.
    relationship_type: str = ""


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
    # Price the moment the thesis FIRST pointed this direction (see
    # engine._capture_inception) -- the earlier baseline the entry-gate drift
    # guard prefers over signaled_price. Corroboration accumulates over days,
    # so measuring "are we too late" only from signal-fire misses the move that
    # happened WHILE we waited. Re-captured on a direction flip; cleared when a
    # signal expires; None when no price feed was reachable at inception (the
    # guard then falls back to signaled_price).
    inception_price: float | None = None
    inception_at: str = ""
    inception_direction: str = ""
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
    # --- Whole-evidence-body synthesis (see DossierSynthesizer), refreshed
    # once a day by the decay pass. These are recorded even when the verdict
    # changes nothing, so the pass's actual effect on outcomes is measurable
    # from the forward record rather than being invisible.
    synthesis_at: str = ""
    synthesis_confidence: float = 0.0
    synthesis_magnitude: float = 0.0
    # How many genuinely DISTINCT underlying facts the evidence represents,
    # as opposed to how many items were scored. Ten articles about one
    # contract award are one fact; the arithmetic aggregate cannot tell the
    # difference and this is the number that says so.
    distinct_fact_count: int = 0
    # Whether the market has plainly already made this connection. The whole
    # strategy is trading the lag BEFORE it does, so this is a veto.
    already_priced_in: bool = False
    # Whether the evidence body collapses to far fewer facts than items --
    # the overlap finding, held SEPARATELY from already_priced_in.
    #
    # They were one field, and the conflation was doing real damage. Across
    # three live days every one of 77 vetoes came back "already priced in",
    # and the verdict text was overwhelmingly about duplication rather than
    # about the market: "one fact repeated across seven counterparties",
    # "nearly all 50 items reduce to one restated macro fact". Those are two
    # different claims with two different remedies -- overlap means the
    # arithmetic overcounted and should be TRIMMED to the honest number,
    # while priced-in means the move is gone and there is nothing to trade.
    #
    # Keeping them apart matters most for falsification. already_priced_in
    # arms _veto_refuted_by_price, which watches the tape for a move that
    # would disprove "the market has absorbed this". Stamped on a duplication
    # finding, that test is checking a hypothesis the model never advanced --
    # which is why, against those 77 vetoes, the re-judge path fired exactly
    # once in three days.
    redundant_evidence: bool = False
    synthesis_note: str = ""
    synthesis_catalyst: str = ""
    # --- What the verdict above was judged AGAINST, so it can be falsified
    # rather than merely re-asserted. A verdict is a claim about a moment: a
    # price ("the market has absorbed this") and a body of evidence ("this is
    # all there is"). Both move. Without a record of either, the claim is
    # unfalsifiable by construction -- which is what it was. ---
    #
    # The price at the instant the verdict was rendered (engine._price_bar,
    # same IB-then-Finnhub fallback the inception baseline uses). None when
    # no source could price the symbol, which leaves the verdict standing --
    # the fail-safe direction.
    synthesis_price: float | None = None
    synthesis_price_at: str = ""
    # The independence_key values present when the verdict was rendered.
    # Compared against the current set to detect that the evidence BODY has
    # materially changed since -- counting KEYS, not items, so an
    # ecosystem-association fan-out (which mints one collective slot, and
    # previously none) cannot manufacture invalidation. See
    # engine._synthesis_premises_changed.
    synthesis_keys: list[str] = field(default_factory=list)
    # The arithmetic score (confidence * magnitude) as it stood BEFORE the
    # synthesis verdict capped or vetoed it. Every vetoed row recorded 0.000
    # for both the raw and the capped number, so a thesis the whole-body pass
    # rated 0.9-but-priced-in was indistinguishable from one it rated 0.05,
    # permanently and for the most expensive pass in the system.
    pre_synthesis_score: float = 0.0
    # confidence * magnitude as the arithmetic aggregate computed it with NO
    # fact cap applied (see effective_corroboration_count). Recorded on every
    # _aggregate; never used as a score, only to SCHEDULE the whole-body pass
    # and to record what it capped.
    #
    # Without it the scheduling is circular, and the circle fails open. The
    # decay pass only synthesises a dossier whose score clears
    # signal_confidence_threshold * synthesis_score_floor_pct, so once the
    # cap pushed a dossier under that floor the verdict stopped being
    # refreshed -- and 36h later it went stale, the cap lapsed, and the score
    # sprang back to the uncapped arithmetic. Reproduced: eight channels
    # carrying one distinct fact at base 0.60/0.45 sit at 0.622 uncapped and
    # 0.270 capped, so the dossier alternated between 0.270 and 0.622 forever,
    # and every time it was at 0.622 it was ABOVE the 0.5 signal bar and could
    # fire the trade on exactly the arithmetic the whole-body pass had
    # rejected. Gating on this field instead keeps the verdict refreshed, so
    # the cap holds continuously.
    arithmetic_score: float = 0.0
    # --- Per-row attribution flags. Three changes in this scoring version
    # move scores in the same region and the same direction; recorded per
    # dossier so the forward-return series can be bucketed by WHICH
    # mechanism touched a row, instead of pooling them behind one version
    # boundary the way SCORING_VERSION 5 had to. ---
    veto_falsified_by_price: bool = False
    synthesis_stale_evidence: bool = False
    ecosystem_slot_counted: bool = False

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
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            # A dossier is the permanent record of accumulated evidence, so a
            # corrupt one is quarantined (not silently overwritten by the next
            # save) and the loss is logged loudly rather than reported as a
            # routine "starting fresh".
            quarantine_corrupt_file(path, exc)
            return Dossier(symbol=symbol)

    def save(self, dossier: Dossier) -> None:
        atomic_write_json(self._path(dossier.symbol), dossier.to_dict(), indent=2)

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

def _keys_of(contributing: list[EvidenceRecord]) -> set[str]:
    """The independent-source slots a set of contributing evidence claims --
    the single definition of that accounting, so nothing can drift from it.

    NON-SHATTERING: a fact label may MERGE items the channel key kept apart,
    never split ones it had already merged. Whichever partition yields fewer
    slots wins.

    The fact key (SCORING_VERSION 8) was introduced to collapse one event
    restated across many outlets into a single slot. Measured on the first
    labelled cohort -- 108 items, everything merged after the 2026-08-13
    cutover -- it did the opposite: 92 slots against the channel key's 64, a
    +43.8% SPLIT, with 13 dossiers splitting, 18 holding and not one
    collapsing. Two model-written labels for one underlying event ("ford
    phases out china lincoln imports" and "ford increases lincoln us
    production 2030", from a single announcement) mint two slots where the
    channel key minted one, and the corroboration bonus is convex in the slot
    count, so every such split inflates the score.

    Taking the smaller partition keeps the mechanism's upside -- a genuine
    merge still counts once -- while making its failure mode unreachable. It
    can only ever LOWER a slot count relative to today, never raise one, so no
    dossier can start signalling because of this."""
    fact = {
        _ECOSYSTEM_SLOT_KEY if is_ecosystem_association(e) else independence_key(e)
        for e in contributing
    }
    channel = {
        _ECOSYSTEM_SLOT_KEY if is_ecosystem_association(e) else channel_key(e)
        for e in contributing
    }
    return fact if len(fact) <= len(channel) else channel


def slot_keys(dossier: Dossier, now: datetime) -> set[str]:
    """The dossier's current independent-source slots, recomputed from raw
    evidence exactly as _aggregate counts them.

    Exists so the synthesis-invalidation trigger (engine._maybe_resynthesize)
    can ask "has the evidence BODY materially changed since the verdict" in
    the same currency the signal bar is denominated in. Counting keys rather
    than items is what makes that trigger ungameable: an ecosystem fan-out of
    thirty correlated macro items mints one key no matter how many arrive, and
    three wire rewrites of one story mint one key (with dedup dropping two
    before they ever get here). Nothing that cannot move the signal bar can
    invalidate a verdict."""
    agreeing = [
        e for e in dossier.evidence
        if e.direction == dossier.direction and not evidence_is_stale(e, now)
    ]
    weighted = [(e, evidence_weight(e, now)) for e in agreeing]
    contributing = [e for e, w in weighted if e.confidence * w >= MIN_SOURCE_CONTRIBUTION]
    return _keys_of(contributing)


def _link_type_corroborates(record: EvidenceRecord) -> bool:
    """Whether this item's LINK TYPE can satisfy the disclosed-link
    corroboration relaxation. Everything but a competitor edge can; a record
    with no recorded type (direct evidence, or written before the field
    existed) is unaffected."""
    if COMPETITOR_SATISFIES_DISCLOSED_LINK:
        return True
    return record.relationship_type != "competitor"


def is_ecosystem_association(record: EvidenceRecord) -> bool:
    """Whether this item arrived over a mere sector-membership association
    rather than a disclosed counterparty link -- the class that collapses to
    one collective slot (see _ECOSYSTEM_SLOT_KEY).

    Direct evidence (relationship_confidence is None) is never in this class,
    and neither is propagation over a genuinely disclosed edge."""
    return (record.is_propagated
            and record.relationship_confidence is not None
            and record.relationship_confidence <= ECOSYSTEM_ASSOCIATION_CONFIDENCE)


_FACT_KEY_NOISE = re.compile(r"[^a-z0-9 ]+")
_FACT_KEY_SPACE = re.compile(r"\s+")
# Long enough to keep a distinguishing label ("lmt sentinel contract award"),
# short enough that a model appending a clause cannot mint a second slot for
# the same event.
MAX_FACT_KEY_CHARS = 60
# How many existing labels to show the updater. Bounded because this rides on
# every per-item call, which is the highest-volume prompt in the system; a
# dossier carrying more distinct facts than this is already far past the
# corroboration ceiling, so the tail cannot change a score.
MAX_LISTED_FACT_KEYS = 25


def normalized_fact_key(raw: str) -> str:
    """Fold a model-written fact label to a comparable form.

    The whole mechanism turns on two items reporting one event getting the
    SAME key, so trivial variation must not mint a second slot: case,
    punctuation, doubled spaces and a trailing clause are all things a model
    varies between calls without meaning anything by it. The updater is also
    shown the keys already on the dossier (see propose_update), which is the
    primary defence -- this is the backstop for when it paraphrases one."""
    folded = _FACT_KEY_NOISE.sub(" ", raw.strip().lower())
    return _FACT_KEY_SPACE.sub(" ", folded).strip()[:MAX_FACT_KEY_CHARS].strip()


def fact_keys_on(dossier: Dossier, now: datetime | None = None) -> list[str]:
    """The distinct fact labels already carried by this dossier's non-stale
    evidence, newest first -- what the updater is shown so it can reuse one
    rather than inventing a synonym."""
    now = now or datetime.now(timezone.utc)
    seen: dict[str, None] = {}
    for record in reversed(dossier.evidence):
        if not record.fact_key or evidence_is_stale(record, now):
            continue
        seen.setdefault(normalized_fact_key(record.fact_key), None)
    return [k for k in seen if k]


def independence_key(record: EvidenceRecord) -> str:
    """What makes two evidence items INDEPENDENT corroboration of each other
    -- the unit `independent_source_count` counts (see _aggregate).

    For DIRECT evidence it is the publisher/form, unchanged: dedup.py already
    collapses syndicated republishes of one wire story onto a single
    publisher name, and an 8-K, a Form 4 and a 10-Q are separate primary
    disclosures rather than restatements of each other.

    For PROPAGATED evidence the origin symbol is part of the key, and this is
    the correction. A story about Lockheed and a story about Raytheon are two
    different facts about Ducommun; they are not one story counted twice, and
    which outlet happened to publish each is irrelevant to whether they
    corroborate each other. Keying on publisher alone collapsed them anyway,
    and that quietly capped the entire strategy: the live feed yields six
    publisher names for the whole universe with one aggregator ("Yahoo")
    accounting for ~69% of items, so a thesis built the way this system is
    designed to build one -- accumulating second-order evidence across
    several disclosed counterparties -- could hardly ever exceed two
    "independent sources". DCO carried 17 agreeing items across RTX, LMT and
    NOC, over 0.85-0.95 disclosed links, with zero opposing mass, and counted
    2. Items 3 through 17 contributed nothing to confidence and nothing to
    magnitude.

    The anti-syndication defence is fully preserved, because the publisher is
    still in the key: two Yahoo articles about the same counterparty on the
    same day remain one source (and dedup drops the second before it ever
    gets here). What changes is only that facts about DIFFERENT companies
    stop being counted as one fact.

    ...which was right about DCO and wrong about the macro tape, and the fact
    key below is what resolves it. Keying on the origin symbol says three
    anchors are three facts, and on the live board that was true for DCO
    (RTX/LMT/NOC -- the whole-body pass counted seven or eight distinct facts)
    and false for BWEN (MSFT/META/EQIX -- the same pass counted one, because
    all three were reporting the same quarter's AI capex). Both are
    same-ecosystem, same-window, multi-anchor propagation over disclosed
    edges: structurally identical in every field this function can see. So
    when the scorer has named the underlying event, that name IS the unit of
    independence, for direct and propagated evidence alike -- two articles
    about one company's earnings are also one fact, which the publisher key
    counted as two. Absent a fact key (evidence merged before this existed,
    or a model that omitted it) the previous behaviour stands unchanged."""
    if record.fact_key:
        return f"fact:{normalized_fact_key(record.fact_key)}"
    return channel_key(record)


def channel_key(record: EvidenceRecord) -> str:
    """The pre-fact-key independence unit: the DISCLOSURE CHANNEL an item
    arrived through. Split out of independence_key so the fact key can be
    measured against what it replaced -- see _keys_of, which refuses to let a
    label split what the channel already merged."""
    if record.is_propagated and record.origin_symbol:
        return f"{record.origin_symbol}|{record.source_name}"
    # Direct EDGAR filings were collapsed to one slot per FORM ("SEC EDGAR
    # (8-K)"), so two distinct filing EVENTS -- a contract award in one filing,
    # a guidance cut in another -- could never corroborate each other, capping
    # a filings-only thesis at roughly one source per form type. Distinguish by
    # filing DAY: different filings (different dates) are different events;
    # multiple parts of one filing (same date) stay collapsed, and dedup
    # already drops a re-ingested filing before it reaches here. Only direct
    # EDGAR evidence -- news still keys on the publisher alone.
    if record.source_name.startswith("SEC EDGAR"):
        day = (record.published_at or "")[:10]
        return f"{record.source_name}|{day}" if day else record.source_name
    return record.source_name


# The decay-scaled confidence an evidence item must carry before it counts
# as an INDEPENDENT SOURCE (as opposed to merely contributing to mass). The
# skeptic can accept an item while scaling its confidence toward zero, and
# such an item was still buying a full source slot -- worth both a
# confidence bonus and a magnitude multiplier. Corroboration from evidence
# the adversarial pass judged worthless is not corroboration.
MIN_SOURCE_CONTRIBUTION = 0.15

# Corroboration bonuses, applied per DOUBLING of the independent-source
# count rather than per additional source (see _aggregate).
#
# Both were linear in (S - 1), which was defensible only while S could not
# realistically exceed 2 or 3 -- and it could not, because the independence
# key was the publisher name and the live feed yields six of those for the
# whole universe. Fixing that key (see independence_key) makes S=7 ordinary
# for a well-corroborated thesis, and under the linear form a DCO-shaped
# dossier came out at confidence 1.00 and magnitude 1.00: certainty, and the
# largest re-rating the scale can express, from eighteen individually modest
# second-order items. That is not a stricter system, it is a differently
# broken one, and it would fire trades on everything.
#
# Logarithmic is both the honest shape and the standard one for combining N
# noisy independent estimates: the second source is worth far more than the
# eighth, and these items are not fully independent anyway (they share a
# sector factor). Calibrated so S=1 and S=2 are IDENTICAL to the old linear
# values -- that is where every live dossier currently sits, so this changes
# nothing at today's operating point and only governs the range that was
# previously unreachable.
#
# Both bonuses are now capped at MAX_CORROBORATION_DOUBLINGS (2.5 doublings,
# ~5.7 sources) -- magnitude used to keep climbing past this point, which is
# what let fan-out saturate the score (see below).
#
#   S:              1      2      3      4     ~6+
#   magnitude x  1.00   1.25   1.40   1.50   1.625 (capped)
#   confidence + 0.00   0.10   0.16   0.20   0.25  (capped)
MAGNITUDE_CORROBORATION_STEP = 0.25
CONFIDENCE_CORROBORATION_STEP = 0.10
# Corroboration must never be able to manufacture near-certainty out of a
# pile of individually weak items -- past this the strongest single agreeing
# item's own confidence is what has to carry the thesis.
MAX_CONFIDENCE_CORROBORATION_BONUS = 0.25

# Both corroboration bonuses stop growing past this many doublings, i.e. past
# an independent-source count of 2**this (~5.7 sources). Beyond it, more
# agreeing items cannot push the score higher and the strongest single item's
# own confidence/magnitude has to carry the thesis.
#
# Confidence was always capped here (MAX_CONFIDENCE_CORROBORATION_BONUS);
# magnitude was NOT. Its multiplier was 1 + 0.25*log2(S) with only the final
# min(1.0, ...) as a ceiling, so at S=8 it was x1.75, S=16 x2.0, S~=21 x2.1 --
# unbounded in S. That let pure fan-out mass (many individually weak,
# sector-correlated second-order items) drive magnitude to its 1.0 ceiling and
# saturate the aggregate on the LEAST independent theses -- the exact error
# the daily synthesis pass exists to catch, on the merge path where synthesis
# was being bypassed. Capping both bonuses at the same doublings makes
# corroboration stop mattering at the same amount of evidence for each.
# See AUDIT-2026-08-FOLLOWUP HIGH-1.
MAX_CORROBORATION_DOUBLINGS = MAX_CONFIDENCE_CORROBORATION_BONUS / CONFIDENCE_CORROBORATION_STEP

# The relationship-edge confidence at or below which propagated evidence is an
# industry-level ASSOCIATION (sector co-membership) rather than a disclosed
# counterparty link. Must match engine.ECOSYSTEM_LINK_CONFIDENCE (asserted by
# a test); kept here because dossier.py cannot import engine.py. Evidence at
# this level still contributes decay MASS (direction, contest discount,
# magnitude base) but must not each mint an "independent source": one
# correlated macro story fanned in from three anchors is one fact, not three
# corroborations, and counting it as three is what let fan-out saturate the
# score. See AUDIT-2026-08-FOLLOWUP HIGH-3 / A2.
ECOSYSTEM_ASSOCIATION_CONFIDENCE = 0.25

# The single slot the whole ecosystem-association class collapses to (see
# _aggregate). A sentinel rather than a real key: independence_key never
# emits a leading NUL, so this can never collide with a publisher, an origin
# symbol or an EDGAR form/day pair.
#
# One, not zero, and not one-per-origin. An ecosystem link IS one real piece
# of information -- "this sector is repricing" -- and the honest accounting
# of one piece of information is one slot. Zero (the previous rule) states
# that it cannot raise a thesis at all, which contradicts the stated design
# intent that it "can raise a thesis but can never single-handedly qualify
# one"; one slot restores exactly that, since 1 < min_independent_sources.
# One-per-origin was considered and rejected: NVDA, AMAT and LRCX reporting
# one capex story would be three origins and three slots, which is the
# fan-out saturation bug rebuilt with extra steps. The class contributes
# min(1, |eco|) and is therefore constant in volume by construction --
# doubling the item count, the origin count, the publisher count or the day
# count changes nothing.
_ECOSYSTEM_SLOT_KEY = "\x00ecosystem-association"

# Whether a COMPETITOR edge satisfies the disclosed-link corroboration
# relaxation (has_disclosed_link_evidence). It does not.
#
# The relaxation exists because a primary filing that states a link answers
# "is the causal channel real" with a primary source, so demanding a third
# journalist to re-answer it means waiting until the market has made the
# connection itself. A competitor disclosure does not answer that question.
# "KLA and Applied Materials name each other as competitors" is a genuinely
# disclosed fact, but it is not a transmission channel the way a supply
# relationship is -- the news does not have to reach one through the other,
# and its SIGN is frequently inverted (a competitor's capacity loss is good
# news here, which is why the Tier 2 rubric lists it that way).
#
# Measured on the live graph: competitor is the largest edge class at 448 of
# 1066, and 375 of those sit at or above DISCLOSED_LINK_CONFIDENCE. So the
# most numerous and most sign-ambiguous class was relaxing the bar from three
# sources to two more often than customer or supplier edges were. Competitor
# evidence still propagates, still contributes mass, and still claims an
# independent SOURCE slot -- it just stops buying the corroboration discount.
#
# Records written before relationship_type existed carry "" and are treated
# as unknown, which keeps the old behaviour for historical evidence rather
# than silently re-scoring it.
COMPETITOR_SATISFIES_DISCLOSED_LINK = False

# Bumped whenever a change alters how confidence/magnitude are computed from
# the same evidence. Stamped onto every daily dossier snapshot so the
# forward-validation record can be split at the boundary instead of silently
# mixing scores that mean different things -- forward data cannot be
# backfilled, and re-scoring old rows with new logic would be look-ahead.
# 4: the daily synthesis verdict is now PERSISTED after every run, so the
# capped score (a veto zeroes it, a trim lowers it) reaches the dossier file
# and therefore the snapshot series. Before this it was computed, used for
# the rest of the decay pass, and discarded unless a signal fired or expired
# in the same pass -- so snapshots recorded the UNCAPPED arithmetic score for
# most dossiers and the capped one for a few, silently mixed.
#
# 5: three related changes that all move scores in the high region the
# forward-return question is asked about, so rows before and after must not be
# pooled:
#   - the magnitude corroboration multiplier is now capped at
#     MAX_CORROBORATION_DOUBLINGS (was unbounded in S), so fan-out mass can no
#     longer saturate magnitude;
#   - ecosystem-association evidence (relationship confidence <=
#     ECOSYSTEM_ASSOCIATION_CONFIDENCE) no longer counts as an independent
#     source, only as mass, so it stops inflating both corroboration bonuses;
#   - the persisted synthesis cap is now re-applied on the MERGE path too (see
#     engine._cap_with_synthesis), not just the daily decay pass, so a vetoed
#     or trimmed dossier stays capped when fresh evidence merges instead of
#     re-firing on the raw arithmetic -- which changes which of the two
#     numbers a merge-driven snapshot records.
# Forward data cannot be backfilled and old rows must never be re-scored, so
# splitting at this boundary is the only honest option.
#
# 6: four changes, three of which push scores UP in the same region near the
# signal bar. Version 5 bundled three changes behind one boundary and the
# resulting series can no longer attribute an outcome to any one of them;
# that is not repeatable, so this version ships per-row attribution flags
# alongside the changes (Dossier.veto_falsified_by_price /
# synthesis_stale_evidence / ecosystem_slot_counted) and records the
# pre-veto arithmetic score. Bucketing v6 rows by mechanism is a filter over
# the snapshot, not an inference.
#   - ecosystem-association evidence now claims ONE collective slot instead
#     of none (_ECOSYSTEM_SLOT_KEY), so a name whose only propagation path is
#     the sector edge stops sitting at a permanent structural zero;
#   - a competitor edge no longer satisfies the disclosed-link corroboration
#     relaxation (COMPETITOR_SATISFIES_DISCLOSED_LINK), which RAISES the bar
#     from two sources to three for competitor-only dossiers -- the one
#     change here that tightens rather than loosens;
#   - an already-priced-in verdict is now falsifiable by price and by a
#     materially changed evidence body, and a falsified verdict is re-judged
#     rather than re-asserted (engine._maybe_resynthesize);
#   - EDGAR ingestion covers 20-F/40-F/6-K, NT 10-K/NT 10-Q, S-1/S-3 and the
#     delisting forms, so filing evidence now reaches foreign private issuers
#     that could previously never produce a single filing item.
#
# 9: what 8 SAID it did, actually happening. The fact key never survived the
# engine's proposal validator -- engine._validated_proposal is a whitelist and
# did not name the field -- so every item merged under 8 carried fact_key="",
# independence_key never got past its first branch, and 8 scored per CHANNEL
# exactly as 7 did. The entry below is an accurate description of a mechanism
# that did not run.
#
# Rows must split here for the same reason 8 claimed to: the unit the score is
# built on moves. They could not split AT 8, because nothing changed there.
#
# What the board looked like at the boundary: 0 of 970 evidence items carried
# a label, including all 77 merged after 8 went live -- a mechanism inert on
# 100% of evidence while raising no error anywhere. Against the whole-body
# pass's own fact count, the 29 synthesised dossiers counted 216 independent
# sources for 110 distinct facts: median 1.8x over, worst 4.0x (NCSM 12
# sources / 3 facts, VVX and KLXE 15 / 4, DCO 18 / 7). Of those 29, 23 see
# their source count fall under per-fact keying, 5 hold and 1 rises -- the key
# splits distinct events from one publisher as readily as it collapses one
# event across several counterparties -- and none drops below
# min_independent_sources, so the correction silences nothing outright.
#
# FORWARD-ONLY, which is the caveat this boundary carries and 8's does not:
# existing evidence keeps fact_key="" and nothing backfills it, so 9 marks
# where the mechanism became live, not where every row became labelled. Early
# 9 rows still rest mostly on unlabelled evidence and converge as it decays
# out. The labelling coverage in diagnostics is what says how far along a
# given row is; read it before comparing a 9 row against a later one.
#
# 8: independence is counted per FACT, not per channel
# (EvidenceRecord.fact_key, assigned by the per-item updater and used by
# independence_key). This moves the unit the whole score is built on, so rows
# must split here. NOTE: see 9 -- the field was dropped before it ever reached
# an EvidenceRecord, so no row ever written under 8 actually did this.
#
# The two keys it replaces were each right about one case and wrong about the
# other. Keying on the publisher collapsed DCO's seventeen items across RTX,
# LMT and NOC to two sources, which capped the strategy. Keying on the origin
# symbol -- v5's fix for that -- then counted BWEN's MSFT, META and EQIX items
# as three independent corroborations when the whole-body pass read them as
# ONE fact: the same quarter's AI capex, reported three times. Both cases are
# same-ecosystem, same-window, multi-anchor propagation over disclosed edges,
# identical in every field the key could see, so no metadata rule separates
# them and the fact itself has to be recorded.
#
# Measured on three weeks of forward capture, this is where the money was:
# benchmark-relative alpha at 5 days ran -0.15% / -0.40% / -3.17% / -1.69%
# for score buckets below 0.65 and +1.80% at 0.65+, while RAW returns were
# positive nearly everywhere -- the score was tracking a rising tape, and the
# theses it inflated were the losing ones.
#
# Absent a fact key the previous behaviour is unchanged, so existing evidence
# and any model that omits the field score exactly as they did under 7.
# 7: the corroboration bonus is paid on distinct FACTS rather than distinct
# channels wherever synthesis has said what that number is
# (effective_corroboration_count), and the whole-body pass can now report
# overlap WITHOUT vetoing (Dossier.redundant_evidence). Both move scores in
# the high region the forward-return question is asked about, and they move
# them in opposite directions -- the first trims inflated corroboration down,
# the second stops a duplication finding from zeroing a thesis outright -- so
# rows must split at this boundary rather than pool with v6.
#
# The measurement that forced it: across three live days the whole-body pass
# returned 77 vetoes and 2 trims, every veto reading "already priced in" while
# its own text described duplication ("nearly all 50 items reduce to one
# restated macro fact"). 22 of 45 dossiers sat at exactly 0.000, no position
# opened for four days, and on the 23 verdicts whose numbers survive in the
# log the arithmetic ran a median 12.4x hot against what synthesis rated the
# same evidence. Routing those to the trim alone would have changed nothing --
# none of the 23 clears the bar on its trimmed score either -- which is what
# says the gap, not the routing, was the defect.
SCORING_VERSION = 9

# How long a persisted synthesis verdict is honoured -- as a cap on the merge
# path (engine._cap_with_synthesis) and as the corroboration ceiling in
# _aggregate. Lives here rather than in engine because scoring now depends on
# it, and two copies of a freshness window is how they drift apart.
SYNTHESIS_CAP_MAX_AGE_HOURS = 36.0
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


def synthesis_verdict_fresh(dossier: Dossier, now: datetime) -> bool:
    """Whether the persisted whole-body verdict is recent enough to be
    honoured. Outside the window there is nothing to honour -- the cap has
    lapsed on its own, and the next daily pass re-judges."""
    if not dossier.synthesis_at:
        return False
    try:
        synth_at = datetime.fromisoformat(dossier.synthesis_at)
    except (TypeError, ValueError):
        return False
    if synth_at.tzinfo is None:
        synth_at = synth_at.replace(tzinfo=timezone.utc)
    return (now - synth_at).total_seconds() / 3600.0 <= SYNTHESIS_CAP_MAX_AGE_HOURS


def effective_corroboration_count(dossier: Dossier, now: datetime) -> int:
    """How many corroborating facts the bonuses may be paid on.

    `independent_source_count` counts distinct DISCLOSURE CHANNELS -- separate
    publishers, separate filing forms, separate origin symbols. That is the
    right unit for the signal bar's "was this seen in more than one place",
    and it is the wrong unit for "how much does this corroborate", because
    five outlets and seven counterparties reacting to one hyperscaler capex
    announcement are one fact seen twelve ways.

    Synthesis already computes the right number and the system already stores
    it: distinct_fact_count is persisted, stamped onto every paper trade and
    rendered in status -- and never once consulted by a score. Meanwhile the
    two passes disagreed by a median factor of 12.4x across 23 live verdicts
    (arithmetic median 0.709 against a synthesised 0.054), and the veto was
    absorbing the entire difference. This is the arithmetic meeting synthesis
    partway instead: the corroboration BONUS is paid on facts, not channels.

    Deliberately narrow:

    - Only the bonus is capped. `independent_source_count` itself is left
      alone, so the source-count gates (min_independent_sources and the
      news-only bar) keep the tested meaning they were written with.
    - Only with a FRESH verdict, and only a positive count. A dossier that
      never reached the synthesis floor carries distinct_fact_count 0, and
      treating that as "zero facts" would zero the corroboration of every
      un-synthesised dossier in the system -- the opposite of the intent.
    - A cap, never a lift, matching every other thing synthesis is allowed to
      do to a score."""
    counted = dossier.independent_source_count
    if not synthesis_verdict_fresh(dossier, now):
        return counted
    if dossier.distinct_fact_count <= 0:
        return counted
    return min(counted, dossier.distinct_fact_count)


def _corroboration_doublings(independent_source_count: int) -> float:
    """log2 of the independent-source count, floored at 0 -- how many times
    the corroboration has DOUBLED. This is the multiplier both bonuses are
    applied per, so that the second independent source is worth much more
    than the eighth. See MAGNITUDE_CORROBORATION_STEP."""
    if independent_source_count < 2:
        return 0.0
    return math.log2(independent_source_count)


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
        # Zeroed with the rest. Leaving the last resolved direction's figure
        # standing would keep a number the aggregate no longer believes in a
        # field the synthesis floor gate reads -- currently unreachable, since
        # _apply_synthesis refuses a NONE direction before it looks, but a
        # stale score sitting behind one guard is how the next reader gets it
        # wrong.
        dossier.arithmetic_score = 0.0
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
    # An item the skeptic scaled to (near) zero still counted as a full
    # independent source, and a source slot is worth a lot: it lifts
    # confidence AND multiplies magnitude. So evidence the adversarial pass
    # judged worthless was buying the corroboration that fires the trade.
    # It still contributes to mass and decay for exactly what it is worth --
    # it just stops claiming a slot it did not earn.
    contributing = [e for e, w in weighted if e.confidence * w >= MIN_SOURCE_CONTRIBUTION]
    # Ecosystem-association evidence contributes mass but never an independent
    # SOURCE. It arrives over a sector-membership link (relationship confidence
    # <= ECOSYSTEM_ASSOCIATION_CONFIDENCE), explicitly NOT a disclosed
    # counterparty relationship, so a single correlated macro story fanned in
    # from several anchors is one fact, not several corroborations. Counting
    # each as an independent slot inflated both corroboration bonuses (they key
    # off this count via _corroboration_doublings) and let pure fan-out mass
    # cross the signal bar. Direct evidence (relationship_confidence is None)
    # and propagation over a genuinely disclosed edge are unaffected.
    # ...but it is not worth NOTHING either, which is what excluding it
    # outright made it worth. The whole class -- every item, every origin,
    # every publisher, every day -- now collapses to exactly one collective
    # slot (_ECOSYSTEM_SLOT_KEY). That is the honest accounting of one piece
    # of information, and it is constant in volume by construction, so the
    # fan-out saturation this exclusion was written to stop remains stopped:
    # thirty correlated macro items still contribute exactly one slot, and it
    # still cannot single-handedly qualify a dossier.
    keys = _keys_of(contributing)
    dossier.ecosystem_slot_counted = _ECOSYSTEM_SLOT_KEY in keys
    dossier.independent_source_count = len(keys)
    # Gated on LINK quality as well as source type. Any filing set this flag,
    # including one propagated over an ECOSYSTEM edge -- an industry-level
    # association at 0.25 confidence, not a disclosed relationship. That
    # silently relaxed the news-only corroboration bar from three sources to
    # two on the weakest link type in the system, which is the opposite of
    # what the bar is for. Direct filings carry relationship_confidence=None
    # and still qualify; so does propagation over a strongly disclosed edge.
    dossier.has_filing_evidence = any(
        e.source_type != "news"
        and (e.relationship_confidence is None
             or e.relationship_confidence >= DISCLOSED_LINK_CONFIDENCE)
        for e in agreeing
    )
    # Competitor edges are excluded here (see
    # COMPETITOR_SATISFIES_DISCLOSED_LINK) -- they corroborate that two firms
    # compete, not that news transmits from one to the other.
    dossier.has_disclosed_link_evidence = any(
        (e.relationship_confidence or 0.0) >= DISCLOSED_LINK_CONFIDENCE
        and _link_type_corroborates(e)
        for e in agreeing
    )
    # The base thesis is ONE item's decay-scaled confidence and magnitude,
    # chosen jointly. These used to be independent maxima over the whole
    # agreeing set, so a dossier could report the confidence of a certain-
    # but-tiny item alongside the magnitude of a speculative-but-large one
    # -- a combined score no single piece of evidence had ever proposed,
    # and higher than any of them. The strongest item is the one with the
    # largest confidence*magnitude product; corroboration bonuses then
    # build on top of it exactly as before.
    best, best_weight = max(weighted, key=lambda ew: ew[0].confidence * ew[0].magnitude * ew[1] * ew[1])
    base_confidence = best.confidence * best_weight
    # Capped so BOTH bonuses stop growing at the same amount of corroboration
    # (see MAX_CORROBORATION_DOUBLINGS). At the cap this reproduces the old
    # confidence bonus exactly -- min(0.25, 0.10*doublings) == 0.10 *
    # min(doublings, 2.5) -- while newly bounding the magnitude multiplier,
    # which previously grew without limit in the source count.
    # Paid on distinct FACTS where synthesis has told us what that number is,
    # otherwise on distinct channels exactly as before. See
    # effective_corroboration_count -- this is the one line that stops the
    # arithmetic manufacturing a 0.94 out of one restated macro story.
    doublings = min(_corroboration_doublings(effective_corroboration_count(dossier, now)),
                    MAX_CORROBORATION_DOUBLINGS)
    # The same figure with NO fact cap applied, kept so the pass that produced
    # the cap can still be scheduled against the arithmetic rather than
    # against its own last verdict. See dossier.arithmetic_score.
    uncapped_doublings = min(_corroboration_doublings(dossier.independent_source_count),
                             MAX_CORROBORATION_DOUBLINGS)
    corroboration_bonus = CONFIDENCE_CORROBORATION_STEP * doublings
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
    base_magnitude = best.magnitude * best_weight
    magnitude_bonus = 1.0 + MAGNITUDE_CORROBORATION_STEP * doublings
    dossier.magnitude = min(1.0, base_magnitude * magnitude_bonus)
    # What the aggregate would have said with no fact cap. Recorded, never
    # traded on -- see the field's own comment for the fail-open this exists
    # to close.
    dossier.arithmetic_score = (
        min(1.0, base_confidence + CONFIDENCE_CORROBORATION_STEP * uncapped_doublings)
        * contest_factor
        * min(1.0, base_magnitude * (1.0 + MAGNITUDE_CORROBORATION_STEP * uncapped_doublings))
    )
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
            "fact_key": {
                "type": "string",
                "description": (
                    "A short lowercase label for the UNDERLYING EVENT this item reports -- the "
                    "thing that happened, not the article about it. Three or four words, no "
                    "punctuation. Two items reporting the SAME event must get the SAME label, "
                    "even when they are about different companies, from different publishers, "
                    "or on different days: 'META raises capex', 'MSFT Q4 capex commentary' and "
                    "'EQIX raises FY guidance' during one earnings season are all "
                    "'ai datacenter capex q2 2026'. Two items reporting DIFFERENT events must "
                    "get different labels, even when they are about the same company: a "
                    "contract award and a guidance raise are 'lmt sentinel contract award' and "
                    "'lmt fy guidance raise'. If a label in the list you were given already "
                    "names this event, REUSE IT VERBATIM rather than writing a synonym."
                ),
            },
            "reasoning": {"type": "string", "description": "One or two sentences."},
        },
        "required": ["is_new_information", "direction", "magnitude", "confidence", "horizon_days",
                     "fact_key", "reasoning"],
    },
}

# What a genuinely tradeable catalyst looks like, with worked magnitude
# anchors. Without this the model was asked to size a price impact with no
# frame of reference at all, and it did what an unanchored scorer always
# does: clustered everything into a narrow, timid band. The live board ran
# magnitudes of 0.10-0.40 across every kind of evidence -- a $150M contract
# award and a conference appearance landed in the same range.
#
# Tiered by what the evidence IS, not by how excited the language is. The
# distinguishing feature of tier 1 is a primary-source commitment with a
# number attached; of tier 3, that it is somebody's opinion about a
# company rather than something that happened to it.
_CATALYST_RUBRIC = (
    "CATALYST TIERS -- anchor `magnitude` to these rather than to how strongly the "
    "source is worded:\n"
    "  TIER 1 (magnitude 0.50-0.90): a committed, quantified change to future revenue "
    "or cost. A signed or awarded contract with a stated value or ceiling; an FDA "
    "510(k)/PMA clearance or CE mark; a named multi-year supply agreement or qualified "
    "design win at a named OEM; an official guidance revision; a plant/fab/line "
    "announcement with a stated site and timeline; a major customer loss or program "
    "cancellation (negative).\n"
    "  TIER 2 (magnitude 0.20-0.50): a concrete operational fact that changes the "
    "outlook without a number attached to THIS company. A disclosed customer's capex "
    "guide-up or guide-down where this company is a named supplier; a competitor's "
    "disclosed capacity loss or exit; a product launch entering an existing market; a "
    "capacity expansion; a regulatory decision affecting the addressable market; an "
    "insider OPEN-MARKET purchase of size (not an award or a tax-withholding sale).\n"
    "  TIER 3 (magnitude 0.00-0.10): commentary rather than event. Analyst notes and "
    "price-target changes, sector sentiment, conference and trade-show appearances, "
    "promotional press releases with no committed counterparty, index/screen mentions, "
    "routine governance changes, and pre-planned 10b5-1 insider sales.\n"
    "A tier-1 fact reaching this company through a LINKED company still deserves a "
    "tier-2-or-better magnitude: the fact is real and quantified, and only the share "
    "of it that lands here is uncertain -- that uncertainty belongs in `confidence`, "
    "not in a magnitude collapsed to zero."
)

# What "a catalyst" concretely means in each of this universe's ecosystems.
# Generic prompting made the model reason about a defense-supplier contract
# award and a data-center power purchase agreement identically, when what
# actually moves each name is domain-specific and well known.
ECOSYSTEM_CATALYSTS = {
    "semi_equipment": (
        "Semiconductor equipment/materials. What moves these names: wafer-fab-equipment "
        "capex revisions at TSMC/Intel/Samsung/Micron, tool orders and shipment "
        "deferrals, new fab announcements and groundbreakings, node transitions, export "
        "controls and license decisions, and photomask/test/inspection demand following "
        "a customer's utilization commentary."
    ),
    "defense_tier2": (
        "Defense and aerospace tier-2 suppliers. What moves these names: DoD contract "
        "awards and IDIQ task orders (the daily defense.gov contract announcements are "
        "the primary source), program-of-record milestones and production-rate "
        "decisions, foreign military sales approvals, prime-contractor backlog and book-"
        "to-bill commentary, and build-rate changes at Boeing/Airbus for the commercial "
        "aerostructures side."
    ),
    "grid_datacenter": (
        "Grid, electrification and data-center buildout. What moves these names: "
        "hyperscaler capex guidance, named data-center site announcements, power "
        "purchase agreements and interconnection-queue decisions, utility rate cases "
        "and capital plans, transformer/switchgear lead times, and transmission "
        "project approvals."
    ),
    "battery_storage": (
        "Battery and energy storage. What moves these names: offtake and supply "
        "agreements with named cell or vehicle makers, DOE grants and loan-programme "
        "decisions, gigafactory milestones, qualification/homologation wins, lithium "
        "and cathode input pricing, and EV production-schedule changes at the OEMs."
    ),
    "medtech_supply": (
        "Medical-device supply chain. What moves these names: FDA 510(k) and PMA "
        "clearances and their timing, recalls and warning letters, reimbursement "
        "(CMS) decisions, procedure-volume commentary from the large device makers, "
        "and single-use component demand tied to a named customer's launch."
    ),
    "auto_supply": (
        "Automotive supply. What moves these names: OEM production schedules and "
        "shutdowns, platform/program wins and losses, content-per-vehicle changes, "
        "recalls, and the EV-vs-ICE mix at a disclosed customer."
    ),
    "energy_services": (
        "Oilfield services and equipment. What moves these names: E&P capex budgets, "
        "rig and frac-spread counts, completion activity, day rates, and a named "
        "customer's drilling programme changes."
    ),
    "industrial_machinery": (
        "Industrial machinery. What moves these names: order intake and book-to-bill, "
        "machine-tool and capital-equipment demand cycles, tariffs on inputs, and "
        "reshoring/capex announcements by disclosed customers."
    ),
    "transport_logistics": (
        "Trucking and logistics. What moves these names: contract rate renewals, spot-"
        "rate direction, freight volumes at a disclosed shipper, fuel surcharges, "
        "driver availability, and a large customer's inventory cycle."
    ),
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
    "higher confidence than propagated news.\n\n"
    + _CATALYST_RUBRIC +
    "\n\nMost news is noise and most single articles should not flip an established "
    "thesis. But 'be conservative' is a rule about VAGUE evidence, not a discount "
    "applied to everything: a concrete tier-1 fact deserves the magnitude it implies, "
    "and scoring a real contract award like a promotional press release is as wrong as "
    "the reverse. Reserve low confidence for evidence that is genuinely vague, "
    "promotional, already priced in, or not specific to this company."
)


class DossierUpdater:
    def __init__(self, api_key: str, model: str, usage: UsageTracker,
                 trace: LLMTrace | None = None):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._usage = usage
        self._trace = trace

    async def propose_update(
        self, dossier: Dossier, evidence_text: str, origin_symbol: str, relationship_note: str,
        relationship_confidence: float | None = None, ecosystem: str = "",
        now: datetime | None = None,
    ) -> dict | None:
        if not self._usage.budget_remaining(CAT_DOSSIER):
            log.info("%s: %s -- deferring dossier update.",
                     dossier.symbol, self._usage.deferral_reason(CAT_DOSSIER))
            return None
        now = now or datetime.now(timezone.utc)
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
        # What counts as a catalyst is domain-specific, and the model was
        # previously left to infer it. A defense supplier's contract award
        # and a data-center operator's power purchase agreement are not the
        # same kind of event, and naming the ecosystem's actual drivers is
        # free -- it is already on the CompanySpec.
        sector = ECOSYSTEM_CATALYSTS.get(ecosystem, "")
        sector_note = f"\n\nSector context for {dossier.symbol}: {sector}" if sector else ""
        # Today's date, so "old/already-priced-in news treated as new" is
        # actually computable against the evidence's own published date rather
        # than the model silently anchoring "now" to its training cutoff (the
        # synthesizer already gets this; the two per-item graders did not).
        # The labels already on this dossier, so the model reuses one instead
        # of paraphrasing it. Normalisation is the backstop; this is the
        # actual mechanism -- a closed vocabulary the model can see beats
        # trying to fuzzy-match free text after the fact.
        existing = fact_keys_on(dossier, now)
        known_facts = (
            "\n\nFacts already recorded on this dossier -- if this item reports one of them, "
            "reuse its label VERBATIM as fact_key:\n"
            + "\n".join(f"  - {k}" for k in existing[:MAX_LISTED_FACT_KEYS])
            if existing else
            "\n\nNo facts recorded on this dossier yet -- write the first fact_key."
        )
        prompt = (
            f"Today: {now.date().isoformat()}\n"
            f"Company: {dossier.symbol}\n"
            f"Current thesis: {current}\n"
            f"{propagation}{sector_note}{known_facts}\n\n"
            f"New evidence:\n{evidence_text}"
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                # Model-appropriate thinking/effort/temperature -- see llm.py.
                # max_tokens is a ceiling over thinking AND the tool call on
                # every thinking-capable model, so 500 (the old value) would
                # truncate before the tool_use block was ever emitted.
                **request_kwargs(self._model, max_tokens=4000, effort="high"),
                system=cacheable_system(_SYSTEM_PROMPT),
                tools=[_UPDATE_TOOL],
                tool_choice={"type": "tool", "name": "update_thesis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad API call kill the ingestion loop
            # An account-level failure trips the shared breaker so the next
            # caller is refused before it reaches the network -- this is the
            # highest-volume call site in the system and the one that logged
            # 11,893 identical billing failures in two hours.
            self._usage.note_failure(exc)
            log.warning("%s: dossier update proposal failed: %s", dossier.symbol, exc)
            return None
        self._usage.record(response.usage.input_tokens, response.usage.output_tokens,
                           model=self._model, category=CAT_DOSSIER)
        proposed = first_tool_use(response)
        if self._trace is not None:
            self._trace.record(CAT_DOSSIER, self._model, dossier.symbol, prompt, proposed,
                               response.usage.input_tokens, response.usage.output_tokens,
                               system=_SYSTEM_PROMPT)
        return proposed

    async def aclose(self) -> None:
        await self._client.close()


# --- Synthesis: reasoning across the accumulated evidence as a body ---

_SYNTHESIS_TOOL = {
    "name": "synthesize_thesis",
    "description": "Judge a company's accumulated evidence as a whole and state the resulting thesis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": list(DIRECTIONS)},
            "distinct_fact_count": {
                "type": "integer", "minimum": 0,
                "description": (
                    "How many genuinely DISTINCT underlying facts this evidence represents -- not "
                    "how many items there are. Three articles about one contract award are ONE "
                    "fact. A contract award, an insider purchase and a customer's guidance raise "
                    "are THREE. This is the number that should decide how corroborated the thesis "
                    "actually is."
                ),
            },
            "confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Confidence in the direction, judging the body of evidence as a whole.",
            },
            "magnitude": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": (
                    "Size of the move the COMBINED evidence implies (0=negligible, 1=major "
                    "re-rating). Overlapping items do not add; genuinely independent ones do."
                ),
            },
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 60},
            "already_priced_in": {
                "type": "boolean",
                "description": (
                    "True ONLY if the market has plainly already absorbed this. The whole "
                    "strategy is trading the lag BEFORE the market connects the dots, so a "
                    "thesis it has already connected is not one.\n\n"
                    "This is a claim about the PRICE, so READ THE PRICE BLOCK and answer from "
                    "it. The move is the evidence: a stock that jumped the session its catalyst "
                    "landed and has held the gain has absorbed it; one that has not moved since "
                    "has not, no matter how old or how widely covered the story is. Age and "
                    "coverage are the WRONG proxy here -- a heavily covered story about an "
                    "anchor is exactly the setup where the thinly-covered supplier has not "
                    "moved yet, and that setup is the entire point of this system. Where the "
                    "block is absent, say so in the note and judge conservatively.\n\n"
                    "Do not set it merely because the evidence is thin or repetitive -- that is "
                    "redundant_evidence, a different finding with a different consequence."
                ),
            },
            "redundant_evidence": {
                "type": "boolean",
                "description": (
                    "True if this file collapses to far fewer facts than it has items -- one "
                    "story restated across many outlets or many counterparties. This is a claim "
                    "about the EVIDENCE, not the price. It does not veto the thesis; it says the "
                    "arithmetic overcounted, and your confidence/magnitude are then used as the "
                    "honest ceiling. Set this, not already_priced_in, when the problem is that "
                    "the corroboration is illusory."
                ),
            },
            "strongest_catalyst": {
                "type": "string",
                "description": "The single fact carrying the thesis, in one line.",
            },
            "thesis": {"type": "string", "description": "Two or three sentences."},
        },
        "required": [
            "direction", "distinct_fact_count", "confidence", "magnitude",
            "horizon_days", "already_priced_in", "redundant_evidence",
            "strongest_catalyst", "thesis",
        ],
    },
}

_SYNTHESIS_SYSTEM_PROMPT = (
    "You are an equity analyst reviewing the COMPLETE accumulated evidence file on one company "
    "and stating what it adds up to. Everything here already survived an adversarial review "
    "individually; your job is the question no per-item review can answer: what does this body "
    "of evidence mean TOGETHER?\n\n"
    "Three things only you can see:\n"
    "1. OVERLAP. Items were scored one at a time, so the same underlying fact scored several "
    "times looks like several corroborating facts. Count DISTINCT facts, not items. This is "
    "the most important judgement you make -- an accumulation of one story restated is not a "
    "corroborated thesis, and treating it as one is how a system talks itself into a trade. "
    "When you find it, say so via redundant_evidence and let your confidence and magnitude be "
    "the honest ceiling; distinct_fact_count is then what the corroboration is actually worth.\n"
    "2. COHERENCE. Do these facts describe one consistent story, or unrelated fragments that "
    "happen to point the same way? A supplier winning a contract, that supplier's customer "
    "guiding capex up, and an insider buying is a coherent thesis. Three unrelated mild "
    "positives is a coincidence, and deserves far less confidence than their sum suggests.\n"
    "3. STALENESS. Given the dates and how widely covered these facts were, has the market "
    "already made this connection? The entire strategy is trading the lag before it does. Say "
    "so plainly via already_priced_in -- it costs nothing to skip a move that is over, and a "
    "great deal to enter one.\n\n"
    "Keep (1) and (3) separate. Thin, repetitive evidence is redundant_evidence. A move the "
    "market has already made is already_priced_in. They have different consequences -- the "
    "first trims the score to what you rate it, the second stops the thesis outright and is "
    "later checked against the tape -- so answering the overlap question with the price flag "
    "both overstates what you found and puts it to a test you did not intend.\n\n"
    + _CATALYST_RUBRIC +
    "\n\nBe willing to conclude the evidence does NOT support a position: direction NONE, or a "
    "confidence well below the individual items'. That is a real and useful answer. Be equally "
    "willing to conclude it supports a LARGER move than any single item implied, when several "
    "genuinely distinct facts reinforce each other."
)


class DossierSynthesizer:
    """The pass that reasons across a dossier's evidence as a whole.

    Everything else in this system is incremental: each evidence item is
    scored alone against a one-line summary of the current thesis, and the
    aggregate is then pure arithmetic over those independent scores
    (see _aggregate). Nothing ever read the evidence file as a body, which
    left three questions structurally unanswerable and all three of them
    decide whether a trade is justified:

      - are these N items N facts, or one fact counted N times?
      - do they tell one coherent story, or are they unrelated coincidences
        that happen to point the same way?
      - has the market already connected these dots, making the lag this
        strategy trades already gone?

    Arithmetic over per-item scores cannot answer any of them, and the
    per-item pass cannot either -- it sees one item. So this runs once a day
    per tradeable with a live thesis: at most a few dozen calls, against a
    budget the deployment runs at a few percent of.

    Its output does not replace the arithmetic aggregate. It CAPS it (see
    engine.py's decay pass): a thesis may fire on the lower of the two.
    Synthesis can veto and it can trim, but it cannot inflate a score into a
    trade on its own -- that keeps one model call from becoming a single
    point of failure for committing capital."""

    def __init__(self, api_key: str, model: str, usage: UsageTracker,
                 trace: LLMTrace | None = None):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._usage = usage
        self._trace = trace

    @staticmethod
    def _evidence_digest(dossier: Dossier, now: datetime, limit: int = 40) -> str:
        """The evidence file as the analyst sees it: one line per non-stale
        agreeing-or-opposing item, newest last, with the date, source, what
        it was about and how it reached this company. Dates and sources are
        included precisely so overlap and staleness are VISIBLE -- they are
        what the two hardest judgements here are made from."""
        live = [e for e in dossier.evidence if not evidence_is_stale(e, now)]
        lines = []
        for e in live[-limit:]:
            route = (
                f"via {e.origin_symbol} ({e.relationship_note[:110]})"
                if e.is_propagated else "direct"
            )
            lines.append(
                f"- [{(e.published_at or e.merged_at or '')[:10]}] {e.source_name} | {route}\n"
                f"    {e.headline[:180]}\n"
                f"    scored {e.direction} magnitude={e.magnitude:.2f} confidence={e.confidence:.2f}: "
                f"{e.reasoning[:220]}"
            )
        return "\n".join(lines)

    async def synthesize(self, dossier: Dossier, ecosystem: str = "",
                         now: datetime | None = None,
                         price_context: str = "") -> dict | None:
        """None on a transient failure or an exhausted budget -- the caller
        keeps the arithmetic aggregate unchanged rather than acting on a
        synthesis it does not have."""
        if not self._usage.budget_remaining(CAT_SYNTHESIS):
            log.info("%s: %s -- deferring synthesis.",
                     dossier.symbol, self._usage.deferral_reason(CAT_SYNTHESIS))
            return None
        now = now or datetime.now(timezone.utc)
        digest = self._evidence_digest(dossier, now)
        if not digest:
            return None
        sector = ECOSYSTEM_CATALYSTS.get(ecosystem, "")
        prompt = (
            f"Company: {dossier.symbol}\n"
            f"Today: {now.date().isoformat()}\n"
            + (f"Sector: {sector}\n" if sector else "")
            + f"\nArithmetic aggregate of the items below (for reference, not a constraint): "
            f"direction={dossier.direction} confidence={dossier.confidence:.2f} "
            f"magnitude={dossier.magnitude:.2f} over {dossier.independent_source_count} "
            f"counted sources.\n"
            # The tape, bracketing the earliest evidence. Empty when no marks
            # exist for this symbol, in which case already_priced_in falls back
            # to being judged from the evidence alone, as it always was.
            + price_context
            + f"\nACCUMULATED EVIDENCE ({len(dossier.evidence)} items, non-stale shown):\n{digest}"
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                **request_kwargs(self._model, max_tokens=8000, effort="high"),
                system=cacheable_system(_SYNTHESIS_SYSTEM_PROMPT),
                tools=[_SYNTHESIS_TOOL],
                tool_choice={"type": "tool", "name": "synthesize_thesis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad API call kill the decay pass
            self._usage.note_failure(exc)
            log.warning("%s: synthesis failed: %s", dossier.symbol, exc)
            return None
        self._usage.record(response.usage.input_tokens, response.usage.output_tokens,
                           model=self._model, category=CAT_SYNTHESIS)
        verdict = first_tool_use(response)
        if self._trace is not None:
            self._trace.record(CAT_SYNTHESIS, self._model, dossier.symbol, prompt, verdict,
                               response.usage.input_tokens, response.usage.output_tokens,
                               system=_SYNTHESIS_SYSTEM_PROMPT)
        return verdict

    async def aclose(self) -> None:
        await self._client.close()
