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
   defense tier-2 suppliers, grid/data-center buildout, battery/storage,
   and medical-device supply -- the last one deliberately uncorrelated with
   the AI/electrification capex cycle driving the other four),
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
4. **Reads what nobody parses, including the parts most readers skip** --
   SEC EDGAR is treated as a LIVE catalyst feed, not a quarterly one, and
   is polled every 15 minutes. An 8-K is filed within four business days
   of a material event (usually the same day), and its *primary document*
   is a cover page saying "a press release is attached as Exhibit 99.1" --
   so `edgar.py` fetches the EXHIBITS too. The product launch, the contract
   award, the guidance revision: they are all in EX-99.1, and reading only
   the cover page meant they reached the dossier engine as a filing that
   says news exists and not one word of what it said. The 8-K item codes
   (1.01 material agreement, 2.02 results, 3.02 dilution...) are expanded
   into plain English so a contract win is distinguishable from a director
   departure before a single token is spent. Alongside: 10-K/10-Q
   customer/supplier disclosures, Form 4 insider transactions parsed into
   readable summaries, SC 13D activist stakes, and 424B5 shelf takedowns
   (dilution -- the cleanest SHORT catalyst this universe offers; a system
   that only reads good news is not a research system). Also **S-1/S-3**,
   the shelf REGISTRATION weeks to months ahead of that takedown, so the
   leading indicator sits alongside the confirming one; **NT 10-K/NT 10-Q**
   late-filing notices, which skew hard toward thin caps and whose reaction
   turns on the *reason given* (boilerplate reads negative, a specific
   legitimate cause does not -- a judgement about prose, which is what the
   LLM is there for); **Form 25/15**, ten days' notice of a delisting that
   `_is_unknown_to_edgar` would otherwise only catch after SEC's ticker file
   lags it; and **20-F/40-F/6-K** for foreign private issuers. That last one
   closes a hole rather than adding a nicety: NVX files 20-F/6-K and nothing
   else, so its dossier could never receive a single filing-evidence item --
   while being a tradeable, and one of only two names still passing the
   thin-coverage screen. It also appears in neither endpoint of any of the
   1066 graph edges, so with no filings it had no evidence path at all. Same
   for the anchors CAMT/TSM/ASML/MGA; TSMC files monthly net revenue on a
   6-K. 6-K ingestion is capped per symbol per day
   (`MAX_6K_ITEMS_PER_SYMBOL_PER_DAY`) because EDGAR independence keys on
   form *and* filing day, so an unbounded cross-filer could corroborate
   itself into a signal. See `edgar.py`.
4b. **Primary sources that are not filings** -- two feeds where the
   government is the publisher, so the evidence cannot be two outlets
   rewording one wire story:

   - **DoD daily contract announcements** (`dod_contracts.py`, war.gov, free):
     every award at or above the DFARS 205.303 threshold, published ~5pm ET
     each business day. **Currently defaults OFF** (`ENABLE_DOD_CONTRACTS`):
     war.gov's Akamai bot gate 403'd every request the feed made in its first
     week live, so until the fetch path demonstrably returns announcements,
     enabling it buys a request-and-warning loop and nothing else. Chosen over USASpending/FPDS for a hard reason: DoD
     awards are withheld from those for **90 days**, ~6x past the 14-day floor
     in `evidence_is_stale`, so that evidence would be *born aged out*.
     (FPDS-NG's ATOM feed no longer exists either — FPDS.gov was decommissioned
     in 2026 and folded into SAM.gov.)

     The hard part is name matching, not fetching. Announcements use legal
     entity names — "Ducommun LaBarge Technologies Inc." → DCO, "Vertex
     Aerospace LLC" → V2X — and **"Vertex" alone collides with Vertex
     Pharmaceuticals**. So matching is whole-word, case-insensitive, against a
     hand-reviewed alias table and nothing else: no fuzzy matching, no
     substrings. A banned-alias list lives in code rather than in a comment so
     that adding a dangerous one fails a test rather than a live dossier. The
     announcement text is passed through **verbatim**, because many "awards"
     are IDIQ ceilings or modifications rather than new revenue and the
     difference lives in the wording — summarising it would destroy exactly
     what the skeptic needs to catch it. Anchor awards are gated on a value
     floor (default $100M); a tradeable's own award never is, since $12M is
     material to a $90M-cap company in a way the same award to Lockheed isn't.

   - **Federal Register** (`federal_register.py`, free, no key): a handful of
     hand-curated searches, never the feed — ~200 documents publish per
     business day and watching that broadly is a disqualifying firehose. Each
     search was written against a specific checkable claim: wind-tower AD/CVD
     proceedings name **BWEN** as a Wind Tower Trade Coalition member; EPA AIM
     Act HFC allowance notices are entity-specific and drive **HDSN**'s
     refrigerant economics; BIS Entity List actions reach **AOSL**; export
     controls and FMVSS reach two whole ecosystems.

     A rule is not a company, so propagation runs from synthetic regulator
     origins (BIS, EPA, ITC, NHTSA) over hand-seeded `regulator` edges. Those
     are **deliberately kept out of the universe** — registering them as
     members would put "BIS" into the EDGAR poll, the news poll and the screen
     — and the edges are seeded at 0.60–0.80, below
     `DISCLOSED_LINK_CONFIDENCE`, so a sector-wide rule can raise a thesis but
     never buys the corroboration discount a quantified customer disclosure
     earns.

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
6. **Judged as a body, not just item by item** -- every score above is
   assigned to ONE piece of evidence in isolation, and the aggregate is
   arithmetic over those independent scores. That leaves three questions
   structurally unanswerable, and each decides whether a trade is
   justified: are these N items N facts or one fact counted N times; do
   they tell one coherent story or are they unrelated coincidences pointing
   the same way; and has the market already made this connection, which is
   the lag the whole strategy exists to trade. A daily synthesis pass
   (`dossier.DossierSynthesizer`) reads the complete evidence file and
   answers them. Its verdict CAPS the arithmetic score -- it can veto and
   it can trim, never lift -- so it catches exactly the errors the
   aggregate is blind to without letting one model call manufacture a
   trade on its own.
7. **Prune-only universe auto-screen** -- a monthly market-cap/analyst-
   coverage recheck flags tickers that no longer fit (acquired, delisted,
   graduated to broad coverage) instead of trusting a hardcoded list to
   stay accurate forever. See `universe_screen.py`.
8. **Forward-tested, not backtested** -- an LLM-driven strategy backtested
   on news it was trained after already "knows" how the story ended. This
   system only ever runs forward, logging every hypothetical trade it
   would make as it happens, so its track record means something.
9. **Checks whether it's already too late** -- a signal firing doesn't mean
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
  edgar.py                SEC EDGAR ingestion: CIK lookup, filing search, text +
                           EX-99 press-release exhibits, 8-K item codes
  news.py                  Finnhub company-news ingestion + quotes + market-cap/analyst lookups
  llm.py                    per-model request shape (thinking/effort/temperature differ by
                             model generation) + pricing table -- see its docstring
  graph.py                  relationship graph store + LLM-based extraction from filings
  dossier.py                 per-company evidence dossier: model, store, LLM update proposal,
                              and DossierSynthesizer (reasons across the WHOLE evidence body)
  skeptic.py                  adversarial second pass that tries to refute proposed updates
  signals.py                    evidence-threshold crossing -> SignalEvent (always logged)
  alerts.py                      optional webhook POST on signals / paper trade opens & closes
  usage.py                        daily Claude API call + USD budget tracker (cost control)
  ratelimit.py                     sliding-window limiter (propagated-evidence cooldown)
  paper_journal.py                hypothetical trade open/mark/close -- NO order-placement code
  prices.py                        read-only IB price feed -- NO order-placement code, optional
  universe_screen.py                monthly market-cap/analyst-coverage prune-only recheck
  screen.py                          candidate screening CLI (python -m smartboi.screen)
  forward_returns.py                  "does score predict forward returns" math (offline, pure)
  tools.py                             operator tools the dashboard runs (screen / forward returns)
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

Two passes attack the same starvation from the other end, and both write
**candidates only, never a graph edge**. "Research anchor suppliers (web)"
uses a web-search-backed Claude call (`research.py`). "Search EDGAR for
anchor suppliers" (`edgar_search.py`) uses EDGAR's own full-text search to
ask **which other filers name this anchor** — a supplier disclosing "Applied
Materials accounted for 22% of net sales" is making exactly the disclosure
the anchor never would, and full-text search is the only mechanism that
surfaces it. No LLM spend; SEC requests only.

Both run on a **daily cadence** (`ENABLE_AUTO_SUPPLIER_RESEARCH`,
`ENABLE_AUTO_EDGAR_SEARCH`), most-inert anchors first, as well as on their
dashboard buttons. `EDGAR_SEARCH_ANCHORS_PER_RUN` (default 25) sets the
full-text search's throughput: one EFTS query plus at most ten document
fetches per anchor, at SEC's 0.3s spacing, is ~3 seconds and zero tokens, so
the old operator-sized cap of 5 was a 64-day rotation over a ~320-anchor list
for no saving worth having. Scheduling the EDGAR one matters more than it sounds:
left on a button it simply never ran, and it is the cheapest mechanism here
that is size-selected in the direction the strategy needs. Reading an
anchor's own filings finds its big customers; asking who *names* the anchor
finds the small filers for whom the anchor is material. The hit also arrives
carrying a ticker **SEC itself supplies**, so unlike every other candidate
path it never passes through name→ticker resolution and cannot land on the
wrong company that way. Each pass records which anchors it has covered
(`data/anchor_research.json`, `data/anchor_edgar_search.json`) so a run
continues through the list rather than re-searching the same first few
forever — selection is deterministic, so without that marker a daily
schedule would never reach the rest of the list.

A full-text hit produces **zero evidence**, and that is the argument rather
than a limitation: if the filer is already in the universe, `_poll_edgar`
has already fetched that 10-K and extracted from it; if it isn't, there is
no dossier to write to. A hit is a lead about *where to look*. It also never
increments `seen_count`, which gates auto-accept as a trade target and is
meant to count filing disclosures, not sightings of a name in a search index.

EFTS has no proximity operator (quoted phrases and implicit AND only), so
document-level AND over-matches — a 10-K can name the anchor in Item 1 and
say "of our net sales" forty pages later. A local regex proximity pass over
the fetched text decides which hits are real, and the candidate carries the
**raw sentence verbatim** rather than a verdict: an IDIQ ceiling, a
historical figure and a live concentration disclosure all match the same
phrases, and only the actual words tell them apart.

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

The name match against SEC's list allows a **prefix in either direction**,
because filing text rarely spells out a registered title — but only where
the difference is pure corporate-form boilerplate. That restriction is not
tidiness. `normalize_company_name` already strips the legal suffixes, so a
name surviving as a *single token* is a bare brand word, and an
unrestricted prefix match lets it claim any registered title starting with
it. Measured live: "PGIM, Inc." — named by a tradeable only as the
counterparty to a note purchase agreement — resolved to **GHY, a
closed-end bond fund**, and was auto-accepted as a tradeable equity. It is
the same mechanism behind the "Vertex" collision `dod_contracts.py` warns
about. So "asml" still matches "asml holding" (`holding` is boilerplate),
while "pgim" no longer matches "pgim high yield bond fund" and "vertex" no
longer matches "vertex aerospace" — those remainders are doing identifying
work. The rule can only ever *refuse* a match the old code accepted; it
never creates one.

**Candidates are auto-accepted by default** (`ENABLE_AUTO_ACCEPT_CANDIDATES`).
The engine already resolves a candidate's ticker, fetches its market cap and
analyst count, and computes a tradeable-vs-anchor recommendation from the
same bounds it screens existing members against -- so accepting by hand
applied exactly that recommendation, and the click was a veto rather than a
judgement. A candidate is not an arbitrary ticker that cleared a threshold:
it exists *because a tradeable company's own SEC filing disclosed a
relationship with it*, which is what makes it automatable at all (and why
`universe_screen.py`'s prune-only stance, which is about names with no
relationship evidence, doesn't apply here).

Anchors and tradeables are held to deliberately different bars:

- **Anchor -- liberal.** It can never become a trade (`signal_source_only`),
  so the worst case is some wasted LLM spend, while the upside is large: it
  turns a dead-end candidate into a live propagation source, which is the
  mechanism this whole strategy runs on.
- **Tradeable -- guarded.** It can produce signals and paper trades, so it
  additionally requires the resolved ticker's registered SEC name to actually
  match the disclosed counterparty name (`EdgarClient.name_matches_ticker`),
  plus repeat disclosure across filings (`AUTO_ACCEPT_MIN_SEEN_COUNT`). The
  name check exists because of a confirmed-live misresolution: a filing
  describing a partnership with *Advantest* was recorded against **ATRO**
  (Astronics), an unrelated aerospace company. An anchor mistake is cheap;
  auto-adding the wrong company as a trade target is not.

Bounded and reversible: at most `AUTO_ACCEPT_MAX_PER_DAY` per UTC day (so one
filing naming a long list of counterparties can't flood the universe), every
acceptance logged and webhook-alerted, and each recorded in
`data/accepted_candidates.json` marked `auto` -- shown as "added: tradeable
(auto)" on the dashboard, and undone by deleting the entry. Widening what's
watched can't by itself create a trade: a dossier still has to form and cross
the signal threshold on its own. Set `AUTO_ACCEPT_TRADEABLES=false` to keep
the guarded half manual while still auto-adding anchors.

You can also accept a candidate with one click on the dashboard -- "+
Tradeable" or "+ Anchor" -- and it's live immediately, no restart: EDGAR/news polling and
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

**`scripts/screen_candidates.py`** is the same market-cap/analyst-count
screening logic as a standalone review tool, for picking a batch of new
tradeable tickers rather than reviewing candidates one at a time on the
dashboard:

```bash
python scripts/screen_candidates.py                     # mines data/universe_candidates.json
python scripts/screen_candidates.py AEIS CEVA POWI       # or screen specific ideas instead
python scripts/screen_candidates.py --min-cap 50 --max-cap 2000 --max-analysts 4
```

By default it screens every resolved-ticker entry the engine has already
discovered, against tighter bounds than the live auto-screen's defaults
(`$100M-$3000M`, `<=6` analysts) -- deliberately tighter, since the point
of a universe refresh is genuinely thin-coverage small-caps, not names
that have already drifted toward the auto-screen's outer bound. Output is
a ranked table (thinnest coverage first) with an ecosystem guess (the
first already-classified company a candidate was discovered in relation
to) -- a starting point for review, never applied automatically; picking
the final list is still your call, same as accepting any other candidate.

### Graph maintenance: one button, one order

Maintenance had accreted into three daily passes, two operator buttons and a
connectivity reconcile with its own dry run — and between them they only ever
asked *is the graph big enough*. Nothing asked *is what we already have
correct*. An audit of the live board answered that: of eleven accepted
tradeables, only four were sound. `GHY` was a closed-end **bond fund**
recorded as "PGIM, Inc." off a note purchase agreement (a lender, the exact
class the extraction filters exist to drop), `TCPA` a junior subordinated note
due 2085, `SCE-PN` a preferred series, `SPWR` and `RJET` delisted shells. Each
was polled hourly and accrued LLM spend against a thesis that cannot exist.

**Graph maintenance** runs one fixed sequence, and the order *is* the argument
for the button — growing before cleaning re-admits the symbol you just
removed, and cleaning before ticker resolution acts on stale data:

| | | |
|---|---|---|
| 1 | **Audit** | Read-only. Every structural fault, most decisive first (`graph_audit.py`). |
| 2 | **Clean** | Quarantine the unfit — only with `apply`. |
| 3 | **Resolve** | Retry ticker resolution and re-screen, so growth sees current recommendations. |
| 4 | **Discover** | EDGAR full-text search — the only pass size-selected toward small counterparties. |
| 5 | **Connect** | The connectivity reconcile, last, acting on everything above produced. |

The audit checks eight things nothing else looked at: delisted tickers,
tradeables that are not common equity, names that no longer verify against
SEC's filer list, financing relationships wearing a supply-chain label,
self-edges, dangling endpoints, edges no filing has re-confirmed in months,
and candidate rows that collapse to one company (1,903 rows contain only 1,797
distinct normalized names — `seen_count` is split across spellings, and
`seen_count` is what gates tradeable auto-accept).

**Quarantine never deletes.** A removed symbol keeps its row and its reason in
`data/quarantined_symbols.json` and is restored by hand by deleting the row —
the same reasoning behind `_block_junk_candidates` marking rather than
deleting: a removal is a judgement, and a judgement you cannot see or reverse
is indistinguishable from data loss. It is also load-bearing rather than a
receipt, because auto-accept and the reconcile both consult it; without it the
next pass re-adds the symbol from the same candidate row and the clean undoes
itself on a schedule. **A symbol with an open paper trade is never touched** —
removing it would strand a position that could then never be marked out — and
neither is a curated `universe.py` anchor, since a runtime pass cannot durably
delete a code-seeded symbol.

The audit half is read-only, so it also runs **daily** on its own and surfaces
in the dashboard's Graph health panel with an alert on anything actionable. The
destructive half stays behind the button.

### Connector growth: the arm that points the other way

Every growth mechanism above adds **anchors**, and that is not a bias anyone
chose — it falls out of where relationships come from. Discovery is filing
extraction, and filing extraction runs *upward*: a small company must disclose
its big customers, because customer concentration is a material risk. So what a
tradeable's 10-K yields is more large names. Nothing ran the other way, and the
board showed it: **59 of 160 anchors carried no edge to any trade target**,
which makes them inert — their news resolves to zero analysis targets and is
discarded unread. Only 4 of 87 tradeable-screened candidates touched one of
them, and three of those four were misresolutions.

Neither existing arm can fix that. The connectivity reconcile's *grow* half
admits a candidate only when it lands connected to a current **tradeable** —
and it is right to, because admitting on anchor-only disclosure is precisely
what grew a 322-anchor universe with 221 inert members. Its *prune* half cannot
help either: 30 of the 59 are curated `universe.py` seeds it is forbidden to
touch.

**Connector growth** is the mirror: admit a candidate because it would connect
an **inert anchor**. It applies every guard the anchor arm does — resolved
ticker, not already a member, not quarantined, common equity, and a live check
that the disclosed name verifies against SEC's filer list — plus two of its
own. It will not lower the screen (a candidate must already carry a
`tradeable` recommendation from the live market-cap/analyst check, so a
mega-cap cannot slip in), and it only acts on `customer`/`supplier`
relationships, because two rivals do not move on each other's contract wins the
way a supplier does.

**Admission grants membership, not trading rights.** The lead is usually
web-sourced, and a web-sourced relationship must never fire a trade. So the
symbol arrives **on probation**: polled, analysed and propagated to, but
`_is_tradeable` returns false for it. It leaves probation on its own, in one of
two directions — its own 10-K discloses the relationship and it is promoted to
a real trade target, or nothing discloses it within
`CONNECTOR_PROBATION_DAYS` (30) and it is dropped back to being an ordinary
candidate. The graph edge is the referee, which is the standard every other
edge here is held to, and it arrives through the relationship backfill that
already runs.

That the arm admits some bad leads is the design working rather than a gap in
it. A dry run against the live board admits Oppenheimer for `ONDS` on a
*placement-agent* relationship — correctly resolved ticker, real disclosure,
useless link. It cannot trade, no filing will ever confirm it, and it reverts
in a month having cost some polling. **A wrong admission costs thirty days of
cheap work; a wrong trade does not come back.** The asymmetry is the point.

Bounded by `CONNECTOR_MAX_PER_DAY` (3) and `CONNECTOR_MAX_PROBATIONARY` (12),
both persisted so a restart cannot hand out a fresh day's worth. Open
probations are reported in diagnostics directly beneath the inert-anchor count
they are working down. Set `ENABLE_CONNECTOR_GROWTH=false` to switch the arm
off entirely.

## Entry timing: are we too late?

A signal firing (evidence crossed the confidence/magnitude/corroboration
bar) and a paper trade opening are deliberately two separate moments, so
time passes between "the evidence justified a position" and "we're about
to actually take it."

How much time is itself a setting, and marking open trades and confirming
an entry run on different clocks. Idle, the price feed polls every
`PRICE_POLL_INTERVAL_SEC` (6h -- a swing/position system needs nothing
tighter to mark positions to market). But while a `SIGNALED` dossier is
waiting on the entry gate, it polls every
`SIGNAL_ENTRY_POLL_INTERVAL_SEC` (15 min) instead. The first signal this
system ever fired never got a single entry evaluation: it fired
mid-session, the next poll was hours out and landed after the close, and
the daily decay pass expired it before an entry was ever attempted. The
tight cadence applies only while something is actually pending, so the
steady-state request rate is unchanged.

Entry needs a price, but NOT specifically IB. Prices resolve IB-first and
Finnhub-second (`engine._price_bar`), so a free Finnhub key alone runs the
full paper-trade loop. This matters more than it sounds: for a long time
the entry gate was the one code path with no fallback -- the drift baseline
and the daily forward-validation marks both already fell back to Finnhub,
while the gate that actually opens the trade did not, so an unreachable
Gateway silently blocked the system's only output.

Two guards then decide whether the entry happens:

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

Every one of these outcomes is written to `logs/decisions.jsonl` with the
numbers that caused it -- which gate failed, by how much, and the price at
the time -- and the diagnostics bundle prints one row per signal *episode*
with its outcome. "Why did this signal not become a trade" is the single
most important question this system can be asked, and answering it must
never require shell access to the host.

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

- **Daily spend budget** (`MAX_DAILY_USD`, default $3.30 -- about $100/month) and **daily call
  budget** (`MAX_DAILY_LLM_CALLS`, default 3000), both checked before every
  call. Either one exhausted defers further evidence (never discards it)
  until UTC midnight -- exactly the same "retry later" path as a transient
  API failure. The call cap alone used to be the whole budget, on the
  reasoning that per-call token sizes are bounded by this codebase's own
  prompts; that stopped being true once per-call cost spanned an order of
  magnitude across configurable models and adaptive thinking unbounded
  output tokens, so spend is now metered directly (`usage.py`, priced from
  `llm.MODEL_PRICES_PER_MTOK`, with an unknown model priced at the most
  expensive entry so a model-string typo never looks free). The call cap is
  kept because it bounds request VOLUME, which dollars do not. See the
  dashboard for actual calls/tokens/spend. Hitting the cap is not a failure
  mode -- it also spreads a relationship-extraction burst (the
  150k-char-per-call pass, which spikes whenever the universe grows) over
  several days instead of consuming a month of budget in an afternoon.

  Models are tiered by VALUE PER CALL, not by how important the call sounds
  (see `config.py`). The per-item dossier update and skeptic run on Haiku:
  they see ONE evidence item each, and no amount of model quality lets a
  per-item scorer answer the questions that decide whether a thesis is real
  -- overlap, coherence, staleness. The synthesis pass, which is the only
  one that *can* answer them, runs on Opus, once a day, and only for a
  dossier already near the signal bar. About a dozen expensive calls a day
  carry the reasoning budget for the whole system
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

Two further corroboration defenses (2026-07 follow-up): `dedup.py` also
collapses **lightly reworded** republishes (token-overlap near-duplicate
check, same symbol, same or previous UTC day) before they burn a scoring
call or count as a second source, and a dossier whose agreeing evidence is
**entirely news** needs `MIN_INDEPENDENT_SOURCES_NEWS_ONLY` (default 3)
distinct publishers instead of the normal bar -- heavily reworded wire copy
can still slip past any headline similarity check, but an SEC filing is a
primary disclosure that cannot be a rewording of a news article, so any
filing-corroborated dossier keeps the normal bar.

### What the elevated bar is, and is not, for

That elevated bar guards against exactly one failure mode: two outlets
rewording a single wire story into two apparent "sources". It is **not** a
general "be more certain" tax, and applying it as one actively fights this
system's premise.

The edge here is reading one high-quality fact about an anchor and
inferring the effect on a thinly-covered supplier *before the market
connects them*. Waiting for a third publisher to write that connection down
means waiting for the edge to disappear -- by the time three outlets have
published "DCO benefits from RTX's guidance raise", the price has moved.
Corroborating *"did the event happen"* is redundant when the event is an
official guidance raise or an 8-K. What needs corroborating is *"is the
causal link real"* -- and the relationship graph answers that from primary
sources.

So a dossier also keeps the normal bar when its agreeing evidence arrived
over a **strongly disclosed relationship edge**
(`dossier.DISCLOSED_LINK_CONFIDENCE`, 0.85): a customer/supplier link a
10-K states outright, usually with a quantified share of revenue attached.
The live graph separates cleanly at that line -- the quantified
concentration disclosures sit at 0.85-0.98:

```
ULH  -> GM   customer  conf=0.95   GM is ULH's top customer, ~25% of revenues
THRM -> GM   customer  conf=0.95   GM accounted for 12% of product revenues
DCO  -> LMT  customer  conf=0.95   a major customer with significant revenue
PUMP -> XOM  customer  conf=0.95   ~24.9% of revenue
```

while speculative or passing-mention links sit at 0.30-0.65 (`FDX -> GOOGL`
"Google Drive is integrated with FedEx Office", an indirect JV competitor)
and correctly leave the elevated bar in place -- those are precisely the
causal claims that *do* need more corroboration.

Backing relaxes the elevated bar back to the normal one; it never drops
below `MIN_INDEPENDENT_SOURCES`, so a single uncorroborated article can
still never fire a signal.

**A competitor edge does not grant backing.** The relaxation exists because
a filing that states a link answers *"is the causal channel real"* from a
primary source. A competitor disclosure does not answer that question:
"KLA and Applied Materials name each other as competitors" is genuinely
disclosed and is not a transmission channel the way a supply relationship
is -- the news does not have to reach one through the other, and its SIGN
is frequently inverted (a competitor's capacity loss is *good* news here,
which is how the Tier 2 catalyst rubric already lists it). This matters
because of the shape of the live graph: `competitor` is the largest edge
class at **448 of 1066 edges**, and **375 of those sit at or above 0.85** --
an 84% clearance rate against 75% for customer and 76% for supplier. The
most numerous and most sign-ambiguous class was relaxing the bar more often
than the channels that actually carry causation. Competitor evidence still
propagates, still contributes mass, and still claims an independent source
slot; it just stops buying the discount
(`dossier.COMPETITOR_SATISFIES_DISCLOSED_LINK`).

### Ecosystem associations get one collective slot

Evidence arriving over a mere sector-membership association
(`ECOSYSTEM_ASSOCIATION_CONFIDENCE`, 0.25 -- not a disclosed counterparty
link) used to contribute mass but **no** independent source slot at all,
to stop a correlated macro story fanned in from several anchors counting as
several corroborations. That stopped the saturation bug and overshot: the
stated design intent is that an ecosystem link *"can raise a thesis but can
never single-handedly qualify one"*, and zero slots implements "cannot
raise a thesis at all".

The whole class now collapses to exactly **one** collective slot, keyed on
the ecosystem rather than on the item, the origin, the publisher or the
day. The accounting is `min(1, |eco|)` and is therefore constant in volume
by construction -- thirty correlated macro items contribute exactly one
slot, same as one does -- and one slot is still below
`MIN_INDEPENDENT_SOURCES`, so it cannot qualify a dossier on its own. The
tempting variant of one slot per distinct origin company was rejected:
NVDA + AMAT + LRCX reporting a single capex story would be three origins
and three slots, which is the saturation bug rebuilt with extra steps.

This matters most for names with no graph edges at all. Thirteen universe
symbols appear in neither endpoint of any of the 1066 edges (PLAB, INTT,
KLIC, AEHR, PLPC, LMB, AGX, NVX, MVST, SLI, MLAB, HURC, CVLG), so the
ecosystem edge is their *only* propagation path and it was worth nothing.

This was found live. DCO sat at 17 agreeing evidence items, decay-weighted
mass 8.88, **zero opposing**, a score 44% above threshold, over 0.85-0.95
10-K-disclosed links to RTX/LMT/NOC -- and could not act, for want of a
third journalist.

## A synthesis verdict has to be falsifiable

The daily whole-body pass can veto a thesis outright by declaring it
`already_priced_in` -- the whole strategy is trading the lag *before* the
market connects the dots, so a move that is over is not one to enter.

That verdict asserts something testable, and until `SCORING_VERSION` 6
nothing in the system could test it. No price was ever compared against it,
and the evidence body it judged was never recorded, so the veto was
re-asserted every day against whatever had arrived since and **the only
thing that could overturn it was another copy of itself**. Live, 27 dossiers
sat at exactly 0.000 on a verdict nothing could contradict.

This is not architectural stickiness -- `_decay_one` calls `recompute_decay`
first, which rebuilds the score from raw evidence and erases the previous
day's zeroes, so the escape hatch has always existed. It just never opened,
because the verdict never changed.

So a verdict now records its own premises: the price at the moment it was
rendered, the independence keys it read, and the arithmetic score it capped.
On the merge path, two things invalidate it:

- **The tape refutes it.** A move of 8%+ *in the thesis direction* since the
  verdict says the market had not, in fact, absorbed this.
- **The evidence body materially changed.** Two or more new independent
  source slots since the verdict mean the whole-body pass never saw what it
  is now capping.

The invalidation counts **slots, not items**, which is what makes it
ungameable: an ecosystem fan-out of thirty correlated macro items mints one
slot no matter how many arrive, and three wire rewrites of one story mint
one (with dedup dropping two before they arrive). Nothing that cannot move
the signal bar can invalidate a verdict.

**A refuted verdict is re-judged, never simply lifted.** This is the part
worth being precise about, because lifting the cap is the tempting version
and it is wrong twice over. A favourable move on a LONG means the price went
*up*, so the entry is more expensive, not less -- and the entry gate measures
drift from the still-earlier inception baseline, so the very moves large
enough to refute a verdict are the ones it then refuses at 12%. Lifting the
cap would also fail *open*: the thesis re-fires on the raw arithmetic that
`_cap_with_synthesis` exists to correct. So the old verdict stays in force
until a fresh whole-body pass replaces it, and every path that cannot produce
one -- no price, no budget, below the synthesis floor -- leaves it standing.
Off-schedule re-synthesis is capped at 5 calls/day (~$0.50) so a heavy merge
day cannot starve the daily pass.

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

  The row also carries what the score *means*: the synthesis verdict behind
  it, the **pre-veto arithmetic score**, the bar the dossier was actually
  held to (`min_sources_required` plus the two backing flags that decide
  it), and a flag per scoring mechanism. The pre-veto score matters more
  than it sounds -- a vetoed row used to record `0.000` for both the raw and
  the capped number, so a thesis the whole-body pass rated 0.9-but-priced-in
  was indistinguishable from one it rated 0.05, permanently, for the most
  expensive pass in the system.
- **`logs/decisions.jsonl`** -- now also records a `below_bar` row for every
  dossier the signal gate refused on the daily pass, and `below_bar_on_merge`
  when fresh evidence still failed to clear it, each with the numbers
  (`_below_bar_reason`). The reason a dossier did *not* qualify used to be
  computed at exactly one moment -- expiry -- and discarded everywhere else,
  so "which gate is binding, on how many names, by how much" had no data
  behind it at all.
- **`logs/price_marks.jsonl`** -- a daily closing-ish price for every
  tradeable (non-anchor) universe symbol, piggybacked on the existing 6h IB
  price poll. Written by `engine.py`'s `_run_daily_price_marks`.

Both passes are scheduled off a PERSISTED wall-clock timestamp
(`engine.py`'s `_daily_pass_due`/`periodic_pass_state.json`), not a
process-local timer -- a process-local marker resets to "due immediately"
on every restart, and since both passes unconditionally append a fresh row
per symbol, several restarts in one day used to silently write a full
duplicate batch on every restart (confirmed live: 6x on one day). Rows
captured before this fix already have the duplicates baked in;
`scripts/analyze_forward_returns.py` dedupes on (symbol, date) before
analyzing, so old logs still produce a correct result.

- **`logs/decisions.jsonl`** -- what the engine DID with each signal
  episode: `trade_opened`, `drift_skip` (once per episode), or
  `signal_expired`, each with the price at decision time when one was in
  hand and the same episode key (`signaled_at`) that `signals.jsonl` rows
  carry. Written by `engine.py` via `signals.log_decision`. Without this,
  skips and expiries survived only as log lines and the entry-timing
  guards were unfalsifiable.

**`scripts/analyze_signal_events.py`** (also the dashboard's "Signal event
study" button) joins signals + decisions + price marks and reports each
outcome group's forward return from the fire date -- the entry-timing
guards' scorecard: a drift-skipped episode whose move kept going is a
trade the guard cost; one that mean-reverted is a chase it saved.

**`scripts/analyze_forward_returns.py`** joins the two by symbol/date and
answers the actual question: does `confidence * magnitude` predict what
the market does next?

```bash
python scripts/analyze_forward_returns.py                       # default 5- and 20-day horizons
python scripts/analyze_forward_returns.py --horizons 5,10,20
```

Pure offline analysis (no network, no engine dependency) of the two logs
above. For each horizon it reports: mean forward return by score bucket
(`<0.2`, `0.2-0.35`, `0.35-0.5`, `>=0.5`) and whether it's monotonic; the
correlation between score and signed forward return; overall hit-rate (%
of theses where the direction was right); a per-symbol breakdown, worst
first; and a benchmark-relative variant -- each return minus its own
ecosystem's mean RAW return over the same window, sign-matched to the
row's own direction (a LONG compares against the ecosystem's raw return
directly, a SHORT against its negation). The ecosystem benchmark is built
from every symbol `price_marks.jsonl` tracks (ecosystem tags from
`universe.py`), not just symbols that happen to have a dossier -- using
only dossier-having symbols made a single-dossier ecosystem's "benchmark"
trivially equal to its own return, zeroing out alpha by construction
rather than measuring anything. Separates alpha (the pick itself) from
sector beta (the whole ecosystem moved). Forward return is always signed
in the THESIS
direction (LONG: price up is a win; SHORT: price down is a win), so a
positive number always means "right so far" regardless of direction. Only
as good as how long the capture above has actually been running -- forward
data can't be backfilled, so an early run on a fresh deployment will
mostly say "not enough data yet," which is the correct answer, not a bug.
See `forward_returns.py` for the join/aggregation math (network-free and
unit tested on synthetic rows) and a counterfactual ledger for signals the
confidence threshold skipped is still a possible future addition.

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
4. Optional, for broker-quality bars: `ENABLE_IB_PRICE_FEED=true`
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
A **Tools** panel at the top runs the two analyses that previously needed a
terminal on the Home Assistant host: **Screen candidates** (market-cap /
analyst-coverage screen of specific tickers, or of every discovered
candidate, with editable bounds), **Forward-return report** (does
`confidence x magnitude` predict forward returns -- bucket table,
correlation, hit rate, sector-relative alpha, per-symbol breakdown), and
**Diagnostics bundle** -- one pasteable snapshot of runtime state:
integrations, universe, graph, every dossier's score, **where evidence is
actually coming from**, spend, signals, trades, candidates, capture-log
coverage, and recent warnings/errors. That evidence-source breakdown is the
one that earns its keep: it is what exposes a collapsed source identity (every
article attributed to a single name), which makes `independent_source_count`
structurally unable to exceed 1 and silently blocks every signal. Credentials
and personal data are omitted by an allow-list and log lines are scrubbed, so
the bundle is safe to paste. Both are
read-only: one does market-data lookups, the other reads the captured
snapshot/price logs. Neither changes a dossier, the universe, or any trade,
and one runs at a time so a screen can't outrun Finnhub's rate limit
alongside the engine's own polling.

Open `http://localhost:8100/` (or `DASHBOARD_PORT`). At the top is a
**Coverage** panel -- how much of the *tradeable* universe is actually live,
which is a different question from how many symbols are configured:

- **Tradeables with a dossier**: how many trade targets have a thesis
  accumulating at all. The headline number. Far below the tradeable count
  means most of the universe is dark, not that the market is quiet.
- **Tradeables connected to the graph**: an unconnected tradeable can never
  receive an anchor's news. It can only build a dossier from its own
  coverage -- and these names are selected for having almost none, so in
  practice it never will.
- **Anchors linked to a tradeable**: an anchor is never its own analysis
  target, so one with no such link is inert by construction. Its news
  resolves to zero targets in `_process_evidence` and is fingerprinted and
  discarded unread, however much of it arrives.

Those three together are what explains a gap between ingestion volume and
signal output. Measured live on 2026-07-29, before the edge-promotion fix:
16 dossiers against 48 tradeables, 20 of 48 tradeables connected, and only
26 of 130 anchors live -- 10,373 dedup fingerprints against 52 LLM calls a
day, with the loudest names in the universe (NVDA, MSFT, AMZN, GOOGL, TSM,
AMAT...) fetched hourly and binned unread.

Below that: which integrations are
enabled, the relationship graph (grouped by filer -- each company's own
disclosed customers/suppliers/competitors/regulators together, strongest
confidence first, rather than one flat table sorted by extraction order),
every company's dossier, open/closed hypothetical trades with win rate and
average R, recent signals, today's LLM call/token usage against the daily
budget, and discovered universe candidates. Auto-refreshes every 10
seconds. Never places orders.

"Dossiers by conviction" ranks the top names by `confidence x magnitude`
against a vertical rule at the 0.50 signal bar, so which ones actually fire
is readable at a glance rather than inferred. Clicking a row -- there or in
the all-dossiers table -- opens that dossier: the score and its components,
mass agreeing vs opposing, what the whole-body synthesis pass did to it,
and the individual evidence items behind it, each with its source, its own
direction/confidence/magnitude, the skeptic's note, and (for anything that
arrived through the graph) which company it was actually about and via
which relationship. Served by `GET /api/dossier/<symbol>` on click rather
than on the refresh cycle -- evidence is the largest thing the system
stores, and it would otherwise be re-sent every 10 seconds for every
dossier to fill a panel nobody has opened.

Almost entirely read-only, except one endpoint: `POST /api/candidates/accept`
adds a discovered candidate into the live universe (the dashboard's "+
Tradeable"/"+ Anchor" buttons) -- bounded to symbols the extraction
pipeline itself already surfaced, never an arbitrary ticker, and it can
only widen what's watched, never place an order or directly create a
trade. See "Bring your own universe" above.

Every state-changing endpoint (that one, the reset/rebuild controls, and
the read-only tool runs) requires a custom `X-SmartBoi-Request` header that
the dashboard's own JS attaches to every POST. A browser won't add a
non-safelisted header to a cross-origin request without a CORS preflight,
which the server answers for no origin, so a page the operator merely
visits can't drive these endpoints -- even the ones (reset, rebuild) that
take no body and would otherwise accept a plain cross-origin form POST. The
dashboard binds all interfaces with no auth of its own, which is exactly
what makes that guard matter. `GET` is left open: those are pure reads, and
the page's own 10-second auto-refresh sends no such header.

## Observability

A `heartbeat: universe=N dossiers=N signaled=N graph_edges=N ...` INFO line
logs roughly every 10 minutes (`engine.py`'s `_log_heartbeat`) regardless
of whether anything actually happened that cycle -- ingestion at this
system's polling cadences (hourly EDGAR/news, 6h prices) can leave the log
quiet for long stretches, and without a heartbeat there was no way to tell
an idle-but-healthy engine from a hung one. `ib_async` (the IB Gateway
client library) logs routine Gateway connectivity blips and account-
summary noise (error codes 1100/1102/2104/2106/2158/322) at ERROR even
though they don't affect price marks -- confirmed live, these drown out
anything that's an actual SmartBoi problem. Its logger level is raised to
WARNING alongside `httpx`/`httpcore`/`aiohttp.access` (quiets its INFO-
level connection chatter), but that alone can't drop these -- ERROR is
ABOVE WARNING in severity, so it still passes a level filter. A
`logging.Filter` on the specific benign codes is what actually removes
them, without raising the whole logger to CRITICAL and hiding a genuine
IB failure along with the noise. See `logging_setup.py`'s
`_IbBenignErrorFilter`.

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

Transaction costs are market-cap-bucketed per trade (50bp/side above $1B,
150bp $300M-$1B, 300bp below, middle bucket when no cap source is
reachable; `TRANSACTION_COST_BPS_PER_SIDE` acts as a floor under all
buckets) -- a flat figure understated friction exactly where this strategy
hunts. SHORTs are flagged `assumes_borrow` when a real account might not
have located shares (a fill it could not have made is not a fill), and the
dashboard reports avg R with and without them. The flag now prefers an
**observable** over a proxy: FINRA/Nasdaq's Reg SHO threshold securities
list (free, no auth, one text file per settlement day) names securities
with persistent failures to deliver, which is the closest thing to a public
daily statement that a name is genuinely hard to borrow. Presence on the
list is decisive; absence is *not*, and falls back to the old sub-$500M/
unknown-cap rule -- the list names securities already failing to deliver,
a subset of what is hard to borrow, so a thin micro-cap can be unborrowable
with nobody having failed on it. The flag can therefore only get stricter
than it was, never looser. Set `ENABLE_REGSHO=false` to skip the fetch.
