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
poll, so none of those ever permanently burn evidence; per-dossier merging
is idempotent (dossier.has_evidence) so retries of a partially-processed
item never double-count."""
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
from smartboi.dossier import (
    Dossier,
    DossierStore,
    DossierUpdater,
    EvidenceRecord,
    has_evidence,
    merge_evidence,
    recompute_decay,
)
from smartboi.edgar import EdgarClient, FilingEvent
from smartboi.graph import RelationshipExtractor, RelationshipGraph, Relationship
from smartboi.news import FinnhubClient
from smartboi.paper_journal import PaperTradeJournal
from smartboi.prices import ReadOnlyPriceFeed
from smartboi.ratelimit import SlidingWindowLimiter
from smartboi.signals import evaluate, favorable_drift_pct, log_signal, signal_expired
from smartboi.skeptic import Skeptic
from smartboi.state import JsonState
from smartboi.universe import SEED_RELATIONSHIPS, CompanySpec, spec_by_symbol
from smartboi.universe_screen import screen_universe
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
        self.backfill_state = JsonState(DATA_DIR / "relationship_backfill.json")
        self.candidates = JsonState(DATA_DIR / "universe_candidates.json")
        self.accepted_candidates = JsonState(DATA_DIR / "accepted_candidates.json")
        self.alerts = AlertSender(settings.alert_webhook_url)
        self.usage = UsageTracker(DATA_DIR / "llm_usage.json", settings.max_daily_llm_calls)

        self.universe: list[CompanySpec] = list(settings.universe)
        self._apply_accepted_candidates()
        self.spec_by_symbol = spec_by_symbol(self.universe)

        self._propagation_limiter = SlidingWindowLimiter(
            settings.max_propagated_evidence_per_link,
            settings.propagated_evidence_cooldown_hours * 3600,
        )

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
        self._dashboard_task: asyncio.Task | None = None
        self._closing = False

    @property
    def symbol_list(self) -> list[str]:
        """The LIVE symbol list -- unlike settings.symbol_list (fixed at
        startup from SYMBOLS/ANCHOR_SYMBOLS), this reflects self.universe,
        which grows at runtime as candidates are accepted (see
        accept_candidate) without requiring a restart."""
        return [c.symbol for c in self.universe]

    def _apply_accepted_candidates(self) -> None:
        known = {c.symbol for c in self.universe}
        for symbol, as_type in self.accepted_candidates.data.items():
            if symbol in known:
                continue
            self.universe.append(
                CompanySpec(symbol, symbol, "accepted", signal_source_only=(as_type == "anchor"),
                            notes="Accepted from a discovered universe candidate")
            )
            known.add(symbol)

    def accept_candidate(self, symbol: str, as_type: str) -> CompanySpec:
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
        spec = CompanySpec(symbol, symbol, "accepted", signal_source_only=(as_type == "anchor"),
                            notes="Accepted from a discovered universe candidate")
        self.universe.append(spec)
        self.spec_by_symbol[symbol] = spec
        self.accepted_candidates.set(symbol, as_type)
        log.info("[CANDIDATE] %s accepted into the universe as %s -- polled starting next cycle.", symbol, as_type)
        return spec

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(message)

    def _seed_graph(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for from_sym, to_sym, rel_type, description, confidence in SEED_RELATIONSHIPS:
            self.graph.add(
                Relationship(from_sym, to_sym, rel_type, description, "manual seed", confidence, now)
            )

    async def start(self) -> None:
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
                "ANTHROPIC_API_KEY not set -- evidence will be collected but not scored yet; "
                "unscored items are retried automatically once a key is configured.",
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
            self._run_decay_pass()

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
                await self._process_filing(symbol, filing)

    async def _process_filing(self, symbol: str, filing: FilingEvent) -> None:
        fp = f"filing:{symbol}:{filing.accession_number}"
        if self.dedup.is_duplicate(fp):
            return

        text = await self.edgar_client.fetch_evidence_text(filing)
        if not text:
            return  # fetch failed/empty -- unregistered, so the next poll retries it

        if filing.form in RELATIONSHIP_EXTRACTION_FORMS and self.extractor is not None:
            await self._extract_relationships(symbol, filing, text)

        evidence_text = f"SEC {filing.form} filed {filing.filing_date} for {symbol}:\n{text[:4000]}"
        scored = await self._process_evidence(
            origin_symbol=symbol,
            evidence_text=evidence_text,
            source_type=filing.form,
            source_name="SEC EDGAR",
            url=filing.document_url,
            headline=f"{symbol} {filing.form} filed {filing.filing_date}",
            published_at=filing.filing_date,
        )
        if scored:
            self.dedup.register(fp, "sec.gov")

    async def _extract_relationships(self, symbol: str, filing: FilingEvent, text: str) -> None:
        """LLM relationship extraction from one filing's text into the
        graph -- shared by regular 10-K/10-Q polling and the one-time
        backfill. graph.add dedupes on (from, to, rel_type), so
        re-extraction of a filing can only ever add edges, never duplicate
        them. Returns without acting if the daily LLM call budget is
        exhausted (extract() returns None) -- retried whenever this filing
        is next polled, same as any other transient-failure path."""
        known = self.symbol_list
        relationships = await self.extractor.extract(symbol, filing.form, text, known)
        if relationships is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        for rel in relationships:
            ticker = (rel.get("counterparty_ticker") or "").upper()
            if not ticker and rel.get("rel_type") != "regulator":
                # A regulator (government body, agency) can never be a
                # ticker -- skip resolution and don't clutter candidates
                # with something that will never be actionable.
                resolved = await self.edgar_client.find_ticker_by_name(rel.get("counterparty_name") or "")
                if resolved:
                    ticker = resolved
            if not ticker or ticker not in known:
                if rel.get("rel_type") != "regulator":
                    await self._record_universe_candidate(symbol, rel, filing, resolved_ticker=ticker)
                continue
            if ticker == symbol:
                continue
            added = self.graph.add(
                Relationship(
                    from_symbol=symbol,
                    to_symbol=ticker,
                    rel_type=rel["rel_type"],
                    description=rel["description"],
                    source=filing.document_url,
                    confidence=float(rel["confidence"]),
                    extracted_at=now,
                )
            )
            if added:
                log.info(
                    "[GRAPH] %s -> %s (%s, confidence=%.2f): %s",
                    symbol, ticker, rel["rel_type"], rel["confidence"], rel["description"],
                )

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
        model didn't supply one but EdgarClient.find_ticker_by_name found a
        match -- candidates are keyed by ticker when one exists (so the
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
        if not ticker and self._looks_like_non_company(name):
            return
        key = ticker or name.upper()
        now = datetime.now(timezone.utc).isoformat()
        entry = self.candidates.get(key)
        is_new = entry is None
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
                await self._extract_relationships(symbol, filing, text)
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
                published = article.published_at or from_date
                fp = fingerprint(symbol, article.headline, published)
                if self.dedup.is_duplicate(fp):
                    continue
                domain = source_domain(article.url) or article.source
                evidence_text = f"News ({article.source}, {published}): {article.headline}\n{article.summary}"
                scored = await self._process_evidence(
                    origin_symbol=symbol,
                    evidence_text=evidence_text,
                    source_type="news",
                    source_name=domain,
                    url=article.url,
                    headline=article.headline,
                    published_at=published,
                )
                if scored:
                    self.dedup.register(fp, domain)

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
        targets: list[tuple[str, str]] = []  # (target_symbol, relationship_note)

        origin_spec = self.spec_by_symbol.get(origin_symbol)
        if origin_spec is None or not origin_spec.signal_source_only:
            targets.append((origin_symbol, ""))

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
            if not self._propagation_limiter.allow(f"{origin_symbol}->{linked_symbol}", now):
                throttled += 1
                continue
            targets.append((linked_symbol, rel.description))
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
        for target_symbol, relationship_note in targets:
            definitive = await self._update_dossier(
                target_symbol, evidence_text, origin_symbol, relationship_note,
                source_type, source_name, url, headline, published_at,
            )
            all_definitive = all_definitive and definitive
        return all_definitive

    async def _update_dossier(
        self,
        target_symbol: str,
        evidence_text: str,
        origin_symbol: str,
        relationship_note: str,
        source_type: str,
        source_name: str,
        url: str,
        headline: str,
        published_at: str,
    ) -> bool:
        """Returns True when this dossier handled the evidence definitively
        (merged, not-new, or refuted); False on a transient failure or an
        exhausted daily LLM call budget, either of which warrants a retry
        on a later poll."""
        evidence_id = f"{source_type}:{url or headline}:{published_at}"
        dossier = self.dossiers.load(target_symbol)
        if has_evidence(dossier, evidence_id):
            return True  # already merged on an earlier, partially-failed pass

        proposed = await self.updater.propose_update(dossier, evidence_text, origin_symbol, relationship_note)
        if proposed is None:
            return False  # transient LLM failure or budget exhausted -- retry this evidence later
        if not proposed.get("is_new_information"):
            return True

        verdict = await self.skeptic.review(evidence_text, proposed, relationship_note)
        if verdict is None:
            return False  # transient LLM failure or budget exhausted -- retry this evidence later
        if verdict.get("refuted"):
            log.info("%s: evidence refuted by skeptic (%s)", target_symbol, verdict.get("reasoning", ""))
            return True

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
            direction=proposed["direction"],
            magnitude=float(proposed["magnitude"]),
            confidence=float(verdict.get("adjusted_confidence", proposed["confidence"])),
            horizon_days=int(proposed["horizon_days"]),
            reasoning=proposed["reasoning"],
            skeptic_note=verdict.get("reasoning", ""),
        )
        merge_evidence(dossier, record)
        self.dossiers.save(dossier)

        signal = evaluate(dossier, self.settings.signal_confidence_threshold, self.settings.min_independent_sources)
        if signal is not None:
            log_signal(Path(self.settings.log_dir) / "signals.jsonl", signal)
            await self.alerts.send(
                "signal",
                f"{signal.direction} signal: {signal.symbol}",
                f"confidence={signal.confidence:.2f} magnitude={signal.magnitude:.2f} "
                f"sources={signal.independent_source_count}. {signal.thesis_summary}",
                asdict(signal),
            )
            if dossier.status == "ACTIVE" or dossier.direction != dossier.signaled_direction:
                # A fresh signal (or a thesis that flipped direction while
                # still SIGNALED-but-unopened) gets a fresh price baseline --
                # see _snapshot_signal_price and _try_open_from_signal below.
                dossier.status = "SIGNALED"
                await self._snapshot_signal_price(dossier)
                self.dossiers.save(dossier)
        return True

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
        if self.price_feed is None:
            return
        try:
            if await self.price_feed.ensure_connected():
                dossier.signaled_price = await self.price_feed.last_price(dossier.symbol)
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
           increasingly stale opportunity."""
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
        )
        await self.alerts.send(
            "paper_trade_opened",
            f"Paper trade opened: {trade.direction} {trade.symbol}",
            f"entry={trade.entry_price:.2f} stop={trade.stop_price:.2f} "
            f"target={trade.target_price:.2f} horizon={trade.horizon_days}d. {trade.thesis_summary}",
            asdict(trade),
        )

    async def _mark_and_execute(self) -> None:
        for symbol in self.dossiers.all_symbols():
            dossier = self.dossiers.load(symbol)
            if dossier.status != "SIGNALED" or self.journal.has_open(symbol):
                continue
            await self._try_open_from_signal(symbol, dossier)

        open_symbols = list(self.journal.open_trades.keys())
        if not open_symbols:
            return
        prices = await self.price_feed.last_prices(open_symbols)
        for symbol, price in prices.items():
            trade = self.journal.open_trades.get(symbol)
            if trade is None:
                continue
            self.journal.update(symbol, price)
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
