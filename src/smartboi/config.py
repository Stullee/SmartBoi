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
    edgar_forms: str = "8-K,10-K,10-Q,4"
    edgar_poll_interval_sec: int = 3600
    # Rolling lookback window checked on every poll (not just first-run
    # backfill) -- dedup.py's fingerprint index prevents reprocessing a
    # filing already seen, so widening this is cheap and just a safety
    # margin against a missed poll cycle.
    edgar_lookback_days: int = 14

    # --- News ingestion (Finnhub company-news, free tier) ---
    enable_news_ingestion: bool = True
    finnhub_api_key: str = ""
    news_poll_interval_sec: int = 900
    news_lookback_days: int = 3

    # --- Claude: relationship extraction, dossier updates, skeptic pass ---
    anthropic_api_key: str = ""
    extraction_model: str = "claude-haiku-4-5-20251001"
    dossier_model: str = "claude-haiku-4-5-20251001"
    skeptic_model: str = "claude-haiku-4-5-20251001"

    # --- Evidence / signal thresholds ---
    # A dossier signals once confidence * magnitude clears this bar AND has
    # at least min_independent_sources distinct-domain corroborating items
    # (dedup.py already collapses syndicated republishes to one source).
    signal_confidence_threshold: float = 0.65
    min_independent_sources: int = 2
    max_horizon_days: int = 56  # ~8 weeks, the top of README's 2-8 week holding window

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

    # --- Universe auto-screen: prunes tickers that no longer fit (acquired,
    # delisted, graduated to broad analyst coverage) rather than trusting a
    # hardcoded list to stay accurate forever. Requires finnhub_api_key.
    # Prune-only by design -- see universe_screen.py's docstring for why it
    # never auto-adds new tickers. ---
    enable_universe_autoscreen: bool = True
    universe_screen_interval_days: int = 30
    universe_min_market_cap_musd: float = 100.0
    universe_max_market_cap_musd: float = 5000.0
    universe_max_analyst_count: int = 6

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
