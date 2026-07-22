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
configured) or hits a transient LLM/API failure stays unregistered and is
retried on later polls, so a missing key or a network blip never
permanently burns evidence; per-dossier merging is idempotent
(dossier.has_evidence) so retries of a partially-processed item never
double-count."""
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
    DossierStore,
    DossierUpdater,
    EvidenceRecord,
    has_evidence,
    merge_evidence,
)
from smartboi.edgar import EdgarClient, FilingEvent
from smartboi.graph import RelationshipExtractor, RelationshipGraph, Relationship
from smartboi.news import FinnhubClient
from smartboi.paper_journal import PaperTradeJournal
from smartboi.prices import ReadOnlyPriceFeed
from smartboi.signals import evaluate, log_signal
from smartboi.skeptic import Skeptic
from smartboi.state import JsonState
from smartboi.universe import SEED_RELATIONSHIPS, spec_by_symbol
from smartboi.universe_screen import screen_universe
from smartboi.webapp import run_dashboard

log = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 30
DATA_DIR = Path("data")
# When the IB Gateway is unreachable, retry the connection this often
# instead of waiting out a full price_poll_interval_sec (6h by default) --
# the Gateway restarting daily, or simply not being up yet, shouldn't cost
# most of a day of price marks.
IB_RETRY_GAP_SEC = 900


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
        self.alerts = AlertSender(settings.alert_webhook_url)

        self.universe = settings.universe
        self.spec_by_symbol = spec_by_symbol(self.universe)

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
        self._backfill_ran = False  # once per process; per-symbol state persists in backfill_state
        self._dashboard_task: asyncio.Task | None = None
        self._closing = False

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
            self.extractor = RelationshipExtractor(self.settings.anthropic_api_key, self.settings.extraction_model)
            self.updater = DossierUpdater(self.settings.anthropic_api_key, self.settings.dossier_model)
            self.skeptic = Skeptic(self.settings.anthropic_api_key, self.settings.skeptic_model)
            log.info("Dossier engine (Claude): ENABLED")
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
            log.info(
                "Read-only IB price feed: ENABLED -- connects on the first price poll and "
                "reconnects automatically (retries every %d min while the Gateway is unreachable).",
                IB_RETRY_GAP_SEC // 60,
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
        log.info("SmartBoi engine running. Universe: %s", self.settings.symbol_list)
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
            not self._backfill_ran
            and self.settings.enable_relationship_backfill
            and self.edgar_client is not None
            and self.extractor is not None
        ):
            self._backfill_ran = True
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
        for symbol in self.settings.symbol_list:
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

        if filing.form == "10-K" and self.extractor is not None:
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
        graph -- shared by regular 10-K polling and the one-time backfill.
        graph.add dedupes on (from, to, rel_type), so re-extraction of a
        filing can only ever add edges, never duplicate them."""
        known = self.settings.symbol_list
        relationships = await self.extractor.extract(symbol, filing.form, text, known)
        now = datetime.now(timezone.utc).isoformat()
        for rel in relationships:
            ticker = (rel.get("counterparty_ticker") or "").upper()
            if not ticker or ticker not in known:
                # A relationship to a company OUTSIDE the universe: not an
                # edge (nothing to propagate to), but a discovery worth
                # surfacing -- proposed as a watchlist candidate for human
                # review, never auto-added (see _record_universe_candidate).
                await self._record_universe_candidate(symbol, rel, filing)
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

    async def _record_universe_candidate(self, symbol: str, rel: dict, filing: FilingEvent) -> None:
        """Persists a disclosed relationship to a company outside the
        universe as a WATCHLIST CANDIDATE (data/universe_candidates.json,
        also on the dashboard), and alerts the first time each one is
        discovered. Deliberately never auto-added to the universe: whether
        a name belongs is an editorial judgment (same reasoning as the
        prune-only auto-screen) -- accepting a candidate means adding its
        ticker to SYMBOLS or ANCHOR_SYMBOLS yourself."""
        name = (rel.get("counterparty_name") or "").strip()
        if not name:
            return
        ticker = (rel.get("counterparty_ticker") or "").upper()
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
                "Add its ticker to SYMBOLS or ANCHOR_SYMBOLS to accept it into the universe.",
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
        symbol is backfilled once ever (persisted), so adding a new symbol
        later backfills just that one."""
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
                    continue  # fetch failed -- left pending, retried on the next process start
                await self._extract_relationships(symbol, filing, text)
                self.backfill_state.set(symbol, {"backfilled_at": datetime.now(timezone.utc).isoformat(),
                                                 "accession": filing.accession_number,
                                                 "filing_date": filing.filing_date})
                done += 1
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                log.exception("%s: relationship backfill failed -- will retry on the next restart.", symbol)
        log.info("Relationship backfill complete: %d/%d symbol(s) processed, graph now has %d edge(s).",
                 done, len(pending), len(self.graph.relationships))

    # --- News ingestion ---

    async def _poll_news(self) -> None:
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=self.settings.news_lookback_days)).isoformat()
        for symbol in self.settings.symbol_list:
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
        dossier engine configured yet, or a transient LLM failure) -- the
        caller then leaves its dedup fingerprint unregistered."""
        if self.updater is None or self.skeptic is None:
            return False  # collected but not scoreable yet -- retried once a key is configured

        universe = set(self.settings.symbol_list)
        targets: list[tuple[str, str]] = []  # (target_symbol, relationship_note)

        origin_spec = self.spec_by_symbol.get(origin_symbol)
        if origin_spec is None or not origin_spec.signal_source_only:
            targets.append((origin_symbol, ""))

        for linked_symbol, rel in self.graph.linked_symbols(origin_symbol, universe):
            linked_spec = self.spec_by_symbol.get(linked_symbol)
            if linked_spec is not None and linked_spec.signal_source_only:
                continue
            targets.append((linked_symbol, rel.description))

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
        (merged, not-new, or refuted); False on a transient failure that
        warrants a retry on a later poll."""
        evidence_id = f"{source_type}:{url or headline}:{published_at}"
        dossier = self.dossiers.load(target_symbol)
        if has_evidence(dossier, evidence_id):
            return True  # already merged on an earlier, partially-failed pass

        proposed = await self.updater.propose_update(dossier, evidence_text, origin_symbol, relationship_note)
        if proposed is None:
            return False  # transient LLM failure -- retry this evidence later
        if not proposed.get("is_new_information"):
            return True

        verdict = await self.skeptic.review(evidence_text, proposed)
        if verdict is None:
            return False  # transient LLM failure -- retry this evidence later
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
            if dossier.status == "ACTIVE":
                dossier.status = "SIGNALED"
                self.dossiers.save(dossier)
        return True

    # --- Price marking / hypothetical execution ---

    async def _mark_and_execute(self) -> None:
        for symbol in self.dossiers.all_symbols():
            dossier = self.dossiers.load(symbol)
            if dossier.status != "SIGNALED" or self.journal.has_open(symbol):
                continue
            try:
                price = await self.price_feed.last_price(symbol)
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                log.exception("%s: could not fetch entry price.", symbol)
                continue
            if price is None:
                continue
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
                dossier.status = "ACTIVE"
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
