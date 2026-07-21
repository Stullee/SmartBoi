# SmartBoi (Evidence Synthesis, Paper-Only)

Runs the [SmartBoi](https://github.com/Stullee/SmartBoi) cross-company
evidence-synthesis pipeline as a Home Assistant add-on, alongside the
existing TradingBot add-on on the same host.

**This system contains no order-placement code whatsoever.** It ingests
SEC EDGAR filings and news, builds a relationship graph, accumulates
per-company evidence dossiers, and logs hypothetical trades it *would*
make -- there is no code path through which it could ever submit a real
order, on any account, paper or live. See the repository's README for the
strategy this implements and why.

## Before you start

Every integration below is optional and the add-on starts fine with none
of them configured -- it just won't do much until you add at least one.
Recommended order:

1. Set `edgar_user_agent` (just your name + an email, e.g. `"Jane Doe
   jane@example.com"` -- SEC requires this on every request or it will
   block/rate-limit you). EDGAR ingestion starts working immediately.
2. Get a free [Finnhub](https://finnhub.io) API key and set
   `finnhub_api_key`. News ingestion starts working immediately, and it's
   also used for the monthly universe auto-screen.
3. Get an Anthropic API key and set `anthropic_api_key`. This turns on the
   actual dossier engine (relationship extraction, dossier updates, the
   adversarial skeptic pass) -- without it, evidence is collected but never
   scored.
4. Only once you want to see actual hypothetical P&L: set
   `enable_ib_price_feed: true` and point `ib_host`/`ib_port` at a running
   IB Gateway/TWS (the same one TradingBot uses, or a separate read-only
   client id against it -- see the main repo's `DEPLOY.md`). This is used
   **only** to read prices and mark hypothetical positions to market --
   never to place an order. Until this is on, signals are still detected
   and logged (`logs/signals.jsonl`, visible on the dashboard), they just
   can't be turned into a tracked hypothetical trade yet.

## Configuration options

| Option | Description |
|---|---|
| `symbols` | Comma-separated tickers. Empty uses the built-in starter watchlist (semiconductor equipment, defense tier-2, grid/data-center, battery/storage ecosystems -- see `src/smartboi/universe.py`) |
| `enable_edgar_ingestion` / `edgar_user_agent` | SEC EDGAR filing ingestion (8-K/10-K/Form 4). Requires a descriptive User-Agent |
| `edgar_forms` | Comma-separated SEC form types to ingest |
| `edgar_poll_interval_sec` / `edgar_lookback_days` | How often to poll, and the rolling lookback window each poll checks |
| `enable_news_ingestion` / `finnhub_api_key` | Finnhub company-news ingestion (free tier) |
| `news_poll_interval_sec` / `news_lookback_days` | How often to poll, and the rolling lookback window |
| `anthropic_api_key` | Powers relationship extraction, dossier updates, and the skeptic pass -- without it, evidence is collected but not scored |
| `extraction_model` / `dossier_model` / `skeptic_model` | Claude model ids for each LLM step (default: a fast/cheap Haiku model for all three) |
| `signal_confidence_threshold` | Minimum `confidence * magnitude` for a dossier to fire a signal |
| `min_independent_sources` | Minimum distinct-domain corroborating sources required (dedup collapses syndicated republishes to one) |
| `max_horizon_days` | Cap on how long a hypothetical position is held before a timeout close |
| `stop_loss_pct` / `take_profit_pct` | Percentage-based stop/target for hypothetical positions (no intraday bar data exists at this holding horizon) |
| `enable_ib_price_feed` | Turns on price marking / hypothetical trade execution. **Never places real orders** -- read-only, see `src/smartboi/prices.py` |
| `ib_host` / `ib_port` / `ib_client_id` | Address of a running IB Gateway/TWS instance for read-only price data |
| `price_poll_interval_sec` | How often to mark open hypothetical positions to market and check for new ones to open |
| `enable_universe_autoscreen` | Monthly market-cap/analyst-coverage recheck that prunes tickers that no longer fit (acquired, delisted, graduated to broad coverage) |
| `universe_min_market_cap_musd` / `universe_max_market_cap_musd` / `universe_max_analyst_count` | Bounds for the auto-screen |
| `log_level` | `debug`, `info`, `warning`, or `error` |
| `alert_webhook_url` | Optional webhook for events needing attention. Empty disables |
| `enable_dashboard` | Read-only dashboard (see below). Enabled by default |

## Dashboard

A read-only dashboard runs alongside ingestion (same process), reachable
directly or as this add-on's Ingress tab. Shows: which integrations are
enabled, the relationship graph, every company's dossier (direction,
confidence, magnitude, evidence count, thesis), open/closed hypothetical
trades with win rate and average R, and recent signals. Auto-refreshes
every 10 seconds. Never places orders -- every handler is a read of
already-persisted state.

## Where things are stored

Everything (logs, the relationship graph, dossiers, dedup index) is
written under this add-on's mapped `/config` share (`smartboi_run/` and
`smartboi_logs/`), visible via the Samba or File Editor/Studio Code Server
add-ons -- no `docker exec` needed to inspect anything.
