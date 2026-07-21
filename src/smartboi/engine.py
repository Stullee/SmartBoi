"""Orchestrates the whole pipeline: EDGAR + news ingestion -> relationship
graph -> dossier updates -> adversarial skeptic pass -> signal evaluation
-> (optional) hypothetical paper trade. See README for the strategy this
implements point-by-point.

Every optional integration degrades gracefully instead of failing to
start -- see config.py's docstring. A completely unconfigured run still
does something useful: nothing (EDGAR/news both need at least a User-Agent
or an API key to fetch anything). Add EDGAR_USER_AGENT and/or
FINNHUB_API_KEY to start collecting evidence; add ANTHROPIC_API_KEY to
start scoring it into dossiers; add IB (ENABLE_IB_PRICE_FEED=true) to start
actually opening/marking hypothetical positions. Signals are detected and
logged (signals.jsonl) the moment ANTHROPIC_API_KEY is present, regardless
of whether IB is configured yet."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smartboi.config import Settings
from smartboi.dedup import DedupIndex, fingerprint, source_domain
from smartboi.dossier import (
    Dossier,
    DossierStore,
    DossierUpdater,
    EvidenceRecord,
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
from smartboi.universe import DEFAULT_UNIVERSE, SEED_RELATIONSHIPS, spec_by_symbol
from smartboi.universe_screen import screen_universe
from smartboi.webapp import run_dashboard

log = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 30
DATA_DIR = Path("data")


def _count_independent_sources(evidence_and_new: list[EvidenceRecord], direction: str) -> int:
    agreeing = [e for e in evidence_and_new if e.direction == direction]
    return len({e.source_name for e in agreeing})


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        log_dir = Path(settings.log_dir)

        self.dedup = DedupIndex(DATA_DIR / "dedup_index.json")
        self.graph = RelationshipGraph(DATA_DIR / "graph.json")
        self.dossiers = DossierStore(DATA_DIR / "dossiers")
        self.journal = PaperTradeJournal(log_dir / "paper_trades.jsonl")
        self.universe_screen_state = JsonState(DATA_DIR / "universe_screen_state.json")

        self.spec_by_symbol = spec_by_symbol(DEFAULT_UNIVERSE)

        self.edgar_client: EdgarClient | None = None
        self.finnhub: FinnhubClient | None = None
        self.extractor: RelationshipExtractor | None = None
        self.updater: DossierUpdater | None = None
        self.skeptic: Skeptic | None = None
        self.price_feed: ReadOnlyPriceFeed | None = None

        self._warned: set[str] = set()
        self._last_edgar_poll = 0.0
        self._last_news_poll = 0.0
        self._last_price_poll = 0.0
        self._last_screen = 0.0
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
                "ANTHROPIC_API_KEY not set -- evidence will be collected but dossiers will not be "
                "scored (no relationship extraction, dossier updates, or signals).",
            )

        if self.settings.enable_ib_price_feed:
            self.price_feed = ReadOnlyPriceFeed(
                self.settings.ib_host, self.settings.ib_port, self.settings.ib_client_id
            )
            try:
                await self.price_feed.connect()
                log.info("Read-only IB price feed: ENABLED")
            except Exception:  # noqa: BLE001 - a missing/unreachable IB Gateway must not stop the rest of the pipeline
                log.exception("Could not connect the IB price feed -- will retry on the next price poll.")
                self.price_feed = None
        else:
            self._warn_once(
                "ib",
                "IB price feed disabled -- signals will be detected and logged (logs/signals.jsonl), "
                "but no hypothetical position can be opened or marked to market until "
                "ENABLE_IB_PRICE_FEED=true. This never places real orders (see prices.py).",
            )

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

    async def _tick(self) -> None:
        now = time.monotonic()
        if self.edgar_client is not None and now - self._last_edgar_poll >= self.settings.edgar_poll_interval_sec:
            self._last_edgar_poll = now
            await self._poll_edgar()
        if self.finnhub is not None and now - self._last_news_poll >= self.settings.news_poll_interval_sec:
            self._last_news_poll = now
            await self._poll_news()
        if self.price_feed is not None and now - self._last_price_poll >= self.settings.price_poll_interval_sec:
            self._last_price_poll = now
            await self._mark_and_execute()
        if (
            self.finnhub is not None
            and self.settings.enable_universe_autoscreen
            and now - self._last_screen >= self.settings.universe_screen_interval_days * 86400
        ):
            self._last_screen = now
            await self._run_universe_screen()

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
        self.dedup.register(fp, "sec.gov")

        text = await self.edgar_client.fetch_text(filing)
        if not text:
            return

        if filing.form == "10-K" and self.extractor is not None:
            known = self.settings.symbol_list
            relationships = await self.extractor.extract(symbol, filing.form, text, known)
            now = datetime.now(timezone.utc).isoformat()
            for rel in relationships:
                ticker = (rel.get("counterparty_ticker") or "").upper()
                if not ticker or ticker not in known or ticker == symbol:
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

        evidence_text = f"SEC {filing.form} filed {filing.filing_date} for {symbol}:\n{text[:4000]}"
        await self._process_evidence(
            origin_symbol=symbol,
            evidence_text=evidence_text,
            source_type=filing.form,
            source_name="SEC EDGAR",
            url=filing.document_url,
            headline=f"{symbol} {filing.form} filed {filing.filing_date}",
            published_at=filing.filing_date,
        )

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
                self.dedup.register(fp, domain)
                evidence_text = f"News ({article.source}, {published}): {article.headline}\n{article.summary}"
                await self._process_evidence(
                    origin_symbol=symbol,
                    evidence_text=evidence_text,
                    source_type="news",
                    source_name=domain,
                    url=article.url,
                    headline=article.headline,
                    published_at=published,
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
    ) -> None:
        if self.updater is None or self.skeptic is None:
            return  # evidence's dedup fingerprint is already registered; just not scored yet

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

        for target_symbol, relationship_note in targets:
            await self._update_dossier(
                target_symbol, evidence_text, origin_symbol, relationship_note,
                source_type, source_name, url, headline, published_at,
            )

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
    ) -> None:
        dossier = self.dossiers.load(target_symbol)
        proposed = await self.updater.propose_update(dossier, evidence_text, origin_symbol, relationship_note)
        if proposed is None or not proposed.get("is_new_information"):
            return

        verdict = await self.skeptic.review(evidence_text, proposed)
        if verdict.get("refuted"):
            log.info("%s: evidence refuted by skeptic (%s)", target_symbol, verdict.get("reasoning", ""))
            return

        record = EvidenceRecord(
            evidence_id=f"{source_type}:{url or headline}:{published_at}",
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
        independent_sources = _count_independent_sources(dossier.evidence + [record], record.direction)
        merge_evidence(dossier, record, independent_sources)
        self.dossiers.save(dossier)

        signal = evaluate(dossier, self.settings.signal_confidence_threshold, self.settings.min_independent_sources)
        if signal is not None:
            log_signal(Path(self.settings.log_dir) / "signals.jsonl", signal)
            if dossier.status == "ACTIVE":
                dossier.status = "SIGNALED"
                self.dossiers.save(dossier)

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
            self.journal.open(
                symbol, dossier.direction, price,
                self.settings.stop_loss_pct, self.settings.take_profit_pct,
                horizon, dossier.thesis_summary, dossier.confidence,
                dossier.independent_source_count, citations,
            )

        open_symbols = list(self.journal.open_trades.keys())
        if not open_symbols:
            return
        prices = await self.price_feed.last_prices(open_symbols)
        for symbol, price in prices.items():
            was_open = self.journal.has_open(symbol)
            self.journal.update(symbol, price)
            if was_open and not self.journal.has_open(symbol):
                # The paper trade just closed (WIN/LOSS/TIMEOUT) -- reset the
                # dossier so future evidence can trigger a fresh signal
                # instead of being permanently stuck at SIGNALED.
                dossier = self.dossiers.load(symbol)
                dossier.status = "ACTIVE"
                self.dossiers.save(dossier)

    # --- Universe auto-screen ---

    async def _run_universe_screen(self) -> None:
        results = await screen_universe(
            DEFAULT_UNIVERSE, self.finnhub,
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
