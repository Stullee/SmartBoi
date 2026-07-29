"""Typed application configuration loaded from environment / .env file.

Deliberately permissive compared to TradingBot's config.py: that bot hard-
fails at startup on a misconfigured safety guard (ALLOW_LIVE_TRADING)
because getting it wrong risks real money. Nothing here ever places a real
order -- see prices.py and paper_journal.py -- so a missing API key is not
a safety issue, just a disabled feature. Each optional integration
(EDGAR, news, the Claude-powered dossier engine, the IB price feed) is
meant to be addable incrementally: the system should start and run
usefully with zero keys configured (ingestion collects what it can,
dossier updates are skipped with a clear one-time warning) rather than
refuse to start. See engine.py's startup log for what's active."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from smartboi.universe import DEFAULT_UNIVERSE, CompanySpec, build_universe


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Universe: comma-separated tickers. SYMBOLS are the tradeable small/
    # mid-caps; ANCHOR_SYMBOLS are the big, heavily-covered giants whose
    # news should propagate to them (never trade targets themselves).
    # Setting EITHER replaces the built-in starter watchlist entirely with
    # your two lists; leaving both empty uses the starter watchlist (see
    # universe.py). Relationships between your symbols are discovered
    # automatically from the tradeable companies' 10-K filings -- including
    # a one-time backfill of each company's most recent 10-K on first run
    # (see enable_relationship_backfill below).
    symbols: str = ""
    anchor_symbols: str = ""

    # --- SEC EDGAR ingestion (8-K material events, 10-K/10-Q customer/
    # supplier disclosures, Form 4 insider transactions) ---
    enable_edgar_ingestion: bool = True
    # One-time relationship backfill: on first run (and once per newly
    # added symbol), extract relationships from each tradeable company's
    # MOST RECENT 10-K regardless of age. Regular polling only sees filings
    # from the last edgar_lookback_days and 10-Ks are annual, so without
    # this the graph would take up to a year to populate. Requires EDGAR
    # and ANTHROPIC_API_KEY; each symbol is only ever backfilled once
    # (tracked in data/relationship_backfill.json).
    enable_relationship_backfill: bool = True
    # SEC requires a descriptive User-Agent with real contact info on every
    # request ("Your Name your@email.com") or it will block/rate-limit --
    # not a secret, just informational, but required for EDGAR ingestion to
    # actually work. Left empty, EDGAR ingestion logs a warning and skips.
    edgar_user_agent: str = ""
    # 10-Q added alongside 10-K: quarterly filings also disclose customer/
    # supplier changes and are run through the same relationship
    # extraction (see engine.py), keeping the graph fresher between annual
    # 10-Ks instead of only updating once a year.
    # Widened beyond the original 8-K/10-K/10-Q/4. The forms filter is
    # applied CLIENT-SIDE to a submissions payload that is fetched whole
    # regardless, so adding a form type costs zero extra HTTP requests --
    # only the per-filing document fetch and scoring for filings that
    # actually appear. What each addition buys, for a universe of thinly-
    # covered small caps:
    #   SC 13D / SC 13D/A -- an activist or strategic investor crossing 5%,
    #     filed within 10 days. One of the largest single-day moves a
    #     micro-cap experiences, and it appears in the ISSUER's filing
    #     history, so it needs no separate feed.
    #   424B5 / 424B3 -- a shelf takedown actually pricing: dilution, and
    #     the cleanest SHORT catalyst this universe offers. A system that
    #     only ever reads good news is not a research system.
    #   8-K/A -- amended material events, which is where a restated
    #     contract value or corrected figure lands.
    edgar_forms: str = "8-K,8-K/A,10-K,10-Q,4,SC 13D,SC 13D/A,424B5,424B3"
    # 15 minutes, not an hour. This is now the system's PRIMARY live
    # catalyst feed, not a slow background sweep of periodic reports: an
    # 8-K carries the company's own press release as an exhibit (see
    # edgar.fetch_evidence_text) and is filed within four business days of
    # a material event, usually the same day. An hourly poll threw away up
    # to an hour of a drift window the strategy's own configuration says is
    # concentrated in the first week.
    #
    # The cost is one cached submissions request per symbol per pass, at
    # EDGAR's 0.3s spacing -- about a minute of wall clock for a 209-symbol
    # universe, against SEC's published 10-requests-per-second allowance.
    # No LLM cost changes: dedup means an already-seen filing is dropped
    # before any scoring call.
    edgar_poll_interval_sec: int = 900
    # Rolling lookback window checked on every poll (not just first-run
    # backfill) -- dedup.py's fingerprint index prevents reprocessing a
    # filing already seen, so widening this is cheap and just a safety
    # margin against a missed poll cycle.
    edgar_lookback_days: int = 14

    # --- News ingestion (Finnhub company-news, free tier) ---
    enable_news_ingestion: bool = True
    finnhub_api_key: str = ""
    # 15 minutes is unnecessarily tight for a strategy that holds positions
    # for weeks (max_horizon_days) -- it just multiplies LLM call volume
    # (and cost) for freshness this system has no use for. 1h is plenty.
    news_poll_interval_sec: int = 3600
    news_lookback_days: int = 3

    # --- Claude: relationship extraction, dossier updates, skeptic pass ---
    anthropic_api_key: str = ""
    #
    # Models are tiered by how hard the judgement is, not uniformly. Every call site ran
    # on the cheapest model the API offers while the daily budget sat at ~6%
    # used -- the two calls that actually decide whether real money would be
    # committed were the two being economised on.
    #
    # dossier/skeptic: these produce the confidence and magnitude that gate a
    #   trade at a hard threshold. Reasoning quality here is the product.
    # extraction: the largest input-token consumer (a 150k-char filing per
    #   call) but a reading task rather than a judgement one, and its output
    #   is already backstopped by the name-verification and biography/lender
    #   filters in engine.py. A mid-tier model is the right trade.
    #
    # Cost is bounded and visible: see max_daily_usd and the dashboard's
    # usage panel. Changing any of these is safe -- llm.py builds the
    # model-appropriate request shape, so a model that rejects `temperature`
    # or requires `budget_tokens` is handled rather than 400-ing into a
    # silent retry loop. Note that changing them also resets the forward-
    # testing clock (see _check_model_provenance and EvidenceRecord's
    # scored_by_model), which is deliberate and will be warned about.
    extraction_model: str = "claude-sonnet-5"
    dossier_model: str = "claude-opus-5"
    skeptic_model: str = "claude-opus-5"

    # --- Cost controls. Every LLM call (extraction/dossier-update/skeptic)
    # checks these before spending anything -- see usage.py and
    # ratelimit.py, both wired in via engine.py. ---
    # Hard daily ceiling on Claude API calls across all three call sites,
    # combined. Once reached, further evidence is deferred (not discarded --
    # it's picked up again once the budget resets at UTC midnight, exactly
    # like a transient API failure). Call-count-based rather than a dollar
    # figure: per-call token sizes here are small and bounded by this
    # codebase's own prompt construction, so this is a robust proxy for
    # spend that won't rot when Anthropic's prices change. See the
    # dashboard for actual calls/tokens used today.
    max_daily_llm_calls: int = 3000
    # Hard daily ceiling on estimated API SPEND, in USD, checked alongside
    # the call cap before every LLM call. 0 disables it (call cap only).
    #
    # The call cap alone is no longer a usable spend proxy, and its failure
    # mode is expensive: per-call cost now spans more than an order of
    # magnitude across the configured models, and adaptive thinking makes
    # output tokens unbounded by max_tokens in practice. A 3000-call day
    # costs a few dollars on Haiku and several hundred on Opus at full
    # thinking. This is the number that actually bounds the bill; the call
    # cap is kept because it bounds request VOLUME, which dollars do not.
    # Priced from llm.MODEL_PRICES_PER_MTOK; an unrecognised model is priced
    # at the most expensive entry rather than free.
    max_daily_usd: float = 25.0
    # Caps how many pieces of PROPAGATED evidence (about a linked company,
    # never the dossier's own direct evidence) get forwarded to one target
    # from one origin within a rolling window -- without this, a heavily-
    # covered anchor linked to a target burns a dossier-update + skeptic
    # call for every single article about it, even once the causal link has
    # already been refused several times running for the same reason.
    max_propagated_evidence_per_link: int = 3
    propagated_evidence_cooldown_hours: int = 6

    # --- Ecosystem-fallback propagation ---
    # An anchor with no DISCLOSED edge to any tradeable is inert: it is never
    # its own analysis target, so its news resolves to zero targets and is
    # discarded unread. Measured live, that was 104 of 130 anchors --
    # including NVDA, AMAT, LRCX, TSM, MSFT, AMZN, UPS and CSX, the loudest
    # and most information-dense feeds in the universe.
    #
    # The graph cannot fix this quickly: edges come from 10-K/10-Q text, so
    # it grows at filing season, not at news speed. This falls back to the
    # ECOSYSTEM link -- fan an inert anchor's news out to the tradeables in
    # its own ecosystem, flagged as an undisclosed, industry-level
    # association rather than a contractual one. That is also the link the
    # literature actually measured: Menzly & Ozbas (2010, JF 65:1555) is
    # industry-level cross-predictability, not firm-level.
    enable_ecosystem_propagation: bool = True
    # Tighter than max_propagated_evidence_per_link on purpose: an ecosystem
    # association is weaker evidence than a disclosed contract, and it fans
    # out to every tradeable in the ecosystem rather than to named
    # counterparties, so it deserves a smaller share of the daily budget.
    max_ecosystem_evidence_per_link: int = 1

    # --- Evidence / signal thresholds ---
    # A dossier signals once confidence * magnitude clears this bar AND has
    # at least min_independent_sources independent corroborating items --
    # distinct publisher domains for news (dedup.py already collapses
    # syndicated republishes of one wire story to a single source), distinct
    # filing types for EDGAR (an 8-K, a Form 4, and a 10-Q each count
    # separately -- independent disclosures, not restatements of each other).
    signal_confidence_threshold: float = 0.65
    min_independent_sources: int = 2
    # Higher bar for dossiers whose agreeing evidence is ENTIRELY news:
    # dedup collapses exact and lightly-reworded republishes, but heavily
    # reworded wire copy can still land as two "independent" publishers --
    # enough to satisfy min_independent_sources=2 and fire a trade on what
    # is one underlying story. Filings are primary disclosures and immune
    # to that failure mode, so any filing-corroborated dossier keeps the
    # normal bar; news-only ones need this many distinct publishers.
    min_independent_sources_news_only: int = 3
    # 21 trading-ish days, not the 56 this started with. The lead-lag
    # literature is specific about where the tradeable component lives: a
    # daily-resolution event study of supplier returns after large customer
    # price moves finds predictability concentrated in the FIRST WEEK, and
    # the one paper that decomposes the effect (Burt & Hrdlicka, JFQA 2021)
    # finds that at horizons past roughly a month the predictability is
    # *entirely* momentum commonality between linked firms -- shared factor
    # exposure and correlated returns -- with no information-diffusion
    # component left. Holding for eight weeks was therefore spending six of
    # them earning momentum beta while calling it evidence synthesis.
    max_horizon_days: int = 21

    # --- Transaction costs ---
    # Applied to every hypothetical trade's realized P&L. Not optional
    # realism: the closest published analogue to this strategy went from
    # ~700% gross to ~50% at 25bp round-trip, and a survey of 204 anomalies
    # found post-publication decay of ~50% before costs and ~93% AFTER
    # them, with the average anomaly netting 4bp/month. This strategy's
    # edge is concentrated in exactly the small, thinly-covered names where
    # spreads are widest, so a paper record that ignores costs is not
    # evidence of anything. Charged per side, so the round-trip is double.
    transaction_cost_bps_per_side: float = 25.0

    # --- Entry timing: "have we missed the correction already" guards.
    # Applied when a SIGNALED dossier is about to become a paper trade
    # (engine.py's _try_open_from_signal) -- neither has any effect without
    # enable_ib_price_feed, since there's no price to check against yet. ---
    # Skip opening if the price has already moved this many percent in the
    # signal's favorable direction since it fired -- the correction likely
    # already happened between signal and entry (e.g. price_poll_interval_sec
    # gaps, or IB being briefly unreachable) and entering now would be
    # chasing a move that's largely over, not capturing it.
    max_favorable_drift_pct: float = 5.0
    # If a signal sits unopened this many days (drift-blocked every poll, or
    # no reachable price feed) it's expired back to ACTIVE instead of being
    # stuck forever waiting to chase an increasingly stale opportunity --
    # fresh evidence can re-signal it later with a clean baseline.
    signal_entry_deadline_days: int = 5

    # --- Paper trade journal (percentage-based stop/target -- this system
    # has no intraday bar/ATR data at a weeks-long holding horizon) ---
    stop_loss_pct: float = 8.0
    take_profit_pct: float = 16.0

    # --- Read-only IB price feed. NEVER places orders -- see prices.py,
    # which contains no order-placement code at all. Off by default; until
    # enabled, dossiers/signals still accumulate and log fully, they just
    # can't be marked to market or opened as a hypothetical position yet. ---
    enable_ib_price_feed: bool = False
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497
    ib_client_id: int = 27
    price_poll_interval_sec: int = 21600  # 6h -- a swing/position system has no need for tighter polling
    # ...but MARKING open trades and CONFIRMING an entry are different jobs.
    # A SIGNALED dossier is waiting on the entry gate, and at a 6-hour
    # cadence three of every four checks land outside market hours -- the
    # first signal this system ever fired (DCO, 2026-07-28) was reset to
    # ACTIVE without a single entry evaluation ever running against it.
    # While any entry is pending, prices are polled at this tighter interval
    # instead (see Engine._price_poll_interval).
    signal_entry_poll_interval_sec: int = 900

    # --- Universe auto-screen: prunes tickers that no longer fit (acquired,
    # delisted, graduated to broad analyst coverage) rather than trusting a
    # hardcoded list to stay accurate forever. Requires finnhub_api_key.
    # Prune-only by design -- see universe_screen.py's docstring for why it
    # never auto-adds new tickers. ---
    enable_universe_autoscreen: bool = True
    universe_screen_interval_days: int = 30
    # Bounds moved from 100/6 to 75/10 in the 2026-07 refresh: a live screen
    # of 15 candidates failed 14 of them, clustered just above the old
    # analyst bound (7,8,8,9,9,9,9,9,10), while everything that DID clear <=6
    # sat below the $100M floor. The old numbers described a window that is
    # close to empty in US small caps -- see universe.py's docstring for the
    # full reasoning, and note this is a "what to watch" bound, deliberately
    # unlike the signal threshold ("when to trade"), which was not loosened.
    universe_min_market_cap_musd: float = 75.0
    universe_max_market_cap_musd: float = 5000.0
    universe_max_analyst_count: int = 10

    # --- Auto-accepting discovered universe candidates ---
    # A candidate is a company a TRADEABLE company's own SEC filing disclosed
    # a relationship with (see engine.py's _record_universe_candidate), and
    # the engine already resolves its ticker, fetches market cap/analyst
    # count, and computes a tradeable-vs-anchor recommendation from the
    # bounds above. Accepting it by hand applied exactly that recommendation,
    # so the click added a veto, not a judgement -- these settings let the
    # engine act on its own recommendation instead. Every auto-accept is
    # logged, webhook-alerted, and recorded in accepted_candidates.json with
    # an "auto" marker, so it stays auditable and is undone by deleting the
    # entry. Nothing here can place an order; this only widens what is
    # watched (see webapp.py's accept endpoint for the same reasoning).
    enable_auto_accept_candidates: bool = True
    # Anchors and tradeables are deliberately NOT held to the same bar. An
    # anchor can never become a trade (signal_source_only), so the worst case
    # is some wasted LLM spend, and the upside is large: it turns a dead-end
    # candidate into a live propagation source, which is the mechanism the
    # whole strategy runs on. A tradeable can produce signals and paper
    # trades, so it additionally requires a verified name match and repeat
    # disclosure (see engine.py's _auto_accept_candidates).
    auto_accept_anchors: bool = True
    auto_accept_tradeables: bool = True
    # How many times a candidate must have been disclosed across filings
    # before it can be auto-accepted as TRADEABLE -- one throwaway mention in
    # a single filing isn't enough to start taking positions on a name.
    auto_accept_min_seen_count: int = 2
    # Ceiling on auto-accepts per day, so one filing that names a long list
    # of counterparties can't flood the universe in a single pass. Raised
    # from 5 to 20 in the 2026-07 refresh: accepting a candidate now also
    # writes the relationship that DISCOVERED it into the graph (see
    # Engine._promote_pending_edges), so an acceptance went from "one more
    # symbol to poll" to "one more propagation path" -- and a 73-candidate
    # backlog draining at 5/day is two weeks of a mostly disconnected graph.
    # An accepted ANCHOR can never become a trade target, so the risk this
    # bounds is polling cost, and the daily LLM budget runs under 2% used.
    auto_accept_max_per_day: int = 20

    log_level: str = "INFO"
    log_dir: str = "logs"
    # Optional: a JSON payload is POSTed here on every signal and paper
    # trade open/close (see alerts.py) -- point it at a Home Assistant
    # webhook trigger or any HTTP endpoint. Empty disables alerts.
    alert_webhook_url: str = ""

    enable_dashboard: bool = True
    dashboard_port: int = 8100

    @property
    def universe(self) -> list[CompanySpec]:
        custom_tradeable = [s for s in self.symbols.split(",") if s.strip()]
        custom_anchors = [s for s in self.anchor_symbols.split(",") if s.strip()]
        if custom_tradeable or custom_anchors:
            return build_universe(custom_tradeable, custom_anchors)
        return DEFAULT_UNIVERSE

    @property
    def symbol_list(self) -> list[str]:
        return [c.symbol for c in self.universe]

    @property
    def edgar_forms_set(self) -> set[str]:
        return {f.strip() for f in self.edgar_forms.split(",") if f.strip()}


def load_settings() -> Settings:
    return Settings()
