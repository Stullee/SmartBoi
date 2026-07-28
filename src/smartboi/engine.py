"""Orchestrates the whole pipeline: EDGAR + news ingestion -> relationship
graph -> dossier updates -> adversarial skeptic pass -> signal evaluation
-> (optional) hypothetical paper trade. See README for the strategy this
implements point-by-point.

Every optional integration degrades gracefully instead of failing to
start -- see config.py's docstring. Add EDGAR_USER_AGENT and/or
FINNHUB_API_KEY to start collecting evidence; add ANTHROPIC_API_KEY to
start scoring it into dossiers; add IB (ENABLE_IB_PRICE_FEED=true) to start
actually opening/marking hypothetical positions. Signals are detected and
logged (signals.jsonl) the moment ANTHROPIC_API_KEY is present, regardless
of whether IB is configured yet.

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
import json
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smartboi.alerts import AlertSender
from smartboi.config import Settings
from smartboi.dedup import DedupIndex, fingerprint, source_domain
from smartboi.edgar import _truncate_head_tail
from smartboi.dossier import (
    DIRECTIONS,
    Dossier,
    DossierStore,
    DossierUpdater,
    EvidenceRecord,
    has_evidence,
    merge_evidence,
    recompute_decay,
)
from smartboi.edgar import EdgarClient, FilingEvent
from smartboi.graph import REL_TYPES, RelationshipExtractor, RelationshipGraph, Relationship
from smartboi.news import FinnhubClient
from smartboi.paper_journal import PaperTradeJournal
from smartboi.prices import ReadOnlyPriceFeed
from smartboi.ratelimit import SlidingWindowLimiter
from smartboi.signals import evaluate, favorable_drift_pct, log_signal, signal_expired
from smartboi.skeptic import Skeptic
from smartboi.state import JsonState
from smartboi.status import snapshot_dossier
from smartboi.universe import SEED_RELATIONSHIPS, CompanySpec, spec_by_symbol
from smartboi.universe_screen import recommend_candidate_type, screen_universe
from smartboi.usage import UsageTracker
from smartboi.webapp import run_dashboard

log = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 30
DATA_DIR = Path("data")
# When the IB Gateway is unreachable, retry the connection this often
# instead of waiting out a full price_poll_interval_sec (6h by default) --
# the Gateway restarting daily, or simply not being up yet, shouldn't cost
# most of a day of price marks.
IB_RETRY_GAP_SEC = 900
# Evidence time-decay is a slow-moving correction, not a live signal --
# recomputing it once a day is plenty (see _run_decay_pass).
DECAY_PASS_INTERVAL_SEC = 86400
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
)

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
        self.accepted_candidates = JsonState(DATA_DIR / "accepted_candidates.json")
        # {date, count} -- the UTC-daily auto-accept budget (see
        # _auto_accept_candidates). Persisted so a restart cannot reset the
        # cap and let a long candidate list through in one afternoon.
        self.auto_accept_state = JsonState(DATA_DIR / "auto_accept_state.json")
        # Which model snapshots produced the existing record -- see
        # _check_model_provenance for why a change to these matters.
        self.model_state = JsonState(DATA_DIR / "model_provenance.json")
        self.alerts = AlertSender(settings.alert_webhook_url)
        self.usage = UsageTracker(DATA_DIR / "llm_usage.json", settings.max_daily_llm_calls)

        self.universe: list[CompanySpec] = list(settings.universe)
        self._apply_accepted_candidates()
        self.spec_by_symbol = spec_by_symbol(self.universe)

        self._propagation_limiter = SlidingWindowLimiter(
            settings.max_propagated_evidence_per_link,
            settings.propagated_evidence_cooldown_hours * 3600,
        )
        # Caches a dossier-update proposal (evidence_id -> proposed dict)
        # between the propose_update call and the skeptic call, so that if
        # the skeptic call is deferred by an exhausted daily LLM call budget
        # (see usage.py), the retry on a later poll doesn't re-pay for
        # propose_update -- it already has the answer, it just needs the
        # skeptic's verdict. In-memory only: on a restart the worst case is
        # falling back to today's behavior (re-propose from scratch), never
        # a new failure mode, so this doesn't need to survive a restart.
        # Values are (proposal, cached_at_monotonic) so entries whose
        # evidence has aged out of every ingestion lookback window (and so
        # can never be retried) are evicted instead of leaking forever.
        self._pending_proposals: dict[str, tuple[dict, float]] = {}
        # (target_symbol, evidence_id) pairs handled definitively WITHOUT a
        # merge (skeptic-refuted, judged not-new, or a malformed proposal).
        # has_evidence() makes the merged outcome idempotent across retries,
        # but these outcomes previously left no marker at all -- so when a
        # SIBLING target of the same evidence item was budget-deferred, the
        # retry re-paid propose_update (and the skeptic) for targets that
        # were already definitively done, and a second skeptic run could
        # even accept what the first had refuted. In-memory only, same
        # restart trade-off as _pending_proposals.
        self._handled_outcomes: dict[str, float] = {}

        self.edgar_client: EdgarClient | None = None
        self.finnhub: FinnhubClient | None = None
        self.extractor: RelationshipExtractor | None = None
        self.updater: DossierUpdater | None = None
        self.skeptic: Skeptic | None = None
        self.price_feed: ReadOnlyPriceFeed | None = None

        self._warned: set[str] = set()
        # None = never polled this process -> due immediately on the first
        # tick. (These are time.monotonic() marks; comparing against an
        # initial 0.0 would compare against machine BOOT time and delay the
        # first pass by up to a full interval after a reboot.)
        self._last_edgar_poll: float | None = None
        self._last_news_poll: float | None = None
        self._last_price_poll: float | None = None
        self._last_decay_pass: float | None = None
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
    def _accepted_entry(value) -> tuple[str, str]:
        """(as_type, source) from an accepted_candidates.json entry. Entries
        were originally a bare "tradeable"/"anchor" string and are now a
        {"as", "source"} dict recording whether a human or the engine
        accepted it (see accept_candidate) -- both shapes are read so an
        existing file keeps working untouched across the upgrade."""
        if isinstance(value, dict):
            return value.get("as", "tradeable"), value.get("source", "manual")
        return value, "manual"

    def _apply_accepted_candidates(self) -> None:
        known = {c.symbol for c in self.universe}
        for symbol, value in self.accepted_candidates.data.items():
            if symbol in known:
                continue
            as_type, source = self._accepted_entry(value)
            self.universe.append(
                CompanySpec(symbol, symbol, "accepted", signal_source_only=(as_type == "anchor"),
                            notes=f"Accepted ({source}) from a discovered universe candidate")
            )
            known.add(symbol)

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
        if as_type == "tradeable" and recommended == "anchor":
            raise ValueError(
                f"{symbol} screens as an ANCHOR, not a trade target "
                f"({entry.get('recommendation_reason', 'past the tradeable bounds')}). "
                "Add it as an anchor instead -- its news will still propagate. "
                "If you really want it tradeable, put it in SYMBOLS."
            )
        spec = CompanySpec(symbol, symbol, "accepted", signal_source_only=(as_type == "anchor"),
                            notes=f"Accepted ({source}) from a discovered universe candidate")
        self.universe.append(spec)
        self.spec_by_symbol[symbol] = spec
        # Stored as a dict (not a bare type string) once a source is
        # recorded, so an auto-accepted symbol is distinguishable from one a
        # human chose -- _apply_accepted_candidates reads both shapes, so
        # existing files written before this keep working untouched.
        self.accepted_candidates.set(symbol, {"as": as_type, "source": source})
        log.info("[CANDIDATE] %s accepted (%s) into the universe as %s -- polled starting next cycle.",
                 symbol, source, as_type)
        return spec

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
                    "(logs/signals.jsonl) while unreachable.",
                    self.settings.ib_host, self.settings.ib_port, IB_RETRY_GAP_SEC // 60,
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
            if self.price_feed is not None:
                self.price_feed.disconnect()
            await self.alerts.aclose()

    @staticmethod
    def _due(last: float | None, interval_sec: float, now: float) -> bool:
        return last is None or now - last >= interval_sec

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
        if self.price_feed is not None and self._due(self._last_price_poll, self.settings.price_poll_interval_sec, now):
            if await self.price_feed.ensure_connected():
                self._last_price_poll = now
                await self._mark_and_execute()
            else:
                self._warn_once(
                    "ib-unreachable",
                    f"IB Gateway unreachable -- the price feed keeps retrying every {IB_RETRY_GAP_SEC // 60} min "
                    "in the background. Set ENABLE_IB_PRICE_FEED=false if you don't want it yet.",
                )
                # Leave the poll pending but back off the connection attempt.
                self._last_price_poll = now - self.settings.price_poll_interval_sec + IB_RETRY_GAP_SEC
        if (
            self.finnhub is not None
            and self.settings.enable_universe_autoscreen
            and self._universe_screen_due()
        ):
            await self._run_universe_screen()
        if self._due(self._last_decay_pass, DECAY_PASS_INTERVAL_SEC, now):
            self._last_decay_pass = now
            self._archive_orphaned_dossiers()
            self._run_decay_pass()
        # Both daily passes are marked done AFTER a successful run, not
        # before: forward data can't be backfilled, so a pass that raised
        # (disk error, feed dropping mid-fetch) must stay due and be
        # retried on the next tick instead of silently losing the day's
        # capture. Duplicate rows from a partial write are handled
        # downstream (dedup_snapshots / last-mark-wins), a lost day is not.
        if self._daily_pass_due("dossier_snapshot"):
            self._run_daily_snapshot()
            self._mark_daily_pass_done("dossier_snapshot")
        # Daily price marks are deliberately NOT gated on IB being enabled
        # or reachable -- they are the raw material for the forward-return
        # validation, and a missed day is permanently unbackfillable. IB is
        # preferred when available; Finnhub's /quote fills in otherwise.
        if (
            (self.price_feed is not None or self.finnhub is not None)
            and now >= self._price_marks_retry_after
            and self._daily_pass_due("price_marks")
        ):
            if await self._run_daily_price_marks():
                self._mark_daily_pass_done("price_marks")
            else:
                self._price_marks_retry_after = time.monotonic() + IB_RETRY_GAP_SEC
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
        dossier-snapshot/price-marks passes: scheduled off a PERSISTED
        wall-clock timestamp (self.periodic_state), not a process-local
        time.monotonic() marker. A process-local marker resets to "due
        immediately" on every restart, and unlike the decay pass or the
        candidate recheck (idempotent -- re-running them early changes
        nothing), these two passes unconditionally APPEND a fresh row per
        symbol every time they run -- so a deployment restarting several
        times in one day would silently write a full duplicate batch on
        every restart. Confirmed live: 6 duplicate dossier_snapshots.jsonl
        batches from 6 restarts in one day, inflating downstream forward-
        return analysis 6x for that day's rows."""
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

    async def _process_filing(self, symbol: str, filing: FilingEvent) -> None:
        fp = f"filing:{symbol}:{filing.accession_number}"
        if self.dedup.is_duplicate(fp):
            return

        text = await self.edgar_client.fetch_evidence_text(filing)
        if not text:
            return  # fetch failed/empty -- unregistered, so the next poll retries it

        if filing.form in RELATIONSHIP_EXTRACTION_FORMS and self.extractor is not None:
            await self._extract_relationships(symbol, filing, text)

        # Head + tail rather than a flat prefix: the first few thousand
        # characters of a filing are mostly the SEC cover page and checkbox
        # boilerplate, so a flat text[:4000] often fed the dossier engine
        # near-zero actual content -- the disclosed items sit further in.
        evidence_text = (
            f"SEC {filing.form} filed {filing.filing_date} for {symbol}:\n"
            f"{_truncate_head_tail(text, 4000)}"
        )
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
            headline=f"{symbol} {filing.form} filed {filing.filing_date}",
            published_at=filing.filing_date,
        )
        if scored:
            self.dedup.register(fp, "sec.gov")

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
            if self._is_lender_relationship(rel):
                log.info(
                    "%s: dropping lender relationship to %s (a credit provider is a disclosed "
                    "counterparty, but its news has no path to this company's fundamentals).",
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
            recommendation = entry.get("recommended_as")
            if recommendation not in ("anchor", "tradeable"):
                continue  # "unknown" (no market data) is never auto-accepted

            if recommendation == "anchor":
                if not self.settings.auto_accept_anchors:
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
        discloses are durable, the sentiment is not). Anchors are skipped --
        a giant's 10-K never names its small suppliers, which is exactly why
        the graph is discovered from the small companies' filings. Each
        symbol is backfilled once ever (persisted), so a symbol accepted at
        runtime (accept_candidate) gets backfilled on the very next tick
        without needing a restart."""
        if time.monotonic() < self._backfill_retry_after:
            return
        pending = [
            spec.symbol
            for spec in self.universe
            if not spec.signal_source_only and not self.backfill_state.get(spec.symbol)
        ]
        if not pending:
            return
        log.info("Relationship backfill: extracting from the most recent 10-K of %d symbol(s): %s",
                 len(pending), ", ".join(pending))
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

    # --- News ingestion ---

    async def _poll_news(self) -> None:
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=self.settings.news_lookback_days)).isoformat()
        for symbol in self.symbol_list:
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
        # (target_symbol, relationship_note, relationship_confidence) --
        # confidence is the graph edge's own extracted confidence (None for
        # direct/non-propagated evidence, which has no edge at all).
        targets: list[tuple[str, str, float | None]] = []
        propagation_keys: dict[str, str] = {}  # target_symbol -> limiter key, propagated targets only

        origin_spec = self.spec_by_symbol.get(origin_symbol)
        if origin_spec is None or not origin_spec.signal_source_only:
            targets.append((origin_symbol, "", None))

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
            targets.append((linked_symbol, rel.description, rel.confidence))
        if throttled:
            self._warn_once(
                f"throttled:{origin_symbol}",
                f"{origin_symbol}: propagated-evidence cooldown engaged for {throttled} linked symbol(s) "
                f"(already sent {self.settings.max_propagated_evidence_per_link} items in the last "
                f"{self.settings.propagated_evidence_cooldown_hours}h) -- further {origin_symbol} evidence won't "
                "reach them until the window rolls off. Raise MAX_PROPAGATED_EVIDENCE_PER_LINK if this is "
                "suppressing real signal.",
            )

        all_definitive = True
        for target_symbol, relationship_note, relationship_confidence in targets:
            outcome = await self._update_dossier(
                target_symbol, evidence_text, origin_symbol, relationship_note, relationship_confidence,
                source_type, source_name, url, headline, published_at,
            )
            # Only a target handled fresh THIS pass consumes a cooldown
            # slot -- "already" means an earlier, partially-deferred pass
            # already handled (and charged) this same evidence for this
            # target, and re-recording it on every retry used to fill the
            # window with phantom events that throttled genuinely new
            # propagation for hours.
            if outcome == "handled" and target_symbol in propagation_keys:
                self._propagation_limiter.record(propagation_keys[target_symbol], now)
            all_definitive = all_definitive and outcome != "deferred"
        return all_definitive

    # How long a cached pending proposal or handled-outcome marker can be
    # retried before its evidence has aged out of every ingestion lookback
    # window (14-day EDGAR is the longest) and the entry is just a leak.
    _EVIDENCE_CACHE_TTL_SEC = 15 * 86400

    def _evict_stale_evidence_caches(self) -> None:
        cutoff = time.monotonic() - self._EVIDENCE_CACHE_TTL_SEC
        self._pending_proposals = {
            k: v for k, v in self._pending_proposals.items() if v[1] >= cutoff
        }
        self._handled_outcomes = {
            k: t for k, t in self._handled_outcomes.items() if t >= cutoff
        }

    def _mark_handled(self, proposal_key: str) -> str:
        """Records a definitive non-merge outcome (not-new, refuted, or a
        malformed proposal) so a retry of the same evidence item -- forced
        by a SIBLING target's deferral -- doesn't re-pay propose_update and
        the skeptic for a target that was already definitively done (and,
        worse, give a nondeterministic second skeptic run the chance to
        accept what the first refuted)."""
        self._handled_outcomes[proposal_key] = time.monotonic()
        self._evict_stale_evidence_caches()
        return "handled"

    @staticmethod
    def _validated_proposal(target_symbol: str, proposed: dict) -> dict | None:
        """Clamped, type-checked copy of a propose_update tool response, or
        None when a required field is missing/invalid -- Anthropic tool use
        does not hard-enforce the declared schema, and indexing a missing
        field used to abort the entire poll cycle after the call was paid."""
        try:
            direction = proposed["direction"]
            if direction not in DIRECTIONS:
                raise ValueError(f"direction {direction!r}")
            return {
                "is_new_information": bool(proposed.get("is_new_information")),
                "direction": direction,
                "magnitude": min(1.0, max(0.0, float(proposed["magnitude"]))),
                "confidence": min(1.0, max(0.0, float(proposed["confidence"]))),
                "horizon_days": max(1, int(proposed["horizon_days"])),
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
            raw = await self.updater.propose_update(
                dossier, evidence_text, origin_symbol, relationship_note, relationship_confidence
            )
            if raw is None:
                return "deferred"  # transient LLM failure or budget exhausted -- retry later
            proposed = self._validated_proposal(target_symbol, raw)
            if proposed is None:
                return self._mark_handled(proposal_key)  # malformed -- dropping is definitive
            if not proposed["is_new_information"]:
                return self._mark_handled(proposal_key)
            self._pending_proposals[proposal_key] = (proposed, time.monotonic())

        verdict = await self.skeptic.review(evidence_text, proposed, relationship_note, relationship_confidence)
        if verdict is None:
            return "deferred"  # transient LLM failure or budget exhausted -- retry later
        self._pending_proposals.pop(proposal_key, None)
        if verdict.get("refuted"):
            log.info("%s: evidence refuted by skeptic (%s)", target_symbol, verdict.get("reasoning", ""))
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
        self.dossiers.save(dossier)
        log.info(
            "[EVIDENCE] %s: accepted %s magnitude=%.2f confidence=%.2f (dossier now: direction=%s "
            "confidence=%.2f magnitude=%.2f sources=%d, status=%s)",
            target_symbol, record.direction, record.magnitude, record.confidence,
            dossier.direction, dossier.confidence, dossier.magnitude,
            dossier.independent_source_count, dossier.status,
        )

        signal = evaluate(dossier, self.settings.signal_confidence_threshold, self.settings.min_independent_sources)
        if signal is not None:
            if dossier.status == "ACTIVE" or dossier.direction != dossier.signaled_direction:
                # A fresh signal (or a thesis that flipped direction while
                # still SIGNALED-but-unopened) gets a fresh price baseline --
                # see _snapshot_signal_price and _try_open_from_signal below.
                # Status/baseline are set BEFORE logging so every logged
                # signal row carries its episode key (signaled_at) and
                # re-logs of one episode can be collapsed downstream.
                dossier.status = "SIGNALED"
                await self._snapshot_signal_price(dossier)
                self.dossiers.save(dossier)
            log_signal(Path(self.settings.log_dir) / "signals.jsonl", signal, episode=dossier.signaled_at)
            await self.alerts.send(
                "signal",
                f"{signal.direction} signal: {signal.symbol}",
                f"confidence={signal.confidence:.2f} magnitude={signal.magnitude:.2f} "
                f"sources={signal.independent_source_count}. {signal.thesis_summary}",
                asdict(signal),
            )
        elif dossier.status == "SIGNALED" and not self.journal.has_open(target_symbol):
            # Newly merged evidence dropped the thesis below the signal bar
            # (or flipped it) while it sat SIGNALED-but-unopened. Left
            # as-is, the next price poll would still open a paper trade on
            # a thesis that no longer qualifies -- possibly in the OPPOSITE
            # direction from the one that signaled, against a baseline
            # snapped for the old thesis.
            self._expire_signal(dossier, "evidence dropped below the signal threshold before an entry was confirmed")
        return "handled"

    # --- Price marking / hypothetical execution ---

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
            if self.price_feed is not None and await self.price_feed.ensure_connected():
                dossier.signaled_price = await self.price_feed.last_price(dossier.symbol)
            if dossier.signaled_price is None and self.finnhub is not None:
                # Finnhub's /quote keeps the drift baseline usable when IB
                # is down or not configured -- a missing baseline silently
                # disables the "are we too late" check for this signal.
                dossier.signaled_price = await self.finnhub.quote(dossier.symbol)
        except Exception:  # noqa: BLE001 - a missing baseline just disables the drift check for this signal
            log.exception("%s: could not snapshot signal-time price.", dossier.symbol)

    def _reset_to_active(self, dossier: Dossier) -> None:
        dossier.status = "ACTIVE"
        dossier.signaled_at = ""
        dossier.signaled_price = None
        dossier.signaled_direction = ""
        dossier.drift_alert_sent = False

    def _expire_signal(self, dossier: Dossier, reason: str) -> None:
        log.info("[SIGNAL] %s: expiring unopened signal (%s) -- resetting to ACTIVE so fresh "
                 "evidence can re-trigger it with a clean baseline.", dossier.symbol, reason)
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
        if (
            evaluate(dossier, self.settings.signal_confidence_threshold,
                     self.settings.min_independent_sources) is None
            or dossier.direction != dossier.signaled_direction
        ):
            self._expire_signal(dossier, "thesis no longer qualifies at entry time")
            return
        try:
            price = await self.price_feed.last_price(symbol)
        except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
            log.exception("%s: could not fetch entry price.", symbol)
            return
        if price is None:
            return

        if dossier.signaled_price is not None:
            drift = favorable_drift_pct(dossier.direction, dossier.signaled_price, price)
            if drift >= self.settings.max_favorable_drift_pct:
                if not dossier.drift_alert_sent:
                    dossier.drift_alert_sent = True
                    self.dossiers.save(dossier)
                    log.info(
                        "[SIGNAL] %s: price already moved %.1f%% favorably since the signal fired "
                        "at $%.2f (now $%.2f) -- likely already priced in, skipping entry.",
                        symbol, drift, dossier.signaled_price, price,
                    )
                    await self.alerts.send(
                        "signal_stale",
                        f"{symbol}: likely already priced in",
                        f"Price moved {drift:.1f}% in the favorable direction since the signal fired "
                        f"at ${dossier.signaled_price:.2f} (now ${price:.2f}) -- skipping entry to avoid "
                        "chasing a move that may already be over.",
                        {"symbol": symbol, "drift_pct": drift, "signaled_price": dossier.signaled_price, "current_price": price},
                    )
                if signal_expired(dossier.signaled_at, self.settings.signal_entry_deadline_days):
                    self._expire_signal(dossier, f"price drifted {drift:.1f}% before an entry could be confirmed")
                return

        if signal_expired(dossier.signaled_at, self.settings.signal_entry_deadline_days):
            self._expire_signal(dossier, "no confirmed entry within the deadline")
            return

        citations = [
            {
                "source_name": e.source_name, "url": e.url,
                "headline": e.headline, "published_at": e.published_at,
            }
            for e in dossier.evidence[-5:]
        ]
        horizon = min(dossier.horizon_days or self.settings.max_horizon_days, self.settings.max_horizon_days)
        trade = self.journal.open(
            symbol, dossier.direction, price,
            self.settings.stop_loss_pct, self.settings.take_profit_pct,
            horizon, dossier.thesis_summary, dossier.confidence,
            dossier.independent_source_count, citations,
            cost_bps_round_trip=self.settings.transaction_cost_bps_per_side * 2,
        )
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

        open_symbols = list(self.journal.open_trades.keys())
        if not open_symbols:
            return
        bars = await self.price_feed.last_bars(open_symbols)
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

    def _run_decay_pass(self) -> None:
        """Once a day, re-scores every dossier's aggregate confidence/
        magnitude/independent_source_count against its EXISTING evidence
        with no new evidence required -- otherwise a dormant dossier (no
        fresh news landing on it) would keep yesterday's confidence
        forever. See dossier.py's evidence_weight/evidence_is_stale for the
        actual decay curve. A SIGNALED-but-not-yet-entered dossier that
        decays below the signal threshold is expired back to ACTIVE (the
        thesis is going cold without ever getting a confirmed entry); once
        a paper trade has actually opened, decay no longer touches that
        dossier -- the open trade has its own stop/target/horizon."""
        now = datetime.now(timezone.utc)
        for symbol in self.dossiers.all_symbols():
            dossier = self.dossiers.load(symbol)
            if not dossier.evidence:
                continue
            before = (dossier.direction, round(dossier.confidence, 3), round(dossier.magnitude, 3),
                      dossier.independent_source_count)
            recompute_decay(dossier, now)
            after = (dossier.direction, round(dossier.confidence, 3), round(dossier.magnitude, 3),
                     dossier.independent_source_count)
            if before == after:
                continue
            if dossier.status == "SIGNALED" and not self.journal.has_open(symbol):
                signal = evaluate(dossier, self.settings.signal_confidence_threshold, self.settings.min_independent_sources)
                if signal is None:
                    self._expire_signal(dossier, "evidence decayed below the signal threshold before an entry was confirmed")
                    continue
            self.dossiers.save(dossier)

    # --- Forward-validation capture (Phase A): daily dossier score
    # snapshots and daily price marks, the raw material for eventually
    # asking "does confidence*magnitude predict forward returns" across
    # every dossier, every day -- not just the handful that become paper
    # trades. Deliberately capture-only: no analysis happens here, and
    # nothing here is gated on ANTHROPIC_API_KEY/IB being configured at
    # all except price marks needing a live price to mark. Forward data
    # can't be backfilled, so this starts accruing from day one. ---

    def _run_daily_snapshot(self) -> None:
        """Appends every dossier's current score to
        logs/dossier_snapshots.jsonl, once a day, unconditionally (even a
        dossier with zero evidence gets a real score=0 row) -- see
        status.py's snapshot_dossier. No LLM/API cost: pure reads of
        already-persisted dossier state."""
        snapshotted_at = datetime.now(timezone.utc).isoformat()
        path = Path(self.settings.log_dir) / "dossier_snapshots.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for symbol in self.dossiers.all_symbols():
                dossier = self.dossiers.load(symbol)
                f.write(json.dumps(snapshot_dossier(dossier, snapshotted_at)) + "\n")

    async def _run_daily_price_marks(self) -> bool:
        """Appends every universe symbol's last price to
        logs/price_marks.jsonl, once a day -- the raw material for joining
        against dossier_snapshots.jsonl by symbol/date. ANCHORS are marked
        too: they never trade, but their prices widen each ecosystem's
        benchmark beyond the handful of tradeables, which is what makes the
        alpha-vs-sector-beta split in the forward-return report meaningful.

        Deliberately NOT dependent on IB: forward data can't be backfilled,
        so a day with the Gateway down (or IB never configured) must not be
        a permanently lost sample. IB is preferred when reachable; whatever
        it misses is filled from Finnhub's /quote. Returns False when no
        price source produced anything -- the caller leaves the pass due so
        the next tick retries instead of marking a lost day done."""
        symbols = [c.symbol for c in self.universe]
        if not symbols:
            return True
        prices: dict[str, float] = {}
        if self.price_feed is not None and await self.price_feed.ensure_connected():
            prices = await self.price_feed.last_prices(symbols)
        missing = [s for s in symbols if s not in prices]
        if missing and self.finnhub is not None:
            for symbol in missing:
                try:
                    quote = await self.finnhub.quote(symbol)
                except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                    log.exception("%s: Finnhub quote failed during daily price marks.", symbol)
                    continue
                if quote is not None:
                    prices[symbol] = quote
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
            as_type, source = self._accepted_entry(value)
            recommended = (self.candidates.get(symbol) or {}).get("recommended_as")
            if recommended != "anchor" or recommended == as_type:
                continue
            self.accepted_candidates.set(symbol, {"as": recommended, "source": source})
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
