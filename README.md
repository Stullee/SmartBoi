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
5. **Adversarial to itself, calibrated by directness** -- every proposed
   dossier update is reviewed by a second, skeptical LLM pass trying to
   refute it before it counts, but the bar differs deliberately for direct
   vs. propagated evidence. Direct evidence (mechanical Form 4 activity,
   vague governance news) is held to a high bar. Propagated evidence is
   judged on whether the ORIGIN fact is concrete and the disclosed
   relationship is direct enough to support the proposed magnitude --
   never refuted merely for being unconfirmed/indirect, since that's the
   normal state of every piece of propagated evidence by definition and
   demanding otherwise would mean point 2 above could never produce a
   signal. When the only problem is that the proposed size is too large
   for how loosely the relationship is disclosed, the skeptic scales
   `adjusted_magnitude` down and accepts it rather than refusing outright
   -- a real fact through a weak relationship is still worth something,
   just less than proposed, and refusing it outright would throw away
   exactly the small, accumulating corroboration point 3 depends on.
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
  usage.py                        daily Claude API call/token budget tracker (cost control)
  ratelimit.py                     sliding-window limiter (propagated-evidence cooldown)
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
rather than asserted as fact). `SEED_RELATIONSHIPS` only ever seeds an
edge when BOTH companies are actually in the live universe -- a custom
`SYMBOLS`/`ANCHOR_SYMBOLS` deployment that doesn't include these
particular tickers gets none of this starter data; every edge in a
custom universe comes from that universe's own filings instead (see
`engine.py`'s `_seed_graph`).

A relationship's `rel_type` must be one of `graph.REL_TYPES` (customer/
supplier/competitor/regulator) -- the LLM extraction tool schema declares
this as an enum, but Anthropic tool use doesn't hard-enforce it, so a
stray value could still slip through. `engine.py`'s `_extract_relationships`
drops (logs and skips) any relationship outside the enum before it ever
reaches the graph, and `RelationshipGraph` prunes any already-persisted
edge with an invalid `rel_type` on load, rewriting `graph.json` clean --
self-healing for anything that got in before this guard existed, with no
manual edit required.

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
for you to review, never auto-added. If the model didn't recognize the
counterparty's ticker, resolution falls back through two tiers before
giving up: `EdgarClient.find_ticker_by_name` matches a normalized name
against SEC's own registered filer list ("ASML" matches the registered
"ASML Holding N.V."), and when that strict match misses --
brand-name-vs-legal-name mismatches ("Google" vs "Alphabet Inc"),
abbreviations ("IBM" vs "International Business Machines Corp"), anything
not phrased close to the SEC filer's exact title -- `FinnhubClient.
search_ticker_by_name` tries Finnhub's own fuzzy/brand-aware `/search`
endpoint (free tier, no extra integration). Both are best-effort and
US-listed-SEC-filer-or-Finnhub-covered only: private companies and
foreign issuers that don't file with the SEC or trade in the US genuinely
have no ticker to find. Government bodies, regulators, and generic
customer-class descriptions ("public utilities") are filtered out
entirely rather than shown as an unactionable dead end (see engine.py's
`_NON_COMPANY_KEYWORDS` -- only ever applied after ticker resolution has
already failed, so a real resolved candidate is never hidden by it).

Accept a candidate with one click on the dashboard -- "+ Tradeable" or "+
Anchor" -- and it's live immediately, no restart: EDGAR/news polling and
the relationship backfill both read the current universe on their next
cycle, not a value fixed at startup. Persisted in
`data/accepted_candidates.json`, so it survives a restart without ever
touching `SYMBOLS`/`ANCHOR_SYMBOLS` by hand (those still work too, if you
prefer static config). A candidate that never resolved to a ticker has no
button -- there's nothing to add without one. The candidate list sorts
addable candidates (resolved ticker, not yet accepted) first, and collapses
still-unresolved ones behind a "N candidate(s) with no resolved ticker"
disclosure instead of listing them inline, so the ones actually worth a
decision aren't buried under a long tail of unresolved-ticker or
already-added entries -- see `status.py`'s `gather_universe_candidates`.

Ticker resolution for a name-keyed candidate isn't a one-shot attempt: once
a day, `engine.py`'s `_run_candidate_ticker_recheck` retries every
still-unresolved candidate through both tiers again -- catches ones
discovered before the Finnhub fallback existed, or where SEC's ticker map
has since caught up with a new listing (a fresh IPO, a name that changed).
A candidate that resolves on recheck is moved from its name key to its
ticker key, merging into an existing ticker-keyed entry (seen count,
related-to list, etc. all combined) if a separate filing had already
discovered the same company with a ticker in the meantime -- nothing is
ever silently duplicated or lost. The same daily pass also suggests
"tradeable" vs "anchor" for every resolved-but-not-yet-accepted candidate,
from its market cap/analyst count against the exact same small/mid-cap
thin-coverage bounds the universe auto-screen applies to existing members
(`UNIVERSE_MIN_MARKET_CAP_MUSD`/`UNIVERSE_MAX_MARKET_CAP_MUSD`/
`UNIVERSE_MAX_ANALYST_COUNT`) -- a big, heavily-covered name gets
recommended as an anchor (news source, matching what it deliberately looks
like), a small/mid-cap thinly-covered one as tradeable. Shown as a bolded
Accept button on the dashboard, with the reason on hover -- a hint, not a
guarantee; you always get the final call. See
`universe_screen.recommend_candidate_type`.

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

## Cost controls

Every evidence item costs at least one Claude call (dossier update) plus a
second (skeptic) if it's judged new -- and propagation multiplies that by
however many linked targets an origin has, so a heavily-covered anchor with
several links can generate real spend fast. Two guards, both in `config.py`:

- **Daily LLM call budget** (`MAX_DAILY_LLM_CALLS`, default 3000): a hard
  ceiling across extraction/dossier-update/skeptic calls combined. Once hit,
  further evidence is deferred (not discarded) until the budget resets at
  UTC midnight -- exactly the same "retry later" path as a transient API
  failure. Call-count-based rather than a dollar figure on purpose: this
  codebase's own prompt construction keeps each call's token size roughly
  bounded, so a call cap is a robust proxy for spend that won't rot when
  Anthropic's pricing changes. See the dashboard for actual calls/tokens
  used today.
- **Propagated-evidence cooldown** (`MAX_PROPAGATED_EVIDENCE_PER_LINK`,
  default 3 per `PROPAGATED_EVIDENCE_COOLDOWN_HOURS`, default 6h): caps how
  many pieces of evidence about one linked (non-direct) origin get scored
  against one target within the window. Without this, a noisy anchor with
  many articles about the same underlying story burns a full dossier-
  update + skeptic call against a target for every one of them, even after
  the causal link has already been refused several times running for the
  same reason. Only throttles PROPAGATED fan-out to other dossiers --
  evidence about a company's own dossier is never throttled.

Also worth tuning down if spend matters more than freshness:
`NEWS_POLL_INTERVAL_SEC` (default 3600 -- this strategy holds positions for
weeks, so 15-minute freshness was pure waste) and `EDGAR_POLL_INTERVAL_SEC`.

The propagation cooldown no longer double-charges on a retry: it now only
consumes a slot once a target's evidence is DEFINITIVELY handled (merged,
refused, or judged not-new), not on every attempt -- a budget-deferred or
transient-failure retry re-checks the same slot instead of burning a second
one for what's the same underlying evidence. Similarly, a dossier-update
proposal that pays for `propose_update` but then has its skeptic call
deferred by the budget is now cached in memory and reused on retry instead
of being re-proposed (and re-paid for) from scratch.

## Corroboration

`MIN_INDEPENDENT_SOURCES` counts genuinely independent evidence, not just
distinct API calls: for news, `dedup.py`'s fingerprint (symbol + normalized
headline + date) collapses syndicated republishes of one wire story into
one data point regardless of which outlet's copy a feed happened to
return, while source IDENTITY is the real publisher name (Reuters,
Bloomberg, ...) -- `engine.py`'s `_poll_news` used to identify a source by
its article URL's domain, but Finnhub's free tier serves every article
through its own `finnhub.io` URLs, so every single article was silently
labeled the same source and `independent_source_count` could never exceed
1 no matter how many outlets actually covered a story (the actual publisher
was already available in `NewsArticle.source`, just never used for
identity). Fixed by preferring the publisher name outright. For SEC
filings, each
filing TYPE counts separately (`"SEC EDGAR (8-K)"`, `"SEC EDGAR (Form 4)"`,
`"SEC EDGAR (10-Q)"`, ...) rather than every filing collapsing onto one
flat `"SEC EDGAR"` source -- a material event, an insider transaction, and
a quarterly filing are independent disclosures, not restatements of each
other, and previously a filing-heavy dossier could never satisfy the
corroboration bar from EDGAR evidence alone no matter how much of it
existed. The graph's own extracted relationship confidence (how directly a
disclosed customer/supplier link connects two companies) is also now
passed directly to both the dossier updater and the skeptic, instead of
being left for them to re-infer purely from the relationship description's
wording on every single evidence item. Filing text sent to the LLM is
capped at 150,000 characters (`fetch_text`'s `max_chars` default) but no
longer by a flat prefix truncation -- `edgar.py`'s `_truncate_head_tail`
keeps roughly the
first two-thirds and the last third of the document with a
`[...document middle omitted...]` marker in between, since financial
statement notes (where customer/supplier/segment disclosures concentrate)
tend to sit near the end of a long filing and a prefix-only cut was
silently starving the graph of exactly the evidence it needs.

## Contested evidence

A dossier's direction and confidence are no longer decided by comparing
individual evidence items -- `dossier.py`'s `_aggregate` sums decay-weighted
mass separately for each side (`mass_agree` for whichever direction has
more accumulated weight, `mass_opposing` for the other) and derives both
fields from the totals:

- **Direction** is whichever side carries more mass. A single strong item
  no longer flips the thesis on its own if the accumulated opposing side
  is heavier; conversely the direction does flip once new evidence tips
  the balance, not just when it individually outweighs the single most
  recent opposing item.
- **Confidence** starts from the agreeing side's own strength (base
  confidence plus a small corroboration bonus for multiple independent
  agreeing sources, capped at 1.0), then gets discounted by how contested
  the dossier is: `confidence *= max(0, 1 - mass_opposing / mass_agree)`.
  Evenly split evidence (`mass_opposing == mass_agree`) zeroes confidence
  out entirely -- a dossier that's just as bullish as it is bearish isn't
  a real thesis yet. Stale opposing evidence (decayed to zero weight, see
  Evidence time-decay above) stops discounting once it's aged out, exactly
  like it stops corroborating.

Both totals are surfaced on the dashboard's dossier table ("Mass (agree vs
oppose)" column) so a low-confidence dossier's cause -- contested vs. just
thin -- is visible at a glance instead of having to guess from the raw
evidence log.

## Forward-validation data capture

Confidence/magnitude scores are worthless as a strategy signal until
they're checked against what the market actually did afterward, and that
check needs a time series that starts now -- a missed day can't be
backfilled later. Two append-only logs capture that raw material at zero
extra LLM cost, piggybacked on work the engine already does daily:

- **`logs/dossier_snapshots.jsonl`** -- every dossier's direction,
  confidence, magnitude, `confidence * magnitude` score, source count, and
  status, once per day, unconditionally (even dossiers with no evidence
  and no changes get a data point, so the resulting series has no gaps to
  explain away). Written by `engine.py`'s `_run_daily_snapshot`, piggybacked
  on the existing daily decay pass. See `status.py`'s `snapshot_dossier`.
- **`logs/price_marks.jsonl`** -- a daily closing-ish price for every
  tradeable (non-anchor) universe symbol, piggybacked on the existing 6h IB
  price poll. Written by `engine.py`'s `_run_daily_price_marks`.

Joining the two by symbol/date is what a later offline analysis (per-
ecosystem benchmarks, a counterfactual ledger for signals the confidence
threshold skipped) will run against -- not built yet, deliberately
sequenced after the engine has real test coverage (see Running the tests).

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
   never places an order. The connection is checked right at startup and
   logged clearly either way (`CONNECTED`, or a warning with the retry
   cadence) rather than only surfacing a failure hours later at the first
   price poll. Until it's reachable, signals are still detected and logged
   (`logs/signals.jsonl`), they just can't become a tracked hypothetical
   trade yet.

## Running

```bash
source .venv/bin/activate
python -m smartboi.main
```

Logs to console and `logs/smartboi.log` (rotating). Runs continuously,
polling EDGAR/news/prices on their own configured intervals.

## Dashboard

A dashboard (`ENABLE_DASHBOARD=true` by default) runs alongside the engine.
Open `http://localhost:8100/` (or `DASHBOARD_PORT`): which integrations are
enabled, the relationship graph (grouped by filer -- each company's own
disclosed customers/suppliers/competitors/regulators together, strongest
confidence first, rather than one flat table sorted by extraction order),
every company's dossier, open/closed hypothetical trades with win rate and
average R, recent signals, today's LLM call/token usage against the daily
budget, and discovered universe candidates. Auto-refreshes every 10
seconds. Never places orders.

Almost entirely read-only, except one endpoint: `POST /api/candidates/accept`
adds a discovered candidate into the live universe (the dashboard's "+
Tradeable"/"+ Anchor" buttons) -- bounded to symbols the extraction
pipeline itself already surfaced, never an arbitrary ticker, and it can
only widen what's watched, never place an order or directly create a
trade. See "Bring your own universe" above.

## Observability

A `heartbeat: universe=N dossiers=N signaled=N graph_edges=N ...` INFO line
logs roughly every 10 minutes (`engine.py`'s `_log_heartbeat`) regardless
of whether anything actually happened that cycle -- ingestion at this
system's polling cadences (hourly EDGAR/news, 6h prices) can leave the log
quiet for long stretches, and without a heartbeat there was no way to tell
an idle-but-healthy engine from a hung one. `ib_async` (the IB Gateway
client library) is set to WARNING alongside `httpx`/`httpcore`/
`aiohttp.access` -- it logs routine Gateway connectivity blips and
account-summary noise at ERROR even though they don't affect price marks,
which otherwise drowns out anything that's an actual SmartBoi problem.

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

Most tests cover dedup fingerprinting, the relationship graph, dossier
evidence merging, signal threshold evaluation, and the paper trade journal
-- all pure logic, none require a live EDGAR/Finnhub/Anthropic/IB
connection.

`tests/test_engine.py` covers the engine loop itself, wired to scripted
fakes (`tests/fakes.py` -- plain classes with queued canned responses, no
mocking framework) instead of real API clients: the retry/registration
semantics around a deferred LLM call, the propagation cooldown's
definitive-only recording, and the full signal -> snapshot -> open -> close
-> reset lifecycle. Runs isolated in a `tmp_path` so it never touches this
repo's real `data/`/`logs/` directories or makes a network/LLM call.

CI (`.github/workflows/ci.yml`) runs the full suite on every push to `main`
and every pull request.

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
