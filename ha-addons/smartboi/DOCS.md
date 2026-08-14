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
| `budget_share_extraction` / `budget_share_synthesis` / `budget_share_research` | Maximum share of the daily budget (both the call cap and the dollar cap) each of those passes may consume. One shared pool is first-come-first-served, and the budget resets at UTC midnight — which is 20:00 ET, just after the US close. That gave relationship extraction thirteen and a half hours of night to spend the whole day before the market ever opened, and the live deployment did exactly that: exhausted before 09:30 ET, so the dossier pass — the only one that turns news into a position, and the only one whose input decays — got nothing on the day it mattered. `1.0` is uncapped; `0.0` switches a category off entirely, which is useful for anything you don't want running unsupervised. The **dossier** pass (updater + skeptic) has no share setting on purpose: it is uncapped, so it is guaranteed whatever the three above cannot reach (≥30% at the defaults) and can still use the whole day when they are idle. Caps rather than fixed partitions, because a partition would idle a third of the budget on a day with no filings while the pass that matters starves. Diagnostics prints per-category spend against each cap and flags any category that has exhausted its share |
| `max_propagated_evidence_per_link` / `propagated_evidence_cooldown_hours` | Caps how many pieces of *propagated* evidence (about a linked company, not the dossier's own direct evidence) get forwarded from one origin to one target within the cooldown window -- prevents a noisy anchor from burning a call for every article about it once the causal link keeps getting refused |
| `enable_ecosystem_propagation` | An anchor with no *disclosed* graph edge to any tradeable is inert: it is never its own analysis target, so its news reaches nothing. Measured live, that was 104 of 130 anchors -- including NVDA, AMAT, TSM, MSFT, AMZN, UPS and CSX, the loudest feeds in the universe. With this on, such an anchor's news instead fans out to the tradeables in its own **ecosystem**, flagged to the model as an industry-level association and explicitly *not* a disclosed contract. It carries a low relationship confidence by construction, so it can raise a thesis but can never on its own satisfy the corroboration bar. Never runs when a disclosed edge exists, or when one exists but is inside its cooldown |
| `max_ecosystem_evidence_per_link` | Ecosystem fan-out's own budget, deliberately tighter than `max_propagated_evidence_per_link`: this evidence is weaker than a disclosed contract and reaches every tradeable in the ecosystem rather than a named counterparty, so it gets a smaller share of the daily LLM budget |
| `signal_confidence_threshold` | Minimum `confidence * magnitude` for a dossier to fire a signal |
| `min_independent_sources` | Minimum distinct-domain corroborating sources required (dedup collapses syndicated republishes to one) |
| `min_independent_sources_news_only` | The higher bar a dossier with no primary-source backing must clear. It guards against two outlets rewording one wire story into two apparent "sources" -- nothing else. Two things restore the normal bar: filing evidence on the agreeing side, or evidence that arrived over a relationship edge a 10-K discloses outright (confidence ≥ 0.85, usually with a quantified share of revenue). In the second case the causal link -- the part actually at risk of being wrong -- was already established by a primary source, and demanding a third journalist instead means waiting until the market has made the connection itself |
| `transaction_cost_bps_per_side` | Baseline round-trip friction charged to every hypothetical trade, per side. Bucketed up automatically for smaller caps, where real spreads are wider. The dashboard reports net and gross R side by side so the drag is visible rather than implicit |
| `transaction_cost_profile` | Which cap-bucket cost table those buckets come from. `institutional` (default) assumes an order large enough to move a thin book: 50/150/300 bps per side above $1B, $300M–$1B, below $300M. `retail` assumes a position small enough that impact is negligible and the cost is the half-spread plus commission: 15/35/75. This matters more than it looks — on the 8%/16% grid the sub-$300M institutional bucket charges 600bp round trip, which turns a nominal 2:1 into **+1.19R / −1.72R and a 59% break-even win rate**; the same trade on the retail table needs 40%. Diagnostics prints the break-even rate for every bucket. Only move it to `retail` if the position size the record represents genuinely cannot move the book — an over-stated cost merely understates a real edge (and `r_multiple_gross` is stored alongside for comparison), whereas an under-stated one invents an edge that was never there |
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
hypothetical trades with win rate (shown with its 95% confidence interval,
because a rate over a dozen-odd trades is mostly noise) and average R,
recent signals, today's
LLM call/token usage against the daily budget, and discovered **universe
candidates** -- companies outside your universe that a filing disclosed a
relationship to. Auto-refreshes every 10 seconds. Never places orders.

**Dossiers by conviction** ranks the top names by `confidence x magnitude`
against a vertical rule at the 0.50 signal bar, so which ones actually fire
is readable at a glance. Click a row -- there or in the all-dossiers table
-- to open that dossier: the score and its components, mass agreeing vs
opposing, what the whole-body synthesis pass did to it, and the individual
evidence items behind it, each with its source, its own
direction/confidence/magnitude, the skeptic's note, and (for anything that
arrived through the relationship graph) which company it was actually about
and via which relationship.

Almost entirely read-only, with one exception: each universe candidate
that resolved to a real ticker gets a one-click **"+ Tradeable"** / **"+
Anchor"** button that adds it to the live universe immediately (no
restart, no editing `symbols`/`anchor_symbols` by hand) -- EDGAR/news
polling and the relationship backfill pick it up on their next cycle. The
button can only accept something the extraction pipeline itself already
discovered and surfaced as a candidate, never an arbitrary ticker, and
widening the universe can't place an order or directly create a trade --
a dossier/signal still has to form independently for the new symbol.

The add-on also grows the universe by itself, toward the large "anchor"
companies whose news currently reaches none of the small names it can
trade. A symbol added that way arrives **on probation**: it is polled and
analysed like any other, but it cannot open a position until its own SEC
filing confirms the relationship it was added for. It is promoted
automatically when that filing arrives, and dropped again if none does
within 30 days -- so a symbol appearing and later disappearing on its own
is the mechanism working, not a fault. Open probations are listed in the
diagnostics bundle under Graph health. Set `enable_connector_growth` to
`false` to turn it off.

## Where things are stored

Everything (logs, the relationship graph, dossiers, dedup index) is
written under this add-on's mapped `/config` share (`smartboi_run/` and
`smartboi_logs/`), visible via the Samba or File Editor/Studio Code Server
add-ons -- no `docker exec` needed to inspect anything.
