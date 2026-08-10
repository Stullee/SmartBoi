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

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

from smartboi.universe import DEFAULT_UNIVERSE, CompanySpec, build_universe


# The trade-governing parameters -- the ones that actually define a strategy
# "generation". A change to any of them starts a new generation whose forward
# record must be measured separately: pooling the old regime's win rate with
# the new one measures two different strategies as a single number, which is
# exactly the contamination the generation split exists to remove. Cosmetic
# things (the app version, the display label) are deliberately excluded, so a
# dashboard-only release cannot fork the record -- only a real rule change can.
_STRATEGY_PARAM_KEYS = (
    "stop_loss_pct",
    "take_profit_pct",
    "signal_confidence_threshold",
    "transaction_cost_profile",
    "max_favorable_drift_pct",
    "max_horizon_days",
)


def strategy_key(strategy: dict | None) -> str:
    """Stable grouping key for a stamped strategy signature -- the governing
    params only, in a fixed order. None/empty (a trade opened before strategy
    stamping existed) maps to "" so those pool into one clearly-labelled
    'legacy' generation rather than each appearing as its own."""
    if not strategy:
        return ""
    return "|".join(f"{k}={strategy.get(k)}" for k in _STRATEGY_PARAM_KEYS)


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
    # Whether the backfill also reads ANCHOR 10-Ks. On by default: regular
    # polling only reads filings inside edgar_lookback_days and 10-Ks are
    # annual, so with this off no anchor's 10-K is ever read until it
    # happens to file inside a 14-day window -- which is why 98-134 of 161
    # anchors were measured live with no graph edge to any tradeable, and an
    # anchor without one is inert (its news resolves to zero targets and is
    # discarded unread). Expect a lower yield per call than the tradeable
    # side: an anchor's customer-concentration disclosures name big
    # customers, not small suppliers. What they do name is single-source
    # supplier risk factors, JV partners and competitors -- the edges that
    # make an inert anchor live. Tradeables are always processed first.
    backfill_anchors: bool = True
    # SEC requires a descriptive User-Agent with real contact info on every
    # request ("Your Name your@email.com") or it will block/rate-limit --
    # not a secret, just informational, but required for EDGAR ingestion to
    # actually work. Left empty, EDGAR ingestion logs a warning and skips.
    edgar_user_agent: str = ""
    # 10-Q added alongside 10-K: quarterly filings also disclose customer/
    # supplier changes and are run through the same relationship
    # extraction (see engine.py), keeping the graph fresher between annual
    # 10-Ks instead of only updating once a year.
    # --- Graph maintenance ---------------------------------------------
    # The relationship graph IS the strategy: an edge is the only path by
    # which an anchor's news reaches a tradeable, so a missing edge is a
    # trade that never happens. Two structural problems made the graph decay
    # rather than improve, both fixed by the passes these settings drive:
    #
    # 1. The initial backfill reads each symbol's latest 10-K exactly ONCE,
    #    ever. Extraction only writes an edge when the counterparty is
    #    ALREADY in the universe -- otherwise it records a watchlist
    #    candidate and moves on. So every symbol read while the universe was
    #    smaller left permanent holes: counterparties accepted later are
    #    named in a filing that is never re-read. Measured live: tradeables
    #    carrying a full thesis with no graph edge at all, i.e. names whose
    #    dossier came entirely from their own filings and which therefore
    #    never used the cross-company mechanism this system exists for.
    # 2. Nothing re-read a filing on any schedule. The only refresh was a
    #    dashboard button a human had to remember to press.
    #
    # This re-extracts the N least-recently-extracted symbols per day
    # (anchors included -- an anchor with no edge to a tradeable is inert,
    # so it has the most to gain), clearing their backfill marker so the
    # existing backfill pass picks them up on the next tick. graph.add
    # dedupes on (from, to, rel_type), so a refresh can only ever ADD an
    # edge, never remove one -- and the daily LLM budget defers the rest to
    # tomorrow exactly as it does for the first-run backfill.
    #
    # At 10/day a ~300-symbol universe fully re-extracts about monthly,
    # which is the right order: 10-Ks are annual, so the value of re-reading
    # is not fresher filing text but a filing re-read against a LARGER
    # universe. Set to 0 (or disable) to go back to once-ever + the button.
    enable_graph_refresh: bool = True
    graph_refresh_symbols_per_day: int = 10
    # Runs the web-search supplier research automatically, most-inert anchors
    # first, instead of only when someone presses the dashboard button.
    #
    # This is the ONLY mechanism that can find the small-cap suppliers of a
    # giant: the filing path structurally cannot: a giant's 10-K names its
    # big CUSTOMERS, not its small suppliers, so an anchor's supplier list is
    # information SEC filings never contain in the direction this strategy
    # needs. Left manual, it simply never ran (measured live: $0.00 spent
    # against a reserved 10-20% research budget share, i.e. an entire class
    # of edge permanently missing).
    #
    # It writes universe CANDIDATES only -- never a relationship edge and
    # never a trade -- because a web-sourced link is a lead, not a
    # disclosure; accepting one backfills its own 10-K, which is where a real
    # edge comes from. That is why this is safe to run unattended.
    # Already-researched anchors are skipped, so the pass works through the
    # anchor list and then costs nothing until new anchors are added.
    enable_auto_supplier_research: bool = True
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
    #   NT 10-K / NT 10-Q -- a Rule 12b-25 late-filing notice, one of the
    #     cleanest small-cap SHORT catalysts there is, and it skews toward
    #     exactly this universe (large caps rarely file one). The reaction is
    #     conditional on the REASON given -- boilerplate reads negative, a
    #     specific legitimate cause does not -- which is a judgement about
    #     prose, i.e. the thing an LLM reading the filing is actually good at.
    #   S-1 / S-3 / S-3/A -- the shelf REGISTRATION, weeks to months ahead of
    #     the 424B5 takedown already ingested. This adds the leading indicator
    #     to the confirming one on the cleanest SHORT catalyst above.
    #   20-F / 40-F / 6-K -- foreign private issuers. This closes a hole
    #     rather than adding a nicety: NVX files 20-F/6-K and NOTHING else,
    #     so its dossier could never receive a single filing-evidence item
    #     and its annual report could never mint a graph edge -- while being
    #     a tradeable, and one of only two names still passing the
    #     thin-coverage screen. Verified against the live graph: NVX appears
    #     in neither endpoint of any of the 1066 edges, so with no filings it
    #     had no evidence path at all. Same for anchors CAMT, TSM, ASML, MGA;
    #     TSMC files monthly net revenue on a 6-K, a primary quantitative
    #     datapoint for the whole semi_equipment ecosystem.
    #   Form 25 / 25-NSE / 15-12B / 15-12G -- defensive. _is_unknown_to_edgar
    #     only prunes a symbol once SEC's ticker file drops it, which lags the
    #     delisting; a Form 25 is ten days' notice.
    edgar_forms: str = (
        "8-K,8-K/A,10-K,10-Q,4,SC 13D,SC 13D/A,424B5,424B3,"
        "NT 10-K,NT 10-Q,S-1,S-3,S-3/A,20-F,40-F,6-K,"
        "25,25-NSE,15-12B,15-12G"
    )
    # Reg SHO threshold securities list (FINRA/Nasdaq, free, no auth): the
    # OBSERVABLE behind paper_journal.assumes_borrow, replacing a market-cap
    # proxy for whether a hypothetical SHORT could have located shares. One
    # plain-text file per trading day. Off only if the fetch is unwanted --
    # with it off the borrow flag simply falls back to the proxy.
    enable_regsho: bool = True
    # Federal Register (free, no API key): regulatory actions that name this
    # universe's companies -- wind-tower AD/CVD for BWEN, EPA HFC allowance
    # allocations for HDSN, BIS Entity List for AOSL, export controls and
    # FMVSS for two ecosystems.
    #
    # Scoped to a handful of HAND-CURATED searches, never the feed:
    # ~200 documents publish per business day and watching that broadly is a
    # disqualifying firehose. See federal_register.CURATED_SEARCHES; adding a
    # search is a deliberate act, not a setting.
    enable_federal_register: bool = True
    # Deliberately longer than the poll gap. The window overlaps and the
    # caller dedupes on document_number, so the cost of overlap is a cheap
    # repeat request while the cost of a gap is a permanently missed rule.
    federal_register_lookback_days: int = 3
    federal_register_poll_interval_sec: int = 21600
    # DoD daily contract announcements (war.gov, free, no auth): every award
    # at or above the DFARS 205.303 threshold, published ~5pm ET each business
    # day. Chosen over USASpending/FPDS because DoD awards are withheld from
    # those for 90 days -- ~6x past dossier.evidence_is_stale's 14-day floor,
    # so that evidence would be born aged out.
    #
    # OFF BY DEFAULT: war.gov is unreachable to any automated client. Measured
    # live 2026-08-10 -- see dod_contracts.py's "WHY THIS IS OFF" section for
    # the full transcript. In short: Akamai returns 403 for every HTML path
    # (both the listing and individual articles), the RSS feed is the only
    # open endpoint, and its description field is a fixed boilerplate sentence
    # carrying no award text and no company names. There is no route to the
    # data that does not involve defeating a bot manager, and the alternatives
    # (USASpending / FPDS / SAM) all sit behind DoD's 90-day publication hold,
    # ~6x past evidence_is_stale's floor, so their evidence is born aged out.
    #
    # Off pending a working FETCH ROUTE, not abandoned: reading the article
    # bodies from the Wayback Machine is still open (a public archive with a
    # documented API, no bot manager, and a 1-3 day lag that sits comfortably
    # inside the 14-day staleness floor the 90-day APIs failed). The parsing
    # module and its tests are kept and still pass -- the alias table, value
    # floor and verbatim pass-through are correct and cost nothing dormant, so
    # the day a route exists this is a fetch-layer change and a settings flip.
    enable_dod_contracts: bool = False
    dod_lookback_days: int = 3
    dod_poll_interval_sec: int = 21600
    # Awards below this are not scored for an ANCHOR. LMT/RTX/NOC/GD/BA appear
    # most business days and their routine awards would dominate the
    # propagation budget while saying nothing a thesis can use. No floor
    # applies to a tradeable's own award: $12M is material to a $90M-cap
    # company in a way the same award to Lockheed is not.
    dod_anchor_value_floor_usd: float = 100_000_000.0
    # Per-symbol daily ceiling on 6-K ingestion.
    #
    # 6-K is the one form added above that arrives at a cadence capable of
    # manufacturing corroboration. Direct EDGAR evidence keys independence on
    # form AND filing DAY (dossier.independence_key), which is correct for
    # 8-K/10-Q but wrong for a cross-filer that pushes 30-60 6-Ks a year:
    # each one would mint a fresh independent source slot. An ASX company
    # dual-filing routine announcements could corroborate itself into a
    # signal. Capped rather than excluded because the 6-K is where a foreign
    # issuer's material news actually lives (TSMC's monthly revenue).
    max_6k_items_per_symbol_per_day: int = 1
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
    # Models are tiered by VALUE PER CALL against a real monthly budget
    # (max_daily_usd), not by how important the call sounds.
    #
    # The instinct is to put the strongest model on the passes that gate a
    # trade -- the per-item dossier update and the skeptic. That is the wrong
    # trade, for a structural reason rather than a cost one: those two passes
    # see ONE evidence item each, and no amount of model quality lets a
    # per-item scorer answer the questions that actually decide whether a
    # thesis is real. Are these ten items ten facts, or one fact counted ten
    # times? Do they cohere, or are they unrelated coincidences pointing the
    # same way? Has the market already connected them? A per-item call cannot
    # see any of that, and the aggregate over those calls is pure arithmetic.
    # Buying a smarter classifier buys a better guess at a question that was
    # never the hard one.
    #
    # So the money goes where reasoning converts into a better decision:
    #
    # extraction (haiku): by far the largest input-token consumer -- a
    #   150k-char filing per call -- and a reading task, already backstopped
    #   by engine.py's name-verification and biography/lender filters. Paying
    #   a premium per token here is paying it on the cheapest kind of work.
    # dossier + skeptic (haiku): high volume, two calls per evidence item.
    #   These are triage -- is this new, which direction, roughly how big, is
    #   it refutable. The catalyst rubric now in their prompts (see
    #   dossier._CATALYST_RUBRIC) is exactly the scaffolding a small model
    #   uses well: it replaces judgement with a lookup.
    # synthesis (opus): once a day, only for a dossier near the signal bar,
    #   reading the COMPLETE evidence file. Low volume, and the only pass
    #   that can see overlap, coherence and staleness. Its verdict caps the
    #   score, so this is the call that decides whether accumulated evidence
    #   becomes a position.
    #
    # Roughly a dozen expensive calls a day instead of several hundred, on
    # the one pass where thinking changes the answer. Changing any of these
    # is safe -- llm.py builds the model-appropriate request shape, so a
    # model that rejects `temperature` or needs `budget_tokens` is handled
    # rather than 400-ing into a silent retry loop. Note that changing them
    # resets the forward-testing clock (see _check_model_provenance and
    # EvidenceRecord.scored_by_model), which is deliberate and warned about.
    extraction_model: str = "claude-haiku-4-5"
    dossier_model: str = "claude-haiku-4-5"
    skeptic_model: str = "claude-haiku-4-5"
    synthesis_model: str = "claude-opus-5"
    # Synthesis only runs on a dossier whose arithmetic score is at least
    # this fraction of the signal threshold. It is a CAP -- it can veto and
    # trim, never lift -- so on a dossier far below the bar the only possible
    # outcomes are "no change" and "further below the bar", neither of which
    # changes a decision and both of which cost an Opus call. Restricting it
    # to dossiers near a decision is what keeps the expensive pass at a
    # handful of calls a day as the universe grows from 17 dossiers toward
    # 48, instead of scaling linearly with the watchlist.
    synthesis_score_floor_pct: float = 0.6

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
    # $3.30/day is ~$100/month. Deliberately close to what the previous
    # all-Haiku configuration actually cost (~$4/day measured live), because
    # the tiering above spends the same order of money in a different place
    # rather than spending more of it.
    #
    # This is a HARD ceiling, and hitting it is not a failure mode: evidence
    # is deferred, never discarded, and picked up when the budget resets at
    # UTC midnight -- the same path as a transient API error. That makes the
    # cap do something useful beyond bounding the bill: a burst of
    # relationship extraction (the 150k-char-per-call pass, which spikes
    # whenever the universe grows or a backfill is queued) spreads itself
    # over several days instead of consuming a month of budget in an
    # afternoon.
    max_daily_usd: float = 3.30
    # --- Per-category shares of the daily budget (see usage.py) ---
    # One shared pool is first-come-first-served, and arrival order has
    # nothing to do with value. The budget resets at UTC midnight = 20:00 ET,
    # just after the US close, which hands relationship extraction thirteen
    # and a half hours of night to spend the whole day before the market ever
    # opens. Measured live: exhausted before 09:30 ET, so the dossier pass --
    # the only thing that can turn news into a position, and the one whose
    # input decays -- got nothing on the day it mattered.
    #
    # Each value is the MAXIMUM FRACTION of both daily caps that category may
    # consume. 1.0 is uncapped; 0.0 switches the category off entirely (useful
    # for anything you don't want running unsupervised).
    #
    # The DOSSIER category (updater + skeptic) is deliberately absent and
    # therefore uncapped: it is guaranteed whatever the three below cannot
    # reach (>=30% at these values) and can use the entire day when they are
    # idle. Caps rather than fixed partitions for exactly that reason -- this
    # system's inputs are bursty, and a partition would idle a third of the
    # budget on a day with no filings while the pass that matters starves.
    budget_share_extraction: float = 0.35
    budget_share_synthesis: float = 0.25
    budget_share_research: float = 0.10
    # A RESERVATION, not a ceiling -- the fraction of the day no other
    # category may consume until synthesis has had its chance at it.
    #
    # The ceilings above turned out to protect nothing: the total-budget
    # check runs first, so once dossier had spent the day every other
    # category was refused however much of its own share was untouched.
    # Measured live after a week on a $10 day: dossier $6.47, extraction
    # $3.54, synthesis $0.00 against a $2.50 ceiling -- and the one pass that
    # judges whether N pieces of evidence are N facts or one fact N times had
    # therefore never run, ever.
    #
    # Timing is why a ceiling could never have worked. The daily decay pass
    # is synthesis's only caller and is scheduled off a persisted wall clock,
    # so whichever hour it first ran at becomes its slot permanently; land
    # late in the UTC day and the budget is always already gone. Set to 0 to
    # go back to a pure ceiling.
    budget_reserve_synthesis: float = 0.15
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
    signal_confidence_threshold: float = 0.5
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
    # Which cost table the per-market-cap buckets come from (see
    # paper_journal.COST_PROFILES). "institutional" assumes an order large
    # enough to move a thin book -- 50/150/300 bps per side by cap bucket.
    # "retail" assumes a position small enough that impact is negligible and
    # the cost is the half-spread plus commission -- 15/35/75 bps per side.
    #
    # This is the single most consequential number in the paper record and
    # it is NOT a modelling detail: on the 8%/16% grid, the sub-$300M
    # institutional bucket (600bp round trip) turns a nominal 2:1 into
    # +1.19R/-1.72R, needing a 59% hit rate to break even; the same trade on
    # the retail table (150bp) is +1.71R/-1.26R, needing 42%. Both are
    # printed per bucket in diagnostics so the assumption is never invisible.
    #
    # Defaults to institutional deliberately. An over-stated cost makes a
    # real edge look smaller -- annoying but recoverable, since the journal
    # records r_multiple_gross alongside the net figure. An under-stated one
    # manufactures an edge that was never there, and there is no way to
    # recover from having believed it. Only move this to "retail" if the
    # position size the record is meant to represent genuinely cannot move
    # the book.
    transaction_cost_profile: str = "retail"

    # --- Entry timing: "have we missed the correction already" guards.
    # Applied when a SIGNALED dossier is about to become a paper trade
    # (engine.py's _try_open_from_signal) -- neither has any effect without
    # enable_ib_price_feed, since there's no price to check against yet. ---
    # Skip opening if the price has already moved this many percent in the
    # signal's favorable direction since it fired -- the correction likely
    # already happened between signal and entry (e.g. price_poll_interval_sec
    # gaps, or IB being briefly unreachable) and entering now would be
    # chasing a move that's largely over, not capturing it.
    max_favorable_drift_pct: float = 12.0
    # If a signal sits unopened this many days (drift-blocked every poll, or
    # no reachable price feed) it's expired back to ACTIVE instead of being
    # stuck forever waiting to chase an increasingly stale opportunity --
    # fresh evidence can re-signal it later with a clean baseline.
    signal_entry_deadline_days: int = 5

    # --- Paper trade journal (percentage-based stop/target -- this system
    # has no intraday bar/ATR data at a weeks-long holding horizon) ---
    stop_loss_pct: float = 50.0
    take_profit_pct: float = 100.0
    # A human-readable name for the CURRENT strategy generation, shown on the
    # dashboard's strategy-record panel (e.g. "hold-to-horizon"). Display only:
    # it is stamped on each paper trade at open but never affects which
    # generation a trade belongs to -- that is decided purely by the trade-
    # governing parameters above (see strategy_signature / strategy_key), so
    # renaming the strategy does not fork its forward record.
    strategy_label: str = "hold-to-horizon"

    # --- Account model: turns the abstract R-multiple record into an actual
    # currency P&L. Each paper trade is sized at initial_trading_capital /
    # max_concurrent_positions (equal weight), and its currency P&L is that
    # notional times the net-of-cost return. This is a MEASUREMENT overlay on
    # the paper journal, not a live account: the journal still records every
    # signal's outcome (it never refuses a signal because "all slots are
    # full"), and the slot count only sets the per-position size. A hard
    # concurrency cap belongs with real order placement, not signal
    # validation, so it is deliberately not enforced here. ---
    initial_trading_capital: float = 5000.0
    trading_currency: str = "EUR"
    max_concurrent_positions: int = 15

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
    # Anchors and tradeables are held to different bars, but BOTH now require a
    # real connection. A tradeable additionally requires a verified name match
    # and repeat disclosure (it can open trades); an anchor requires only that
    # accepting it lands it CONNECTED to a tradeable -- i.e. the disclosure that
    # discovered it came from a tradeable, so _promote_pending_edges writes a
    # live edge (see engine.py's _auto_accept_candidates and _tradeable_links).
    # That connectivity gate is why this can default OFF without losing good
    # additions: the earlier "anchors are liberal, worst case is wasted spend"
    # stance is exactly what grew a 322-anchor universe with 221 inert members
    # whose news reached nothing (AUDIT-2026-08 A2). Turn it back on freely --
    # the gate makes a re-flood impossible -- or grow on demand from the
    # dashboard's connectivity reconcile.
    auto_accept_anchors: bool = False
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

    def strategy_signature(self) -> dict:
        """The strategy 'fingerprint' stamped on each paper trade at open, so
        the closed record can later be segmented by strategy generation (see
        status.gather_strategy_generations). The trade-governing params define
        the generation; `label` and `version` ride along for display only and
        never change which generation a trade belongs to -- so a cosmetic
        release or a rename can't fork the forward record, only a real rule
        change can. `version` is the running add-on version (SMARTBOI_VERSION,
        set in the Docker image), used purely to label when a generation began."""
        sig = {k: getattr(self, k) for k in _STRATEGY_PARAM_KEYS}
        sig["label"] = self.strategy_label
        sig["version"] = os.environ.get("SMARTBOI_VERSION", "")
        return sig


def load_settings() -> Settings:
    return Settings()
