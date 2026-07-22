# SmartBoi

LLM-driven cross-company evidence synthesis for equity research. Ingests
SEC EDGAR filings and news for a small/mid-cap universe, builds a
relationship graph between companies, accumulates per-company evidence
dossiers with an adversarial skeptic pass, and logs the hypothetical
("paper") trades the evidence would justify -- so the strategy's validity
can be judged on real forward performance before any capital, or even a
real broker paper account, is ever involved.

**This system contains no order-placement code whatsoever.** It is
hardcoded paper-only: there is no code path, misconfigured flag, or env
var through which it could ever submit a real order, on any account,
paper or live. `prices.py` (the only thing that ever talks to a broker)
is read-only -- it fetches quotes and account equity, nothing else. See
"Paper-only, by construction" below.

**⚠️ Not financial advice.** This is engineering/research tooling. The
watchlist in `src/smartboi/universe.py` is a starting-point candidate list
for a research universe, not a recommendation to trade anything.

## The strategy this implements

Public markets are slow to connect dots across sources and across
companies (gradual information diffusion, post-news drift, and the
Cohen-Frazzini finding that economically linked companies react to each
other's news with a lag of days to weeks). This system is built to exploit
that specific inefficiency, not to race anyone on speed:

1. **A universe where synthesis wins** -- small/mid-cap companies with thin
   analyst coverage, grouped into ecosystems (semiconductor equipment,
   defense tier-2 suppliers, grid/data-center buildout, battery/storage),
   where a big, heavily-covered player's news plausibly moves a thinly-
   covered second-order name with a lag. See `src/smartboi/universe.py`.
2. **Second-order effects, not headlines** -- a relationship graph
   (customer/supplier/competitor/regulator edges, seeded from known
   industry relationships and extracted from 10-K/10-Q filings) means
   every piece of evidence is checked against "who else does this affect,"
   not just the company it's literally about. See `graph.py`.
3. **Accumulated evidence, not reactive headlines** -- each company has a
   living dossier: direction, magnitude, confidence, horizon, and cited
   evidence. The signal is accumulated, corroborated evidence crossing a
   threshold, not any single article. Syndicated republishes of the same
   story are deduped to one data point, and evidence itself decays over
   time so an old, unconfirmed claim can't prop up confidence forever --
   see `dossier.py` and `dedup.py`.
4. **Reads what nobody parses** -- SEC EDGAR full-text filings (8-Ks,
   10-K/10-Q customer/supplier disclosures, Form 4 insider transactions,
   parsed into readable transaction summaries) as a first-class evidence
   source, not an afterthought. See `edgar.py`.
5. **Adversarial to itself** -- every proposed dossier update is reviewed
   by a second, skeptical LLM pass trying to refute it before it counts.
   See `skeptic.py`.
6. **Prune-only universe auto-screen** -- a monthly market-cap/analyst-
   coverage recheck flags tickers that no longer fit (acquired, delisted,
   graduated to broad coverage) instead of trusting a hardcoded list to
   stay accurate forever. See `universe_screen.py`.
7. **Forward-tested, not backtested** -- an LLM-driven strategy backtested
   on news it was trained after already "knows" how the story ended. This
   system only ever runs forward, logging every hypothetical trade it
   would make as it happens, so its track record means something.
8. **Checks whether it's already too late** -- a signal firing doesn't mean
   entering blind. At entry time, if the price already moved past
   `MAX_FAVORABLE_DRIFT_PCT` in the signal's favorable direction since it
   fired, the correction likely already happened between signal and entry
   and the trade is skipped rather than chasing a move that's largely over
   (requires `ENABLE_IB_PRICE_FEED`). See "Entry timing" below.

## Paper-only, by construction

Not a config flag -- an architectural guarantee. `prices.py` (the only
module that ever connects to a broker) contains zero order-placement
methods: no `placeOrder`, no `Trade`, no bracket. It fetches historical
bars for a last price and reads account equity, full stop. Every
"trade" this system makes is a `PaperTrade` dataclass (`paper_journal.py`)
appended to `logs/paper_trades.jsonl` -- there is no other output. Even
when a live IB account is connected for market data (`ENABLE_IB_PRICE_FEED`),
it is used purely as a read-only price source.

## Architecture

```
src/smartboi/
  config.py            typed settings loaded from .env -- every integration
                        degrades gracefully when unconfigured (see its docstring)
  universe.py           starter watchlist: 4 ecosystems + anchor companies +
                        seeded relationships (see below)
  dedup.py               source fingerprinting: collapses syndicated
                        republishes to one data point
  edgar.py                SEC EDGAR ingestion: CIK lookup, filing search, text fetch
  news.py                  Finnhub company-news ingestion + market-cap/analyst-count lookups
  graph.py                  relationship graph store + LLM-based extraction from filings
  dossier.py                 per-company evidence dossier: model, store, LLM update proposal
  skeptic.py                  adversarial second pass that tries to refute proposed updates
  signals.py                    evidence-threshold crossing -> SignalEvent (always logged)
  alerts.py                      optional webhook POST on signals / paper trade opens & closes
  paper_journal.py                hypothetical trade open/mark/close -- NO order-placement code
  prices.py                        read-only IB price feed -- NO order-placement code, optional
  universe_screen.py                monthly market-cap/analyst-coverage prune-only recheck
  status.py                          dashboard data gathering (pure reads of persisted state)
  webapp.py                           read-only dashboard, runs alongside the engine
  engine.py                            orchestrates ingestion -> graph -> dossier -> signals -> paper trades
  main.py                              entry point
```

## The starter universe

Four ecosystems where a big player's news plausibly spills over onto a
thinly-covered second-order name: semiconductor equipment & materials
(second-order to TSMC/Intel/Samsung capex news), defense & aerospace
tier-2 suppliers (second-order to prime-contractor awards), grid/
electrification/data-center buildout (second-order to hyperscaler capex
announcements), and battery/energy storage (second-order to EV/policy
news). Each ecosystem also includes "anchor" companies -- large,
efficiently-priced names that are never trade targets but whose news is
exactly what should propagate to the smaller names via the relationship
graph. See `src/smartboi/universe.py` for the full list and
`SEED_RELATIONSHIPS` for the manually-seeded, well-documented edges
(the rest are left for the extraction pipeline to find in actual filings
rather than asserted as fact).

### Bring your own universe

The starter watchlist is just a default. Set `SYMBOLS` (your tradeable
small/mid-caps) and `ANCHOR_SYMBOLS` (the big, heavily-covered giants
whose news should propagate to them -- never trade targets themselves)
and the built-in list is replaced entirely. You do NOT configure the
relationships between them: those are discovered automatically from the
tradeable companies' 10-K filings -- a small supplier must disclose its
dominant customers, which is why the graph is learned from the small
companies' filings, not the giants'. On first run (and once for each
newly added symbol) a one-time backfill extracts from each tradeable
company's most recent 10-K regardless of age
(`ENABLE_RELATIONSHIP_BACKFILL`), so the graph populates immediately
instead of over a year of annual filings.

When a filing discloses a relationship to a company OUTSIDE the universe,
it's recorded as a **universe candidate** (`data/universe_candidates.json`,
shown on the dashboard, webhook-alerted on first discovery) -- a proposal
for you to review, never auto-added. Accept one by adding its ticker to
`SYMBOLS` or `ANCHOR_SYMBOLS`.

## Entry timing: are we too late?

A signal firing (evidence crossed the confidence/magnitude/corroboration
bar) and a paper trade opening are deliberately two separate moments --
the price feed only polls every `PRICE_POLL_INTERVAL_SEC` (6h by default),
so time passes between "the evidence justified a position" and "we're
about to actually take it." Two guards close that gap (both require
`ENABLE_IB_PRICE_FEED=true` -- without a price feed there's no price to
check a signal against, and signals just log as before):

- **Favorable drift** (`MAX_FAVORABLE_DRIFT_PCT`, default 5%): the price
  the moment a dossier flips to SIGNALED is snapshotted
  (`Dossier.signaled_price`). At entry time, if the price has already
  moved this many percent in the signal's favorable direction (up for
  LONG, down for SHORT) since that snapshot, the correction likely already
  happened -- the trade is skipped rather than chasing a move that's
  largely over. Alerted once per signal, not every poll.
- **Entry deadline** (`SIGNAL_ENTRY_DEADLINE_DAYS`, default 5): a signal
  stuck unopened this long (drift-blocked every poll, or IB briefly
  unreachable) is expired back to `ACTIVE` instead of waiting forever on an
  increasingly stale opportunity -- fresh evidence can re-signal it later
  with a clean baseline. A thesis that flips direction while still
  SIGNALED-but-unopened also gets a fresh baseline immediately, since the
  old one no longer means anything.

Both are visible on the dashboard's Dossiers table (Signaled @ column).

## Evidence time-decay

Evidence doesn't count forever. Each item stays at full weight through its
own predicted `horizon_days` (it hasn't had a chance to prove out yet),
then decays linearly and is excluded entirely once it's aged past 2x its
horizon (floored at 14 days so a short-horizon item isn't discarded almost
immediately) -- by then the market either already reacted (priced in) or
the predicted move never happened (thesis didn't pan out), so it stops
propping up the dossier's confidence either way. This runs both when new
evidence merges AND once a day with no new evidence, so a dormant dossier
keeps fading instead of freezing at its last score. A `SIGNALED`-but-
unopened dossier that decays below the signal threshold is expired back to
`ACTIVE` the same way an entry-timing expiry would. See `dossier.py`'s
`evidence_weight`/`evidence_is_stale`/`recompute_decay`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env -- every integration is optional, see the recommended order below
```

Recommended order to bring integrations online:

1. `EDGAR_USER_AGENT` (just your name + an email -- SEC requires this on
   every request). EDGAR ingestion starts working immediately.
2. A free [Finnhub](https://finnhub.io) `FINNHUB_API_KEY`. News ingestion
   and the universe auto-screen start working immediately.
3. `ANTHROPIC_API_KEY`. This turns on the actual dossier engine
   (relationship extraction, dossier updates, the skeptic pass) --
   without it, evidence is collected but never scored.
4. Only once you want real hypothetical P&L: `ENABLE_IB_PRICE_FEED=true`
   plus `IB_HOST`/`IB_PORT` pointed at a running IB Gateway/TWS. Read-only,
   never places an order. Until this is on, signals are still detected and
   logged (`logs/signals.jsonl`), they just can't become a tracked
   hypothetical trade yet.

## Running

```bash
source .venv/bin/activate
python -m smartboi.main
```

Logs to console and `logs/smartboi.log` (rotating). Runs continuously,
polling EDGAR/news/prices on their own configured intervals.

## Dashboard

A read-only dashboard (`ENABLE_DASHBOARD=true` by default) runs alongside
the engine. Open `http://localhost:8100/` (or `DASHBOARD_PORT`): which
integrations are enabled, the relationship graph, every company's dossier,
open/closed hypothetical trades with win rate and average R, and recent
signals. Auto-refreshes every 10 seconds. Never places orders.

## Running on Home Assistant OS

See [`DEPLOY.md`](DEPLOY.md) -- ships as a proper Home Assistant add-on
(`ha-addons/smartboi`), installable alongside the existing TradingBot
add-on on the same host, configured through the HA UI instead of a `.env`
file.

## Alerts

Set `ALERT_WEBHOOK_URL` to get a JSON payload POSTed on every signal and
every paper trade open/close -- so a headless deployment tells you when
something happened instead of relying on you checking the dashboard. Point
it at a Home Assistant webhook trigger
(`http://homeassistant.local:8123/api/webhook/<your-id>`) with an
automation that forwards to a mobile notification, or any other HTTP
endpoint. The payload is `{event, title, message, data, sent_at}` where
`event` is `signal`, `paper_trade_opened`, or `paper_trade_closed`.

## Running the tests

```bash
pip install -r requirements.txt
pytest
```

Tests cover dedup fingerprinting, the relationship graph, dossier evidence
merging, signal threshold evaluation, and the paper trade journal -- all
pure logic, none require a live EDGAR/Finnhub/Anthropic/IB connection.

## Known limitations / possible next steps

- The universe auto-screen is prune-only by design (see
  `universe_screen.py`'s docstring) -- it never adds new candidate tickers
  automatically, since that's an editorial judgment call, not a threshold
  check.
- EDGAR full-text extraction looks for relationships in 10-K and 10-Q
  filings (where customer/supplier disclosures concentrate); 8-Ks and
  Form 4s feed the dossier engine as direct evidence but aren't run
  through relationship extraction.
- No portfolio construction yet (the long-conviction / short-sector-ETF
  hedge, 20-40 position sizing described in the original strategy write-up)
  -- this is the ingestion + dossier + paper-journal layer; portfolio
  construction is a natural next step once dossiers/signals prove out on
  real forward data.
- Percentage-based stop/target for paper trades, not ATR-based -- there's
  no intraday bar data at a weeks-long holding horizon the way TradingBot's
  intraday strategies have.
