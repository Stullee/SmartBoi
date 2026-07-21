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

from smartboi.universe import DEFAULT_UNIVERSE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Universe: comma-separated tickers. Empty uses the built-in starter
    # watchlist (see universe.py) -- ecosystem/anchor/seed-relationship
    # metadata only applies to symbols that are actually in that list.
    symbols: str = ""

    # --- SEC EDGAR ingestion (8-K material events, 10-K customer/supplier
    # disclosures, Form 4 insider transactions) ---
    enable_edgar_ingestion: bool = True
    # SEC requires a descriptive User-Agent with real contact info on every
    # request ("Your Name your@email.com") or it will block/rate-limit --
    # not a secret, just informational, but required for EDGAR ingestion to
    # actually work. Left empty, EDGAR ingestion logs a warning and skips.
    edgar_user_agent: str = ""
    edgar_forms: str = "8-K,10-K,4"
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
    alert_webhook_url: str = ""

    enable_dashboard: bool = True
    dashboard_port: int = 8100

    @property
    def symbol_list(self) -> list[str]:
        if self.symbols.strip():
            return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]
        return [c.symbol for c in DEFAULT_UNIVERSE]

    @property
    def edgar_forms_set(self) -> set[str]:
        return {f.strip() for f in self.edgar_forms.split(",") if f.strip()}


def load_settings() -> Settings:
    return Settings()
