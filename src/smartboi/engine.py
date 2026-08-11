"""Orchestrates the whole pipeline: EDGAR + news ingestion -> relationship
graph -> dossier updates -> adversarial skeptic pass -> signal evaluation
-> (optional) hypothetical paper trade. See README for the strategy this
implements point-by-point.

Every optional integration degrades gracefully instead of failing to
start -- see config.py's docstring. Add EDGAR_USER_AGENT and/or
FINNHUB_API_KEY to start collecting evidence; add ANTHROPIC_API_KEY to
start scoring it into dossiers. Signals are detected and logged
(signals.jsonl) the moment ANTHROPIC_API_KEY is present. Opening and
marking hypothetical positions needs a price source -- IB
(ENABLE_IB_PRICE_FEED=true) when available, otherwise Finnhub's /quote,
which the same FINNHUB_API_KEY already covers (see _price_bar). IB is
preferred, never required: it was the sole entry price source for a while,
which meant an unreachable Gateway silently blocked the system's only
output.

Evidence is only marked as seen (dedup-registered) once it has been
handled DEFINITIVELY -- scored into dossiers, judged not-new, or refuted
by the skeptic. Evidence that can't be scored yet (no ANTHROPIC_API_KEY
configured), hits a transient LLM/API failure, or is deferred by the daily
LLM call budget (usage.py) stays unregistered and is retried on a later
poll; per-dossier merging is idempotent (dossier.has_evidence plus the
in-memory handled-outcome cache) so retries of a partially-processed item
never double-count or double-pay. The retry vehicle is the item simply
REAPPEARING in a later poll, so deferral is only lossless while the item
stays inside its ingestion lookback window (news_lookback_days /
edgar_lookback_days) -- an item still unscored when it ages out is dropped."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smartboi.alerts import AlertSender
from smartboi.config import Settings
from smartboi.dedup import DedupIndex, fingerprint, source_domain
from smartboi.edgar import _truncate_head_tail, describe_8k_items
from smartboi.dossier import (
    DIRECTIONS,
    SCORING_VERSION,
    Dossier,
    DossierStore,
    DossierSynthesizer,
    DossierUpdater,
    EvidenceRecord,
    has_evidence,
    merge_evidence,
    recompute_decay,
    slot_keys,
)
from smartboi.dod_contracts import DodContractsClient, awards_from_page, business_days_back
from smartboi.edgar import EdgarClient, FilingEvent, normalize_company_name
from smartboi.federal_register import (
    CURATED_SEARCHES,
    REGULATOR_SYMBOLS,
    FederalRegisterClient,
)
from smartboi.graph import REL_TYPES, RelationshipExtractor, RelationshipGraph, Relationship
from smartboi.news import FinnhubClient
from smartboi.paper_journal import PaperTradeJournal, cost_bps_per_side_for_cap
from smartboi.prices import PriceBar, ReadOnlyPriceFeed
from smartboi.ratelimit import SlidingWindowLimiter
from smartboi.regsho import RegShoClient
from smartboi.signals import (
    evaluate,
    favorable_drift_pct,
    required_sources,
    is_regular_trading_hours,
    is_trading_day,
    log_decision,
    log_signal,
    signal_expired,
)
from smartboi.skeptic import Skeptic
from smartboi.state import JsonState
from smartboi.status import _STALE_EDGE_DAYS, snapshot_dossier
from smartboi.universe import SEED_RELATIONSHIPS, CompanySpec, spec_by_symbol
from smartboi.universe_screen import guess_ecosystem, recommend_candidate_type, screen_universe
from smartboi.usage import CAT_EXTRACTION, CAT_RESEARCH, CAT_SYNTHESIS, UsageTracker
from smartboi.webapp import run_dashboard

log = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 30
DATA_DIR = Path("data")
# When the IB Gateway is unreachable, retry the connection this often
# instead of waiting out a full price_poll_interval_sec (6h by default) --
# the Gateway restarting daily, or simply not being up yet, shouldn't cost
# most of a day of price marks.
IB_RETRY_GAP_SEC = 900
# How long to wait before retrying a failed Reg SHO refresh. The threshold
# list publishes once per settlement day, so a failure means the file is not
# there yet (or the host is unreachable) -- neither of which resolves in
# seconds. Hourly is frequent enough to pick the file up the same day and
# slow enough that a persistent failure is a handful of requests, not
# hundreds.
REGSHO_RETRY_GAP_SEC = 3600
# Evidence time-decay is a slow-moving correction, not a live signal --
# recomputing it once a day is plenty (see _run_decay_pass). It runs off the
# same persisted daily schedule as the snapshot passes below rather than a
# process-local timer; see _daily_pass_due for why that distinction matters.
# Daily dossier-score snapshot and daily price marks (see _run_daily_snapshot
# / _run_daily_price_marks): the raw material for validating whether
# confidence*magnitude predicts forward returns. Forward data can't be
# backfilled, so this starts accruing from day one regardless of when the
# analysis side of that question gets built.
DAILY_SNAPSHOT_INTERVAL_SEC = 86400
# How often already-discovered, still-ticker-less universe candidates get
# another resolution attempt (see _run_candidate_ticker_recheck) -- daily is
# plenty for something that only changes when SEC's ticker map gains a new
# listing or a name happens to now match Finnhub's fuzzy search; there's no
# reason to burn API calls checking more often than that.
CANDIDATE_RECHECK_INTERVAL_SEC = 86400
# One INFO line roughly this often (see _log_heartbeat) so an idle-but-
# healthy engine -- nothing new to ingest this cycle, which is normal for
# long stretches at this system's polling cadences -- is distinguishable in
# the log from a hung one, instead of the log just going quiet for hours.
HEARTBEAT_INTERVAL_SEC = 600
# Filing forms run through relationship extraction, in addition to feeding
# the dossier engine as direct evidence -- 8-Ks and Form 4s are event-driven
# and rarely restate customer/supplier relationships, so extraction time is
# spent on the two disclosure-heavy forms.
RELATIONSHIP_EXTRACTION_FORMS = ("10-K", "10-Q")
# How much of a filing's text reaches the dossier updater as one evidence
# item. Raised from 4,000: with 8-K exhibits now included (see
# edgar.fetch_evidence_text), the highest-value content in the whole system
# -- the company's own press release announcing a contract award, product
# launch or guidance revision -- arrives in this string, and 4,000 chars
# (~1,000 tokens) truncated a typical release mid-way through the paragraph
# carrying the actual numbers. ~3,000 tokens per filing item is affordable:
# filings are a small minority of evidence volume, and the live deployment
# runs at ~6% of its daily LLM call budget.
FILING_EVIDENCE_CHARS = 12_000
# How far below the signal threshold a SIGNALED-but-unopened dossier must
# fall before a NON-entry-gate path (freshly merged evidence, the daily decay
# pass) is allowed to expire it, expressed as a fraction of the threshold --
# but only until the entry gate has evaluated the episode once. See
# Engine._should_expire_unopened for why the grace period exists at all.
SIGNAL_EXPIRY_HYSTERESIS = 0.8
# Best-effort filter on universe candidates that never resolve to a real
# ticker (see _record_universe_candidate): government bodies, regulators,
# generic customer-class descriptions ("public utilities"), and lenders are
# never investable, so they're just noise in a list meant to be acted on.
# Deliberately only applied AFTER ticker resolution has already failed --
# this can never hide a candidate that actually resolved to a ticker.
# Values a model returns to mean "I don't know a ticker" while still filling
# the field in. Confirmed live: a BAE Systems relationship was recorded
# against the ticker "NULL" and accepted into the universe as an anchor,
# because the literal string is truthy where JSON null would not have been.
_PLACEHOLDER_TICKERS = frozenset({"NULL", "NONE", "N/A", "NA", "NIL", "TBD", "UNKNOWN", "-", "?"})

# Phrases that mark a disclosed "supplier" as a LENDER rather than a supply-
# chain counterparty. A credit agreement genuinely is a disclosed
# relationship, and extraction is right to find it -- but a bank's news has
# no propagation path to a borrower's fundamentals, which is the only thing
# this graph is for. Confirmed live: Bank of America, Wintrust and M&T
# entered the universe this way, off credit-facility disclosures in
# UFPT/LMB/INTT filings. They pass the non-company keyword filter because
# that only applies when ticker resolution FAILS, and a major bank resolves
# perfectly well.
_LENDER_PHRASES = (
    "lender", "credit facility", "credit agreement", "revolving credit",
    "revolving loan", "term loan", "delayed draw", "financing facilit",
    "loan and security agreement", "underwriter",
    # A second wave that got through the list above and reached the graph
    # live: M&T Bank recorded as a "supplier" to Taylor Devices off a
    # "demand line of credit" disclosure. Bank debt is disclosed in a dozen
    # near-synonyms and no single phrase covers them.
    "line of credit", "loan agreement", "promissory note", "notes payable",
    "bank facilit", "financing agreement", "mortgage", "borrower",
)

# Phrases that mark an extracted "relationship" as coming from an EXECUTIVE
# BIOGRAPHY rather than a business dealing. 8-K item 5.02 officer
# appointments (and the bio paragraphs carried into 10-K Part III) name a
# string of well-known employers, and extraction reads "served as VP of
# Illinois Tool Works" as a disclosed link to ITW. Confirmed live: EPAC->ITW,
# EPAC->GE, VVX->RTX and NCSM->APO all came from CV history. These are worse
# than merely useless -- a bio edge feeds an unrelated mega-cap's news into a
# small-cap dossier, and the thesis built on it is pure noise. Phrases are
# deliberately narrow (unambiguous CV markers only) so a genuine commercial
# disclosure that happens to use "served" is not dropped with them.
# A candidate's stashed pending edges (see _record_universe_candidate) are
# persisted JSON, so they are bounded: enough to keep every distinct filer
# that disclosed a counterparty, without letting a widely-named company grow
# an unbounded record.
_MAX_PENDING_EDGES = 12
_MAX_REL_DESCRIPTION = 500

# Ecosystem buckets that are not real ecosystems: "accepted" is where a
# runtime-accepted symbol lands when its discoverer's ecosystem can't be
# inferred, and "?" is guess_ecosystem's own no-answer. Both have held
# dozens of mutually unrelated companies at once, so neither is a basis for
# fanning news between their members.
_UNCLASSIFIED_ECOSYSTEMS = frozenset({"accepted", "?", ""})

# Confidence stamped on an ecosystem-fallback link. Deliberately far below
# dossier.DISCLOSED_LINK_CONFIDENCE (0.85): ecosystem-propagated evidence
# must never satisfy the disclosed-link corroboration relaxation in
# signals.evaluate, so it can raise a thesis but can never single-handedly
# qualify one. It is also passed to the dossier updater and the skeptic, so
# both are told in numbers how weak the causal link is.
ECOSYSTEM_LINK_CONFIDENCE = 0.25

# How fresh the persisted daily synthesis verdict must be for the merge path
# to re-apply it as a cap (see _cap_with_synthesis). The decay pass refreshes
# every calendar day, so 36h is one day plus margin for a missed/failed pass:
# long enough that a merge between decay passes still honours the last
# whole-body verdict, short enough that a stale verdict cannot suppress a
# thesis indefinitely once the daily pass stops updating it.
_SYNTHESIS_CAP_MAX_AGE_HOURS = 36.0

# How many NEW independent-source slots must appear since a verdict was
# rendered before its premises count as materially changed (see
# _maybe_resynthesize). Two, not one: a single new slot is the ordinary
# accumulation the verdict already anticipated, while two independent facts
# the whole-body pass never saw is a different evidence body than the one it
# judged.
#
# Counting SLOTS rather than items is what makes this ungameable, and it is
# the same accounting the signal bar uses (dossier.slot_keys). An
# ecosystem-association burst of thirty correlated macro items mints exactly
# one slot no matter how many arrive; three wire rewrites of one story mint
# one, with dedup dropping two before they arrive. Nothing that cannot move
# the signal bar can invalidate a verdict.
_RESYNTHESIS_MIN_NEW_KEYS = 2

# How far the price must move IN THE THESIS DIRECTION, since the verdict was
# rendered, before an already-priced-in verdict counts as refuted by the tape.
#
# 8% is chosen against the two numbers that bracket it: it is beyond a single
# session's noise for a micro-cap (3-5% daily vol is ordinary in this
# universe), and it is below max_favorable_drift_pct (12%), so a verdict this
# refutes describes a move a re-judged thesis could still plausibly enter.
# Above 12% the entry gate would refuse the trade anyway and re-judging buys
# nothing but an Opus call.
_VETO_FALSIFICATION_DRIFT_PCT = 8.0

# Ceiling on off-schedule re-synthesis calls per UTC day. At synthesis
# pricing this is roughly $0.50/day of Opus in the worst case, and it exists
# so a heavy merge day cannot consume the allowance the daily decay pass
# needs. On exhaustion the verdict STANDS -- the fail-safe direction, since a
# standing veto suppresses a thesis and never opens a trade.
_MAX_RESYNTHESIS_PER_DAY = 5


# Ticker shapes that are NOT the operating-company common stock this system
# builds a thesis about, and must never become trade targets:
#
# - a hyphen (or dot) marks a preferred series or a share class: SCE-PN is a
#   Southern California Edison preferred, TAP-A a Molson Coors class share.
#   A preferred is a fixed-income-like instrument -- it does not respond to
#   an operating catalyst the way the common does, so a thesis built from
#   customer-concentration news is simply not about that security.
# - a five-letter symbol ending in Y or F is the OTC ADR / foreign-ordinary
#   convention (SCRNY, CAJPY, MBGAF, BMWYY): thinly traded, often no SEC
#   filings at all, and priced off an overnight home market.
#
# All of these remain perfectly good ANCHORS -- the underlying company's news
# still propagates. Confirmed live: SCE-PN, TCPA, SCRNY and KODK were
# auto-accepted as TRADEABLE, and SCE-PN accrued the fourth-highest dossier
# score on the board off a utility bond-issuance story.
def is_common_equity(symbol: str) -> bool:
    return _non_common_reason(symbol) == ""


def _non_common_reason(symbol: str) -> str:
    symbol = symbol.upper()
    if "-" in symbol or "." in symbol:
        return "preferred series or share class"
    if len(symbol) == 5 and symbol[-1] in ("Y", "F"):
        return "OTC ADR or foreign ordinary"
    return ""


# The minimum confidence a disclosed relationship must carry to count as a
# real connection during a connectivity reconcile (see
# Engine.reconcile_universe_connectivity). The growth candidates a tradeable's
# own 10-K discloses sit well above this; the floor only keeps a sub-threshold
# passing mention from being treated as a propagation path worth polling a
# whole company's news for.
_CONNECTED_GROWTH_MIN_CONFIDENCE = 0.5


def _anchor_equity_ok(symbol: str) -> bool:
    """Whether a symbol is acceptable as an ANCHOR -- looser than
    is_common_equity, which gates TRADEABLE accepts.

    An anchor never becomes a trade; it exists only to propagate its news
    along disclosed edges. So an OTC ADR or foreign ordinary (Advantest's
    ATEYY, Infineon's IFNNY) is a perfectly good anchor and is allowed here,
    even though is_common_equity blocks it for tradeables. What stays out is a
    preferred series or share class (a hyphen/dot ticker like SCE-PM): a
    securitization/financing vehicle with no company-level news of its own."""
    return "-" not in symbol and "." not in symbol


def _clamp_unit(value, default: float = 0.0) -> float:
    """A model-supplied 0-1 number, clamped. Tool schemas declare min/max but
    Anthropic tool use does not hard-enforce them, so an out-of-range value
    can arrive -- and here it would flow straight into a trade decision."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _clamped_confidence(value, default: float = 0.5) -> float:
    """The extractor's confidence, coerced into [0, 1]. Anthropic tool use
    does not hard-enforce the declared schema, so this must survive a
    missing or non-numeric field rather than raising."""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


_BIOGRAPHY_PHRASES = (
    "prior to joining", "before joining", "prior to his", "prior to her",
    "previously served", "previously held", "previously was", "previously led",
    "most recently served",
    "held various positions", "held senior positions", "held a number of positions",
    "began his career", "began her career", "began their career",
    "started his career", "started her career", "started their career",
    "where he served", "where she served", "where they served",
    "was employed by", "his tenure at", "her tenure at", "years of experience at",
    "former employer", "worked there for", "worked together",
)

# "...held a Group President role at Illinois Tool Works", "...held global
# operations leadership roles at IDEX". Individually, "role at" is too broad
# to filter on and an executive title is far too broad; TOGETHER they are a
# CV line and essentially nothing else. Requiring both is what lets the
# filter catch these without touching a commercial disclosure -- none of
# "GM accounted for 12% of product revenues", "PACCAR is one of SRI's
# principal customers" or "Boeing generated 13% of 2025 net revenues"
# contains either half.
_BIOGRAPHY_TITLES = (
    "ceo", "cfo", "coo", "cto", "chief ", "president", "vice president",
    "evp", "svp", "management team", "board member", "director of",
)
_BIOGRAPHY_ROLE_MARKERS = ("role at", "roles at", "positions at", "position at")

_NON_COMPANY_KEYWORDS = (
    "government", "department", "agency", "administration", "bureau",
    "army", "navy", "air force", "military", "board", "authority",
    "commission", "ministry", "federal reserve", "internal revenue",
    "regulatory", "utilities", "utility", "organizations", "organization",
    "producers", "manufacturers", "operators", "owners", "customers",
    "companies", "corporations", "brands", "institutions", "agencies",
    "capital partners", "capital management", "bank", "financial",
    "equipment finance",
)


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        log_dir = Path(settings.log_dir)

        self.dedup = DedupIndex(DATA_DIR / "dedup_index.json")
        self.graph = RelationshipGraph(DATA_DIR / "graph.json")
        self.dossiers = DossierStore(DATA_DIR / "dossiers")
        self.journal = PaperTradeJournal(log_dir / "paper_trades.jsonl")
        self.universe_screen_state = JsonState(DATA_DIR / "universe_screen_state.json")
        self.periodic_state = JsonState(DATA_DIR / "periodic_pass_state.json")
        self.backfill_state = JsonState(DATA_DIR / "relationship_backfill.json")
        self.candidates = JsonState(DATA_DIR / "universe_candidates.json")
        # Which anchors have HAD a supplier-research call, as opposed to
        # which ones produced a candidate -- see research.researched_anchors.
        # Separate file rather than a sentinel row in universe_candidates so
        # a bookkeeping marker can never be mistaken for a candidate by
        # ticker resolution, the screen, auto-accept or the dashboard count.
        self.research_state = JsonState(DATA_DIR / "anchor_research.json")
        # The same bookkeeping for the EDGAR full-text search, kept SEPARATE
        # from research_state on purpose. The two passes ask different
        # questions of the same anchor -- "who does the web say supplies it"
        # versus "which other FILERS name it" -- so an anchor covered by one
        # has not been covered by the other, and sharing a marker would let
        # whichever pass ran first permanently suppress the other.
        self.edgar_search_state = JsonState(DATA_DIR / "anchor_edgar_search.json")
        self.accepted_candidates = JsonState(DATA_DIR / "accepted_candidates.json")
        # Symbols removed from the live universe by a graph-maintenance clean,
        # with the finding that removed them and the row they had. Quarantine
        # rather than delete, for the same reason _block_junk_candidates marks
        # rather than deletes: a removal is a judgement, and a judgement a
        # human cannot see or reverse is indistinguishable from data loss.
        #
        # It is also load-bearing, not just a receipt. Without it the next
        # auto-accept or connectivity reconcile would re-add the symbol from
        # the same candidate row that produced it, and the clean would undo
        # itself on a schedule.
        self.quarantine = JsonState(DATA_DIR / "quarantined_symbols.json")
        # Last audit result, for the dashboard's graph-health panel. Persisted
        # rather than recomputed on each page load because the audit needs
        # SEC's ticker map and a name check per accepted symbol -- cheap on a
        # daily cadence, wasteful on every 30-second dashboard poll.
        self.audit_state = JsonState(DATA_DIR / "graph_audit.json")
        # Per-accession "relationship extraction already ran" markers for the
        # POLL path. Extraction (a paid ~150k-char call) runs before dossier
        # scoring in _process_filing, and the filing's dedup fingerprint is
        # registered only once scoring completes DEFINITIVELY -- so when the
        # dossier budget is exhausted, the filing stays unregistered and the
        # next EDGAR poll re-fetches and RE-EXTRACTS it, burning the extraction
        # budget on a filing already extracted (graph.add dedupes, so it is
        # pure spend). This marker is checked before extracting and set after,
        # so a budget-deferred filing retries only its SCORING, not extraction.
        self.extracted_filings = JsonState(DATA_DIR / "extracted_filings.json")
        self._prune_extracted_filings()
        # {date, count} -- the UTC-daily auto-accept budget (see
        # _auto_accept_candidates). Persisted so a restart cannot reset the
        # cap and let a long candidate list through in one afternoon.
        self.auto_accept_state = JsonState(DATA_DIR / "auto_accept_state.json")
        # Off-schedule re-synthesis budget (see _maybe_resynthesize). Kept
        # separate from the LLM usage budget rather than folded into it: this
        # bounds how much of the day's synthesis allowance a busy MERGE day
        # can consume, so re-judgement can never starve the daily pass that
        # is synthesis's real job.
        self.resynthesis_state = JsonState(DATA_DIR / "resynthesis_state.json")
        # Per-symbol 6-K ingestion counts for one filing date (see
        # _within_6k_daily_cap).
        self.sixk_state = JsonState(DATA_DIR / "sixk_state.json")
        # Which model snapshots produced the existing record -- see
        # _check_model_provenance for why a change to these matters.
        self.model_state = JsonState(DATA_DIR / "model_provenance.json")
        self.alerts = AlertSender(settings.alert_webhook_url)
        self.usage = UsageTracker(
            DATA_DIR / "llm_usage.json", settings.max_daily_llm_calls,
            daily_usd_budget=settings.max_daily_usd,
            # DOSSIER is absent on purpose -- see usage.py. It is guaranteed
            # whatever these three cannot reach, and can use the whole day
            # when they are idle.
            category_shares={
                CAT_EXTRACTION: settings.budget_share_extraction,
                CAT_SYNTHESIS: settings.budget_share_synthesis,
                CAT_RESEARCH: settings.budget_share_research,
            },
            # Reserved, not merely capped -- see usage.py. Synthesis runs on
            # the daily decay pass, whose slot drifts to whatever hour it
            # first ran at, so it cannot rely on being early in the day.
            category_reserved={CAT_SYNTHESIS: settings.budget_reserve_synthesis},
        )

        self.universe: list[CompanySpec] = list(settings.universe)
        self._apply_accepted_candidates()
        self.spec_by_symbol = spec_by_symbol(self.universe)

        self._propagation_limiter = SlidingWindowLimiter(
            settings.max_propagated_evidence_per_link,
            settings.propagated_evidence_cooldown_hours * 3600,
        )
        # Ecosystem fan-out gets its OWN, tighter budget rather than sharing
        # the disclosed-link one: it reaches every tradeable in an ecosystem
        # rather than a named counterparty, so at 20 anchors x 4 tradeables
        # a single shared allowance would let the weaker evidence crowd out
        # the stronger.
        self._ecosystem_limiter = SlidingWindowLimiter(
            settings.max_ecosystem_evidence_per_link,
            settings.propagated_evidence_cooldown_hours * 3600,
        )
        # Caches a dossier-update proposal (evidence_id -> proposed dict)
        # between the propose_update call and the skeptic call, so that if
        # the skeptic call is deferred by an exhausted daily LLM call budget
        # (see usage.py), the retry on a later poll doesn't re-pay for
        # propose_update -- it already has the answer, it just needs the
        # skeptic's verdict. Values are (proposal, cached_at_wallclock) so
        # entries whose evidence has aged out of every ingestion lookback
        # window (and so can never be retried) are evicted instead of leaking
        # forever.
        self._pending_proposals: dict[str, tuple[dict, float]] = {}
        # (target_symbol, evidence_id) pairs handled definitively WITHOUT a
        # merge (skeptic-refuted, judged not-new, or a malformed proposal).
        # has_evidence() makes the merged outcome idempotent across retries,
        # but these outcomes previously left no marker at all -- so when a
        # SIBLING target of the same evidence item was budget-deferred, the
        # retry re-paid propose_update (and the skeptic) for targets that
        # were already definitively done, and a second skeptic run could
        # even accept what the first had refuted.
        self._handled_outcomes: dict[str, float] = {}
        # Both caches are PERSISTED (with wall-clock stamps) rather than kept
        # purely in-memory. The HA deployment restarts several times a day, and
        # a purely-in-memory _handled_outcomes reintroduced a fixed bug through
        # the restart door: a refuted/not-new marker was forgotten on restart,
        # so the still-unregistered evidence (a sibling target had deferred)
        # was re-proposed and a nondeterministic second skeptic run could
        # ACCEPT what the first had refuted. Persisting closes that door and
        # also spares the re-paid propose_update on a mid-flight restart.
        # Federal Register poll clock. Process-local like the news/EDGAR
        # polls: a restart re-reading a 3-day window is a handful of cheap
        # requests that dedupe to nothing, not a cost worth persisting for.
        self._last_fedreg_poll: float | None = None
        self._last_dod_poll: float | None = None
        # Backoff after a failed Reg SHO refresh -- see the retry_after gate
        # in _tick for why a bare "not done yet" retries every 30 seconds.
        self._regsho_retry_after: float = 0.0
        self._retry_state = JsonState(DATA_DIR / "retry_state.json")
        self._load_retry_state()

        self.edgar_client: EdgarClient | None = None
        self.finnhub: FinnhubClient | None = None
        self.extractor: RelationshipExtractor | None = None
        self.updater: DossierUpdater | None = None
        self.skeptic: Skeptic | None = None
        self.synthesizer: DossierSynthesizer | None = None
        self.price_feed: ReadOnlyPriceFeed | None = None
        # Free and unauthenticated -- no key to be missing and no budget to
        # compete for, so the only gate is the setting. With it off, or with
        # the list empty, the borrow flag falls back to the market-cap proxy
        # (see paper_journal.assumes_borrow).
        self.regsho: RegShoClient | None = (
            RegShoClient() if settings.enable_regsho else None
        )
        # Also free and unauthenticated. Scoped to a handful of hand-written
        # searches -- the Federal Register publishes ~200 documents a business
        # day and watching it broadly is a disqualifying firehose.
        self.fedreg: FederalRegisterClient | None = (
            FederalRegisterClient(user_agent=settings.edgar_user_agent or "SmartBoi")
            if settings.enable_federal_register else None
        )
        # DoD daily contract announcements. Free, no auth, ~0-3 matched items
        # per business day once the value floor and the alias table have had
        # their say.
        self.dod: DodContractsClient | None = (
            DodContractsClient(user_agent=settings.edgar_user_agent or "SmartBoi")
            if settings.enable_dod_contracts else None
        )

        self._warned: set[str] = set()
        # None = never polled this process -> due immediately on the first
        # tick. (These are time.monotonic() marks; comparing against an
        # initial 0.0 would compare against machine BOOT time and delay the
        # first pass by up to a full interval after a reboot.)
        self._last_edgar_poll: float | None = None
        self._last_news_poll: float | None = None
        self._last_price_poll: float | None = None
        # Whether a SIGNALED-but-unopened dossier is waiting on the entry
        # gate, which tightens the price-poll cadence (see
        # _price_poll_interval). Kept as a flag rather than rescanning every
        # dossier file each 30-second tick. Set True whenever a dossier
        # becomes SIGNALED and recomputed exactly by _mark_and_execute, which
        # already loads every dossier -- so it starts True (one accurate
        # recompute on the first poll) and any stale True self-corrects on
        # the very next poll, at the cost of one wasted price call.
        self._entry_pending: bool = True
        self._last_candidate_recheck: float | None = None
        self._last_heartbeat: float | None = None
        # Backfill retries after a deferred extraction are held off until
        # this monotonic time -- without it, an exhausted daily LLM budget
        # would re-fetch each pending symbol's full 10-K text from EDGAR
        # every 30-second tick for the rest of the day, achieving nothing.
        self._backfill_retry_after: float = 0.0
        # Same shape for a failed daily price-mark pass (every source down):
        # stays DUE (the day must not be lost) but retries on a backoff, not
        # every 30-second tick.
        self._price_marks_retry_after: float = 0.0
        self._dashboard_task: asyncio.Task | None = None
        self._closing = False

    @property
    def symbol_list(self) -> list[str]:
        """The LIVE symbol list -- unlike settings.symbol_list (fixed at
        startup from SYMBOLS/ANCHOR_SYMBOLS), this reflects self.universe,
        which grows at runtime as candidates are accepted (see
        accept_candidate) without requiring a restart."""
        return [c.symbol for c in self.universe]

    @staticmethod
    def _accepted_entry(value) -> tuple[str, str, str]:
        """(as_type, source, ecosystem) from an accepted_candidates.json
        entry. Entries were originally a bare "tradeable"/"anchor" string,
        then a {"as", "source"} dict recording whether a human or the engine
        accepted it, and now additionally carry the ecosystem accept_candidate
        classified them into -- every shape is read, so an existing file keeps
        working untouched across the upgrade."""
        if isinstance(value, dict):
            return (
                value.get("as", "tradeable"),
                value.get("source", "manual"),
                value.get("ecosystem") or "accepted",
            )
        return value, "manual", "accepted"

    def _apply_accepted_candidates(self) -> None:
        """Rebuilds runtime-accepted symbols into the live universe at
        startup.

        The ecosystem comes from the persisted entry, not a hardcoded
        "accepted". accept_candidate has always classified an acceptance into
        the ecosystem of whoever disclosed it (guess_ecosystem), but only
        stored {"as", "source"} -- so the classification lived in memory and
        died at every restart, and this function rebuilt all of them into the
        literal "accepted" bucket. That bucket is in _UNCLASSIFIED_ECOSYSTEMS,
        which makes _ecosystem_targets return [] and _can_produce_evidence
        return False, so every restart quietly re-converted the accepted
        anchors into inert symbols whose news is never fetched. Live, that was
        64 anchors -- and auto-accept keeps manufacturing more at up to 20 a
        day. See _reclassify_accepted_ecosystems for the repair of entries
        written before the ecosystem was persisted."""
        known = {c.symbol for c in self.universe}
        for symbol, value in self.accepted_candidates.data.items():
            if symbol in known:
                continue
            as_type, source, ecosystem = self._accepted_entry(value)
            self.universe.append(
                CompanySpec(symbol, symbol, ecosystem, signal_source_only=(as_type == "anchor"),
                            notes=f"Accepted ({source}) from a discovered universe candidate")
            )
            known.add(symbol)

    def _reclassify_accepted_ecosystems(self) -> int:
        """One-shot repair for acceptances written before the ecosystem was
        persisted: re-run guess_ecosystem over each unclassified accepted
        symbol's own discovery record and write the answer back.

        Without this, every already-accepted symbol stays in the
        "accepted" bucket forever -- persisting the field only helps symbols
        accepted from here on, and the live deployment has a 69-symbol
        backlog that would never be revisited (a candidate is only accepted
        once). Runs after the universe is built so guess_ecosystem resolves
        against curated AND already-classified accepted specs, and iterates
        until it stops making progress, so a chain (accepted symbol
        discovered via another accepted symbol) resolves rather than
        depending on dict order."""
        repaired = 0
        for _ in range(len(self.accepted_candidates.data) or 1):
            progressed = 0
            for symbol, value in list(self.accepted_candidates.data.items()):
                as_type, source, ecosystem = self._accepted_entry(value)
                if ecosystem not in _UNCLASSIFIED_ECOSYSTEMS:
                    continue
                related = (self.candidates.get(symbol) or {}).get("related_to") or []
                guessed = guess_ecosystem(related, self.spec_by_symbol)
                if guessed in _UNCLASSIFIED_ECOSYSTEMS:
                    continue
                self.accepted_candidates.set(
                    symbol, {"as": as_type, "source": source, "ecosystem": guessed}
                )
                spec = self.spec_by_symbol.get(symbol)
                if spec is not None:
                    updated = CompanySpec(
                        spec.symbol, spec.name, guessed,
                        signal_source_only=spec.signal_source_only, notes=spec.notes,
                    )
                    self.universe = [updated if c.symbol == symbol else c for c in self.universe]
                    self.spec_by_symbol[symbol] = updated
                progressed += 1
            repaired += progressed
            if not progressed:
                break
        if repaired:
            log.info(
                "[UNIVERSE] Reclassified %d previously-unclassified accepted symbol(s) into a real "
                "ecosystem. Their news can now reach a tradeable via ecosystem-fallback propagation; "
                "before this they were inert on every restart.", repaired,
            )
        return repaired

    def accept_candidate(self, symbol: str, as_type: str, source: str = "manual") -> CompanySpec:
        """Adds a discovered universe candidate (see
        _record_universe_candidate) into the LIVE universe, no restart
        required: EDGAR/news polling picks it up on their next due tick
        (both iterate self.symbol_list, not a value fixed at startup), and
        the relationship backfill runs it on the next tick too (see
        _run_relationship_backfill, no longer gated to "once per process").
        Persisted so it survives a restart without editing SYMBOLS/
        ANCHOR_SYMBOLS by hand. Idempotent -- accepting an already-accepted
        symbol just returns its existing spec."""
        symbol = symbol.upper()
        existing = self.spec_by_symbol.get(symbol)
        if existing is not None:
            return existing

        # A large, heavily-covered company must never become a trade target,
        # however it was requested. The engine already computed that judgement
        # (recommend_candidate_type, from live market cap and analyst count);
        # this stops a click -- or a caller passing the wrong type -- from
        # overriding it. Confirmed live: a deployment accumulated ten
        # mega/large caps as TRADEABLE this way, including a $323B pharma,
        # which then accrued dossiers and LLM spend on a name whose news has
        # no diffusion lag left to trade.
        entry = self.candidates.get(symbol) or {}
        recommended = entry.get("recommended_as")
        if as_type == "tradeable" and not is_common_equity(symbol):
            raise ValueError(
                f"{symbol} does not look like common equity ({_non_common_reason(symbol)}). "
                "Add it as an anchor instead -- the underlying company's news still propagates, "
                "and the thesis this system builds is about operating-company equity."
            )
        # DEFAULT-DENY, not default-allow. This used to reject only an
        # explicit "anchor", which meant recommended_as = None sailed
        # through -- and None is the normal state for a freshly discovered
        # candidate, or for any candidate at all when the market-cap/analyst
        # lookup hasn't run. So the one path this guard exists to block
        # (adding a name with zero vetting, which is the incident the
        # comment above describes) was reachable by clicking "+ Tradeable"
        # on anything new. Unvetted and screens-as-anchor now get the same
        # answer, with the same escape hatch.
        if as_type == "tradeable" and recommended != "tradeable":
            reason = (
                entry.get("recommendation_reason")
                or ("no market-cap/analyst screen has run for it yet -- run the universe screen, "
                    "or the 'Screen candidates' tool, and accept it once it has a recommendation")
            )
            raise ValueError(
                f"{symbol} does not screen as a trade target ({reason}). "
                "Add it as an anchor instead -- its news will still propagate. "
                "If you really want it tradeable, put it in SYMBOLS."
            )
        # Classified into the ecosystem of whoever disclosed it, not parked
        # in a flat "accepted" bucket. That bucket had become a pseudo-
        # ecosystem holding 64 unrelated anchors (PepsiCo, United Airlines,
        # Brookfield...) next to five unrelated tradeables, which is
        # meaningless on its own and actively harmful to ecosystem-fallback
        # propagation (see _ecosystem_targets), where it would have fanned
        # airline news into a photographic-film company.
        ecosystem = guess_ecosystem(entry.get("related_to") or [], self.spec_by_symbol)
        if ecosystem in ("?", ""):
            ecosystem = "accepted"
        spec = CompanySpec(symbol, symbol, ecosystem, signal_source_only=(as_type == "anchor"),
                            notes=f"Accepted ({source}) from a discovered universe candidate")
        self.universe.append(spec)
        self.spec_by_symbol[symbol] = spec
        # Stored as a dict (not a bare type string) once a source is
        # recorded, so an auto-accepted symbol is distinguishable from one a
        # human chose -- _apply_accepted_candidates reads both shapes, so
        # existing files written before this keep working untouched.
        #
        # The ECOSYSTEM is persisted alongside. It used not to be, so this
        # classification existed only in memory: the very next restart
        # rebuilt the symbol into the flat "accepted" bucket, which
        # _ecosystem_targets treats as unclassified and refuses to propagate
        # from. Every acceptance was silently undone within hours.
        self.accepted_candidates.set(
            symbol, {"as": as_type, "source": source, "ecosystem": ecosystem}
        )
        promoted = self._promote_pending_edges(symbol)
        log.info("[CANDIDATE] %s accepted (%s) into the universe as %s -- polled starting next cycle, "
                 "%d disclosed relationship(s) written into the graph.", symbol, source, as_type, promoted)
        return spec

    def _promote_pending_edges(self, symbol: str) -> int:
        """Writes the relationships that DISCOVERED this candidate into the
        graph, now that both ends are in the universe.

        Without this a newly accepted symbol arrives unconnected, and an
        anchor with no edge to a tradeable is inert by construction: it is
        never its own analysis target, so an article about it resolves to
        zero targets and is fingerprinted and discarded without a single
        LLM call. It could only ever gain an edge by the discovering filer
        filing again -- annually, for a 10-K -- because that filer is
        already marked backfilled and backfill skips anchors anyway.

        Costs nothing: the relationship was already extracted and paid for
        when the candidate was recorded (see _record_universe_candidate),
        so this is a deterministic replay, not a re-extraction. graph.add
        dedupes on (from, to, rel_type), so promoting twice is harmless."""
        entry = self.candidates.get(symbol) or {}
        pending = entry.get("pending_edges") or []
        if not pending:
            return 0
        known = set(self.symbol_list)
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        for p in pending:
            from_symbol = p.get("from_symbol") or ""
            rel_type = p.get("rel_type") or ""
            # The discovering filer can itself have left the universe since
            # (pruned, demoted, reset) -- an edge to a non-member is dead
            # weight the graph should not carry.
            if from_symbol not in known or from_symbol == symbol or rel_type not in REL_TYPES:
                continue
            if self.graph.add(
                Relationship(
                    from_symbol=from_symbol,
                    to_symbol=symbol,
                    rel_type=rel_type,
                    description=str(p.get("description") or ""),
                    source=str(p.get("source") or ""),
                    confidence=_clamped_confidence(p.get("confidence")),
                    extracted_at=now,
                )
            ):
                added += 1
                log.info("[GRAPH] %s -> %s (%s, confidence=%.2f) promoted from a discovered candidate: %s",
                         from_symbol, symbol, rel_type, _clamped_confidence(p.get("confidence")),
                         str(p.get("description") or "")[:70])
        return added

    def _tradeable_links(self, entry: dict) -> list[tuple[str, str, float]]:
        """The disclosed edges from a CURRENT tradeable that this candidate's
        pending edges would write into the graph the moment it is accepted
        (see _promote_pending_edges). Non-empty means the candidate lands
        CONNECTED -- its news reaches a real analysis target.

        Only a pending edge whose from_symbol is tradeable RIGHT NOW counts:
        an edge from another anchor makes an anchor->anchor link, which still
        resolves to zero dossiers (see gather_coverage), so it does not make
        the candidate worth polling. This is the predicate the connectivity
        reconcile and the gated anchor auto-accept both decide on."""
        tradeables = {c.symbol for c in self.universe if not c.signal_source_only}
        links = set()
        for p in entry.get("pending_edges") or []:
            frm = p.get("from_symbol") or ""
            rel = p.get("rel_type") or ""
            conf = _clamped_confidence(p.get("confidence"))
            if frm in tradeables and rel in REL_TYPES and conf >= _CONNECTED_GROWTH_MIN_CONFIDENCE:
                links.add((frm, rel, round(conf, 2)))
        return sorted(links)

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(message)

    def _check_model_provenance(self) -> None:
        """Warns loudly when a model snapshot changes while a track record
        already exists.

        Forward-only testing is this system's central validity claim, and a
        model swap quietly invalidates the part of the record that predates
        it: the new snapshot's training cutoff may postdate evidence it is
        now being asked to "predict", and look-ahead contamination of that
        kind is measurable, large, and not fixable by prompting. The correct
        response to this warning is to treat what follows as a NEW track
        record reported separately -- not to keep appending to the old one,
        and never to re-score existing dossier_snapshots.jsonl rows with the
        new model."""
        current = {
            "extraction_model": self.settings.extraction_model,
            "dossier_model": self.settings.dossier_model,
            "skeptic_model": self.settings.skeptic_model,
        }
        previous = self.model_state.get("models")
        if previous and previous != current:
            changed = [k for k in current if previous.get(k) != current.get(k)]
            log.warning(
                "[PROVENANCE] Model snapshot(s) changed since the last run: %s. "
                "Everything scored before now was judged by a DIFFERENT model, so the "
                "forward track record is no longer one continuous out-of-sample test. "
                "Treat what follows as a new record, report it separately, and do not "
                "re-score historical snapshots with the new model.",
                ", ".join(f"{k}: {previous.get(k)} -> {current[k]}" for k in changed),
            )
            self.model_state.set("changed_at", datetime.now(timezone.utc).isoformat())
        self.model_state.set("models", current)

    def _seed_regulator_edges(self) -> None:
        """Hand-seeded `regulator` edges from each pseudo-member to the
        companies its curated searches declare.

        Seeded at 0.60-0.80, deliberately BELOW dossier.DISCLOSED_LINK_CONFIDENCE
        (0.85). A sector-wide rule can raise a thesis but must never buy the
        corroboration discount a quantified customer disclosure earns: "the EPA
        set HFC allowances" is real and material, and it is not evidence that
        some other item about the company is independently corroborated.

        Targeted searches seed a little higher than ecosystem-wide ones for the
        obvious reason -- a proceeding that names the company is a tighter
        causal link than a rule that moves its whole sector."""
        if not self.settings.enable_federal_register:
            return
        known = set(self.symbol_list)
        now = datetime.now(timezone.utc).isoformat()
        for search in CURATED_SEARCHES:
            targets = list(search.targets)
            if search.ecosystem:
                targets += [c.symbol for c in self.universe
                            if c.ecosystem == search.ecosystem and not c.signal_source_only]
            for target in targets:
                if target not in known or target == search.regulator:
                    continue
                # Direction follows the graph's convention: rel_type describes
                # what to_symbol IS to from_symbol (see graph.Relationship), so
                # the REGULATOR is the target of the edge, not its source.
                # Reachability is unaffected either way -- linked_symbols walks
                # both directions -- but the description text is read by the
                # dossier updater and the skeptic, and a backwards edge reads
                # as "AOSL is a regulator of BIS".
                self.graph.add(Relationship(
                    target, search.regulator, "regulator",
                    f"{search.regulator} ({REGULATOR_SYMBOLS.get(search.regulator, '')}) is a "
                    f"regulator of {target}: actions matching '{search.term}' bear on it. "
                    f"{search.note}",
                    "regulator seed",
                    0.80 if target in search.targets else 0.60,
                    now,
                ))

    def _seed_graph(self) -> None:
        """Seeds the well-documented DEFAULT_UNIVERSE relationships
        (SEED_RELATIONSHIPS) -- but only the ones where BOTH companies are
        actually in the live universe. A custom SYMBOLS/ANCHOR_SYMBOLS
        deployment that doesn't include e.g. UCTT/AMAT must not have their
        relationship silently seeded into its graph just because it's
        hardcoded here; every edge in a custom universe should come from
        that universe's own filings, same as any symbol accepted later via
        the dashboard."""
        known = set(self.symbol_list)
        now = datetime.now(timezone.utc).isoformat()
        for from_sym, to_sym, rel_type, description, confidence in SEED_RELATIONSHIPS:
            if from_sym not in known or to_sym not in known:
                continue
            self.graph.add(
                Relationship(from_sym, to_sym, rel_type, description, "manual seed", confidence, now)
            )

    async def start(self) -> None:
        self._check_model_provenance()
        self._seed_graph()
        self._seed_regulator_edges()
        # Repairs acceptances written before the ecosystem was persisted (see
        # _apply_accepted_candidates). Runs here rather than in __init__ so it
        # resolves against the seeded graph and the fully-built universe.
        self._reclassify_accepted_ecosystems()

        if self.settings.enable_edgar_ingestion and self.settings.edgar_user_agent.strip():
            self.edgar_client = EdgarClient(self.settings.edgar_user_agent, DATA_DIR / "edgar_cik_cache.json")
            log.info("EDGAR ingestion: ENABLED")
        else:
            self._warn_once(
                "edgar",
                "EDGAR ingestion disabled -- set ENABLE_EDGAR_INGESTION=true and EDGAR_USER_AGENT "
                "('Your Name your@email.com') to enable.",
            )

        if self.settings.enable_news_ingestion and self.settings.finnhub_api_key:
            self.finnhub = FinnhubClient(self.settings.finnhub_api_key)
            log.info("News ingestion: ENABLED")
        else:
            self._warn_once(
                "news",
                "News ingestion disabled -- set ENABLE_NEWS_INGESTION=true and FINNHUB_API_KEY to enable.",
            )

        if self.settings.anthropic_api_key:
            self.extractor = RelationshipExtractor(self.settings.anthropic_api_key, self.settings.extraction_model, self.usage)
            self.updater = DossierUpdater(self.settings.anthropic_api_key, self.settings.dossier_model, self.usage)
            self.skeptic = Skeptic(self.settings.anthropic_api_key, self.settings.skeptic_model, self.usage)
            # Its OWN model, and the only expensive one in the pipeline.
            # Synthesis is the hardest judgement here (is this N facts or
            # one fact N times?) and the only pass that can make it, and
            # it runs at most once a day per near-the-bar dossier -- so a
            # dozen-odd calls carry the reasoning budget for the whole
            # system. See config.py's model-tiering note.
            self.synthesizer = DossierSynthesizer(
                self.settings.anthropic_api_key, self.settings.synthesis_model, self.usage
            )
            log.info(
                "Dossier engine (Claude): ENABLED (daily LLM call budget: %d, see MAX_DAILY_LLM_CALLS)",
                self.settings.max_daily_llm_calls,
            )
        else:
            self._warn_once(
                "anthropic",
                "ANTHROPIC_API_KEY not set -- evidence will be collected but not scored yet. "
                "Unscored items are retried automatically once a key is configured, but only "
                "while they remain inside the ingestion lookback windows (news: "
                f"{self.settings.news_lookback_days}d, EDGAR: {self.settings.edgar_lookback_days}d) "
                "-- anything older by then is not scored.",
            )

        # Logged unconditionally, at startup, in one greppable line. Every
        # one of these is overridable from the add-on's options.json, which
        # wins over the code default -- so "the documented threshold" and
        # "the threshold this deployment is running" are different
        # questions, and until now the log answered neither: `grep -i
        # threshold` over the entire live log returned nothing at all.
        log.info(
            "Signal bar in force: score >= %.3f, sources >= %d (>= %d when news-only), "
            "scoring_version=%d, synthesis floor %.3f. These are what the record is being "
            "written against -- the code defaults are 0.65/2/3 and options.json overrides them.",
            self.settings.signal_confidence_threshold,
            self.settings.min_independent_sources,
            self.settings.min_independent_sources_news_only,
            SCORING_VERSION,
            self.settings.signal_confidence_threshold * self.settings.synthesis_score_floor_pct,
        )

        if self.settings.enable_ib_price_feed:
            self.price_feed = ReadOnlyPriceFeed(
                self.settings.ib_host, self.settings.ib_port, self.settings.ib_client_id
            )
            # Checked right away rather than waiting for the first price
            # poll (up to price_poll_interval_sec, 6h by default) so a
            # misconfigured host/port or a Gateway that's simply not up yet
            # is visible in the startup log immediately, not silently hours
            # later. Never blocks startup -- ensure_connected() itself never
            # raises, and the poll loop keeps retrying regardless.
            if await self.price_feed.ensure_connected():
                log.info("Read-only IB price feed: CONNECTED to %s:%s.", self.settings.ib_host, self.settings.ib_port)
            else:
                log.warning(
                    "Read-only IB price feed: could not connect to %s:%s at startup -- will keep retrying "
                    "every %d min in the background. Signals are still detected and logged "
                    "(logs/signals.jsonl), and entries/marks fall back to Finnhub quotes, while unreachable.",
                    self.settings.ib_host, self.settings.ib_port, IB_RETRY_GAP_SEC // 60,
                )
        if not self._has_price_source():
            log.warning(
                "No price source configured (ENABLE_IB_PRICE_FEED is off and there is no FINNHUB_API_KEY) "
                "-- signals will be detected and logged, but no paper trade can ever be OPENED or marked. "
                "A Finnhub free-tier key alone is enough for the full paper-trade loop.",
            )
        else:
            self._warn_once(
                "ib",
                "IB price feed disabled -- signals will be detected and logged (logs/signals.jsonl), "
                "but no hypothetical position can be opened or marked to market until "
                "ENABLE_IB_PRICE_FEED=true. This never places real orders (see prices.py).",
            )

        if self.alerts.enabled:
            log.info("Webhook alerts: ENABLED (signals and paper trade opens/closes)")
        else:
            log.info("Webhook alerts: disabled -- set ALERT_WEBHOOK_URL (e.g. a Home Assistant "
                     "webhook trigger) to get notified of signals and paper trades.")

        if self.settings.enable_dashboard:
            self._dashboard_task = asyncio.create_task(self._run_dashboard_safely())

    async def _run_dashboard_safely(self) -> None:
        try:
            await run_dashboard(self)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dashboard failure must never take down ingestion
            log.exception("Dashboard failed to start or crashed; ingestion continues.")

    async def run_forever(self) -> None:
        await self.start()
        log.info("SmartBoi engine running. Universe: %s", self.symbol_list)
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad tick must not kill the process
                    log.exception("Unhandled error in engine tick, continuing.")
                await asyncio.sleep(TICK_INTERVAL_SEC)
        except asyncio.CancelledError:
            log.info("Engine stopping...")
            raise
        finally:
            self._closing = True
            if self._dashboard_task is not None:
                self._dashboard_task.cancel()
            if self.edgar_client is not None:
                await self.edgar_client.aclose()
            if self.finnhub is not None:
                await self.finnhub.aclose()
            if self.extractor is not None:
                await self.extractor.aclose()
            if self.updater is not None:
                await self.updater.aclose()
            if self.skeptic is not None:
                await self.skeptic.aclose()
            if self.synthesizer is not None:
                await self.synthesizer.aclose()
            # Both own an httpx.AsyncClient and both were missing here, so
            # every restart leaked two connection pools.
            if self.fedreg is not None:
                await self.fedreg.aclose()
            if self.dod is not None:
                await self.dod.aclose()
            if self.price_feed is not None:
                self.price_feed.disconnect()
            await self.alerts.aclose()

    @staticmethod
    def _due(last: float | None, interval_sec: float, now: float) -> bool:
        return last is None or now - last >= interval_sec

    def _price_poll_interval(self) -> float:
        """price_poll_interval_sec normally (6h by default: marking open
        trades to market needs nothing tighter), but the shorter
        signal_entry_poll_interval_sec while an entry is pending.

        Marking and entering are different jobs on different clocks. A
        signal that fires mid-session and then waits up to six hours for the
        next poll will usually be looked at while the market is shut -- and
        in the meantime the daily decay pass or newly merged evidence can
        expire it back to ACTIVE. That is exactly what happened to the first
        signal this system ever produced: it never got a single entry
        evaluation. The tight cadence only applies while something is
        actually waiting, so the steady-state IB request rate is unchanged."""
        # OPEN TRADES tighten it too, not just pending entries. They used
        # not to: _entry_pending goes False the moment a signal becomes a
        # position, so the interval reverted to 6 hours exactly when the
        # system first had money (hypothetically) at risk. Marks are checked
        # against the day's accumulating high/low, so a stop breached at
        # 10am was visible at the next poll -- up to six hours later, and
        # the fill recorded is `min(stop_price, price_at_detection)`, so a
        # position still falling when we finally looked booked a WORSE fill
        # than a real stop order would have taken. That biases the R
        # statistic this journal exists to make honest.
        #
        # The cost is negligible and bounded by position count, not universe
        # size: a handful of open symbols at the entry cadence is a few
        # requests an hour, against IB's ~60-per-10-minutes and Finnhub's
        # 60-per-minute.
        if self._entry_pending or self.journal.open_trades:
            return min(self.settings.signal_entry_poll_interval_sec, self.settings.price_poll_interval_sec)
        return self.settings.price_poll_interval_sec

    async def _run_entry_and_marking_poll(self) -> None:
        """The price poll: evaluate pending entries and mark open trades.

        Reads the clock ITSELF rather than taking the tick's opening
        timestamp. It used to be tested against a `now` captured before a
        full EDGAR + news sweep ran, so on a slow tick the due-check compared
        against a clock that was already minutes stale and judged the poll
        not-due when it was. That systematically under-ran the one pass that
        produces this system's only output."""
        now = time.monotonic()
        price_interval = self._price_poll_interval()
        if not self._has_price_source() or not self._due(self._last_price_poll, price_interval, now):
            return
        # IB is opportunistic here, not required. It used to gate the whole
        # pass: an unreachable Gateway meant _mark_and_execute never ran, so
        # no signal could become a paper trade and no open trade could be
        # marked -- even with a perfectly good Finnhub quote available and
        # already being used for the daily price marks. Now the connection
        # attempt only decides which SOURCE serves the pass (see _price_bar),
        # never whether it happens.
        ib_up = self.price_feed is not None and await self.price_feed.ensure_connected()
        if self.price_feed is not None and not ib_up:
            self._warn_once(
                "ib-unreachable",
                f"IB Gateway unreachable -- the price feed keeps retrying every {IB_RETRY_GAP_SEC // 60} min "
                "in the background. Entries and trade marks fall back to Finnhub quotes meanwhile. "
                "Set ENABLE_IB_PRICE_FEED=false if you don't want it at all.",
            )
        if ib_up or self.finnhub is not None:
            self._last_price_poll = now
            await self._mark_and_execute()
        else:
            # IB down and no Finnhub key: nothing can price anything.
            # Leave the poll pending but back off the connection attempt.
            self._last_price_poll = now - price_interval + IB_RETRY_GAP_SEC

    def _log_heartbeat(self) -> None:
        signaled = sum(1 for s in self.dossiers.all_symbols() if self.dossiers.load(s).status == "SIGNALED")
        log.info(
            "heartbeat: universe=%d dossiers=%d signaled=%d graph_edges=%d "
            "pending_candidates=%d open_trades=%d",
            len(self.symbol_list), len(self.dossiers.all_symbols()), signaled,
            len(self.graph.relationships), len(self.candidates.data), len(self.journal.open_trades),
        )

    async def _tick(self) -> None:
        now = time.monotonic()
        # ORDER MATTERS, and it used to be wrong in three compounding ways.
        #
        # The two passes that can FIRE a signal (the daily decay pass) and the
        # one that can ACT on it (the price/entry poll) both ran at the BOTTOM
        # of the tick, behind a full EDGAR sweep of 209 symbols and a news
        # sweep that spends two LLM calls per article. Three consequences,
        # all of which cost trades:
        #
        #  1. `now` was captured once at the top and then reused to decide
        #     whether the price poll was due -- after ingestion had already
        #     burned minutes of wall clock. The entry poll was therefore
        #     tested against a stale clock and judged not-due more often than
        #     it should have been.
        #  2. A signal fired by the decay pass could not get an entry
        #     evaluation in the same tick, because the price poll had already
        #     stamped _last_price_poll and would not be due again for a full
        #     entry interval.
        #  3. A signal fired mid-news-poll could be EXPIRED by a later article
        #     in the very same poll (see _update_dossier) without the entry
        #     gate ever having seen it once.
        #
        # So both now run FIRST, in the order fire-then-act, and the price
        # poll re-reads the clock. Ingestion is the slow, latency-tolerant
        # part of the tick and belongs behind them.
        if self._daily_pass_due("decay_pass"):
            self._archive_orphaned_dossiers()
            await self._run_decay_pass()
            self._mark_daily_pass_done("decay_pass")
        await self._run_entry_and_marking_poll()

        if (
            self.settings.enable_relationship_backfill
            and self.edgar_client is not None
            and self.extractor is not None
        ):
            # No longer gated to "once per process": _run_relationship_backfill
            # itself no-ops cheaply (an in-memory list comprehension against
            # backfill_state, no I/O) once nothing is pending, so calling it
            # every tick is fine -- and it's what lets a symbol accepted at
            # runtime (accept_candidate) get backfilled on the very next
            # tick instead of only on the next process restart.
            await self._run_relationship_backfill()
        if self.edgar_client is not None and self._due(self._last_edgar_poll, self.settings.edgar_poll_interval_sec, now):
            self._last_edgar_poll = now
            await self._poll_edgar()
        if self.finnhub is not None and self._due(self._last_news_poll, self.settings.news_poll_interval_sec, now):
            self._last_news_poll = now
            await self._poll_news()
        # Ingestion above may have fired a signal. Run the entry poll again so
        # it gets its evaluation inside THIS tick rather than waiting out a
        # full entry interval -- _fire_signal clears _last_price_poll on a
        # fresh episode, so this second call is a cheap no-op (one _due check)
        # unless something actually became SIGNALED.
        await self._run_entry_and_marking_poll()

        if (
            self.finnhub is not None
            and self.settings.enable_universe_autoscreen
            and self._universe_screen_due()
        ):
            await self._run_universe_screen()
        # (The decay pass runs at the TOP of the tick -- see the ordering
        # note there. It is scheduled off persisted wall-clock rather than a
        # process-local marker: a monotonic marker resets to "due
        # immediately" on every restart, and this pass is not the harmless
        # idempotent re-score it once was -- it both FIRES and EXPIRES
        # signals, so a restart-triggered pass is an extra chance to kill a
        # pending signal before the entry gate has looked at it.)
        #
        # Both daily passes are marked done AFTER a successful run, not
        # before: forward data can't be backfilled, so a pass that raised
        # (disk error, feed dropping mid-fetch) must stay due and be
        # retried on the next tick instead of silently losing the day's
        # capture. Duplicate rows from a partial write are handled
        # downstream (dedup_snapshots / last-mark-wins), a lost day is not.
        if self._daily_pass_due("dossier_snapshot"):
            # Gated on the return value, exactly like price marks below.
            # This used to call and mark unconditionally, so the comment
            # above described a property only ONE of the two passes had --
            # and it was missing from the pass whose data is the less
            # replaceable of the two. A snapshot that wrote nothing (no
            # dossiers loaded yet on a cold start, a read that returned
            # empty) was recorded as a completed day, and the day was gone.
            if self._run_daily_snapshot():
                self._mark_daily_pass_done("dossier_snapshot")
        # Daily price marks are deliberately NOT gated on IB being enabled
        # or reachable -- they are the raw material for the forward-return
        # validation, and a missed day is permanently unbackfillable. IB is
        # preferred when available; Finnhub's /quote fills in otherwise.
        # Weekends are SKIPPED, not merely tolerated. Both price sources
        # answer on a Saturday -- with Friday's close, a real-looking number
        # -- so the pass wrote a duplicate mark under a weekend date key.
        # forward_returns._price_on_or_after takes the first date at or after
        # its target, so a snapshot whose entry_date + horizon lands on a
        # weekend joins to that stale row instead of walking on to Monday,
        # truncating the realized window by a day or two. It never extends
        # it, so the error is one-directional: it attenuates every measured
        # return, hit rate and correlation toward zero, and occasionally
        # manufactures an exact 0.00% row out of nothing. Neither default
        # horizon (5, 20) is a multiple of 7, so a meaningful share of rows
        # are affected rather than a rare few -- and none of it is
        # repairable afterwards, because a weekend row is indistinguishable
        # from a genuine one once written.
        if (
            (self.price_feed is not None or self.finnhub is not None)
            and now >= self._price_marks_retry_after
            and is_trading_day()
            and self._daily_pass_due("price_marks")
        ):
            if await self._run_daily_price_marks():
                self._mark_daily_pass_done("price_marks")
            else:
                self._price_marks_retry_after = time.monotonic() + IB_RETRY_GAP_SEC
        # One plain-text file a day, free and unauthenticated, refreshed on
        # trading days only (the list is published per SETTLEMENT day).
        #
        # The retry_after gate is load-bearing, not belt-and-braces. A failure
        # deliberately does NOT mark the pass done (so it is retried rather
        # than skipped for the day), and _daily_pass_due therefore stays true
        # -- which without a backoff means a retry on EVERY tick. Measured
        # live at 0.54.0: 48 attempts in 27 minutes, each walking back six
        # days, i.e. a few hundred requests an hour at nasdaqtrader.com for a
        # file that publishes once. Same shape, and the same fix, as
        # _price_marks_retry_after.
        if (
            self.regsho is not None
            and is_trading_day()
            and now >= self._regsho_retry_after
            and self._daily_pass_due("regsho")
        ):
            if await self.regsho.refresh():
                self._mark_daily_pass_done("regsho")
            else:
                self._regsho_retry_after = now + REGSHO_RETRY_GAP_SEC
        if self.fedreg is not None and self._due(
            self._last_fedreg_poll, self.settings.federal_register_poll_interval_sec, now
        ):
            self._last_fedreg_poll = now
            await self._poll_federal_register()
        if self.dod is not None and self._due(
            self._last_dod_poll, self.settings.dod_poll_interval_sec, now
        ):
            self._last_dod_poll = now
            await self._poll_dod_contracts()
        if (
            self.edgar_client is not None
            and self._due(self._last_candidate_recheck, CANDIDATE_RECHECK_INTERVAL_SEC, now)
        ):
            self._last_candidate_recheck = now
            await self._run_candidate_ticker_recheck()
            # Both act on the recommendation the recheck just refreshed:
            # reconcile repairs existing acceptances, then auto-accept adds
            # new ones. Reconcile runs FIRST so a correction lands before the
            # daily auto-accept budget is spent.
            self._reconcile_accepted_types()
            await self._auto_accept_candidates()
        # Graph maintenance. Both are marked done unconditionally: they are
        # daily passes whose failure mode is "nothing happened today", and
        # retrying an expensive extraction/web-search pass every 30-second
        # tick until it succeeds is far worse than waiting a day. Neither
        # loses work -- the refresh's cleared markers stay cleared, and each
        # researched anchor is marked individually as it completes.
        if self.settings.enable_graph_refresh and self._daily_pass_due("graph_refresh"):
            self._mark_daily_pass_done("graph_refresh")
            self._run_graph_refresh()
        if (
            self.settings.enable_auto_supplier_research
            and self.settings.anthropic_api_key.strip()
            and self._daily_pass_due("supplier_research")
        ):
            self._mark_daily_pass_done("supplier_research")
            await self._run_auto_supplier_research()
        # No anthropic_api_key check, unlike the pass above: this one spends
        # EDGAR requests, not tokens. It is gated on the EDGAR client instead,
        # which is what it actually needs.
        if (
            self.settings.enable_auto_edgar_search
            and self.edgar_client is not None
            and self._daily_pass_due("edgar_search")
        ):
            self._mark_daily_pass_done("edgar_search")
            await self._run_auto_edgar_search()
        # The audit is READ-ONLY, so unlike the passes above it is safe to run
        # unattended: it surfaces a bad symbol within a day instead of waiting
        # for someone to press the maintenance button. The destructive half
        # (quarantine) stays behind that button deliberately.
        if self._daily_pass_due("graph_audit"):
            self._mark_daily_pass_done("graph_audit")
            await self._run_daily_graph_audit()
        if self._due(self._last_heartbeat, HEARTBEAT_INTERVAL_SEC, now):
            self._last_heartbeat = now
            self._log_heartbeat()

    def _universe_screen_due(self) -> bool:
        """Scheduled off the PERSISTED last-screen timestamp (wall clock),
        so the monthly cadence survives restarts -- a process-local timer
        would reset on every restart and, at a 30-day interval, in practice
        never fire."""
        last = self.universe_screen_state.get("last_screened_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        interval = timedelta(days=self.settings.universe_screen_interval_days)
        return datetime.now(timezone.utc) - last_dt >= interval

    def _daily_pass_due(self, state_key: str) -> bool:
        """Same pattern as _universe_screen_due, for the once-a-day
        dossier-snapshot / price-marks / decay passes: scheduled off a
        PERSISTED wall-clock timestamp (self.periodic_state), not a
        process-local time.monotonic() marker. A process-local marker resets
        to "due immediately" on every restart, and unlike the candidate
        recheck (idempotent -- re-running it early changes nothing), each of
        these passes does something a restart must not be allowed to repeat:

        - the snapshot/price-mark passes unconditionally APPEND a fresh row
          per symbol, so a deployment restarting several times in one day
          silently writes a full duplicate batch each time. Confirmed live:
          6 duplicate dossier_snapshots.jsonl batches from 6 restarts in one
          day, inflating downstream forward-return analysis 6x for that day.
        - the decay pass EXPIRES a SIGNALED-but-unopened dossier that has
          slipped below the signal bar, so a restart-triggered pass is an
          extra chance to kill a pending signal before the (6-hourly) price
          poll has evaluated it for entry even once."""
        last = self.periodic_state.get(state_key)
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return datetime.now(timezone.utc) - last_dt >= timedelta(seconds=DAILY_SNAPSHOT_INTERVAL_SEC)

    def _mark_daily_pass_done(self, state_key: str) -> None:
        self.periodic_state.set(state_key, datetime.now(timezone.utc).isoformat())

    # --- EDGAR ingestion ---

    async def _poll_edgar(self) -> None:
        since_date = (date.today() - timedelta(days=self.settings.edgar_lookback_days)).isoformat()
        for symbol in self.symbol_list:
            if await self._is_unknown_to_edgar(symbol):
                continue
            # Deliberately NOT gated on _can_produce_evidence, unlike the
            # news poll. An anchor's own 10-K/10-Q is how that anchor gets
            # its edges in the first place (ENTG->TSM, MRNA->MRK, FDX->UPS
            # were all discovered this way), so skipping edge-less anchors
            # here would be self-locking: no edge -> never polled -> never
            # extracted -> never gains an edge. The wasted work when a
            # filing reaches no dossier is one HTTP fetch, not an LLM call
            # (_process_evidence resolves zero targets and returns without
            # scoring), and that is the correct price for graph discovery.
            try:
                filings = await self.edgar_client.recent_filings(symbol, self.settings.edgar_forms_set, since_date)
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                log.exception("%s: EDGAR poll failed", symbol)
                continue
            for filing in filings:
                try:
                    await self._process_filing(symbol, filing)
                except Exception:  # noqa: BLE001 - one bad filing must not abort the rest of the poll
                    log.exception("%s: processing filing %s failed", symbol, filing.accession_number)

    async def _is_unknown_to_edgar(self, symbol: str) -> bool:
        """Whether EDGAR's ticker map has no CIK for this symbol -- and, if
        it is a runtime-accepted one, drops it from the universe on the spot.

        A symbol EDGAR does not know cannot ever produce filing evidence,
        which for a system built on primary disclosures makes it inert. It
        still costs a submissions lookup every hourly poll and emits a
        warning every time. Confirmed live: AXL, SUP, NR, NULL, CNCO, EES,
        BMWYY, VLKAY, HYMTF and JBT logged this once an hour, forever,
        drowning real problems in the bundle's warnings tail.

        This is a stronger and faster signal than the monthly market-cap
        screen: a foreign ADR line (BMWYY, VLKAY, HYMTF) has a perfectly
        good market cap and would never be pruned by that path, yet files
        nothing with the SEC. Curated symbols are recorded for the
        diagnostics bundle rather than removed, same rule as the screen --
        a curated list is a deliberate choice."""
        if await self.edgar_client.cik_for(symbol) is not None:
            return False
        if symbol in self.accepted_candidates.data:
            self.accepted_candidates.delete(symbol)
            self.universe = [c for c in self.universe if c.symbol != symbol]
            self.spec_by_symbol = spec_by_symbol(self.universe)
            self._archive_orphaned_dossiers()
            log.warning(
                "[UNIVERSE] %s dropped: EDGAR has no CIK for it, so it can never produce filing "
                "evidence. Polling it was pure cost.", symbol,
            )
            return True
        unknown = set(self.universe_screen_state.get("curated_unknown_to_edgar") or [])
        if symbol not in unknown:
            unknown.add(symbol)
            self.universe_screen_state.set("curated_unknown_to_edgar", sorted(unknown))
            self._warn_once(
                f"edgar-unknown-{symbol}",
                f"[UNIVERSE] {symbol} is CURATED but EDGAR has no CIK for it -- it can never produce "
                "filing evidence. Left in place (a curated list is not the engine's to overrule); "
                "remove it from universe.py / SYMBOLS if it is genuinely delisted.",
            )
        return True

    async def _poll_dod_contracts(self) -> None:
        """One pass over the last few business days of DoD announcements.

        Deduped per (day, symbol, award) so an overlapping lookback re-reads a
        page without re-scoring what it already found. Direct evidence: an
        award to Ducommun IS news about Ducommun, so it goes to that dossier
        rather than propagating from anywhere -- which also means it needs no
        graph edge to be useful, and reaches names the graph has never linked.

        Logged with the matched alias on every item. The alias table is the
        one part of this that can be quietly wrong, and "which rule matched"
        is the only thing that makes a bad match diagnosable from a log."""
        if self.dod is None:
            return
        universe = set(self.symbol_list)
        anchors = {c.symbol for c in self.universe if c.signal_source_only}
        for day in business_days_back(self.settings.dod_lookback_days):
            try:
                page = await self.dod.fetch_day(day)
            except Exception:  # noqa: BLE001 - one bad day must not stop the rest
                log.exception("[DOD] %s: fetch failed", day)
                continue
            if not page:
                continue
            awards = awards_from_page(
                page, universe, day.isoformat(), anchors=anchors,
                value_floor=self.settings.dod_anchor_value_floor_usd,
            )
            log.info("[DOD] %s: %d announcement(s) matched a universe company.",
                     day, len(awards))
            for award in awards:
                # hashlib, NOT hash(): str hashing is salted per process, so
                # a built-in hash would change on every restart and re-score
                # every award the engine had already read.
                digest = hashlib.sha1(award.text.encode("utf-8")).hexdigest()[:16]
                fingerprint = f"dod:{day.isoformat()}:{award.symbol}:{digest}"
                if self.dedup.is_duplicate(fingerprint):
                    continue
                log.info("[DOD] %s: %s matched via alias %r, value=%s",
                         day, award.symbol, award.matched_alias,
                         f"${award.value_usd:,.0f}" if award.value_usd else "unparsed")
                try:
                    handled = await self._process_evidence(
                        origin_symbol=award.symbol,
                        evidence_text=award.evidence_text,
                        source_type="contract_award",
                        source_name="DoD Contract Announcements",
                        url=f"https://www.war.gov/News/Contracts/Contract/Article/{day.isoformat()}/",
                        headline=f"{award.symbol}: DoD contract award announced {day.isoformat()}",
                        published_at=day.isoformat(),
                    )
                except Exception:  # noqa: BLE001 - one bad award must not abort the pass
                    log.exception("[DOD] %s: processing the %s award failed", day, award.symbol)
                    continue
                if handled:
                    self.dedup.register(fingerprint, "war.gov")

    async def _poll_federal_register(self) -> None:
        """One pass over the curated regulatory searches.

        Each matched document is scored against the dossiers its search
        declares, propagating from the issuing body's pseudo-member over a
        seeded `regulator` edge. Deduped on document_number so overlapping
        lookback windows re-read the same rule without re-scoring it.

        Logged verbosely on purpose: this is the pass most likely to be
        diagnosed from a deployment's logs rather than from a test, since the
        API response shape cannot be exercised offline."""
        if self.fedreg is None:
            return
        for search in CURATED_SEARCHES:
            try:
                documents = await self.fedreg.fetch(
                    search, lookback_days=self.settings.federal_register_lookback_days,
                )
            except Exception:  # noqa: BLE001 - one bad search must not stop the rest
                log.exception("[FEDREG] %s: search failed", search.key)
                continue
            for doc in documents:
                fingerprint = f"fedreg:{doc.document_number}"
                if self.dedup.is_duplicate(fingerprint):
                    continue
                log.info("[FEDREG] %s: scoring %s (%s) -- %s",
                         search.key, doc.document_number, doc.doc_type, doc.title[:120])
                try:
                    handled = await self._process_evidence(
                        origin_symbol=search.regulator,
                        evidence_text=doc.evidence_text,
                        source_type="regulatory",
                        # The issuing agency AND the search key, so two
                        # different EPA proceedings are two sources rather than
                        # collapsing onto one "EPA" slot -- the independence
                        # key is the source name for direct evidence, and one
                        # agency running two unrelated rulemakings is two
                        # facts, not one restated.
                        source_name=f"Federal Register ({search.regulator}/{search.key})",
                        url=doc.url,
                        headline=doc.title[:300],
                        published_at=doc.publication_date,
                    )
                except Exception:  # noqa: BLE001 - one bad document must not abort the pass
                    log.exception("[FEDREG] %s: processing %s failed", search.key, doc.document_number)
                    continue
                if handled:
                    self.dedup.register(fingerprint, "federalregister.gov")

    def _6k_count(self, symbol: str, filing_date: str) -> int:
        """How many 6-Ks this symbol has already had ingested for this filing
        date. Rolls when the date changes, so the store stays a handful of
        keys rather than growing one per symbol per day forever."""
        if self.sixk_state.get("date") != filing_date:
            return 0
        return int((self.sixk_state.get("counts") or {}).get(symbol, 0) or 0)

    def _spend_6k_slot(self, symbol: str, filing_date: str) -> None:
        if self.sixk_state.get("date") != filing_date:
            self.sixk_state.set("date", filing_date)
            self.sixk_state.set("counts", {})
        counts = dict(self.sixk_state.get("counts") or {})
        counts[symbol] = int(counts.get(symbol, 0) or 0) + 1
        self.sixk_state.set("counts", counts)

    def _within_6k_daily_cap(self, symbol: str, filing: FilingEvent) -> bool:
        """Whether this 6-K may be ingested, given how many already were for
        this symbol on this filing date. Every other form passes through.

        The 6-K is the only high-cadence form in edgar_forms, and direct EDGAR
        evidence keys independence on form AND filing day
        (dossier.independence_key) -- correct for an 8-K, wrong for a
        cross-filer pushing routine announcements weekly, where each one would
        mint a fresh independent-source slot and the filer could corroborate
        itself into a signal.

        Read-only, like the propagation limiter's would_allow: the slot is
        spent by _spend_6k_slot only once the filing text has actually been
        fetched. Consuming it here would let a failed fetch burn the day's
        allowance on a filing that was never ingested, and the next poll --
        which is supposed to retry exactly that case -- would find the cap
        already spent."""
        if filing.form != "6-K":
            return True
        if self._6k_count(symbol, filing.filing_date) >= self.settings.max_6k_items_per_symbol_per_day:
            log.info("%s: 6-K daily cap reached for %s -- skipping %s.",
                     symbol, filing.filing_date, filing.accession_number)
            return False
        return True

    async def _process_filing(self, symbol: str, filing: FilingEvent) -> None:
        fp = f"filing:{symbol}:{filing.accession_number}"
        if self.dedup.is_duplicate(fp):
            return
        if not self._within_6k_daily_cap(symbol, filing):
            return

        text = await self.edgar_client.fetch_evidence_text(filing)
        if not text:
            return  # fetch failed/empty -- unregistered, so the next poll retries it
        if filing.form == "6-K":
            self._spend_6k_slot(symbol, filing.filing_date)

        if (filing.form in RELATIONSHIP_EXTRACTION_FORMS and self.extractor is not None
                and not self.extracted_filings.get(fp)):
            # Marked only when extraction ACTUALLY ran (returned True) -- a
            # budget-deferred or transiently-failed extraction stays unmarked
            # and is retried, exactly like the dossier-scoring path.
            if await self._extract_relationships(symbol, filing, text):
                self.extracted_filings.set(fp, datetime.now(timezone.utc).isoformat())

        # Head + tail rather than a flat prefix: the first few thousand
        # characters of a filing are mostly the SEC cover page and checkbox
        # boilerplate, so a flat text[:N] often fed the dossier engine
        # near-zero actual content -- the disclosed items sit further in.
        evidence_text = (
            f"SEC {filing.form} filed {filing.filing_date} for {symbol}:\n"
            f"{_truncate_head_tail(text, FILING_EVIDENCE_CHARS)}"
        )
        # The item codes go in the HEADLINE, not only the body: the headline
        # is what survives into the evidence record and into a paper trade's
        # citations, so "DCO 8-K (Item 1.01: Entry into a Material Definitive
        # Agreement)" is a readable audit trail where "DCO 8-K filed
        # 2026-07-28" was not.
        item_description = describe_8k_items(filing.items) if filing.form.startswith("8-K") else ""
        headline = f"{symbol} {filing.form} filed {filing.filing_date}"
        if item_description:
            headline = f"{headline} -- {item_description}"
        scored = await self._process_evidence(
            origin_symbol=symbol,
            evidence_text=evidence_text,
            source_type=filing.form,
            # Differentiated by FORM TYPE, not left as a flat "SEC EDGAR" --
            # independent_source_count (dossier.py) counts distinct
            # source_name values, so a company's 8-K, Form 4, and 10-Q are
            # genuinely independent disclosures (a material event, an
            # insider transaction, a quarterly filing are not the same fact
            # restated) and deserve to count as separate corroborating
            # sources. Left at form-type granularity rather than per-filing:
            # two 8-Ks close together are more likely the same unfolding
            # story than two truly independent confirmations.
            source_name=f"SEC EDGAR ({filing.form})",
            url=filing.document_url,
            headline=headline,
            published_at=filing.filing_date,
        )
        if scored:
            self.dedup.register(fp, "sec.gov")

    def _prune_extracted_filings(self) -> None:
        """Drop per-accession extraction markers older than the EDGAR lookback
        (plus a few days' margin): past it a filing can no longer be re-polled
        -- it either got scored and is dedup-registered, or aged out of the
        lookback window -- so the marker is dead weight. Keeps
        extracted_filings.json from growing without bound over a long run."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=self.settings.edgar_lookback_days + 3)).isoformat()
        kept = {k: v for k, v in self.extracted_filings.data.items()
                if isinstance(v, str) and v >= cutoff}
        if len(kept) != len(self.extracted_filings.data):
            self.extracted_filings.overwrite(kept)

    async def _extract_relationships(self, symbol: str, filing: FilingEvent, text: str) -> bool:
        """LLM relationship extraction from one filing's text into the
        graph -- shared by regular 10-K/10-Q polling and the one-time
        backfill. graph.add dedupes on (from, to, rel_type), so
        re-extraction of a filing can only ever add edges, never duplicate
        them. Returns False (without acting) if the daily LLM call budget
        is exhausted or the API call failed transiently (extract() returns
        None) -- the caller must NOT treat the filing as extracted; it is
        retried whenever this filing is next polled, same as any other
        transient-failure path. Returns True once extraction actually ran."""
        known = self.symbol_list
        relationships = await self.extractor.extract(symbol, filing.form, text, known)
        if relationships is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        for rel in relationships:
            # The tool schema says these are objects; the model does not
            # always agree, and a bare string here used to raise
            # AttributeError on the first .get() below. That exception
            # escapes AFTER the call has been paid for and BEFORE
            # backfill_state is set, so the filing stays permanently due:
            # every future poll re-pays for the same extraction and dies at
            # the same element. Two mega-cap filings were stuck in exactly
            # that loop. Skipping the malformed element keeps the other
            # relationships in the response -- which are usually fine --
            # instead of discarding a paid-for call over one bad entry.
            if not isinstance(rel, dict):
                log.warning(
                    "%s: relationship extraction returned a non-object entry (%r) -- skipping it. "
                    "The rest of the response is still applied.",
                    symbol, rel,
                )
                continue
            if self._is_lender_relationship(rel):
                log.info(
                    "%s: dropping lender relationship to %s (a credit provider is a disclosed "
                    "counterparty, but its news has no path to this company's fundamentals).",
                    symbol, rel.get("counterparty_name"),
                )
                continue
            if self._is_biography_relationship(rel):
                log.info(
                    "%s: dropping biography-derived relationship to %s (an executive's former "
                    "employer is CV history, not a business relationship).",
                    symbol, rel.get("counterparty_name"),
                )
                continue
            if rel.get("rel_type") not in REL_TYPES:
                # The extraction tool schema declares an enum for rel_type,
                # but Anthropic tool use doesn't hard-enforce it -- a stray
                # value (seen live: "partner") can still slip through.
                # Dropped outright rather than recorded as a candidate or
                # written into the graph, where REL_TYPES-based logic
                # elsewhere assumes only the four known values ever appear.
                log.warning(
                    "%s: dropping relationship with invalid rel_type %r (counterparty: %s)",
                    symbol, rel.get("rel_type"), rel.get("counterparty_name"),
                )
                continue
            ticker = (rel.get("counterparty_ticker") or "").upper()
            if ticker in _PLACEHOLDER_TICKERS:
                ticker = ""  # a filled-in "I don't know", not a real symbol
            if not ticker and rel.get("rel_type") != "regulator":
                # A regulator (government body, agency) can never be a
                # ticker -- skip resolution and don't clutter candidates
                # with something that will never be actionable.
                name = rel.get("counterparty_name") or ""
                resolved = await self.edgar_client.find_ticker_by_name(name)
                if not resolved and self.finnhub is not None and not self._is_unsearchable(name):
                    # SEC's own filer title is often a legal name a filing
                    # never uses ("Alphabet Inc" vs "Google") -- Finnhub's
                    # fuzzy, brand-aware search catches most of what the
                    # strict SEC-title match misses. Guarded on the client
                    # actually existing: news ingestion is an OPTIONAL
                    # integration, and an unguarded call here crashed the
                    # entire EDGAR poll on every Finnhub-less deployment
                    # the moment a filing named an unresolvable company.
                    resolved = await self.finnhub.search_ticker_by_name(name)
                if resolved:
                    ticker = resolved
            if not ticker or ticker not in known:
                if rel.get("rel_type") != "regulator":
                    await self._record_universe_candidate(symbol, rel, filing, resolved_ticker=ticker)
                continue
            if ticker == symbol:
                continue
            try:
                confidence = min(1.0, max(0.0, float(rel["confidence"])))
                description = str(rel["description"])
            except (KeyError, TypeError, ValueError):
                # Anthropic tool use doesn't hard-enforce the schema (see
                # rel_type above) -- a missing/null field must skip this one
                # relationship, not crash the whole poll after a paid call.
                log.warning("%s: dropping malformed relationship to %s (bad description/confidence).",
                            symbol, ticker)
                continue
            added = self.graph.add(
                Relationship(
                    from_symbol=symbol,
                    to_symbol=ticker,
                    rel_type=rel["rel_type"],
                    description=description,
                    source=filing.document_url,
                    confidence=confidence,
                    extracted_at=now,
                )
            )
            if added:
                log.info(
                    "[GRAPH] %s -> %s (%s, confidence=%.2f): %s",
                    symbol, ticker, rel["rel_type"], confidence, description,
                )
        return True

    @staticmethod
    def _is_lender_relationship(rel: dict) -> bool:
        """Whether a disclosed "supplier" is really a lender. Restricted to
        the supplier direction on purpose: a bank can legitimately be a
        CUSTOMER (a company selling services to it), which is a real
        propagation path -- only "our lender" is the dead end."""
        if rel.get("rel_type") != "supplier":
            return False
        text = f"{rel.get('description', '')} {rel.get('quote', '')}".lower()
        return any(phrase in text for phrase in _LENDER_PHRASES)

    @staticmethod
    def _is_biography_relationship(rel: dict) -> bool:
        """Whether an extracted relationship came from an executive
        biography. Applied to EVERY rel_type, unlike the lender filter: a CV
        line gets labelled customer/supplier/competitor essentially at
        random, so restricting by type would let most of them through."""
        text = f"{rel.get('description', '')} {rel.get('quote', '')}".lower()
        if any(phrase in text for phrase in _BIOGRAPHY_PHRASES):
            return True
        return (
            any(t in text for t in _BIOGRAPHY_TITLES)
            and any(m in text for m in _BIOGRAPHY_ROLE_MARKERS)
        )

    @staticmethod
    def _is_unsearchable(name: str) -> bool:
        """Whether a free-text counterparty name is worth spending a Finnhub
        ticker search on. Never-investable descriptions (government bodies,
        generic customer classes) can't resolve, and Finnhub 422s on long or
        punctuation-heavy queries rather than returning an empty result --
        both just produce a daily warning per candidate forever."""
        stripped = name.strip()
        if not stripped or len(stripped) > 48:
            return True
        return Engine._looks_like_non_company(stripped)

    @staticmethod
    def _looks_like_non_company(name: str) -> bool:
        lowered = name.lower()
        return any(keyword in lowered for keyword in _NON_COMPANY_KEYWORDS)

    async def _record_universe_candidate(self, symbol: str, rel: dict, filing: FilingEvent, resolved_ticker: str = "") -> None:
        """Persists a disclosed relationship to a company outside the
        universe as a WATCHLIST CANDIDATE (data/universe_candidates.json,
        also on the dashboard with a one-click Accept), and alerts the
        first time each one is discovered. Deliberately never auto-added:
        whether a name belongs is an editorial judgment (same reasoning as
        the prune-only auto-screen). `resolved_ticker` is set when the
        model didn't supply one but EdgarClient.find_ticker_by_name or
        FinnhubClient.search_ticker_by_name found a match -- candidates are
        keyed by ticker when one exists (so the
        dashboard's Accept button has something to act on), by name
        otherwise. Regulators and names matching an obvious non-company
        pattern (government bodies, generic customer-class descriptions,
        lenders) are dropped rather than recorded -- see
        _NON_COMPANY_KEYWORDS; this only ever filters candidates that were
        ALREADY unresolvable to a ticker, never a resolved one."""
        name = (rel.get("counterparty_name") or "").strip()
        if not name:
            return
        ticker = resolved_ticker or (rel.get("counterparty_ticker") or "").upper()
        if ticker in _PLACEHOLDER_TICKERS:
            ticker = ""
        if not ticker and self._looks_like_non_company(name):
            return
        key = ticker or name.upper()
        now = datetime.now(timezone.utc).isoformat()
        entry = self.candidates.get(key)
        is_new = entry is None
        if is_new and ticker:
            # A previous pass (before a ticker resolved) may have already
            # stored this same company under its name key -- merge into
            # that history instead of starting a fresh seen_count/sources
            # trail and leaving the old entry as an orphaned duplicate.
            orphan_key = name.upper()
            orphan = self.candidates.get(orphan_key) if orphan_key != key else None
            if orphan is not None:
                entry = orphan
                entry["ticker"] = ticker
                is_new = False
                self.candidates.delete(orphan_key)
        if is_new:
            entry = {
                "name": name, "ticker": ticker,
                "related_to": [], "rel_types": [],
                "description": "", "sources": [],
                "seen_count": 0, "first_seen_at": now,
            }
        elif ticker and not entry.get("ticker"):
            entry["ticker"] = ticker  # a later pass resolved a ticker this one didn't have yet
        # Counted once per FILING, not once per extraction pass.
        #
        # seen_count gates tradeable auto-accept, and it means "disclosed
        # across filings" (see config.auto_accept_min_seen_count): one
        # throwaway mention in a single filing must not be enough to start
        # taking positions on a name. The increment was unconditional, which
        # was harmless only while a filing was read exactly once -- the
        # backfill was once-ever. 0.47.0 turned that into a rolling monthly
        # re-extraction, so re-reading the SAME 10-K bumped the counter
        # again, and a counterparty named in exactly one filing crossed the
        # repeat-disclosure bar on the next refresh cycle without any new
        # disclosure existing anywhere. The guard defeated itself on a
        # schedule.
        #
        # Keyed on document_url because that is what `sources` already
        # dedupes on immediately below -- the data needed to count this
        # correctly was being maintained one line away. Existing entries
        # therefore need no migration: a candidate whose sources already
        # carry the URL simply stops re-counting it.
        if filing.document_url not in entry["sources"]:
            entry["seen_count"] += 1
        entry["last_seen_at"] = now
        entry["description"] = rel.get("description") or entry["description"]
        if symbol not in entry["related_to"]:
            entry["related_to"].append(symbol)
        rel_type = rel.get("rel_type") or ""
        if rel_type and rel_type not in entry["rel_types"]:
            entry["rel_types"].append(rel_type)
        if filing.document_url not in entry["sources"]:
            entry["sources"] = (entry["sources"] + [filing.document_url])[-5:]
        # The relationship itself, kept whole so it can be written into the
        # graph the moment this ticker joins the universe (see
        # _promote_pending_edges). Without this the disclosure was simply
        # DISCARDED: _extract_relationships records the candidate and moves
        # on without adding an edge, the filer is then marked backfilled and
        # never re-extracted, and backfill skips anchors entirely -- so a
        # candidate accepted AFTER its discovering filing was processed
        # became a symbol that gets polled hourly and can never connect to
        # anything, until the filer's next annual 10-K a year later.
        # Confirmed live: 40 accepted candidates, 237 discovered, and a
        # graph of only 61 edges, with accepted anchors like ENTG, GEHC,
        # DUK, LDOS, STM, SEDG and NVMI carrying no edge at all.
        if rel_type:
            pending = [
                p for p in entry.get("pending_edges", [])
                if not (p.get("from_symbol") == symbol and p.get("rel_type") == rel_type)
            ]
            pending.append({
                "from_symbol": symbol,
                "rel_type": rel_type,
                "description": (rel.get("description") or "")[:_MAX_REL_DESCRIPTION],
                "confidence": _clamped_confidence(rel.get("confidence")),
                "source": filing.document_url,
            })
            entry["pending_edges"] = pending[-_MAX_PENDING_EDGES:]
        self.candidates.set(key, entry)
        if is_new:
            log.info("[CANDIDATE] %s (%s) is a %s of %s -- proposed as a universe candidate (never auto-added).",
                     name, ticker or "no ticker", rel_type or "counterparty", symbol)
            await self.alerts.send(
                "universe_candidate",
                f"Universe candidate: {name}" + (f" ({ticker})" if ticker else ""),
                f"Disclosed as a {rel_type or 'counterparty'} of {symbol} in {filing.form} "
                f"filed {filing.filing_date}. {entry['description']} "
                + ("Accept it from the dashboard, or add its ticker to SYMBOLS/ANCHOR_SYMBOLS."
                   if ticker else "No ticker could be resolved -- likely private or not SEC-listed."),
                entry,
            )

    async def _run_candidate_ticker_recheck(self) -> None:
        """Daily housekeeping pass over already-discovered universe
        candidates (see CANDIDATE_RECHECK_INTERVAL_SEC), two parts:

        1. Retries ticker resolution (EdgarClient.find_ticker_by_name, then
           FinnhubClient.search_ticker_by_name) for every candidate that
           still has no ticker -- catches ones discovered before the
           Finnhub fallback existed, or where SEC's ticker map has since
           caught up with a new listing. A newly-resolved candidate is
           re-keyed from its name key to its ticker (merging into an
           existing ticker-keyed entry if a separate discovery path
           already created one), so it becomes addable on the dashboard
           instead of stuck ticker-less forever.
        2. For every ticker-having, not-yet-accepted candidate without a
           recommendation yet, suggests "tradeable" vs "anchor" from its
           market cap/analyst count -- see
           universe_screen.recommend_candidate_type. A hint for which
           Accept button to click, not a guarantee.

        Both steps are free of LLM cost -- a cached EDGAR name-map lookup
        plus a couple of throttled Finnhub calls per still-unresolved or
        unrecommended candidate."""
        for key, entry in list(self.candidates.data.items()):
            ticker = entry.get("ticker") or ""
            changed = False
            if not ticker:
                name = entry.get("name") or ""
                if not name:
                    continue
                resolved = await self.edgar_client.find_ticker_by_name(name)
                if not resolved and self.finnhub is not None and not self._is_unsearchable(name):
                    # Finnhub's search 422s on anything that isn't plausibly a
                    # company query, and this runs daily over every
                    # still-unresolved candidate -- confirmed live, ~40
                    # warnings per pass for names like "State and federal
                    # government agencies" that will never resolve to a
                    # ticker no matter how often they're retried. Skipping
                    # them saves the call and stops the noise from burying
                    # real problems in the log.
                    resolved = await self.finnhub.search_ticker_by_name(name)
                if not resolved:
                    continue
                ticker = resolved
                entry["ticker"] = ticker
                existing = self.candidates.get(ticker)
                if existing is not None:
                    existing["seen_count"] = existing.get("seen_count", 0) + entry.get("seen_count", 0)
                    existing["related_to"] = sorted(set(existing.get("related_to", [])) | set(entry.get("related_to", [])))
                    existing["rel_types"] = sorted(set(existing.get("rel_types", [])) | set(entry.get("rel_types", [])))
                    existing["last_seen_at"] = max(existing.get("last_seen_at", ""), entry.get("last_seen_at", ""))
                    entry = existing
                self.candidates.delete(key)
                log.info("[CANDIDATE] %s: resolved ticker %s on recheck.", entry.get("name"), ticker)
                changed = True

            # Recomputed whenever the BOUNDS it was derived from have changed,
            # not just when it's missing. A recommendation is only meaningful
            # relative to the thresholds that produced it, and those do move:
            # the 2026-07 recalibration went from 100/6 to 75/10, which flips
            # e.g. an $800M/8-analyst company from "anchor" to "tradeable".
            # Keying only on absence would leave every already-seen candidate
            # frozen at the old calibration forever -- and since
            # _auto_accept_candidates acts on this value, silently keep
            # auto-adding them under superseded thresholds.
            bounds = [
                self.settings.universe_min_market_cap_musd,
                self.settings.universe_max_market_cap_musd,
                float(self.settings.universe_max_analyst_count),
            ]
            # Accepted symbols are deliberately NOT excluded here: their
            # recommendation is what _reconcile_accepted_types acts on, and
            # freezing it at acceptance time meant post-acceptance drift (a
            # company graduating past the tradeable ceiling, coverage
            # thickening) could never be detected -- the reconcile pass's
            # entire documented purpose. Refreshes only when the bounds it
            # was derived from changed, so this stays two Finnhub calls per
            # candidate per recalibration, not per day.
            if (
                self.finnhub is not None
                and entry.get("recommendation_bounds") != bounds
            ):
                market_cap = await self.finnhub.market_cap_musd(ticker)
                analysts = await self.finnhub.analyst_count(ticker)
                recommendation, reason = recommend_candidate_type(
                    market_cap, analysts,
                    self.settings.universe_min_market_cap_musd,
                    self.settings.universe_max_market_cap_musd,
                    self.settings.universe_max_analyst_count,
                )
                previous = entry.get("recommended_as")
                entry["recommended_as"] = recommendation
                entry["recommendation_reason"] = reason
                entry["recommendation_bounds"] = bounds
                if previous and previous != recommendation:
                    log.info(
                        "[CANDIDATE] %s: recommendation changed %s -> %s under the current bounds (%s).",
                        ticker, previous, recommendation, reason,
                    )
                changed = True

            if changed:
                self.candidates.set(ticker, entry)

    def _block_junk_candidates(self) -> int:
        """Applies the extraction-time relevance filters RETROACTIVELY to
        candidates already on file.

        The lender and biography filters run inside _extract_relationships,
        so they protect everything discovered after they shipped -- but the
        candidate list is persistent and long-lived, and entries recorded
        before them stayed perfectly eligible for auto-accept. Confirmed
        live: Danaher, ManpowerGroup, IDEX and SPX were all auto-accepted as
        anchors off a single EPAC executive's CV, and Piper Sandler entered
        as a 'supplier' for acting as an at-the-market offering agent.

        Marked rather than deleted: a candidate is a discovered fact, and
        the dashboard shows the block reason so a human can disagree. Only
        not-yet-accepted entries are touched -- retro-blocking something
        already in the universe would say nothing about the symbol's
        membership, which reset_accepted_candidates exists to manage."""
        blocked = 0
        for key, entry in list(self.candidates.data.items()):
            ticker = (entry.get("ticker") or "").upper()
            if entry.get("auto_accept_blocked") or ticker in self.accepted_candidates.data:
                continue
            description = entry.get("description") or ""
            rel_types = entry.get("rel_types") or [""]
            reason = ""
            if self._is_biography_relationship({"description": description}):
                reason = "derived from an executive biography (a former employer is CV history, not a business relationship)"
            elif any(self._is_lender_relationship({"rel_type": t, "description": description}) for t in rel_types):
                reason = "a credit provider rather than a supply-chain counterparty (its news has no path to this company's fundamentals)"
            if not reason:
                continue
            entry["auto_accept_blocked"] = f"{reason} -- filtered retroactively"
            self.candidates.set(key, entry)
            blocked += 1
            log.info("[CANDIDATE] %s blocked from auto-add: %s", ticker or key, reason)
        if blocked:
            log.warning("[CANDIDATE] %d already-discovered candidate(s) blocked from auto-add by the "
                        "relevance filters, which had only applied to newly-extracted ones.", blocked)
        return blocked

    async def _auto_accept_candidates(self) -> None:
        """Acts on the tradeable-vs-anchor recommendation the engine already
        computed for each discovered candidate (see
        _run_candidate_ticker_recheck), instead of leaving every one of them
        waiting on a dashboard click that would apply exactly that same
        recommendation.

        A candidate is not an arbitrary ticker that happened to clear a
        threshold -- it exists because a TRADEABLE company's own SEC filing
        disclosed a business relationship with it, so its relevance is
        established by construction and its ecosystem is inferable from the
        company that disclosed it. That's what makes this automatable at all,
        and it's the specific reason universe_screen.py's prune-only stance
        (which is about names with no relationship evidence) doesn't apply.

        Anchors and tradeables are held to deliberately different bars:

        - ANCHOR: liberal. An anchor can never become a trade
          (signal_source_only), so the worst case is some wasted LLM spend --
          while the upside is large, since it converts a dead-end candidate
          into a live propagation source, which is the mechanism this whole
          strategy runs on.
        - TRADEABLE: guarded. It can produce signals and paper trades, so it
          additionally requires (a) the resolved ticker's registered SEC name
          to actually match the disclosed counterparty name -- the guard
          against the confirmed-live Advantest->ATRO class of misresolution,
          see EdgarClient.name_matches_ticker -- and (b) repeat disclosure
          across filings, so a single throwaway mention can't start a
          position.

        Bounded and reversible: at most auto_accept_max_per_day per UTC day
        (one filing naming a long list of counterparties can't flood the
        universe), every acceptance logged and webhook-alerted, and each one
        recorded in accepted_candidates.json marked "auto" so it can be
        audited and undone by deleting the entry. Widening what's watched
        can't by itself create a trade -- a dossier still has to form and
        cross the signal threshold on its own."""
        if not self.settings.enable_auto_accept_candidates:
            return
        self._block_junk_candidates()
        today = datetime.now(timezone.utc).date().isoformat()
        if self.auto_accept_state.get("date") != today:
            self.auto_accept_state.set("date", today)
            self.auto_accept_state.set("count", 0)
        accepted_today = self.auto_accept_state.get("count", 0)

        for key, entry in list(self.candidates.data.items()):
            if accepted_today >= self.settings.auto_accept_max_per_day:
                self._warn_once(
                    f"auto-accept-cap:{today}",
                    f"Auto-accept daily cap reached ({self.settings.auto_accept_max_per_day}) -- further "
                    "candidates stay pending for the dashboard, and are reconsidered tomorrow.",
                )
                return
            ticker = (entry.get("ticker") or "").upper()
            if not ticker or ticker in self.spec_by_symbol or ticker in self.accepted_candidates.data:
                continue
            if ticker in self.quarantine.data:
                # Removed by a graph-maintenance clean as structurally unfit
                # (delisted, not common equity, misresolved, or a financing
                # relationship). The candidate row that discovered it is still
                # on file and still eligible, so without this the very next
                # auto-accept pass re-adds it and the clean undoes itself.
                # Delete the row from quarantined_symbols.json to reconsider.
                continue
            if entry.get("auto_accept_blocked"):
                # The field was previously only ever WRITTEN -- the
                # name-mismatch guard set it and returned in the same breath,
                # so nothing ever read it back. That made it useless as a
                # durable decision: any other code path that marked a
                # candidate unfit (see _block_junk_candidates) was silently
                # ignored on the next pass. The dashboard and the diagnostics
                # bundle both surface this field, so it has to actually gate.
                continue
            recommendation = entry.get("recommended_as")
            if recommendation not in ("anchor", "tradeable"):
                continue  # "unknown" (no market data) is never auto-accepted

            if recommendation == "anchor":
                if not self.settings.auto_accept_anchors:
                    continue
                # Connectivity gate: only auto-accept an anchor that lands
                # CONNECTED to a tradeable. An anchor with no such edge is inert
                # by construction (its news reaches no dossier), and
                # unconditional anchor accepts are exactly what grew the
                # 221-inert-anchor board (AUDIT-2026-08 A2). The same predicate
                # the on-demand connectivity reconcile decides on -- that path
                # additionally applies the equity/name-match filters, since it
                # is a curated (dry-run-reviewed) growth; here the anchor bar
                # stays deliberately liberal on everything except connectivity.
                # Left pending, not blocked, on a miss: a tradeable may disclose
                # the same name later, and then it qualifies. auto_accept_anchors
                # is off by default, so this only bites if it is re-enabled.
                if not self._tradeable_links(entry):
                    continue
            else:
                if not self.settings.auto_accept_tradeables:
                    continue
                if entry.get("seen_count", 0) < self.settings.auto_accept_min_seen_count:
                    continue
                if self.edgar_client is None:
                    continue
                if not await self.edgar_client.name_matches_ticker(entry.get("name") or "", ticker):
                    # Recorded so this is visible on the dashboard as the
                    # reason it stayed pending, rather than looking stuck.
                    if not entry.get("auto_accept_blocked"):
                        entry["auto_accept_blocked"] = (
                            f"ticker {ticker} does not match the disclosed name "
                            f"{entry.get('name')!r} in SEC's filer list -- needs a human check"
                        )
                        self.candidates.set(key, entry)
                        log.warning("[CANDIDATE] %s: not auto-accepted -- %s", ticker, entry["auto_accept_blocked"])
                    continue

            spec = self.accept_candidate(ticker, recommendation, source="auto")
            accepted_today += 1
            self.auto_accept_state.set("count", accepted_today)
            log.info(
                "[CANDIDATE] %s auto-accepted as %s (%s). Disclosed by: %s.",
                spec.symbol, recommendation, entry.get("recommendation_reason", ""),
                ", ".join(entry.get("related_to", [])) or "unknown",
            )
            await self.alerts.send(
                "candidate_auto_accepted",
                f"Auto-added {spec.symbol} as {recommendation}",
                f"{entry.get('name')} was disclosed as a {'/'.join(entry.get('rel_types', [])) or 'counterparty'} of "
                f"{', '.join(entry.get('related_to', [])) or 'a tradeable company'}. "
                f"{entry.get('recommendation_reason', '')}. Remove it from accepted_candidates.json to undo.",
                {"symbol": spec.symbol, "as": recommendation, "reason": entry.get("recommendation_reason", "")},
            )

    # --- One-time relationship backfill ---

    async def _run_relationship_backfill(self) -> None:
        """Extracts relationships from each tradeable symbol's MOST RECENT
        10-K, regardless of age -- regular polling only sees filings from
        the last edgar_lookback_days and 10-Ks are annual, so a fresh
        deployment would otherwise take up to a year to populate its graph.

        Graph extraction only: an old 10-K is NOT fed to the dossier engine
        as evidence (year-old 'news' is long priced in; the relationships it
        discloses are durable, the sentiment is not). Each symbol is
        backfilled once ever (persisted), so a symbol accepted at runtime
        (accept_candidate) gets backfilled on the very next tick without
        needing a restart.

        ANCHORS ARE INCLUDED, tradeables first. They used to be skipped, on
        the reasoning that a giant's 10-K never names its small suppliers --
        which is why the graph is discovered from the small companies'
        filings in the first place. That reasoning is half right and the
        half it misses is expensive: regular polling only reads filings
        inside edgar_lookback_days and 10-Ks are annual, so skipping the
        backfill meant no anchor's 10-K was EVER read until it happened to
        file a new one inside a 14-day window. Measured live, 98-134 of 161
        anchors had no graph edge to any tradeable at all -- and an anchor
        without such an edge is inert by construction, its news resolving to
        zero targets and being discarded unread. The strategy is anchor news
        propagating to thin-coverage names; most anchors were not
        propagating anything.

        Expect a lower yield per call than from the tradeable side, because
        the original reasoning does hold for customer-concentration
        disclosures: those name big customers, not small suppliers. What an
        anchor 10-K does reliably name is single-source supplier risk
        factors, JV partners and named competitors -- and those are exactly
        the edges that turn an inert anchor live. TRADEABLES ARE ORDERED
        FIRST so that the higher-yield work happens before a tight daily
        budget defers the rest to tomorrow (see max_daily_usd -- deferral is
        the designed behaviour here, not a failure).

        Set BACKFILL_ANCHORS=false to restore the old tradeable-only
        behaviour if the anchor yield does not justify its share of the
        budget."""
        if time.monotonic() < self._backfill_retry_after:
            return
        tradeables = [
            spec.symbol for spec in self.universe
            if not spec.signal_source_only and not self.backfill_state.get(spec.symbol)
        ]
        anchors = [
            spec.symbol for spec in self.universe
            if spec.signal_source_only and not self.backfill_state.get(spec.symbol)
        ] if self.settings.backfill_anchors else []
        pending = tradeables + anchors
        if not pending:
            return
        log.info("Relationship backfill: extracting from the most recent 10-K of %d symbol(s) "
                 "(%d tradeable, %d anchor): %s",
                 len(pending), len(tradeables), len(anchors), ", ".join(pending[:40]))
        done = 0
        for symbol in pending:
            try:
                filing = await self.edgar_client.latest_filing(symbol, "10-K")
                if filing is None:
                    # No 10-K on record (foreign issuer, fresh IPO) -- mark done,
                    # there is nothing to extract from and never will be here.
                    log.info("%s: no 10-K available to backfill from.", symbol)
                    self.backfill_state.set(symbol, {"backfilled_at": datetime.now(timezone.utc).isoformat(),
                                                     "accession": None})
                    continue
                text = await self.edgar_client.fetch_text(filing)
                if not text:
                    continue  # fetch failed -- left pending, retried on the next tick
                if not await self._extract_relationships(symbol, filing, text):
                    # Extraction was deferred (budget exhausted or a
                    # transient API failure) -- the symbol must NOT be
                    # marked backfilled, or its 10-K would silently never
                    # be extracted at all until next year's annual filing.
                    # Back off before retrying so a spent daily budget
                    # doesn't turn this into a 30-second fetch loop.
                    self._backfill_retry_after = time.monotonic() + IB_RETRY_GAP_SEC
                    continue
                self.backfill_state.set(symbol, {"backfilled_at": datetime.now(timezone.utc).isoformat(),
                                                 "accession": filing.accession_number,
                                                 "filing_date": filing.filing_date})
                done += 1
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                log.exception("%s: relationship backfill failed -- will retry on a later tick.", symbol)
        log.info("Relationship backfill complete: %d/%d symbol(s) processed, graph now has %d edge(s).",
                 done, len(pending), len(self.graph.relationships))

    # --- Graph maintenance (see config's graph-maintenance settings) ---

    def _run_graph_refresh(self) -> int:
        """Re-queues the N least-recently-extracted symbols for relationship
        re-extraction, by clearing their once-ever backfill marker so the
        backfill pass above picks them up on the next tick.

        This exists because the backfill is once-ever and extraction only
        writes an edge when the counterparty is ALREADY in the universe --
        otherwise it records a watchlist candidate and moves on. Every symbol
        read while the universe was smaller therefore left permanent holes,
        and the only repair was a dashboard button a human had to remember to
        press. The observable symptom was tradeables carrying a full thesis
        with NO graph edge at all: names whose dossier came entirely from
        their own filings, which never used the cross-company mechanism this
        system exists for.

        Oldest-first, and anchors are included (unlike the rebuild button,
        which skipped them) -- an anchor with no edge to any tradeable is
        inert by construction, its news resolving to zero targets and being
        discarded unread, so it is exactly what most needs re-reading.

        Safe by construction: graph.add dedupes on (from, to, rel_type), so a
        refresh can only ever ADD an edge, never remove one, and it touches no
        dossier, trade or log. Cost is bounded to N extraction calls a day,
        and a spent daily budget defers the rest to tomorrow exactly as it
        does on first run."""
        limit = max(0, self.settings.graph_refresh_symbols_per_day)
        if limit == 0:
            return 0
        dated: list[tuple[str, str]] = []
        for spec in self.universe:
            if spec.signal_source_only and not self.settings.backfill_anchors:
                continue
            marker = self.backfill_state.get(spec.symbol)
            if not marker:
                # No marker means it is already pending -- the backfill pass
                # will read it regardless, so spending a refresh slot on it
                # would just displace a symbol that has actually gone stale.
                continue
            stamp = marker.get("backfilled_at", "") if isinstance(marker, dict) else ""
            dated.append((stamp, spec.symbol))
        if not dated:
            return 0
        # Plain ISO-8601 string sort: lexicographic order IS chronological for
        # these stamps, and an unparseable/absent one ("") sorts first, which
        # is the right priority -- unknown age is the stalest case.
        dated.sort()
        selected = [symbol for _, symbol in dated[:limit]]
        for symbol in selected:
            self.backfill_state.delete(symbol)
        # A previous budget-exhausted backfill may have backed off; this is a
        # fresh day's work, so let the next tick try immediately.
        self._backfill_retry_after = 0.0
        log.info("[GRAPH] Refresh: re-queued %d least-recently-extracted symbol(s) for "
                 "re-extraction against the current %d-symbol universe: %s",
                 len(selected), len(self.universe), ", ".join(selected))
        return len(selected)

    async def _run_auto_supplier_research(self) -> None:
        """Runs the web-search supplier research on a daily cadence instead of
        only when someone presses the dashboard button.

        This is the only mechanism that can find the small-cap SUPPLIERS of a
        giant. The filing path structurally cannot: a giant's 10-K names its
        big customers, not its small suppliers, so the direction this strategy
        needs is information SEC filings never contain. Left manual it simply
        never ran -- measured live at $0.00 spent against a reserved research
        budget share, i.e. an entire class of edge permanently missing.

        Writes universe CANDIDATES only -- never a relationship edge and never
        a trade -- because a web-sourced link is a lead, not a disclosure.
        Accepting a candidate backfills its own 10-K, which is where a real
        edge comes from. That is what makes this safe to run unattended.

        Imported locally: this is the engine's only dependency on tools.py,
        which is otherwise a leaf module built on top of the engine's state."""
        from smartboi.tools import run_supplier_research

        try:
            report = await run_supplier_research(self)
        except Exception:  # noqa: BLE001 - a research failure must never kill the tick
            log.exception("[GRAPH] Automatic supplier research failed -- retrying tomorrow.")
            return
        headline = next((line for line in (report or "").splitlines() if line.strip()), "(no output)")
        log.info("[GRAPH] Automatic supplier research: %s", headline)

    async def _run_auto_edgar_search(self) -> None:
        """Runs the EDGAR full-text search on a daily cadence instead of only
        when someone presses the dashboard button.

        Same omission as _run_auto_supplier_research, and it cost more. This
        is the cheapest size-selected mechanism in the system: it asks which
        OTHER filers name an anchor, and a filer disclosing "X accounted for
        22% of net sales" is by construction small enough for X to matter to
        it -- the exact direction a giant's own 10-K never contains. The hit
        arrives carrying a ticker SEC itself supplies, so unlike every other
        candidate path it never passes through name->ticker resolution and
        cannot land on the wrong company that way.

        It costs no LLM tokens at all (the proximity test is a regex over
        fetched filing text), so unlike supplier research it needs no API key
        and is not gated on the daily budget -- only on EDGAR's own request
        spacing. Writes universe CANDIDATES only, never an edge: a regex
        proximity hit is a lead about where to look, and accepting one
        backfills the symbol's own filings, which is where a real edge comes
        from. See edgar_search.py.

        Imported locally, like the research pass, because tools.py is a leaf
        module built on top of the engine's state."""
        from smartboi.tools import run_edgar_supplier_search

        try:
            report = await run_edgar_supplier_search(self)
        except Exception:  # noqa: BLE001 - a search failure must never kill the tick
            log.exception("[GRAPH] Automatic EDGAR supplier search failed -- retrying tomorrow.")
            return
        headline = next((line for line in (report or "").splitlines() if line.strip()), "(no output)")
        log.info("[GRAPH] Automatic EDGAR supplier search: %s", headline)

    # --- News ingestion ---

    def _ecosystem_targets(self, origin_symbol: str) -> list[str]:
        """Tradeables sharing an inert anchor's ecosystem -- the fallback
        propagation path when no disclosed edge exists (see
        _process_evidence and enable_ecosystem_propagation).

        Anchors only: a tradeable is already its own analysis target, and
        fanning one small-cap's news across its peers is a different
        (competitor read-across) thesis that this system has no disclosed
        basis for.

        The "accepted" bucket is excluded deliberately. It is not an
        ecosystem -- it is where runtime-accepted symbols land when their
        discoverer's ecosystem can't be inferred, and it has held dozens of
        mutually unrelated companies at once. Fanning news across it would
        be pure noise dressed as a sector link."""
        spec = self.spec_by_symbol.get(origin_symbol)
        if spec is None or not spec.signal_source_only:
            return []
        if spec.ecosystem in _UNCLASSIFIED_ECOSYSTEMS:
            return []
        return [
            c.symbol for c in self.universe
            if not c.signal_source_only and c.ecosystem == spec.ecosystem and c.symbol != origin_symbol
        ]

    def _can_produce_evidence(self, symbol: str) -> bool:
        """Whether news about this symbol can reach ANY dossier.

        A tradeable always can -- it is its own analysis target. An anchor
        never is (that is what signal_source_only means), so its news is
        only worth fetching if it has a graph edge to a tradeable. With no
        such edge, _process_evidence resolves zero targets and the article
        is fingerprinted and dropped without a single LLM call.

        ...or, with enable_ecosystem_propagation, if it shares a classified
        ecosystem with at least one tradeable (see _ecosystem_targets).
        Keeping the two in agreement matters: this decides whether the
        symbol is POLLED at all, so an anchor that the fallback would happily
        fan out from must not be skipped before its news is ever fetched.

        Measured live 2026-07-29: 104 of 130 anchors had no edge to any
        tradeable -- including every loud name in the universe (NVDA, MSFT,
        AMZN, GOOGL, META, TSLA, INTC, AMAT, LRCX, TSM, UPS, CSX...). They
        accounted for ~2,500 wasted Finnhub calls a day and the bulk of
        10,373 dedup fingerprints, against 52 LLM calls actually spent."""
        spec = self.spec_by_symbol.get(symbol)
        if spec is None or not spec.signal_source_only:
            return True
        universe = set(self.symbol_list)
        if any(
            not (self.spec_by_symbol.get(linked) or spec).signal_source_only
            for linked, _ in self.graph.linked_symbols(symbol, universe)
        ):
            return True
        return bool(self.settings.enable_ecosystem_propagation and self._ecosystem_targets(symbol))

    async def _poll_news(self) -> None:
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=self.settings.news_lookback_days)).isoformat()
        skipped: list[str] = []
        for symbol in self.symbol_list:
            if not self._can_produce_evidence(symbol):
                skipped.append(symbol)
                continue
            try:
                articles = await self.finnhub.recent_news(symbol, from_date, to_date)
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                log.exception("%s: news poll failed", symbol)
                continue
            for article in articles:
                # An article with no timestamp gets an EMPTY published_at
                # rather than a substituted date: substituting from_date
                # (which slides forward every day) used to give the same
                # undated story a brand-new fingerprint and evidence_id
                # every day it stayed in Finnhub's response window, so it
                # was re-billed and re-merged as fresh evidence daily.
                # Decay for a dateless record falls back to its merge time
                # (see dossier._age_days), so nothing becomes immortal.
                published = article.published_at or ""
                fp = fingerprint(symbol, article.headline, published)
                if self.dedup.is_duplicate(fp):
                    continue
                near_fp = self.dedup.find_near_duplicate(symbol, article.headline, published)
                if near_fp is not None:
                    # A lightly reworded republish of a story already scored
                    # (same symbol, same/previous day, high headline-token
                    # overlap): register THIS variant's fingerprint against
                    # the original's source identity so future polls
                    # exact-match it cheaply, and skip it entirely -- it
                    # must neither burn a scoring call nor count as a second
                    # independent source for the same underlying story.
                    log.debug("%s: skipping near-duplicate headline %r (matches %r)",
                              symbol, article.headline, near_fp)
                    self.dedup.register(fp, self.dedup.domain_for(near_fp) or "unknown")
                    continue
                # The real publisher (Reuters, Bloomberg, ...) is what makes
                # two articles genuinely independent sources -- Finnhub's
                # free-tier article URLs all point at finnhub.io itself, so
                # source_domain(article.url) was always non-empty and this
                # used to prefer it, collapsing every single article onto
                # one source identity ("finnhub.io") no matter how many
                # distinct publishers actually covered a story. Only fall
                # back to the URL's domain if Finnhub ever ships a genuinely
                # empty source field.
                publisher = (article.source or "").strip() or source_domain(article.url) or "unknown"
                evidence_text = (
                    f"News ({article.source}, {published or 'undated'}): {article.headline}\n{article.summary}"
                )
                try:
                    scored = await self._process_evidence(
                        origin_symbol=symbol,
                        evidence_text=evidence_text,
                        source_type="news",
                        source_name=publisher,
                        url=article.url,
                        headline=article.headline,
                        published_at=published,
                    )
                except Exception:  # noqa: BLE001 - one bad article must not abort the rest of the poll
                    log.exception("%s: processing article %r failed", symbol, article.headline)
                    continue
                if scored:
                    self.dedup.register(fp, publisher)
        if skipped:
            self._warn_once(
                "anchors-unconnected",
                f"Skipping news for {len(skipped)} anchor(s) with no graph edge to any tradeable: "
                f"{', '.join(skipped)}. Their news cannot reach a dossier, so fetching it was pure "
                "cost. They start being polled the moment an edge to a tradeable appears -- accept "
                "their pending universe candidates, or drop them from ANCHOR_SYMBOLS.",
            )

    # --- Shared evidence -> dossier pipeline ---

    async def _process_evidence(
        self,
        origin_symbol: str,
        evidence_text: str,
        source_type: str,
        source_name: str,
        url: str,
        headline: str,
        published_at: str,
    ) -> bool:
        """Returns True when the item was handled definitively (every
        affected dossier merged/rejected/refuted it, or nothing needed
        scoring); False when it should be retried on a later poll (no
        dossier engine configured yet, a transient LLM failure, or the
        daily LLM call budget was exhausted) -- the caller then leaves its
        dedup fingerprint unregistered."""
        if self.updater is None or self.skeptic is None:
            return False  # collected but not scoreable yet -- retried once a key is configured

        universe = set(self.symbol_list)
        # (target_symbol, relationship_note, relationship_confidence,
        # relationship_type) -- confidence is the graph edge's own extracted
        # confidence and type is its REL_TYPES kind (both None/"" for
        # direct/non-propagated evidence, which has no edge at all). The type
        # rides along because confidence alone cannot say whether a link is a
        # causal transmission channel: see dossier.COMPETITOR_SATISFIES_
        # DISCLOSED_LINK, where a competitor edge is high-confidence and still
        # must not buy the corroboration discount.
        targets: list[tuple[str, str, float | None, str]] = []
        propagation_keys: dict[str, str] = {}  # target_symbol -> limiter key, propagated targets only
        ecosystem_keys: set[str] = set()  # of those, the ones on the ecosystem limiter

        origin_spec = self.spec_by_symbol.get(origin_symbol)
        # A regulator is an ORIGIN ONLY -- there is no such thing as a thesis
        # about the EPA. It is deliberately NOT a universe member (that would
        # put "BIS" into the EDGAR poll, the news poll, the screen and every
        # count), so origin_spec is None for it and the signal_source_only
        # check below cannot be what protects it. This is.
        is_regulator = origin_symbol in REGULATOR_SYMBOLS
        if not is_regulator and (origin_spec is None or not origin_spec.signal_source_only):
            targets.append((origin_symbol, "", None, ""))

        now = time.monotonic()
        throttled = 0
        for linked_symbol, rel in self.graph.linked_symbols(origin_symbol, universe):
            linked_spec = self.spec_by_symbol.get(linked_symbol)
            if linked_spec is not None and linked_spec.signal_source_only:
                continue
            # Propagated-evidence cooldown: a heavily-covered origin (e.g. a
            # noisy anchor) can generate many near-duplicate articles about
            # the same underlying story in a short window; without this, each
            # one burns a full dossier-update + skeptic LLM call against the
            # SAME target even when the causal link keeps getting refused for
            # the same reason. Direct evidence (target == origin, handled
            # above) is never throttled -- only fan-out to OTHER dossiers.
            # Pre-filter with would_allow (read-only) rather than allow(): the
            # slot is only actually consumed (record, below) once this
            # target's processing DEFINITIVELY completes, so a deferred/
            # retried attempt (budget exhaustion, transient failure) doesn't
            # burn a second slot for what's the same underlying evidence.
            key = f"{origin_symbol}->{linked_symbol}"
            if not self._propagation_limiter.would_allow(key, now):
                throttled += 1
                continue
            propagation_keys[linked_symbol] = key
            targets.append((linked_symbol, rel.description, rel.confidence, rel.rel_type))
        # Ecosystem fallback: only for an origin with NO disclosed link to
        # any tradeable at all. A disclosed contractual relationship is
        # strictly better evidence, so this never runs alongside one and
        # never doubles up on a target that already has one.
        #
        # `not throttled` is load-bearing, not belt-and-braces. Without it,
        # an origin whose disclosed links were all inside their cooldown
        # window would fall through to the ecosystem path and reach the very
        # same targets by a weaker route -- defeating the rate limit it just
        # hit, and swapping good evidence for worse.
        if not propagation_keys and not throttled and self.settings.enable_ecosystem_propagation:
            for linked_symbol in self._ecosystem_targets(origin_symbol):
                key = f"eco:{origin_symbol}->{linked_symbol}"
                if not self._ecosystem_limiter.would_allow(key, now):
                    throttled += 1
                    continue
                propagation_keys[linked_symbol] = key
                ecosystem_keys.add(key)
                targets.append((
                    linked_symbol,
                    f"{origin_symbol} and {linked_symbol} are both in the {origin_spec.ecosystem} "
                    "ecosystem. NOTE: this is an industry-level association inferred from sector "
                    "membership, NOT a contractual relationship disclosed in any filing -- there is "
                    "no stated customer, supplier or competitor link between these two companies.",
                    ECOSYSTEM_LINK_CONFIDENCE,
                    # Not a REL_TYPES kind at all -- sector co-membership, no
                    # graph edge behind it. Left empty rather than borrowing a
                    # real type it does not have.
                    "",
                ))

        if throttled:
            self._warn_once(
                f"throttled:{origin_symbol}",
                f"{origin_symbol}: propagated-evidence cooldown engaged for {throttled} linked symbol(s) "
                f"(already sent {self.settings.max_propagated_evidence_per_link} items in the last "
                f"{self.settings.propagated_evidence_cooldown_hours}h) -- further {origin_symbol} evidence won't "
                "reach them until the window rolls off. Raise MAX_PROPAGATED_EVIDENCE_PER_LINK if this is "
                "suppressing real signal.",
            )

        if not targets:
            # No target resolved. WHY decides whether this item is finished
            # or merely postponed, and conflating the two silently destroyed
            # evidence: a True return makes the caller register the dedup
            # fingerprint, and a registered fingerprint is never reconsidered.
            #
            # Throttled-to-empty is the damaging case. The propagation
            # cooldown is a RATE LIMIT, not a filter -- but when every link
            # from a busy origin was inside its window, `targets` came back
            # empty and the article was dropped permanently rather than
            # retried after the window rolled off. That preferentially
            # destroyed evidence from the most active anchors, which is
            # exactly the evidence worth having.
            if throttled:
                log.info("%s: all %d propagation target(s) are inside the cooldown window -- "
                         "leaving this item unregistered so it is retried once the window rolls off.",
                         origin_symbol, throttled)
                return False
            log.debug("%s: no propagation target (no edge to a tradeable) -- nothing to score.",
                      origin_symbol)
            return True

        all_definitive = True
        for target_symbol, relationship_note, relationship_confidence, relationship_type in targets:
            outcome = await self._update_dossier(
                target_symbol, evidence_text, origin_symbol, relationship_note, relationship_confidence,
                source_type, source_name, url, headline, published_at,
                relationship_type=relationship_type,
            )
            # Only a target handled fresh THIS pass consumes a cooldown
            # slot -- "already" means an earlier, partially-deferred pass
            # already handled (and charged) this same evidence for this
            # target, and re-recording it on every retry used to fill the
            # window with phantom events that throttled genuinely new
            # propagation for hours.
            if outcome == "handled" and target_symbol in propagation_keys:
                key = propagation_keys[target_symbol]
                limiter = self._ecosystem_limiter if key in ecosystem_keys else self._propagation_limiter
                limiter.record(key, now)
            all_definitive = all_definitive and outcome != "deferred"
        return all_definitive

    # How long a cached pending proposal or handled-outcome marker can be
    # retried before its evidence has aged out of every ingestion lookback
    # window (14-day EDGAR is the longest) and the entry is just a leak.
    _EVIDENCE_CACHE_TTL_SEC = 15 * 86400

    def _load_retry_state(self) -> None:
        """Restore the propose/skeptic retry caches persisted by the last run,
        pruning anything already aged past the ingestion lookback window (so a
        long downtime cannot resurrect a marker whose evidence can never be
        re-ingested). Malformed persisted entries are skipped rather than
        trusted -- the file is small JSON that a human could have edited."""
        cutoff = time.time() - self._EVIDENCE_CACHE_TTL_SEC
        handled = self._retry_state.get("handled_outcomes") or {}
        self._handled_outcomes = {
            k: float(t) for k, t in handled.items()
            if isinstance(t, (int, float)) and t >= cutoff
        }
        pending = self._retry_state.get("pending_proposals") or {}
        restored: dict[str, tuple[dict, float]] = {}
        for k, v in pending.items():
            # Persisted as [proposal, wall_time]; tolerate anything malformed.
            if (isinstance(v, list) and len(v) == 2 and isinstance(v[0], dict)
                    and isinstance(v[1], (int, float)) and v[1] >= cutoff):
                restored[k] = (v[0], float(v[1]))
        self._pending_proposals = restored

    def _persist_retry_state(self) -> None:
        self._retry_state.update({
            "handled_outcomes": self._handled_outcomes,
            "pending_proposals": {
                k: [proposal, t] for k, (proposal, t) in self._pending_proposals.items()
            },
        })

    def _evict_stale_evidence_caches(self) -> None:
        cutoff = time.time() - self._EVIDENCE_CACHE_TTL_SEC
        self._pending_proposals = {
            k: v for k, v in self._pending_proposals.items() if v[1] >= cutoff
        }
        self._handled_outcomes = {
            k: t for k, t in self._handled_outcomes.items() if t >= cutoff
        }

    def _log_refutation(self, target_symbol: str, evidence_id: str, origin_symbol: str, verdict: dict) -> None:
        """Append one row to logs/skeptic_refutations.jsonl for every
        skeptic-refuted item. A refutation otherwise left NO stored record, so
        the refutation rate -- the one number that says whether the cheap
        trade-gating skeptic is bleeding real propagated evidence -- was
        unmeasurable. Paired with the pre/post numbers on accepted records,
        this feeds smartboi.skeptic_report (see tools.run_skeptic_report)."""
        path = Path(self.settings.log_dir) / "skeptic_refutations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({
                "symbol": target_symbol,
                "evidence_id": evidence_id,
                "is_propagated": target_symbol != origin_symbol,
                "model": self.settings.skeptic_model,
                "reasoning": str(verdict.get("reasoning") or "")[:300],
                "at": datetime.now(timezone.utc).isoformat(),
            }) + "\n")

    def _mark_handled(self, proposal_key: str) -> str:
        """Records a definitive non-merge outcome (not-new, refuted, or a
        malformed proposal) so a retry of the same evidence item -- forced
        by a SIBLING target's deferral -- doesn't re-pay propose_update and
        the skeptic for a target that was already definitively done (and,
        worse, give a nondeterministic second skeptic run the chance to
        accept what the first refuted). Persisted so this survives the
        several-restarts-a-day reality (see _load_retry_state)."""
        self._handled_outcomes[proposal_key] = time.time()
        self._evict_stale_evidence_caches()
        self._persist_retry_state()
        return "handled"

    @staticmethod
    def _validated_proposal(target_symbol: str, proposed: dict, max_horizon_days: int) -> dict | None:
        """Clamped, type-checked copy of a propose_update tool response, or
        None when a required field is missing/invalid -- Anthropic tool use
        does not hard-enforce the declared schema, and indexing a missing
        field used to abort the entire poll cycle after the call was paid.

        horizon_days is clamped to [1, max_horizon_days]: it previously had
        only a floor, so a mis-scored horizon (a model returning 900) would
        keep the item at full decay weight for YEARS -- staleness scales off
        horizon_days (see dossier._stale_cutoff_days) -- quietly defeating the
        decay guarantee. Nothing is predictive past the strategy's own max
        hold, so that is the natural ceiling."""
        try:
            direction = proposed["direction"]
            if direction not in DIRECTIONS:
                raise ValueError(f"direction {direction!r}")
            return {
                "is_new_information": bool(proposed.get("is_new_information")),
                "direction": direction,
                "magnitude": min(1.0, max(0.0, float(proposed["magnitude"]))),
                "confidence": min(1.0, max(0.0, float(proposed["confidence"]))),
                "horizon_days": min(max_horizon_days, max(1, int(proposed["horizon_days"]))),
                "reasoning": str(proposed.get("reasoning") or ""),
            }
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("%s: malformed dossier-update proposal (%s) -- skipping this evidence.",
                        target_symbol, exc)
            return None

    @staticmethod
    def _adjusted(verdict: dict, key: str, fallback: float) -> float:
        try:
            return min(1.0, max(0.0, float(verdict.get(key, fallback))))
        except (TypeError, ValueError):
            return fallback

    async def _update_dossier(
        self,
        target_symbol: str,
        evidence_text: str,
        origin_symbol: str,
        relationship_note: str,
        relationship_confidence: float | None,
        source_type: str,
        source_name: str,
        url: str,
        headline: str,
        published_at: str,
        relationship_type: str = "",
    ) -> str:
        """Outcome of folding one evidence item into one dossier:
        "handled"  -- definitively handled fresh this pass (merged, judged
                      not-new, refuted, or dropped as malformed);
        "already"  -- was definitively handled on an earlier, partially-
                      deferred pass of the same item (idempotent retry);
        "deferred" -- transient failure or exhausted daily LLM budget;
                      warrants a retry on a later poll."""
        evidence_id = f"{source_type}:{url or headline}:{published_at}"
        dossier = self.dossiers.load(target_symbol)
        if has_evidence(dossier, evidence_id):
            return "already"  # merged on an earlier, partially-failed pass
        proposal_key = f"{target_symbol}:{evidence_id}"
        if proposal_key in self._handled_outcomes:
            return "already"  # judged not-new/refuted on an earlier pass

        # Cache the proposal across a budget-deferred skeptic call: without
        # this, deferring at the skeptic step (verdict is None, below) would
        # otherwise mean the RETRY re-pays for propose_update from scratch
        # even though nothing about the proposal changed -- it's the exact
        # same evidence, just waiting for the skeptic's turn.
        cached = self._pending_proposals.get(proposal_key)
        proposed = cached[0] if cached is not None else None
        if proposed is None:
            target_spec = self.spec_by_symbol.get(target_symbol)
            raw = await self.updater.propose_update(
                dossier, evidence_text, origin_symbol, relationship_note, relationship_confidence,
                ecosystem=target_spec.ecosystem if target_spec is not None else "",
            )
            if raw is None:
                return "deferred"  # transient LLM failure or budget exhausted -- retry later
            proposed = self._validated_proposal(target_symbol, raw, self.settings.max_horizon_days)
            if proposed is None:
                return self._mark_handled(proposal_key)  # malformed -- dropping is definitive
            if not proposed["is_new_information"]:
                # Logged like its siblings (malformed warns, refuted infos).
                # This is the only drop point that costs exactly ONE LLM call
                # rather than two, so without a line here the daily call
                # count cannot be decomposed from the outside -- and the
                # updater prompt cannot be tuned without knowing what
                # fraction of items it silently discards.
                log.info("%s: evidence judged not new information -- not merged.", target_symbol)
                return self._mark_handled(proposal_key)
            # Wall-clock stamp (not monotonic) so the cache survives a restart:
            # a mid-flight restart between propose and skeptic otherwise re-pays
            # propose_update. Persisted immediately for the same reason.
            self._pending_proposals[proposal_key] = (proposed, time.time())
            self._persist_retry_state()

        verdict = await self.skeptic.review(evidence_text, proposed, relationship_note, relationship_confidence)
        if verdict is None:
            return "deferred"  # transient LLM failure or budget exhausted -- retry later
        self._pending_proposals.pop(proposal_key, None)
        self._persist_retry_state()
        if verdict.get("refuted"):
            log.info("%s: evidence refuted by skeptic (%s)", target_symbol, verdict.get("reasoning", ""))
            self._log_refutation(target_symbol, evidence_id, origin_symbol, verdict)
            return self._mark_handled(proposal_key)

        record = EvidenceRecord(
            evidence_id=evidence_id,
            source_type=source_type,
            source_name=source_name,
            url=url,
            headline=headline,
            published_at=published_at,
            origin_symbol=origin_symbol,
            is_propagated=(target_symbol != origin_symbol),
            relationship_note=relationship_note,
            relationship_confidence=relationship_confidence,
            relationship_type=relationship_type,
            scored_by_model=self.settings.dossier_model,
            reviewed_by_model=self.settings.skeptic_model,
            direction=proposed["direction"],
            magnitude=self._adjusted(verdict, "adjusted_magnitude", proposed["magnitude"]),
            confidence=self._adjusted(verdict, "adjusted_confidence", proposed["confidence"]),
            horizon_days=proposed["horizon_days"],
            reasoning=proposed["reasoning"],
            skeptic_note=str(verdict.get("reasoning") or ""),
            # The pre-skeptic numbers are kept so the skeptic pass's actual
            # effect (does it earn its 2x cost per item?) is measurable
            # later instead of being overwritten and lost.
            proposed_confidence=proposed["confidence"],
            proposed_magnitude=proposed["magnitude"],
        )
        merge_evidence(dossier, record)
        # Snapshot the price the instant the thesis first points somewhere
        # tradeable, so the entry gate can later measure how much of the
        # favourable move already happened WHILE corroboration accumulated
        # (see _capture_inception and the drift guard in _try_open_from_signal).
        await self._capture_inception(dossier)
        merged_at = datetime.now(timezone.utc)
        # Before re-applying yesterday's verdict, ask whether it is still a
        # judgement of THIS dossier: two new independent sources, or a price
        # that has refuted an already-priced-in call, mean the whole-body pass
        # never saw what it is now capping. Bounded, and fails safe -- if it
        # cannot produce a fresh verdict the old one stands (_maybe_resynthesize).
        await self._maybe_resynthesize(dossier, merged_at)
        # Re-apply the last whole-body synthesis verdict as a cap: a merge
        # recomputes the arithmetic from scratch and would otherwise erase a
        # veto/trim the daily pass had applied, letting a synthesis-blocked
        # thesis re-fire on the raw score (see _cap_with_synthesis).
        self._cap_with_synthesis(dossier, merged_at)
        self.dossiers.save(dossier)
        log.info(
            "[EVIDENCE] %s: accepted %s magnitude=%.2f confidence=%.2f (dossier now: direction=%s "
            "confidence=%.2f magnitude=%.2f sources=%d, status=%s)",
            target_symbol, record.direction, record.magnitude, record.confidence,
            dossier.direction, dossier.confidence, dossier.magnitude,
            dossier.independent_source_count, dossier.status,
        )

        signal = evaluate(dossier, self.settings.signal_confidence_threshold, self.settings.min_independent_sources,
                          self.settings.min_independent_sources_news_only)
        if signal is None and dossier.status == "ACTIVE":
            # New evidence merged and the thesis still did not qualify. Logged
            # with the merge as its own event so "which lever is binding" can
            # be asked of the evidence that ARRIVED, not just of the daily
            # population -- the two answer different questions and the event
            # name keeps them separable.
            self._record_decision(
                "below_bar_on_merge", target_symbol, dossier.direction, dossier.signaled_at,
                reason=self._below_bar_reason(dossier, "after new evidence merged"),
            )
        if signal is not None:
            await self._fire_signal(dossier, signal)
        elif (
            dossier.status == "SIGNALED"
            and not self.journal.has_open(target_symbol)
            and self._should_expire_unopened(dossier)
        ):
            # Newly merged evidence dropped the thesis below the signal bar
            # (or flipped it) while it sat SIGNALED-but-unopened. Left
            # as-is, the next price poll would still open a paper trade on
            # a thesis that no longer qualifies -- possibly in the OPPOSITE
            # direction from the one that signaled, against a baseline
            # snapped for the old thesis. _should_expire_unopened is what
            # keeps a MARGINAL dip from killing an episode the entry gate
            # has not evaluated even once.
            self._expire_signal(dossier, self._below_bar_reason(dossier, "when fresh evidence merged"))
        return "handled"

    # --- Price marking / hypothetical execution ---

    def _has_price_source(self) -> bool:
        """Whether ANY price source exists -- IB or Finnhub's /quote.

        The entry gate used to be reachable only through IB: _tick called
        _mark_and_execute exclusively under `self.price_feed is not None`, so
        a deployment without ENABLE_IB_PRICE_FEED could accumulate evidence,
        cross the bar, fire a signal and log it -- and then never, under any
        circumstances, open the paper trade that is the entire point of the
        system. Finnhub's /quote was already trusted enough to set the
        signal-time drift BASELINE (_snapshot_signal_price) and to write the
        daily forward-validation price marks (_run_daily_price_marks); there
        was never a reason it couldn't also price an entry."""
        return self.price_feed is not None or self.finnhub is not None

    async def _price_bar(self, symbol: str) -> PriceBar | None:
        """One symbol's latest bar, IB first and Finnhub second.

        IB is preferred where it works: it is a real historical daily bar
        with true session extremes. But it fails in ways that are invisible
        from here and routine in practice -- a Gateway whose market-data
        farms are down (live: "farms not connected: eufarm; euhmds"), a
        symbol with no market-data subscription on the account, a share
        class SMART won't route. Every one of those returns None, and a None
        at the entry gate used to mean no trade, ever, with no fallback and
        no diagnostic. Finnhub's /quote covers US common stock on the free
        tier and carries the session high/low, so it is a genuine substitute
        rather than a degraded one."""
        if self.price_feed is not None:
            try:
                bar = await self.price_feed.last_bar(symbol)
            except Exception:  # noqa: BLE001 - fall through to Finnhub, never propagate
                log.exception("%s: IB price lookup failed -- trying Finnhub.", symbol)
            else:
                if bar is not None:
                    return bar
        if self.finnhub is not None:
            try:
                quote = await self.finnhub.quote_bar(symbol)
            except Exception:  # noqa: BLE001 - a missing price is a no-op, never a crash
                log.exception("%s: Finnhub quote lookup failed.", symbol)
                return None
            if quote is not None:
                return PriceBar(close=quote[0], high=quote[1], low=quote[2])
        return None

    async def _price_bars(self, symbols: list[str]) -> dict[str, PriceBar]:
        """Same IB-then-Finnhub resolution as _price_bar, batched. IB is
        asked for the whole list at once (it paces its own requests), then
        Finnhub fills in whatever IB could not price -- the same shape
        _run_daily_price_marks already used, so an open paper trade is
        marked to market on exactly the days the forward-validation record
        has a price for it, not fewer."""
        bars: dict[str, PriceBar] = {}
        if self.price_feed is not None:
            try:
                bars = dict(await self.price_feed.last_bars(symbols))
            except Exception:  # noqa: BLE001 - fall through to Finnhub
                log.exception("IB batch price lookup failed -- trying Finnhub.")
        missing = [s for s in symbols if s not in bars]
        if missing and self.finnhub is not None:
            for symbol in missing:
                try:
                    quote = await self.finnhub.quote_bar(symbol)
                except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                    log.exception("%s: Finnhub quote lookup failed.", symbol)
                    continue
                if quote is not None:
                    bars[symbol] = PriceBar(close=quote[0], high=quote[1], low=quote[2])
        return bars

    async def _fire_signal(self, dossier: Dossier, signal) -> None:
        """Logs a signal and, for a fresh episode, flips the dossier to
        SIGNALED with a fresh price baseline.

        Shared by the two paths that can produce one: newly merged evidence
        crossing the bar, and the daily decay pass re-scoring an existing
        dossier. The decay path used to be able to EXPIRE a signal but never
        to fire one, so a dossier that came to qualify without new evidence
        -- because decay lifted a contested thesis, or because the bar
        itself moved -- sat ACTIVE indefinitely with nothing to re-evaluate
        it. Confirmed live: DCO at score 0.288 against a 0.2 threshold with
        both source bars satisfied, ACTIVE, waiting for an article that
        might never come."""
        if dossier.status == "ACTIVE" or dossier.direction != dossier.signaled_direction:
            # A fresh signal (or a thesis that flipped direction while still
            # SIGNALED-but-unopened) gets a fresh price baseline -- see
            # _snapshot_signal_price and _try_open_from_signal. Status and
            # baseline are set BEFORE logging so every logged signal row
            # carries its episode key (signaled_at) and re-logs of one
            # episode can be collapsed downstream.
            dossier.status = "SIGNALED"
            # A fresh episode gets a fresh grace period. entry_attempts gates
            # _should_expire_unopened's first-look leniency, so a re-fire --
            # notably a direction flip that still qualifies, which arrives here
            # on the merge path -- must NOT inherit the prior episode's attempt
            # count. Otherwise the flipped episode is born past its grace period
            # and can be expired unseen by the next merge, the exact failure the
            # grace period exists to prevent. The decay path already resets via
            # _reset_to_active before re-firing; the merge path did not.
            dossier.entry_attempts = 0
            await self._snapshot_signal_price(dossier)
            self.dossiers.save(dossier)
            # Pull the next price poll forward to the entry cadence rather
            # than letting this wait up to price_poll_interval_sec (6h) for
            # its first entry evaluation.
            self._entry_pending = True
            # ...and make that first evaluation happen on the NEXT TICK (30s)
            # rather than a full entry interval (15 min) from now. Clearing
            # the marker is what makes the difference between a fresh episode
            # being offered to the entry gate promptly and it spending its
            # first quarter-hour exposed to every expiry path in the system
            # -- newly merged evidence dipping it below the bar, or the decay
            # pass -- without the gate ever having seen it once. The first
            # signal this system ever fired died exactly that way.
            self._last_price_poll = None
        log_signal(Path(self.settings.log_dir) / "signals.jsonl", signal, episode=dossier.signaled_at)
        await self.alerts.send(
            "signal",
            f"{signal.direction} signal: {signal.symbol}",
            f"confidence={signal.confidence:.2f} magnitude={signal.magnitude:.2f} "
            f"sources={signal.independent_source_count}. {signal.thesis_summary}",
            asdict(signal),
        )

    async def _snapshot_signal_price(self, dossier: Dossier) -> None:
        """Records the price the moment a dossier becomes SIGNALED, so
        entry time can later tell whether the market has already moved
        ("are we too late") -- see favorable_drift_pct in signals.py and
        _try_open_from_signal. Left as None if no price feed is configured/
        reachable; the drift check then simply has nothing to compare
        against and is skipped (same graceful-degradation pattern as
        everywhere else IB is optional)."""
        dossier.signaled_at = datetime.now(timezone.utc).isoformat()
        dossier.signaled_direction = dossier.direction
        dossier.drift_alert_sent = False
        dossier.signaled_price = None
        try:
            if self.price_feed is not None:
                await self.price_feed.ensure_connected()
            # _price_bar is IB-then-Finnhub: Finnhub's /quote keeps the drift
            # baseline usable when IB is down or not configured -- a missing
            # baseline silently disables the "are we too late" check.
            bar = await self._price_bar(dossier.symbol)
            dossier.signaled_price = bar.close if bar is not None else None
        except Exception:  # noqa: BLE001 - a missing baseline just disables the drift check for this signal
            log.exception("%s: could not snapshot signal-time price.", dossier.symbol)

    async def _capture_inception(self, dossier: Dossier) -> None:
        """Snapshots the price the moment a thesis first points somewhere
        tradeable (or flips to a new direction), so the entry-gate drift guard
        can measure the favourable move that ALREADY happened while
        corroboration accumulated -- not just since the signal fired.

        Measured live over 91 fires: the median had already moved ~3-5% in its
        favour before firing, and ~1 in 5 had moved >12% (the guard's own bar)
        pre-fire -- invisible to a signal-time baseline. Captured once per
        direction run (a flip re-captures); cleared when a signal expires
        (_reset_to_active) so each fresh accumulation window measures its own
        start. Graceful: no reachable price just leaves it unset, and the guard
        falls back to signaled_price."""
        if dossier.direction not in ("LONG", "SHORT"):
            return
        if dossier.inception_at and dossier.inception_direction == dossier.direction:
            return
        try:
            if self.price_feed is not None:
                await self.price_feed.ensure_connected()
            bar = await self._price_bar(dossier.symbol)
        except Exception:  # noqa: BLE001 - a missing baseline just disables the pre-signal drift check
            log.exception("%s: could not snapshot inception price.", dossier.symbol)
            return
        if bar is not None:
            dossier.inception_price = bar.close
            dossier.inception_at = datetime.now(timezone.utc).isoformat()
            dossier.inception_direction = dossier.direction

    async def _capture_synthesis_price(self, dossier: Dossier, now: datetime) -> None:
        """Snapshots the price at the instant a synthesis verdict is rendered,
        so the verdict becomes a claim the tape can later refute.

        `already_priced_in` asserts something testable -- the market has
        absorbed this -- and until this existed nothing in the system ever
        compared it to a price. A verdict could be violently wrong and be
        re-asserted every day forever, because the only thing that could
        contradict it was another copy of itself.

        Same IB-then-Finnhub fallback as the inception baseline (_price_bar),
        and the same graceful failure: no reachable price leaves it unset,
        which leaves the verdict standing. That is the fail-safe direction --
        an unfalsifiable veto suppresses a thesis, it never opens a trade."""
        try:
            if self.price_feed is not None:
                await self.price_feed.ensure_connected()
            bar = await self._price_bar(dossier.symbol)
        except Exception:  # noqa: BLE001 - a missing price just leaves the verdict unfalsifiable
            log.exception("%s: could not snapshot synthesis-time price.", dossier.symbol)
            return
        if bar is not None:
            dossier.synthesis_price = bar.close
            dossier.synthesis_price_at = now.isoformat()

    def _reset_to_active(self, dossier: Dossier) -> None:
        dossier.status = "ACTIVE"
        dossier.signaled_at = ""
        dossier.signaled_price = None
        dossier.signaled_direction = ""
        dossier.drift_alert_sent = False
        dossier.entry_attempts = 0
        # Cleared so the next episode measures its own accumulation window from
        # scratch: a fresh thesis re-captures its inception on the next
        # directional merge (see _capture_inception).
        dossier.inception_price = None
        dossier.inception_at = ""
        dossier.inception_direction = ""

    def _should_expire_unopened(self, dossier: Dossier) -> bool:
        """Whether a SIGNALED-but-unopened dossier that no longer clears the
        bar should be expired NOW, by a path that is not the entry gate
        (newly merged evidence, or the daily decay pass).

        Both of those paths used to expire unconditionally, with no dwell
        time and no requirement that the entry gate had ever run. That is a
        real trade-killer at this system's cadences: the news poll walks 209
        symbols spending two LLM calls per article, so an episode fired
        early in a poll is exposed for the whole rest of that poll, and ONE
        skeptic-approved contrary item is enough to end it -- confidence is
        multiplied by `1 - mass_opposing/mass_agree` (dossier._aggregate), so
        first opposing evidence moves the score sharply. The episode dies,
        its price baseline is wiped, and the entry gate never saw it.

        Two carve-outs, and only two:
        - A direction FLIP always expires immediately. A stale SIGNALED
          status pointing the wrong way is worse than no signal at all.
        - Before the gate has evaluated this episode even once, a dip has to
          be MATERIAL (below `SIGNAL_EXPIRY_HYSTERESIS` of the bar), not
          marginal. Once entry_attempts > 0 the gate has had its look and
          the normal, strict bar applies again -- this is a grace period for
          the first evaluation, not a permanently softer threshold."""
        if dossier.direction != dossier.signaled_direction:
            return True
        if dossier.entry_attempts > 0:
            return True
        score = dossier.confidence * dossier.magnitude
        return score < self.settings.signal_confidence_threshold * SIGNAL_EXPIRY_HYSTERESIS

    def _record_decision(self, event: str, symbol: str, direction: str, episode: str,
                         price: float | None = None, reason: str = "") -> None:
        """Appends to logs/decisions.jsonl -- what the engine DID with a
        signal episode. Never allowed to break the engine: the ledger is
        analysis material, not control flow."""
        try:
            log_decision(Path(self.settings.log_dir) / "decisions.jsonl",
                         event, symbol, direction, episode, price=price, reason=reason)
        except OSError:
            log.exception("Could not append to decisions.jsonl")

    def _required_sources(self, dossier: Dossier) -> int:
        """The independent-source bar this dossier is held to, resolved by
        the same function signals.evaluate uses so a diagnostic can never
        report a bar the gate did not apply."""
        return required_sources(
            dossier,
            self.settings.min_independent_sources,
            self.settings.min_independent_sources_news_only,
        )

    def _below_bar_reason(self, dossier: Dossier, when: str) -> str:
        """Why evaluate() refused a dossier, in numbers -- which of the two
        gates it failed and by how much. Reports the source bar this dossier
        was actually held to (see _required_sources), so a news-only dossier
        reports the elevated one rather than the base setting."""
        required = self._required_sources(dossier)
        unbacked = not (dossier.has_filing_evidence or dossier.has_disclosed_link_evidence)
        parts = []
        if dossier.independent_source_count < required:
            parts.append(
                f"sources {dossier.independent_source_count}/{required}"
                + (" (no filing or disclosed-link backing)" if unbacked else "")
            )
        score = dossier.confidence * dossier.magnitude
        if score < self.settings.signal_confidence_threshold:
            parts.append(f"score {score:.3f} < {self.settings.signal_confidence_threshold:.3f}")
        if dossier.direction == "NONE":
            parts.append("direction resolved to NONE")
        return f"thesis fell below the signal bar {when} (" + ", ".join(parts or ["no longer qualifies"]) + ")"

    def _expire_signal(self, dossier: Dossier, reason: str, price: float | None = None) -> None:
        log.info("[SIGNAL] %s: expiring unopened signal (%s) -- resetting to ACTIVE so fresh "
                 "evidence can re-trigger it with a clean baseline.", dossier.symbol, reason)
        # Ledger BEFORE the reset wipes the episode key -- an expiry that
        # leaves only a log line is unanalyzable (see event_study.py).
        self._record_decision(
            "signal_expired", dossier.symbol,
            dossier.signaled_direction or dossier.direction,
            dossier.signaled_at, price=price, reason=reason,
        )
        self._reset_to_active(dossier)
        self.dossiers.save(dossier)

    async def _try_open_from_signal(self, symbol: str, dossier: Dossier) -> None:
        """Whether/how a SIGNALED-but-not-yet-open dossier becomes a paper
        trade this poll -- the "are we too late" gate. Two guards, both a
        no-op without enable_ib_price_feed (no baseline price to compare
        against, see _snapshot_signal_price):

        1. Favorable drift: if the price already moved
           max_favorable_drift_pct in the signal's favorable direction
           since it fired, the correction likely already happened between
           signal and entry -- skip rather than chase a move that's largely
           over. Alerted once per signal (drift_alert_sent), not every poll.
        2. Entry deadline: a signal stuck unopened (drift-blocked every
           poll, or IB unreachable) for signal_entry_deadline_days is
           expired back to ACTIVE rather than left waiting forever on an
           increasingly stale opportunity.

        Before either guard, the thesis is RE-CHECKED against the signal
        bar at entry time: hours can pass between the signal firing and
        this poll, and evidence merged in between may have dropped the
        dossier below threshold or flipped its direction while the status
        stayed SIGNALED. Opening from the stale status alone would take a
        position the current evidence no longer justifies -- possibly in
        the opposite direction from the thesis that actually signaled."""
        # Recorded BEFORE any early return: reaching this function at all is
        # what "the entry gate has looked at this episode" means, and the
        # pre-gate expiry grace period (see _should_expire_unopened) keys off
        # it. Persisted below alongside whatever this evaluation decides; an
        # expiry resets it with the rest of the episode state. Persisted
        # immediately rather than left to whichever branch happens to save:
        # most of the paths out of this function (drift skip on a repeat
        # poll, no price yet, a successful open) do not write the dossier at
        # all, and a counter that only survives on the expiry paths would be
        # exactly backwards.
        dossier.entry_attempts += 1
        self.dossiers.save(dossier)

        # Each failure mode gets its own reason string, with the numbers that
        # caused it: "no longer qualifies" told a reader nothing about WHY a
        # signal died, and the decisions ledger exists precisely so that
        # question is answerable after the fact.
        if evaluate(dossier, self.settings.signal_confidence_threshold,
                    self.settings.min_independent_sources,
                    self.settings.min_independent_sources_news_only) is None:
            self._expire_signal(dossier, self._below_bar_reason(dossier, "at entry time"))
            return
        if dossier.direction != dossier.signaled_direction:
            self._expire_signal(
                dossier,
                f"thesis flipped {dossier.signaled_direction} -> {dossier.direction} "
                "before an entry was confirmed",
            )
            return
        # Outside the regular session no price source refuses to answer --
        # they return the last close -- so without this the engine opens at
        # a price no order could have been filled at and stamps it with the
        # current time. Checked BEFORE the price fetch (an out-of-hours poll
        # need not spend an API call) but AFTER the deadline, which is the
        # ordering that matters: the deadline check used to sit below an
        # early return and the result was a signal that never opened AND
        # never expired. Any new early return here has to carry the deadline
        # with it or it recreates exactly that.
        if not is_regular_trading_hours():
            if signal_expired(dossier.signaled_at, self.settings.signal_entry_deadline_days):
                self._expire_signal(dossier, "the entry deadline passed outside regular trading hours")
            return
        # IB first, Finnhub second (see _price_bar). Entry used to be the one
        # place in the system with NO fallback -- _snapshot_signal_price and
        # _run_daily_price_marks both already fell back to Finnhub, but the
        # gate that actually opens the trade did not, so an IB outage was a
        # total block on the system's only output.
        bar = await self._price_bar(symbol)
        price = bar.close if bar is not None else None
        if price is None:
            # No price from ANY source. Deliberately still deadline-checked
            # rather than a bare return: the deadline check used to sit below
            # this early return, so a signal on an unpriceable symbol never
            # opened AND never expired -- it stayed SIGNALED forever, holding
            # the tightened entry poll cadence open and blocking the dossier
            # from ever producing a fresh, cleanly-baselined signal later.
            if signal_expired(dossier.signaled_at, self.settings.signal_entry_deadline_days):
                self._expire_signal(dossier, "no price available from any source within the entry deadline")
            else:
                self._warn_once(
                    f"no-entry-price:{symbol}",
                    f"{symbol}: signal is waiting on the entry gate but no price source could price it "
                    "(IB unreachable/unsubscribed and no Finnhub quote). It will expire at the entry "
                    f"deadline ({self.settings.signal_entry_deadline_days}d) if nothing can price it.",
                )
            return

        # Prefer the thesis-INCEPTION baseline over the signal-fire one. A
        # signal fires only after corroboration accumulates over days, by which
        # time much of the favourable move can already be gone: measured live,
        # ~1 in 5 fires had already moved >12% (this very threshold) before
        # firing, invisible to a fire-time baseline. inception_price is snapped
        # when the thesis first pointed this direction (see _capture_inception),
        # so this measures the TOTAL move since the thesis began, not just since
        # it fired. Falls back to signaled_price when no inception was captured.
        baseline = dossier.signaled_price
        since = "the signal fired"
        if dossier.inception_price is not None and dossier.inception_direction == dossier.direction:
            baseline, since = dossier.inception_price, "the thesis began forming"
        if baseline is not None:
            drift = favorable_drift_pct(dossier.direction, baseline, price)
            if drift >= self.settings.max_favorable_drift_pct:
                if not dossier.drift_alert_sent:
                    dossier.drift_alert_sent = True
                    self.dossiers.save(dossier)
                    log.info(
                        "[SIGNAL] %s: price already moved %.1f%% favorably since %s "
                        "at $%.2f (now $%.2f) -- likely already priced in, skipping entry.",
                        symbol, drift, since, baseline, price,
                    )
                    # Once per episode (same gate as the alert): the price
                    # at skip time is what lets the event study ask whether
                    # the skipped move kept going or was indeed over.
                    self._record_decision(
                        "drift_skip", symbol, dossier.direction, dossier.signaled_at,
                        price=price,
                        reason=f"drifted {drift:.1f}% favorably from {baseline:.2f} (since {since})",
                    )
                    await self.alerts.send(
                        "signal_stale",
                        f"{symbol}: likely already priced in",
                        f"Price moved {drift:.1f}% in the favorable direction since {since} "
                        f"at ${baseline:.2f} (now ${price:.2f}) -- skipping entry to avoid "
                        "chasing a move that may already be over.",
                        {"symbol": symbol, "drift_pct": drift, "baseline_price": baseline, "current_price": price},
                    )
                if signal_expired(dossier.signaled_at, self.settings.signal_entry_deadline_days):
                    self._expire_signal(dossier, f"price drifted {drift:.1f}% before an entry could be confirmed",
                                        price=price)
                return

        if signal_expired(dossier.signaled_at, self.settings.signal_entry_deadline_days):
            self._expire_signal(dossier, "no confirmed entry within the deadline", price=price)
            return

        citations = [
            {
                "source_name": e.source_name, "url": e.url,
                "headline": e.headline, "published_at": e.published_at,
            }
            for e in dossier.evidence[-5:]
        ]
        horizon = min(dossier.horizon_days or self.settings.max_horizon_days, self.settings.max_horizon_days)
        # Market cap decides the transaction-cost bucket (and the borrow
        # flag for shorts): a flat per-side figure understates friction
        # exactly where this strategy hunts. Best-effort -- with no lookup
        # source the journal assumes the middle bucket, never the cheapest.
        market_cap: float | None = None
        if self.finnhub is not None:
            try:
                market_cap = await self.finnhub.market_cap_musd(symbol)
            except Exception:  # noqa: BLE001 - a failed lookup must not block the entry; the cost model has a fallback
                log.warning("%s: market-cap lookup for cost bucketing failed.", symbol)
        cost_per_side = cost_bps_per_side_for_cap(
            market_cap,
            self.settings.transaction_cost_bps_per_side,
            self.settings.transaction_cost_profile,
        )
        # Equal-weight notional: the account is split into a fixed number of
        # slots, and each trade takes one slot's worth. This sizes the record
        # in real currency without capping how many signals are measured (see
        # config's account-model note).
        slots = max(1, self.settings.max_concurrent_positions)
        position_value = self.settings.initial_trading_capital / slots
        trade = self.journal.open(
            symbol, dossier.direction, price,
            self.settings.stop_loss_pct, self.settings.take_profit_pct,
            horizon, dossier.thesis_summary, dossier.confidence,
            dossier.independent_source_count, citations,
            cost_bps_round_trip=cost_per_side * 2,
            market_cap_musd=market_cap,
            position_value=position_value,
            strategy=self.settings.strategy_signature(),
            # The episode key, the other half of the score, and the synthesis
            # verdict as it stands right now -- everything needed to ask, after
            # the fact, WHY this trade was opened. The dossier keeps evolving
            # after this moment (every merge and every decay pass recomputes
            # the aggregate, and thesis_summary is regenerated from whichever
            # three items are newest), so anything not captured here is not
            # recoverable later by reading the dossier file.
            episode=dossier.signaled_at,
            magnitude=dossier.magnitude,
            synthesis={
                "synthesis_at": dossier.synthesis_at,
                "synthesis_confidence": dossier.synthesis_confidence,
                "synthesis_magnitude": dossier.synthesis_magnitude,
                "distinct_fact_count": dossier.distinct_fact_count,
                "already_priced_in": dossier.already_priced_in,
            },
            # The borrow OBSERVABLE, where one is in hand: presence on the Reg
            # SHO threshold list is a fact about persistent failures to
            # deliver, not the market-cap inference that stood in for it. See
            # paper_journal.assumes_borrow -- absence still falls through to
            # the proxy, so this can only tighten the flag.
            on_threshold_list=(
                self.regsho.is_threshold(symbol) if self.regsho is not None else None
            ),
        )
        self._record_decision("trade_opened", symbol, dossier.direction, dossier.signaled_at,
                              price=price)
        await self.alerts.send(
            "paper_trade_opened",
            f"Paper trade opened: {trade.direction} {trade.symbol}",
            f"entry={trade.entry_price:.2f} stop={trade.stop_price:.2f} "
            f"target={trade.target_price:.2f} horizon={trade.horizon_days}d. {trade.thesis_summary}",
            asdict(trade),
        )

    def _is_tradeable(self, symbol: str) -> bool:
        spec = self.spec_by_symbol.get(symbol)
        return spec is not None and not spec.signal_source_only

    async def _mark_and_execute(self) -> None:
        pending = False
        for symbol in self.dossiers.all_symbols():
            # Dossier FILES outlive universe membership: a symbol demoted to
            # anchor (or dropped entirely) keeps its file, and a stale
            # SIGNALED one would otherwise open a paper trade on something
            # this system is no longer allowed to trade -- an anchor exists
            # precisely because it must never be a trade target.
            if not self._is_tradeable(symbol):
                continue
            dossier = self.dossiers.load(symbol)
            if dossier.status != "SIGNALED" or self.journal.has_open(symbol):
                continue
            await self._try_open_from_signal(symbol, dossier)
            # _try_open_from_signal mutates this dossier in place: an expiry
            # flips it to ACTIVE, an open leaves it SIGNALED with a trade on
            # the books. Anything still SIGNALED and unopened (drift-blocked,
            # or no price available) keeps the tight cadence alive.
            if dossier.status == "SIGNALED" and not self.journal.has_open(symbol):
                pending = True
        self._entry_pending = pending

        # Enforced BEFORE the price fetch and independent of it: a trade on a
        # symbol nothing can price would otherwise stay open forever, pinning
        # its dossier at SIGNALED so no fresh signal could replace it.
        for trade in self.journal.expire_past_horizon():
            await self.alerts.send(
                "paper_trade_closed",
                f"Paper trade closed: {trade.symbol} {trade.status} (stale mark)",
                f"{trade.direction} entry={trade.entry_price:.2f} exit={trade.exit_price:.2f} "
                f"R={trade.r_multiple:.2f}. Closed at its horizon without a fresh price -- no "
                "source could mark it.",
                asdict(trade),
            )
            dossier = self.dossiers.load(trade.symbol)
            self._reset_to_active(dossier)
            self.dossiers.save(dossier)

        open_symbols = list(self.journal.open_trades.keys())
        if not open_symbols:
            return
        bars = await self._price_bars(open_symbols)
        for symbol, bar in bars.items():
            trade = self.journal.open_trades.get(symbol)
            if trade is None:
                continue
            # The day's high/low, not just the close: a stock that breached
            # the stop intraday and recovered by the close is a real stop-out
            # for any live position -- evaluating on close alone erased
            # exactly those losses and flattered the paper record.
            self.journal.update(symbol, bar.close, high=bar.high, low=bar.low)
            if trade.status != "OPEN":
                # The paper trade just closed (WIN/LOSS/TIMEOUT) -- notify,
                # then reset the dossier so future evidence can trigger a
                # fresh signal instead of being permanently stuck at SIGNALED.
                await self.alerts.send(
                    "paper_trade_closed",
                    f"Paper trade closed: {trade.symbol} {trade.status}",
                    f"{trade.direction} entry={trade.entry_price:.2f} exit={trade.exit_price:.2f} "
                    f"R={trade.r_multiple:.2f}",
                    asdict(trade),
                )
                dossier = self.dossiers.load(symbol)
                self._reset_to_active(dossier)
                self.dossiers.save(dossier)

    # --- Evidence time-decay ---

    @staticmethod
    def _decay_fingerprint(dossier: Dossier) -> tuple:
        return (
            dossier.direction,
            round(dossier.confidence, 3),
            round(dossier.magnitude, 3),
            dossier.independent_source_count,
            dossier.has_filing_evidence,
            dossier.has_disclosed_link_evidence,
        )

    async def _run_decay_pass(self) -> None:
        """Once a day, re-scores every dossier's aggregate confidence/
        magnitude/independent_source_count against its EXISTING evidence
        with no new evidence required -- otherwise a dormant dossier (no
        fresh news landing on it) would keep yesterday's confidence
        forever. See dossier.py's evidence_weight/evidence_is_stale for the
        actual decay curve.

        Re-scoring can move a dossier ACROSS the signal bar in EITHER
        direction, and both are acted on here:

        - a SIGNALED-but-unentered dossier that falls below is expired back
          to ACTIVE (the thesis is going cold without ever getting a
          confirmed entry);
        - an ACTIVE dossier that now clears it signals.

        The second case was missing, and it is not hypothetical. Evidence
        merging is the only other thing that evaluates a dossier, so
        anything that comes to qualify WITHOUT new evidence -- decay lifting
        a contested thesis as the opposing side ages out faster, or the bar
        itself moving under it -- had nothing to re-evaluate it and sat
        ACTIVE indefinitely. Confirmed live: DCO at score 0.288 against a
        0.2 threshold with both source bars satisfied, sitting ACTIVE,
        waiting for an article that might never come.

        Once a paper trade has actually opened, decay no longer touches that
        dossier -- the open trade has its own stop/target/horizon."""
        now = datetime.now(timezone.utc)
        for symbol in self.dossiers.all_symbols():
            # Per-symbol isolation, matching _poll_edgar and _poll_news. This
            # pass runs FIRST in the tick and the tick has no per-pass
            # try/except, so one symbol raising here skipped the dossier
            # snapshot, the daily price marks, the entry/marking poll and the
            # heartbeat -- every capture file at once, silently, while the
            # process stayed alive and looked healthy. Nothing here is
            # transactional across symbols, so one bad dossier should cost
            # that dossier and not the day's data.
            try:
                await self._decay_one(symbol, now)
            except Exception:  # noqa: BLE001 - deliberately broad; see above
                log.exception("%s: decay pass failed for this symbol -- continuing.", symbol)

    async def _decay_one(self, symbol: str, now: datetime) -> None:
        """One symbol's decay, synthesis and re-evaluation.

        Split out of _run_decay_pass only so a raise can be contained to one
        symbol. `return` here means "done with this symbol", which is what
        `continue` meant when this was inline."""
        dossier = self.dossiers.load(symbol)
        if not dossier.evidence:
            return
        # Every field recompute_decay writes that can change a SIGNAL
        # DECISION belongs in this comparison, not just the scores: an
        # unchanged tuple skips the save entirely, so anything omitted
        # is recomputed in memory and thrown away on a dormant dossier.
        # The two backing flags gate which corroboration bar applies
        # (see signals.evaluate), so leaving them out meant a dossier
        # whose scores had settled could never persist them.
        before = self._decay_fingerprint(dossier)
        recompute_decay(dossier, now)
        changed = before != self._decay_fingerprint(dossier)

        # Evaluated on EVERY pass, not only when the score moved. A
        # dossier can sit above the bar with perfectly stable numbers --
        # nothing decaying, no new evidence -- and it still needs to
        # signal. Gating this on `changed` (as an earlier version did)
        # reproduced the original bug in a subtler form: the one dossier
        # that most obviously qualified was the one whose score had
        # settled, so it was skipped every single day. evaluate() is a
        # pure function over fields already in hand, so running it
        # unconditionally costs nothing.
        if self.journal.has_open(symbol):
            if changed:
                self.dossiers.save(dossier)
            return
        # Synthesis runs here and nowhere else: once a day, only for a
        # dossier that has resolved a direction, so this is a few dozen
        # calls against a budget the deployment runs at a few percent of.
        synthesized = await self._apply_synthesis(dossier, now)
        # Saved once, AFTER synthesis, rather than only when the decay
        # fingerprint moved.
        #
        # The decay-only save meant a synthesis verdict was computed, used to
        # cap the score for the rest of this pass, and then thrown away: the
        # fields _apply_synthesis writes (synthesis_at/note/catalyst/
        # distinct_fact_count/already_priced_in and the capped scores) reached
        # disk only when something ELSE happened to save the dossier in the
        # same pass -- a signal firing or an expiry. For an ACTIVE dossier
        # that stayed below the bar, which is the common case, the most
        # expensive judgement in the system left no trace at all. That made
        # Dossier's own claim -- verdicts "recorded even when the verdict
        # changes nothing, so the pass's actual effect on outcomes is
        # measurable" -- untrue, and left the per-trade synthesis stamp
        # (paper_journal.PaperTrade) reading a verdict that was usually stale
        # or absent rather than the one that had just run.
        if changed or synthesized:
            self.dossiers.save(dossier)
        signal = evaluate(dossier, self.settings.signal_confidence_threshold,
                          self.settings.min_independent_sources,
                          self.settings.min_independent_sources_news_only)
        if signal is None:
            if dossier.status == "SIGNALED" and self._should_expire_unopened(dossier):
                self._expire_signal(dossier, self._below_bar_reason(dossier, "on the daily decay pass"))
            else:
                # Every below-bar dossier, every day -- not only the ones that
                # happened to be sitting SIGNALED. The reason a dossier did
                # NOT qualify was previously computed at exactly one moment
                # (expiry) and discarded everywhere else, so the population
                # question -- which gate is actually binding, on how many
                # names, by how much -- had no data behind it at all. One
                # bounded row per dossier per day, joining the daily snapshot
                # on symbol/date.
                self._record_decision(
                    "below_bar", symbol, dossier.direction, dossier.signaled_at,
                    reason=self._below_bar_reason(dossier, "on the daily decay pass"),
                )
        elif dossier.status == "ACTIVE":
            log.info("[SIGNAL] %s: qualifies on the daily decay pass with no new evidence "
                     "(score=%.3f, sources=%d).", symbol,
                     dossier.confidence * dossier.magnitude, dossier.independent_source_count)
            await self._fire_signal(dossier, signal)

    async def _apply_synthesis(self, dossier: Dossier, now: datetime) -> bool:
        """Runs the whole-evidence-body pass and folds its verdict into the
        dossier as a CAP on the arithmetic aggregate.

        A cap, never a lift. The arithmetic aggregate is a mechanical sum
        over independently-scored items and has no way to notice that ten of
        them are one story restated, that the facts do not cohere, or that
        the market already made the connection -- so synthesis is given the
        power to veto and to trim, which is exactly the set of errors it can
        see and the aggregate cannot. It is deliberately NOT given the power
        to raise a score into a trade: that would make one model call a
        single point of failure for committing capital, and this system's
        whole premise is that a thesis has to survive accumulation and an
        adversarial pass rather than one confident opinion.

        Failure is a no-op, not a block: a transient error or an exhausted
        budget leaves the arithmetic aggregate exactly as it was.

        Returns True when a verdict was actually applied to this dossier --
        the caller persists on that, so the verdict survives the pass even
        when it changed no decision (see _decay_one). False means nothing
        was written: no synthesizer, no resolved direction, a score too far
        below the bar for a cap to matter, or a failed/deferred call."""
        if self.synthesizer is None or dossier.direction not in ("LONG", "SHORT"):
            return False
        # Only where it can change the outcome. Synthesis is a CAP -- it can
        # veto and trim, never lift -- so on a dossier far below the bar the
        # only reachable outcomes are "unchanged" and "even further below",
        # neither of which changes a decision. Skipping those is what keeps
        # the one expensive pass in the pipeline at a handful of calls a day
        # rather than one per watchlist entry.
        floor = self.settings.signal_confidence_threshold * self.settings.synthesis_score_floor_pct
        if dossier.confidence * dossier.magnitude < floor:
            return False
        spec = self.spec_by_symbol.get(dossier.symbol)
        verdict = await self.synthesizer.synthesize(
            dossier, ecosystem=spec.ecosystem if spec is not None else "", now=now,
        )
        if verdict is None:
            return False

        dossier.synthesis_at = now.isoformat()
        dossier.synthesis_note = str(verdict.get("thesis") or "")[:600]
        dossier.synthesis_catalyst = str(verdict.get("strongest_catalyst") or "")[:300]
        # Coerced defensively, like every other read of raw tool output here
        # (see _validated_proposal / _clamp_unit, which exist because
        # Anthropic tool use does not hard-enforce the schema). This was the
        # only unguarded int() in the file, and it sits inside the decay pass
        # -- so before the isolation above, one malformed field would have
        # silently stopped all five capture files on every tick.
        try:
            dossier.distinct_fact_count = int(verdict.get("distinct_fact_count") or 0)
        except (TypeError, ValueError):
            log.warning(
                "%s: synthesis returned a non-numeric distinct_fact_count (%r) -- treating as 0.",
                dossier.symbol, verdict.get("distinct_fact_count"),
            )
            dossier.distinct_fact_count = 0
        dossier.already_priced_in = bool(verdict.get("already_priced_in"))
        # What this verdict is a claim ABOUT, recorded so it can later be
        # falsified rather than merely re-asserted: the arithmetic it capped,
        # the evidence body it read, and the price it judged. All three are
        # written on EVERY path including the veto -- a claim whose premises
        # are not recorded is unfalsifiable by construction, which is exactly
        # what the veto was.
        dossier.pre_synthesis_score = dossier.confidence * dossier.magnitude
        dossier.synthesis_keys = sorted(slot_keys(dossier, now))
        await self._capture_synthesis_price(dossier, now)
        # A fresh verdict re-opens both questions its predecessor closed.
        dossier.veto_falsified_by_price = False
        dossier.synthesis_stale_evidence = False

        dossier.synthesis_confidence = _clamp_unit(verdict.get("confidence"))
        dossier.synthesis_magnitude = _clamp_unit(verdict.get("magnitude"))

        if verdict.get("direction") != dossier.direction or dossier.already_priced_in:
            # Synthesis disagrees with the resolved direction, or says the
            # move is over. Either way this is not a thesis to enter on --
            # zero the score rather than trading against the only pass that
            # looked at the evidence as a whole.
            #
            # synthesis_confidence/magnitude are deliberately NOT zeroed here
            # any more. They used to be, and the cost was total: every vetoed
            # row recorded 0.0 for both the raw and the capped number, so a
            # thesis this pass rated 0.9-but-priced-in could never be
            # distinguished from one it rated 0.05 -- permanently, and for the
            # most expensive pass in the system. Zeroing them also silently
            # disarmed the falsification path below, because a lifted veto
            # falls through to min(score, synthesis_confidence) in
            # _cap_with_synthesis and min(anything, 0.0) is 0.0. The scores
            # this pass actually caps (confidence/magnitude) are still zeroed,
            # so the VETO's effect is unchanged.
            reason = "already priced in" if dossier.already_priced_in else "direction disagrees"
            log.info("[SYNTHESIS] %s: vetoed (%s, synthesis rated %.2f/%.2f on an arithmetic score of "
                     "%.3f) -- %s", dossier.symbol, reason, dossier.synthesis_confidence,
                     dossier.synthesis_magnitude, dossier.pre_synthesis_score,
                     dossier.synthesis_note[:160])
            dossier.confidence = 0.0
            dossier.magnitude = 0.0
            return True

        before = dossier.confidence * dossier.magnitude
        dossier.confidence = min(dossier.confidence, dossier.synthesis_confidence)
        dossier.magnitude = min(dossier.magnitude, dossier.synthesis_magnitude)
        after = dossier.confidence * dossier.magnitude
        if after < before:
            log.info(
                "[SYNTHESIS] %s: score trimmed %.3f -> %.3f (%d distinct fact(s) behind %d counted "
                "source(s)) -- %s", dossier.symbol, before, after,
                dossier.distinct_fact_count, dossier.independent_source_count,
                dossier.synthesis_catalyst[:120],
            )
        return True

    def _synthesis_verdict_fresh(self, dossier: Dossier, now: datetime) -> bool:
        """Whether a persisted verdict is still inside the window in which it
        is honoured as a cap. Outside it there is nothing to invalidate --
        the cap has already lapsed on its own."""
        if not dossier.synthesis_at:
            return False
        try:
            synth_at = datetime.fromisoformat(dossier.synthesis_at)
        except (TypeError, ValueError):
            return False
        if synth_at.tzinfo is None:
            synth_at = synth_at.replace(tzinfo=timezone.utc)
        return (now - synth_at).total_seconds() / 3600.0 <= _SYNTHESIS_CAP_MAX_AGE_HOURS

    async def _veto_refuted_by_price(self, dossier: Dossier) -> bool:
        """Whether the tape has refuted an already-priced-in verdict.

        The veto asserts something testable: the market has absorbed this, so
        there is no residual move. A material move IN THE THESIS DIRECTION
        after the verdict says otherwise -- whatever the market had absorbed,
        it was not this thesis.

        What this does NOT do is lift the cap and let the thesis fire. That
        was the tempting version and it is wrong twice over. A favourable move
        on a LONG means the price went UP, so the entry is more expensive, not
        less -- and the entry gate measures drift from the still-earlier
        inception baseline, so the very moves large enough to refute a verdict
        are the ones it then refuses (12%, engine._try_open_from_signal). The
        honest conclusion from a refuted verdict is not "enter now", it is
        "this judgement was wrong, judge it again" -- so this feeds
        re-synthesis, and a fresh whole-body verdict decides.

        No price, or no baseline, returns False: the verdict stands."""
        if not dossier.already_priced_in or dossier.synthesis_price is None:
            return False
        if dossier.direction not in ("LONG", "SHORT"):
            return False
        try:
            bar = await self._price_bar(dossier.symbol)
        except Exception:  # noqa: BLE001 - an unpriceable symbol leaves the verdict standing
            log.exception("%s: could not price the synthesis-veto falsification check.", dossier.symbol)
            return False
        if bar is None or dossier.synthesis_price <= 0:
            return False
        drift = favorable_drift_pct(dossier.direction, dossier.synthesis_price, bar.close)
        return drift >= _VETO_FALSIFICATION_DRIFT_PCT

    def _resynthesis_budget_remaining(self, now: datetime) -> bool:
        today = now.date().isoformat()
        if self.resynthesis_state.get("date") != today:
            self.resynthesis_state.set("date", today)
            self.resynthesis_state.set("count", 0)
        return int(self.resynthesis_state.get("count", 0) or 0) < _MAX_RESYNTHESIS_PER_DAY

    def _spend_resynthesis_budget(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.resynthesis_state.get("date") != today:
            self.resynthesis_state.set("date", today)
            self.resynthesis_state.set("count", 0)
        self.resynthesis_state.set("count", int(self.resynthesis_state.get("count", 0) or 0) + 1)

    async def _maybe_resynthesize(self, dossier: Dossier, now: datetime) -> None:
        """Re-judges a persisted verdict whose PREMISES have changed, on the
        merge path, before that verdict is re-applied as a cap.

        A synthesis verdict is a claim about a moment: a body of evidence
        ("this is all there is") and a price ("the market has absorbed it").
        Both move between daily passes, and until now neither could contradict
        the verdict -- so a veto was re-asserted every day against whatever
        arrived, and the only thing that could overturn it was another copy of
        itself. That is not stickiness in the architecture: _decay_one calls
        recompute_decay first, which rebuilds the score from raw evidence and
        erases the previous day's zeroes, so the escape hatch has always
        existed. It just never opened, because the verdict never changed.

        Deliberately re-JUDGES rather than lifting the cap. Skipping the cap
        on a stale verdict would fail open -- the thesis re-fires on raw
        arithmetic, which is the exact bug _cap_with_synthesis was written to
        stop. Here the cap stays in force until a real verdict replaces it,
        and every path that cannot produce one (no budget, no price, no
        synthesizer) leaves the old verdict standing."""
        if self.synthesizer is None or not self._synthesis_verdict_fresh(dossier, now):
            return

        reasons = []
        # An empty key set means the verdict predates this record, NOT that
        # every current source is new. Without this, the first merge after
        # deploying would read every pre-existing verdict as fully invalidated
        # and spend the whole daily re-synthesis allowance on a migration
        # artifact. Self-healing: the next daily pass records keys for every
        # dossier, so this is a one-pass condition rather than a permanent
        # exemption.
        stale_evidence = bool(dossier.synthesis_keys) and (
            len(slot_keys(dossier, now) - set(dossier.synthesis_keys)) >= _RESYNTHESIS_MIN_NEW_KEYS
        )
        if stale_evidence:
            reasons.append("new independent source(s) since the verdict")
        refuted_by_price = await self._veto_refuted_by_price(dossier)
        if refuted_by_price:
            reasons.append(
                f"price moved >={_VETO_FALSIFICATION_DRIFT_PCT:.0f}% toward the thesis since the "
                "already-priced-in verdict"
            )
        if not reasons:
            return

        if not self._resynthesis_budget_remaining(now):
            self._warn_once(
                f"resynthesis-cap:{now.date().isoformat()}",
                f"Off-schedule re-synthesis daily cap reached ({_MAX_RESYNTHESIS_PER_DAY}) -- "
                "invalidated verdicts stay in force until the next daily pass re-judges them.",
            )
            return

        log.info("[SYNTHESIS] %s: verdict premises changed (%s) -- re-judging.",
                 dossier.symbol, "; ".join(reasons))
        self._spend_resynthesis_budget(now)
        if not await self._apply_synthesis(dossier, now):
            # No fresh verdict (below the synthesis floor, budget exhausted,
            # or a transient failure). The old one stands and nothing is
            # attributed -- no re-judgement actually happened.
            return
        # Stamped AFTER, and only on a verdict that actually replaced its
        # predecessor: these say "the verdict now in force exists BECAUSE its
        # premises were falsified", which is the row the forward record needs
        # to bucket. Set before the call they would be cleared by it, since a
        # fresh verdict re-opens both questions (see _apply_synthesis).
        dossier.veto_falsified_by_price = refuted_by_price
        dossier.synthesis_stale_evidence = stale_evidence

    def _cap_with_synthesis(self, dossier: Dossier, now: datetime) -> None:
        """Re-applies the LAST PERSISTED synthesis verdict as a cap on the
        freshly recomputed arithmetic aggregate, on the merge path.

        _apply_synthesis runs only on the once-a-day decay pass, but a merge
        recomputes confidence/magnitude from scratch (dossier._aggregate),
        which erases any cap the morning's synthesis had applied -- so a thesis
        the whole-body pass vetoed as already-priced-in, or trimmed for
        overlap, would re-fire the instant one more item merged, on the raw
        arithmetic the synthesis exists to correct (AUDIT-2026-08 2.1.1 / A1).

        This is the cheap half of the fix: it does NOT run a fresh synthesis
        (no LLM call) -- it re-applies the verdict already computed and
        persisted by the last decay pass. A cap, never a lift, exactly like
        _apply_synthesis: a veto (already_priced_in) zeroes the score, a trim
        takes the min of the arithmetic and the synthesized values. Honoured
        only while the verdict is still fresh (_SYNTHESIS_CAP_MAX_AGE_HOURS),
        so once the daily pass stops refreshing a dossier the cap lapses rather
        than suppressing it forever, and a genuine direction flip is re-judged
        by the next decay pass within a day. No verdict, or a stale one, is a
        no-op -- identical to today's behaviour, so this can only ever lower a
        score, never raise one.

        Unconditional on the verdict's premises having changed: invalidation
        is handled upstream by _maybe_resynthesize, which REPLACES a verdict
        whose premises moved rather than skipping its cap. Skipping here would
        fail open -- the thesis would re-fire on the raw arithmetic this
        method exists to correct."""
        if not self._synthesis_verdict_fresh(dossier, now):
            return
        if dossier.already_priced_in:
            dossier.confidence = 0.0
            dossier.magnitude = 0.0
            return
        dossier.confidence = min(dossier.confidence, dossier.synthesis_confidence)
        dossier.magnitude = min(dossier.magnitude, dossier.synthesis_magnitude)

    # --- Forward-validation capture (Phase A): daily dossier score
    # snapshots and daily price marks, the raw material for eventually
    # asking "does confidence*magnitude predict forward returns" across
    # every dossier, every day -- not just the handful that become paper
    # trades. Deliberately capture-only: no analysis happens here, and
    # nothing here is gated on ANTHROPIC_API_KEY/IB being configured at
    # all except price marks needing a live price to mark. Forward data
    # can't be backfilled, so this starts accruing from day one. ---

    def _run_daily_snapshot(self) -> bool:
        """Appends every dossier's current score to
        logs/dossier_snapshots.jsonl, once a day, unconditionally (even a
        dossier with zero evidence gets a real score=0 row) -- see
        status.py's snapshot_dossier. No LLM/API cost: pure reads of
        already-persisted dossier state.

        Returns False when it wrote NOTHING, so the caller leaves the pass
        due and the next tick retries rather than recording a day that has
        no rows in it. Forward data cannot be backfilled: a day marked done
        without being captured is not a gap that gets filled in later, it is
        a sample that never existed. Writing zero rows is the failure this
        guards -- it is the shape a cold start takes (dossier directory not
        yet populated when the first tick fires), and it is indistinguishable
        after the fact from a genuinely empty day."""
        snapshotted_at = datetime.now(timezone.utc).isoformat()
        path = Path(self.settings.log_dir) / "dossier_snapshots.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with path.open("a") as f:
            for symbol in self.dossiers.all_symbols():
                dossier = self.dossiers.load(symbol)
                f.write(json.dumps(snapshot_dossier(
                    dossier, snapshotted_at, self._required_sources(dossier),
                )) + "\n")
                written += 1
        if not written:
            log.warning(
                "Daily dossier snapshot wrote no rows -- no dossiers exist yet. Leaving the pass "
                "due so the next tick retries; a day marked done with no rows is unbackfillable."
            )
        return written > 0

    async def _run_daily_price_marks(self) -> bool:
        """Appends every universe symbol's last price to
        logs/price_marks.jsonl, once a day -- the raw material for joining
        against dossier_snapshots.jsonl by symbol/date. ANCHORS are marked
        too: they never trade, but their prices widen each ecosystem's
        benchmark beyond the handful of tradeables, which is what makes the
        alpha-vs-sector-beta split in the forward-return report meaningful.

        FINNHUB FIRST, IB only for the remainder -- the reverse of every
        other price path here, and deliberately so. IB caps historical-data
        requests at roughly 60 per rolling 10 minutes per connection; this
        pass covers the WHOLE universe (209 symbols live), so routing it
        through IB blows that budget by several times over and the pacing
        violation lands on the shared connection that also has to price
        entries. IB's request budget is worth reserving for the two jobs
        that need a broker-quality bar: confirming an entry, and marking
        the handful of open paper trades. A forward-validation mark only
        needs a close, which Finnhub's /quote gives for one cheap HTTP
        request per symbol with no pacing coupling at all.

        Still not dependent on either source individually: forward data
        can't be backfilled, so a day with one source down must not be a
        permanently lost sample. Returns False when NO source produced
        anything -- the caller leaves the pass due so the next tick retries
        instead of marking a lost day done."""
        symbols = [c.symbol for c in self.universe]
        if not symbols:
            return True
        prices: dict[str, float] = {}
        if self.finnhub is not None:
            for symbol in symbols:
                try:
                    quote = await self.finnhub.quote(symbol)
                except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                    log.exception("%s: Finnhub quote failed during daily price marks.", symbol)
                    continue
                if quote is not None:
                    prices[symbol] = quote
        missing = [s for s in symbols if s not in prices]
        if missing and self.price_feed is not None and await self.price_feed.ensure_connected():
            prices.update(await self.price_feed.last_prices(missing))
        if not prices:
            return False
        marked_at = datetime.now(timezone.utc).isoformat()
        path = Path(self.settings.log_dir) / "price_marks.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for symbol, price in prices.items():
                f.write(json.dumps({"marked_at": marked_at, "symbol": symbol, "price": price}) + "\n")
        if len(prices) < len(symbols):
            log.warning("Daily price marks: %d of %d symbols priced (no source had the rest).",
                        len(prices), len(symbols))
        return True

    def _reconcile_accepted_types(self) -> None:
        """Corrects any runtime-accepted symbol whose stored type contradicts
        the engine's own recommendation for it.

        Acceptance used to be a one-way door: whatever type went in stayed,
        even once market data said otherwise. Confirmed live -- a deployment
        held ten mega/large caps as TRADEABLE, including a $323B pharma,
        every one of which the engine itself recommended as an anchor. The
        accept guard now stops that at the door, but existing state needs
        repairing too, and a recommendation can also change later (a company
        graduates past the tradeable ceiling, or coverage thickens) with no
        one watching.

        DEMOTE-ONLY: it only ever turns a trade target into a news source,
        never the reverse. Demotion is safe (an anchor can never open a
        position); promotion is not -- an anchor was accepted under the
        liberal bar with NO name-match or repeat-disclosure check, and
        auto-promoting it to tradeable here would bypass exactly the guards
        _auto_accept_candidates runs before any symbol may take positions.
        A demoted symbol's dossier is archived by the same pass that
        handles any other orphan."""
        for symbol, value in list(self.accepted_candidates.data.items()):
            as_type, source, ecosystem = self._accepted_entry(value)
            recommended = (self.candidates.get(symbol) or {}).get("recommended_as")
            if recommended != "anchor" or recommended == as_type:
                continue
            # The ecosystem is carried through, not dropped: this pass
            # rewrites the entry wholesale, so omitting it would silently
            # demote a classified symbol back into the inert "accepted"
            # bucket -- the exact failure _apply_accepted_candidates was
            # just fixed for.
            self.accepted_candidates.set(
                symbol, {"as": recommended, "source": source, "ecosystem": ecosystem}
            )
            spec = self.spec_by_symbol.get(symbol)
            if spec is not None:
                corrected = CompanySpec(spec.symbol, spec.name, spec.ecosystem,
                                        signal_source_only=(recommended == "anchor"),
                                        notes=f"Accepted ({source}), corrected to {recommended} by reconciliation")
                self.universe = [corrected if c.symbol == symbol else c for c in self.universe]
                self.spec_by_symbol[symbol] = corrected
            log.warning(
                "[UNIVERSE] %s was accepted as %s but screens as %s (%s) -- corrected automatically.",
                symbol, as_type, recommended,
                (self.candidates.get(symbol) or {}).get("recommendation_reason", ""),
            )

    def rebuild_relationship_graph(self) -> dict:
        """Queues every tradeable's most recent 10-K for relationship
        re-extraction, by clearing the once-ever backfill marker. The next
        tick picks it up (see _run_relationship_backfill); graph.add dedupes
        on (from, to, rel_type), so this can only ever ADD edges.

        Exists because extraction only writes an edge when the counterparty
        is ALREADY in the universe -- otherwise it records a watchlist
        candidate and moves on. Every candidate accepted after its
        discovering filing was processed therefore left a permanent hole:
        the filer is marked backfilled and never re-read, and backfill skips
        anchors, so the relationship that justified the acceptance could
        only reappear with next year's 10-K. _promote_pending_edges closes
        this going forward; this closes it for everything already
        discovered.

        Costs one extraction call per tradeable -- 48 against a 3000/day
        budget running at under 2% -- and no price or trade state is
        touched."""
        pending = [s.symbol for s in self.universe if not s.signal_source_only]
        for symbol in pending:
            self.backfill_state.delete(symbol)
        self._backfill_retry_after = 0.0
        log.warning("[GRAPH] Queued %d tradeable symbol(s) for relationship re-extraction against the "
                    "current %d-symbol universe. Edges are only ever added, never removed.",
                    len(pending), len(self.universe))
        return {"queued": len(pending), "symbols": pending, "edges_before": len(self.graph.relationships)}

    def reset_accepted_candidates(self) -> dict:
        """Drops every runtime-accepted symbol, returning the universe to the
        curated DEFAULT_UNIVERSE (or SYMBOLS/ANCHOR_SYMBOLS), and archives the
        dossiers that orphans.

        Exists because there was no way back: accepted symbols persisted with
        no removal path, so a deployment that accumulated wrong additions
        could only be fixed by hand-editing JSON on the Home Assistant host --
        exactly the thing the dashboard exists to avoid. Archives rather than
        deletes the dossiers, same as _archive_orphaned_dossiers.

        Candidates themselves are left intact: they are discovered facts from
        filings, and clearing them would just make the engine rediscover the
        same names and re-pay for the ticker/market-data lookups."""
        removed = sorted(self.accepted_candidates.data)
        for symbol in removed:
            self.accepted_candidates.delete(symbol)
        self.universe = list(self.settings.universe)
        self._apply_accepted_candidates()
        self.spec_by_symbol = spec_by_symbol(self.universe)
        self._archive_orphaned_dossiers()
        log.warning("[UNIVERSE] Reset %d runtime-accepted symbol(s): %s. Universe is back to the curated list.",
                    len(removed), ", ".join(removed) or "none")
        return {"removed": removed, "universe_size": len(self.universe)}

    async def _run_daily_graph_audit(self) -> None:
        """Runs the read-only audit daily and persists its summary for the
        dashboard, so a delisted or misresolved symbol surfaces within a day
        instead of waiting for someone to press the maintenance button.

        Records nothing destructive and alerts only on ACTIONABLE findings: a
        stale edge or a dangling endpoint is information, not a fault, and an
        alert that fires every day for something nobody should act on trains
        the operator to ignore the channel."""
        from smartboi.graph_audit import summarize

        try:
            findings = await self.audit_universe()
        except Exception:  # noqa: BLE001 - the audit must never kill the tick
            log.exception("[GRAPH] Daily universe audit failed -- retrying tomorrow.")
            return
        summary = self.persist_audit(findings)
        log.info("[GRAPH] Universe audit: %d finding(s), %d actionable%s.",
                 summary["total"], summary["actionable"],
                 f" ({', '.join(summary['symbols_at_fault'])})" if summary["symbols_at_fault"] else "")
        if summary["actionable"]:
            await self.alerts.send(
                "graph_audit",
                f"{summary['actionable']} universe fault(s) need a decision",
                "The daily audit found symbols that are structurally unfit to hold: "
                + ", ".join(summary["symbols_at_fault"])
                + ". Press 'Graph maintenance' on the dashboard for the detail and to quarantine them "
                  "(nothing is deleted, and a symbol with an open trade is never touched).",
                summary,
            )

    def persist_audit(self, findings: list) -> dict:
        """Stores the audit's verdict for the dashboard's graph-health panel
        and returns its summary. Called by BOTH the daily pass and the
        maintenance button, so pressing the button refreshes the panel rather
        than leaving it showing yesterday's number next to a fresh report."""
        from smartboi.graph_audit import summarize

        summary = summarize(findings)
        self.audit_state.set("last", {
            "at": datetime.now(timezone.utc).isoformat(),
            **summary,
            # Bounded: the panel shows counts and the at-fault symbols, and the
            # full evidence lives in the maintenance report. An unbounded list
            # would put every stale edge in the dashboard payload.
            "findings": [
                {"kind": f.kind, "subject": f.subject, "detail": f.detail,
                 "actionable": f.actionable, "blocked_reason": f.blocked_reason}
                for f in findings[:60]
            ],
        })
        return summary

    async def audit_universe(self) -> list:
        """Every structural fault in the universe and the graph -- see
        graph_audit.py for what is checked and why.

        Read-only. Resolves the two things the pure checks cannot (SEC's live
        ticker map, and a per-symbol name verification) and hands everything
        else in. Both async results degrade to "not checked" rather than to a
        failing verdict: an unreachable SEC must never be read as evidence that
        the whole universe is delisted."""
        from smartboi.graph_audit import audit

        live_tickers = await self.edgar_client.live_tickers() if self.edgar_client else None

        # Only symbols the audit can actually act on are worth verifying, and
        # only where a disclosed name exists to verify against.
        name_verified: dict[str, bool] = {}
        if self.edgar_client is not None:
            for symbol in sorted(self.accepted_candidates.data):
                disclosed = (self.candidates.get(symbol) or {}).get("name") or ""
                if not disclosed:
                    continue
                if live_tickers is not None and symbol not in live_tickers:
                    continue  # dead already; the name check would only add noise
                try:
                    name_verified[symbol] = await self.edgar_client.name_matches_ticker(disclosed, symbol)
                except Exception:  # noqa: BLE001 - one bad lookup must not stop the audit
                    log.exception("%s: name verification failed during the audit", symbol)

        return audit(
            accepted=self.accepted_candidates.data,
            candidates=self.candidates.data,
            relationships=self.graph.relationships,
            universe_symbols=set(self.symbol_list),
            curated_symbols={c.symbol for c in self.settings.universe},
            open_positions=set(self.journal.open_trades),
            live_tickers=live_tickers,
            name_verified=name_verified,
            is_common_equity=is_common_equity,
            lender_phrases=_LENDER_PHRASES,
            normalize_name=normalize_company_name,
            stale_edge_days=_STALE_EDGE_DAYS,
        )

    def quarantine_from_findings(self, findings: list, apply: bool = False) -> dict:
        """Removes symbols the audit found structurally unfit from the live
        universe, recording the finding that removed each one.

        Never deletes: the row moves to data/quarantined_symbols.json with its
        reason and its original accepted entry, so a wrong verdict is visible
        and reversible by hand. Only ACTIONABLE findings are acted on -- a
        curated seed symbol and a symbol with an open paper trade are both
        reported and skipped, the latter because removing it would strand a
        position that could then never be marked out.

        apply=False computes exactly what it would do and mutates nothing, the
        same contract reconcile_universe_connectivity offers."""
        actionable: dict[str, list] = {}
        for finding in findings:
            if finding.actionable and finding.is_symbol_fault:
                actionable.setdefault(finding.subject, []).append(finding)

        planned = [
            {"symbol": symbol,
             "reasons": [f"{f.kind}: {f.detail}" for f in found],
             "as": (self.accepted_candidates.get(symbol) or {}).get("as", "")}
            for symbol, found in sorted(actionable.items())
        ]
        if not apply or not planned:
            return {"applied": False, "quarantined": [], "would_quarantine": planned}

        now = datetime.now(timezone.utc).isoformat()
        for row in planned:
            symbol = row["symbol"]
            self.quarantine.set(symbol, {
                "quarantined_at": now,
                "reasons": row["reasons"],
                "was": self.accepted_candidates.get(symbol),
            })
            self.accepted_candidates.delete(symbol)
        # Rebuild from the curated base + the surviving accepted set, exactly
        # as the connectivity reconcile does, so the in-memory universe matches
        # what the next restart would load rather than drifting until then.
        self.universe = list(self.settings.universe)
        self._apply_accepted_candidates()
        self.spec_by_symbol = spec_by_symbol(self.universe)
        self._archive_orphaned_dossiers()
        removed = [row["symbol"] for row in planned]
        log.warning("[UNIVERSE] Graph maintenance quarantined %d symbol(s): %s. "
                    "Recoverable from data/quarantined_symbols.json.",
                    len(removed), ", ".join(removed))
        return {"applied": True, "quarantined": removed, "would_quarantine": planned}

    async def reconcile_universe_connectivity(self, apply: bool = False) -> dict:
        """One connectivity reconcile of the ANCHOR set: GROW with candidates
        that will land connected to a tradeable, PRUNE runtime-accepted anchors
        that are inert.

        The board this exists for: 221 of 322 anchors carried no edge to any
        tradeable (AUDIT-2026-08 A2), because auto-accept added anchors with no
        connectivity requirement and hand-editing accepted_candidates.json adds
        a symbol without ever running _promote_pending_edges. An inert anchor is
        pure cost -- its news resolves to zero analysis targets and is discarded
        unread (see gather_coverage).

        GROW: accept every discovered candidate that (a) has a resolved ticker
        not already in the universe, (b) carries a disclosed edge from a CURRENT
        tradeable (_tradeable_links -- so accept_candidate's promotion lands it
        live, not inert), (c) is anchorable equity (_anchor_equity_ok -- ADRs
        fine, preferred/share classes out), and (d) whose ticker verifies
        against the disclosed name in SEC's filer list (the Advantest->ATRO
        misresolution guard, now applied to anchors too). Accepting as an anchor
        writes its tradeable edge into the graph.

        PRUNE: drop every RUNTIME-ACCEPTED anchor with no edge to any tradeable.
        Curated DEFAULT_UNIVERSE anchors are never removed here -- a runtime
        pass cannot durably delete a code-seeded symbol (it returns next
        restart), and dropping a curated giant like NVDA is an editorial call.
        Inert seed anchors are REPORTED (inert_seed_anchors) for a human to
        remove from universe.py, not touched.

        apply=False is a dry run: it computes and returns exactly what it would
        do -- including the live SEC name-match checks, so the preview is real
        -- without mutating anything. Runs on the event loop, like the other
        universe mutators, because it reads/writes state the polling coroutines
        touch between awaits."""
        known = set(self.symbol_list)

        # --- GROW: candidates that land connected ---
        to_add: list[dict] = []
        add_skipped: list[dict] = []
        for entry in list(self.candidates.data.values()):
            ticker = (entry.get("ticker") or "").upper()
            if not ticker or ticker in known or ticker in self.accepted_candidates.data:
                continue
            if ticker in self.quarantine.data:
                continue  # quarantined by a clean -- see _auto_accept_candidates
            links = self._tradeable_links(entry)
            if not links:
                continue  # anchor-only disclosure (or sub-floor): would land inert
            if not _anchor_equity_ok(ticker):
                add_skipped.append({"symbol": ticker,
                                    "reason": _non_common_reason(ticker) or "not anchorable equity"})
                continue
            if self.edgar_client is not None and not await self.edgar_client.name_matches_ticker(
                    entry.get("name") or "", ticker):
                add_skipped.append({"symbol": ticker,
                                    "reason": f"name {entry.get('name')!r} does not match {ticker} "
                                              "in SEC's filer list"})
                continue
            ecosystem = guess_ecosystem(entry.get("related_to") or [], self.spec_by_symbol)
            to_add.append({
                "symbol": ticker,
                "name": entry.get("name") or ticker,
                "ecosystem": ecosystem if ecosystem not in ("?", "") else "accepted",
                # The screen's own verdict, carried through to the accept
                # below. This used to be hardcoded "anchor", which quietly
                # converted every candidate this arm grows -- INCLUDING ones
                # that screen as tradeable -- into an anchor permanently:
                # _auto_accept_candidates skips anything already in
                # accepted_candidates, and there is no promote-to-tradeable
                # path anywhere. So a one-way loss of exactly the scarce
                # thing (a name small enough to trade), inflicted by the tool
                # whose stated purpose is fixing connectivity. Anything
                # without a tradeable recommendation still becomes an anchor,
                # which is the conservative direction.
                #
                # is_common_equity is re-checked HERE rather than relied on
                # inside accept_candidate, because the filter applied above is
                # _anchor_equity_ok, which is deliberately LOOSER -- it admits
                # OTC ADRs and foreign ordinaries, which are fine as anchors
                # and are rejected as tradeables. Passing one through as
                # "tradeable" would raise ValueError out of accept_candidate
                # and abort the whole apply loop partway, leaving the reconcile
                # half-applied.
                "as_type": ("tradeable"
                            if entry.get("recommended_as") == "tradeable" and is_common_equity(ticker)
                            else "anchor"),
                "links": [f"{frm}:{rel}({conf:.2f})" for frm, rel, conf in links],
            })

        added: list[str] = []
        if apply:
            for a in to_add:
                spec = self.accept_candidate(a["symbol"], a["as_type"], source="connected")
                added.append(spec.symbol)

        # --- PRUNE: runtime-accepted anchors with no edge to a tradeable ---
        tradeables = {c.symbol for c in self.universe if not c.signal_source_only}
        adj: dict[str, set[str]] = {}
        for r in self.graph.relationships:
            adj.setdefault(r.from_symbol, set()).add(r.to_symbol)
            adj.setdefault(r.to_symbol, set()).add(r.from_symbol)
        inert_accepted, inert_seed = [], []
        for c in self.universe:
            if not c.signal_source_only:
                continue  # a tradeable is never inert in this sense
            if adj.get(c.symbol, set()) & tradeables:
                continue  # live: reaches at least one tradeable
            (inert_accepted if c.symbol in self.accepted_candidates.data else inert_seed).append(c.symbol)

        pruned: list[str] = []
        if apply and inert_accepted:
            for symbol in inert_accepted:
                self.accepted_candidates.delete(symbol)
            pruned = sorted(inert_accepted)
            # Rebuild the live universe from the curated base + the surviving
            # accepted set, exactly like reset_accepted_candidates, so the
            # in-memory universe and spec index match what the next restart
            # would load. Grown anchors persist -- accept_candidate wrote them
            # into accepted_candidates above.
            self.universe = list(self.settings.universe)
            self._apply_accepted_candidates()
            self.spec_by_symbol = spec_by_symbol(self.universe)
            self._archive_orphaned_dossiers()

        log.warning(
            "[UNIVERSE] Connectivity reconcile (%s): %s %d connected anchor(s), %s %d inert accepted "
            "anchor(s); %d curated seed anchor(s) inert (reported, not touched).",
            "APPLIED" if apply else "dry-run",
            "added" if apply else "would add", len(added if apply else to_add),
            "pruned" if apply else "would prune", len(pruned if apply else inert_accepted),
            len(inert_seed),
        )
        return {
            "applied": apply,
            "added": sorted(added) if apply else None,
            "would_add": to_add,
            "pruned": pruned if apply else None,
            "would_prune": sorted(inert_accepted),
            "inert_seed_anchors": sorted(inert_seed),
            "add_skipped": add_skipped,
            "universe_size": len(self.symbol_list),
        }

    def reset_runtime_state(self) -> dict:
        """Starts a clean measurement window. Archives every OPEN paper trade
        (they were opened under the previous scoring regime and never reached
        an outcome, so they must not close into the record) and resets every
        dossier's SIGNAL and SYNTHESIS episode to ACTIVE so signals re-fire
        from scratch under the current scoring rules.

        Deliberately KEEPS, rather than wipes: each dossier's accumulated
        evidence (real filings/news that simply re-aggregate under the current
        rules -- deleting it would only re-pay to re-ingest the same items),
        the relationship graph, the universe, and the SCORING_VERSION-stamped
        forward-capture logs (dossier_snapshots / price_marks / signals /
        decisions split cleanly at the version boundary downstream, so old rows
        stay segregated instead of being destroyed). For a total wipe, clear
        data/dossiers by hand.

        Exists because a scoring-rules change (a SCORING_VERSION bump) leaves
        the live board carrying signals and open trades produced by the OLD
        rules -- saturated theses, a synthesis veto the merge path re-fired,
        positions beyond the modeled slot count -- with no one-click way to
        clear them and let the new regime accumulate clean."""
        archived_trades = self.journal.archive_open_trades()
        self._entry_pending = False
        reset = 0
        for symbol in self.dossiers.all_symbols():
            dossier = self.dossiers.load(symbol)
            had_episode = (dossier.status != "ACTIVE" or dossier.signaled_at
                           or dossier.synthesis_at)
            dossier.status = "ACTIVE"
            dossier.signaled_at = ""
            dossier.signaled_price = None
            dossier.signaled_direction = ""
            dossier.drift_alert_sent = False
            dossier.entry_attempts = 0
            dossier.inception_price = None
            dossier.inception_at = ""
            dossier.inception_direction = ""
            # Stale synthesis verdicts are cleared too, so a v4 veto can't cap a
            # v5 score through the merge path's freshness window (see
            # _cap_with_synthesis) before the daily pass re-synthesizes.
            dossier.synthesis_at = ""
            dossier.synthesis_confidence = 0.0
            dossier.synthesis_magnitude = 0.0
            dossier.already_priced_in = False
            dossier.synthesis_note = ""
            dossier.synthesis_catalyst = ""
            dossier.distinct_fact_count = 0
            # The verdict's premises and the attribution flags go with it --
            # a cleared verdict must not leave behind a price baseline or a
            # key set that a LATER verdict would be falsified against.
            dossier.synthesis_price = None
            dossier.synthesis_price_at = ""
            dossier.synthesis_keys = []
            dossier.pre_synthesis_score = 0.0
            dossier.veto_falsified_by_price = False
            dossier.synthesis_stale_evidence = False
            if had_episode:
                self.dossiers.save(dossier)
                reset += 1
        log.warning(
            "[RESET] Clean measurement window: archived %d open paper trade(s) (%s), reset %d "
            "dossier(s) to ACTIVE. Evidence, graph, universe and version-stamped forward logs kept.",
            len(archived_trades), ", ".join(archived_trades) or "none", reset,
        )
        return {"archived_open_trades": archived_trades, "dossiers_reset": reset}

    def _archive_orphaned_dossiers(self) -> None:
        """Moves dossiers for symbols that are no longer tradeable out of the
        live directory into data/dossiers_archived/.

        Nothing previously cleaned these up, so a demotion (or a universe
        refresh) silently left the file behind: still loaded by every decay
        pass, still snapshotted into the forward-validation log as though it
        were a live thesis, and -- before the guard in _mark_and_execute --
        still able to open a paper trade. Archived rather than deleted
        because the accumulated evidence is real history, and because a
        symbol promoted back to tradeable should not silently start from a
        blank thesis."""
        archive = DATA_DIR / "dossiers_archived"
        moved = []
        for symbol in self.dossiers.all_symbols():
            if self._is_tradeable(symbol):
                continue
            archive.mkdir(parents=True, exist_ok=True)
            source = self.dossiers.dir_path / f"{symbol}.json"
            try:
                source.replace(archive / f"{symbol}.json")
            except OSError:
                log.exception("%s: could not archive orphaned dossier.", symbol)
                continue
            moved.append(symbol)
        if moved:
            log.info(
                "[DOSSIER] Archived %d dossier(s) for symbols no longer tradeable: %s "
                "(moved to %s, not deleted).",
                len(moved), ", ".join(moved), archive,
            )

    # --- Universe auto-screen ---

    async def _run_universe_screen(self) -> None:
        results = await screen_universe(
            self.universe, self.finnhub,
            self.settings.universe_min_market_cap_musd,
            self.settings.universe_max_market_cap_musd,
            self.settings.universe_max_analyst_count,
        )
        path = Path(self.settings.log_dir) / "universe_screen.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "screened_at": datetime.now(timezone.utc).isoformat(),
                        "results": [asdict(r) for r in results],
                    }
                )
                + "\n"
            )
        self.universe_screen_state.set("last_screened_at", datetime.now(timezone.utc).isoformat())
        self._prune_dead_symbols(results)
        self._demote_graduated_tradeables(results)

    def _prune_dead_symbols(self, results: list) -> list[str]:
        """Drops runtime-accepted symbols the screen found NO market data for
        at all -- delisted, acquired, or an OTC/foreign line no data source
        covers. Until now the screen only logged: a dead ticker kept costing
        an EDGAR poll, a news poll and any resulting LLM calls on every
        cycle, forever, while being incapable of ever being priced, marked
        or traded. Confirmed live: a literal "NULL", plus BMWYY/VLKAY/HYMTF
        as never-screened anchors.

        Only RUNTIME-ACCEPTED symbols are pruned. A curated symbol (from
        universe.py, or SYMBOLS/ANCHOR_SYMBOLS) is a deliberate human
        choice: removing it here would be un-undoable from the dashboard and
        would silently fight the operator's own list on every screen. Those
        are reported loudly instead, and recorded so the diagnostics bundle
        can show them without needing the log."""
        dead = [r.symbol for r in results if r.market_cap_musd is None]
        pruned, curated = [], []
        for symbol in dead:
            if symbol in self.accepted_candidates.data:
                self.accepted_candidates.delete(symbol)
                pruned.append(symbol)
            else:
                curated.append(symbol)
        self.universe_screen_state.set("curated_no_market_data", sorted(curated))
        if pruned:
            self.universe = [c for c in self.universe if c.symbol not in set(pruned)]
            self.spec_by_symbol = spec_by_symbol(self.universe)
            self._archive_orphaned_dossiers()
            log.warning(
                "[UNIVERSE] Pruned %d runtime-accepted symbol(s) with no market data at all: %s. "
                "They can never be priced or traded, so polling them was pure cost.",
                len(pruned), ", ".join(pruned),
            )
        if curated:
            log.warning(
                "[UNIVERSE] %d CURATED symbol(s) returned no market data: %s. Left in place "
                "(a curated list is a deliberate choice, not the screen's to overrule) -- remove "
                "them from universe.py / SYMBOLS if they are genuinely dead.",
                len(curated), ", ".join(sorted(curated)),
            )
        return pruned

    def _demote_graduated_tradeables(self, results: list) -> list[str]:
        """Demotes any RUNTIME-ACCEPTED tradeable the monthly screen finds has
        outgrown the thin-coverage bounds -- graduated past the market-cap
        ceiling, or picked up analyst coverage -- to an anchor: a news source,
        never a trade target.

        The accept-time guard and the bounds-change reconcile pass leave a
        hole. A runtime-accepted tradeable's cached recommendation is only
        re-derived when the OPERATOR moves the universe bounds (see
        _run_candidate_ticker_recheck's `recommendation_bounds` guard), so a
        company that graduates past the ceiling on its own keeps opening paper
        trades forever -- the correlated-large-cap rot the monthly screen
        exists to catch (README point 1; AUDIT-2026-08-FOLLOWUP HIGH-2). The
        screen already re-fetches market data every run, so drive demotion off
        ITS result rather than the frozen cached recommendation.

        DEMOTE-ONLY, and only for GRADUATED names: a sub-floor small-cap still
        screens as a valid (just smaller) trade target, so
        recommend_candidate_type still calls it 'tradeable' and it is left
        alone; only a name now recommended as 'anchor' is demoted. Curated
        symbols are never touched -- a hand-picked list is the operator's call
        (screen_universe reports them loudly instead). Safe like
        _reconcile_accepted_types: an anchor can never open a position, so this
        cannot bypass the tradeable-accept guards."""
        demoted = []
        for r in results:
            # Anchors are exempt from the bounds; still_fits needs nothing; no
            # market data at all is _prune_dead_symbols' job, not a demotion.
            if r.is_anchor or r.still_fits or r.market_cap_musd is None:
                continue
            if r.symbol not in self.accepted_candidates.data:
                continue  # curated -- not the screen's to overrule
            as_type, source, ecosystem = self._accepted_entry(self.accepted_candidates.data[r.symbol])
            if as_type != "tradeable":
                continue  # already an anchor
            recommendation, _reason = recommend_candidate_type(
                r.market_cap_musd, r.analyst_count,
                self.settings.universe_min_market_cap_musd,
                self.settings.universe_max_market_cap_musd,
                self.settings.universe_max_analyst_count,
            )
            if recommendation != "anchor":
                continue  # sub-floor etc. -- still a legitimate (smaller) trade target
            self.accepted_candidates.set(
                r.symbol, {"as": "anchor", "source": source, "ecosystem": ecosystem}
            )
            spec = self.spec_by_symbol.get(r.symbol)
            if spec is not None:
                corrected = CompanySpec(
                    spec.symbol, spec.name, spec.ecosystem, signal_source_only=True,
                    notes=f"Accepted ({source}), demoted to anchor by the monthly screen ({r.reason})",
                )
                self.universe = [corrected if c.symbol == r.symbol else c for c in self.universe]
                self.spec_by_symbol[r.symbol] = corrected
            demoted.append(r.symbol)
            log.warning(
                "[UNIVERSE] %s outgrew the thin-coverage bounds (%s) -- demoted from tradeable to "
                "anchor so it stops opening paper trades.", r.symbol, r.reason,
            )
        return demoted
