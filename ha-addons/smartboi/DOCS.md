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
   client id against it -- see the main repo's `DEPLOY.md`). The connection
   is checked and logged right at startup (`CONNECTED` or a clear warning
   with a retry cadence), not silently discovered hours later at the first
   price poll. This is used **only** to read prices and mark hypothetical
   positions to market -- never to place an order. Until this is on,
   signals are still detected and logged (`logs/signals.jsonl`, visible on
   the dashboard), they just can't be turned into a tracked hypothetical
   trade yet.

## Configuration options

| Option | Description |
|---|---|
| `symbols` | Comma-separated TRADEABLE tickers (your small/mid-caps). Setting this (or `anchor_symbols`) replaces the built-in starter watchlist entirely; both empty uses the starter watchlist (see `src/smartboi/universe.py`) |
| `anchor_symbols` | Comma-separated ANCHOR tickers -- big, heavily-covered giants (e.g. `AAPL,MSFT,TSM`) whose news should propagate to your tradeable names. Never trade targets themselves. Relationships between anchors and tradeables are discovered automatically from the tradeable companies' 10-K filings |
| `enable_edgar_ingestion` / `edgar_user_agent` | SEC EDGAR filing ingestion (8-K/10-K/10-Q/Form 4). Requires a descriptive User-Agent |
| `enable_relationship_backfill` | One-time extraction from each tradeable company's most recent 10-K (regardless of age) so the relationship graph populates on first run instead of over a year of annual filings. Each symbol is only ever backfilled once. Ongoing 10-Qs (quarterly) keep the graph refreshed between annual 10-Ks |
| `edgar_forms` | Comma-separated SEC form types to ingest |
| `edgar_poll_interval_sec` / `edgar_lookback_days` | How often to poll, and the rolling lookback window each poll checks |
| `enable_news_ingestion` / `finnhub_api_key` | Finnhub company-news ingestion (free tier) |
| `news_poll_interval_sec` / `news_lookback_days` | How often to poll, and the rolling lookback window |
| `anthropic_api_key` | Powers relationship extraction, dossier updates, and the skeptic pass -- without it, evidence is collected but not scored |
| `extraction_model` / `dossier_model` / `skeptic_model` | Claude model ids for each LLM step (default: a fast/cheap Haiku model for all three) |
| `max_daily_llm_calls` | Hard daily ceiling on Claude API calls, combined across extraction/dossier-update/skeptic. Deferred evidence retries once the budget resets at UTC midnight, never discarded. See the dashboard for actual calls/tokens used today |
| `max_propagated_evidence_per_link` / `propagated_evidence_cooldown_hours` | Caps how many pieces of *propagated* evidence (about a linked company, not the dossier's own direct evidence) get forwarded from one origin to one target within the cooldown window -- prevents a noisy anchor from burning a call for every article about it once the causal link keeps getting refused |
| `signal_confidence_threshold` | Minimum `confidence * magnitude` for a dossier to fire a signal |
| `min_independent_sources` | Minimum distinct-domain corroborating sources required (dedup collapses syndicated republishes to one) |
| `min_independent_sources_news_only` | The higher bar a dossier corroborated **only** by news must clear. Two outlets rewording one wire story can slip past dedup as two "independent" sources; an SEC filing is a primary disclosure that can't be, so any filing evidence on the agreeing side restores the normal bar |
| `transaction_cost_bps_per_side` | Baseline round-trip friction charged to every hypothetical trade, per side. Bucketed up automatically for smaller caps, where real spreads are wider. The dashboard reports net and gross R side by side so the drag is visible rather than implicit |
| `max_horizon_days` | Cap on how long a hypothetical position is held before a timeout close |
| `max_favorable_drift_pct` | "Are we too late" guard (requires `enable_ib_price_feed`): skip opening a signaled trade if the price already moved this many percent in the favorable direction since the signal fired -- the correction likely already happened between signal and entry |
| `signal_entry_deadline_days` | A signal stuck unopened this many days (drift-blocked every poll, or IB unreachable) is expired back to ACTIVE instead of waiting forever on an increasingly stale opportunity |
| `stop_loss_pct` / `take_profit_pct` | Percentage-based stop/target for hypothetical positions (no intraday bar data exists at this holding horizon) |
| `enable_ib_price_feed` | Turns on price marking / hypothetical trade execution. **Never places real orders** -- read-only, see `src/smartboi/prices.py` |
| `ib_host` / `ib_port` / `ib_client_id` | Address of a running IB Gateway/TWS instance for read-only price data |
| `price_poll_interval_sec` | How often to mark open hypothetical positions to market when nothing is waiting to be entered |
| `signal_entry_poll_interval_sec` | The tighter cadence used **while a signal is waiting for an entry**. Marking open trades and confirming an entry are different jobs: at the idle 6-hour cadence a signal that fires mid-session is usually next looked at while the market is shut, and can be expired by decay before it ever gets an entry evaluation. Only applies while something is actually pending, so the steady-state request rate is unchanged |
| `enable_universe_autoscreen` | Monthly market-cap/analyst-coverage recheck. Prunes tickers that no longer fit (acquired, graduated to broad coverage), and **automatically removes** runtime-accepted symbols with no market data at all (delisted, or an OTC/foreign line no source covers). Curated symbols are reported in the diagnostics bundle instead of removed -- a curated list is a deliberate choice, not the screen's to overrule |
| `universe_min_market_cap_musd` / `universe_max_market_cap_musd` / `universe_max_analyst_count` | Bounds for the auto-screen |
| `log_level` | `debug`, `info`, `warning`, or `error` |
| `alert_webhook_url` | Optional: a JSON payload is POSTed here on every signal and paper trade open/close. Point it at a Home Assistant webhook trigger (`http://homeassistant.local:8123/api/webhook/<your-id>`) and attach an automation that sends a mobile notification. Empty disables |
| `enable_dashboard` | Read-only dashboard (see below). Enabled by default |

## Dashboard

A dashboard runs alongside ingestion (same process), reachable directly or
as this add-on's Ingress tab. Shows: which integrations are enabled, the
relationship graph, every company's dossier (direction, confidence,
magnitude, evidence count, thesis, signal-time price), open/closed
hypothetical trades with win rate and average R, recent signals, today's
LLM call/token usage against the daily budget, and discovered **universe
candidates** -- companies outside your universe that a filing disclosed a
relationship to. Auto-refreshes every 10 seconds. Never places orders.

Almost entirely read-only, with one exception: each universe candidate
that resolved to a real ticker gets a one-click **"+ Tradeable"** / **"+
Anchor"** button that adds it to the live universe immediately (no
restart, no editing `symbols`/`anchor_symbols` by hand) -- EDGAR/news
polling and the relationship backfill pick it up on their next cycle. The
button can only accept something the extraction pipeline itself already
discovered and surfaced as a candidate, never an arbitrary ticker, and
widening the universe can't place an order or directly create a trade --
a dossier/signal still has to form independently for the new symbol.

## Where things are stored

Everything (logs, the relationship graph, dossiers, dedup index) is
written under this add-on's mapped `/config` share (`smartboi_run/` and
`smartboi_logs/`), visible via the Samba or File Editor/Studio Code Server
add-ons -- no `docker exec` needed to inspect anything.
