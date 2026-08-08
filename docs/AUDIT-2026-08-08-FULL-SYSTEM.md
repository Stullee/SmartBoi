# Full-system multi-agent audit — 2026-08-08

**Tree audited:** 0.48.0 (`d86ecbf`) for the code pass, with a dedicated
delta pass re-verifying every claim against 0.49.0 (`a010b44`). This
document lives on 0.49.0 and line references are to that tree unless a
finding explicitly says otherwise.

**Method.** Twenty agents across three waves. Wave 1: eight independent
domain auditors (engine orchestration, relationship graph, news/sourcing,
dossier/scoring/LLM, signals/trade realism, profitability/statistics,
hardening/durability, maintenance/ops), each followed by a separate
adversarial verifier instructed to *refute* its findings against the code
and to correct severity independently. Wave 2: seven angles the repository
cannot answer on its own — the academic premise, LLM state of the art,
data-source gap analysis, operating economics and capacity, a red team,
capability gaps, and the 0.48→0.49 delta. Wave 3: root-cause collapse,
sequencing, and an empirical completeness critic that *executed* the
system rather than reading it.

**Result:** 127 code findings (3 CRITICAL, 32 HIGH, 70 MEDIUM, 22 LOW);
113 CONFIRMED by an adversarial verifier, 14 PLAUSIBLE, 0 refuted. Plus 10
new findings from the completeness pass and 7 strategy verdicts. The four
highest-stakes claims were additionally re-verified by hand by the lead
auditor against the source before being written down here.

**Baseline:** 585 tests pass in 5.4s on 0.48.0, 591 in 5.7s on 0.49.0. CI
green on the last two runs. Working tree clean; nothing in this audit
changed a line of source.

The three prior audits (`AUDIT-2026-07*.md`, `AUDIT-2026-08.md`) were read
first. Findings already reported there are not repeated except where they
are **still unfixed**, in which case they are marked as such.

---

## 0. Bottom line

The engineering is better than the strategy, and the record-keeping is
better than the record.

Per-incident craft here is above the bar of most professional shops. The
definitive-vs-deferred evidence contract, `effective_sample_count` and
`cluster_bootstrap_ci` written before any auditor asked for them,
`ecosystem_benchmark_return` excluding a symbol from its own benchmark,
cap-bucketed transaction costs charged on both legs, per-record model
provenance, six machine-enforced schema-drift guards between `Settings`
and the add-on manifest — this is publication-grade experimental hygiene,
and almost every constant in the tree carries the live incident that
calibrated it.

What is missing is not craft. It is that **three separate things each,
independently, prevent the system from answering its own question today**:

1. The daily LLM budget is consumed front-to-back over a hardcoded
   universe list, so the forward record is a sample of roughly the first
   thirty symbols — not of the strategy. Seven of nine ecosystems and all
   ~144 auto-accepted symbols receive zero scored evidence on a typical
   day, permanently.
2. The score saturates at two pieces of evidence. Above the signal bar it
   has almost no dynamic range: at six or more sources, every per-item
   magnitude from 0.60 to 0.90 produces the identical score of 0.950. The
   independent variable of the entire measurement stack is close to a
   point mass over more than a third of its decision-relevant range.
3. `SCORING_VERSION` is written at three sites and filtered at **zero**.
   The mechanism that exists specifically to stop incompatible scoring
   regimes being pooled is inert, and the scoring path changes roughly
   twice a day against a sample that needs years.

None of the three is a crash. All three are silent, and each one alone is
sufficient to make the accumulated record uninterpretable. That is the
shape of this system's risk: it is hardened against everything that
announces itself and unguarded against everything that does not.

On strategy: the premise is real but has decayed far more than the design
assumes, and the system is pointed at the part of it that survived least
well. On economics: the API bill is irrelevant; capacity (~$1.5M) and
friction (~6.7%/yr at current turnover) are what bind.

**This is a strong research engine attached to a measurement apparatus
that cannot yet measure, pursuing a thesis that has thinned since 2008.
All three are fixable, in that order, and the order matters more than the
fixes.**

---

## 0b. Remediation status (updated 2026-08-08, 0.52.0)

Phases 0 and 1 have shipped. This section is the honest ledger; the rest of
the document is left as written at audit time so the two can be compared.
Everything below is Lane A — `SCORING_VERSION`, `_STRATEGY_PARAM_KEYS` and
every threshold are untouched, so the forward record has not forked.

| Finding | Status |
|---|---|
| C1 universe wipe on a Finnhub outage | **Fixed** — `lookup_failed` sentinel + a 10% blast-radius refusal |
| C2 corporate actions fabricate P&L | **Fixed (detection, not adjustment)** — split-vs-real-move classifier; VOID on the trade path, unjoinable on the panel, re-baselined on the drift guard |
| C3 forward-return lookahead | **Fixed** — both capture passes keyed on the last completed session *and* gated on the market being shut |
| C4 `SCORING_VERSION` filtered nowhere | **Fixed** — reader-side filter, default = version in force, plus a pre-registered decision gate |
| N1 rationing lottery | **Fixed** — persisted rotating ingestion cursor |
| N2 auto-accept demand pump | **Partial** — rotation means accepted symbols are now reachable; they still double LLM demand and cost idle HTTP polls |
| N3 score saturates at two items | **Open — Lane B**, belongs in Fork 2 |
| N4 corrupt state hard-crashes | **Fixed** — `persist.py` across all five stores, plus the add-on entrypoint |
| N5 EUR account, USD prices | **Warned, not fixed** — no FX exists; startup warning states the label is cosmetic |
| N6 citations misattribute propagated evidence | **Fixed** — full provenance incl. `origin_symbol`, `is_propagated`, `rel_type` |
| Unauthenticated dashboard on the LAN | **Mitigated** — configurable bind host + startup warning; the real fix is auth |
| No backup | **Fixed** — nightly tarball, 14 daily + 8 weekly, age-based retention |
| Silence indistinguishable from death | **Partial** — `boot: auto`, a cheap `/api/health` watchdog, quarantine events repeated on every heartbeat; no ingestion-staleness alarm yet |

**Three defects were found in the fixes themselves**, by re-checking each
against the finding it claimed to close rather than trusting the commit
message. All three are corrected:

- the session anchor initially filed a **mid-session price as a session
  close** (`last_completed_session` answers "which session closed"; the
  price sources answer "what is the last print" — they agree only when the
  market is shut);
- `dominant_rel_type` was inferred by keyword-matching the relationship
  note, and the ecosystem-fallback note's own disclaimer contains
  "customer, supplier or competitor", so the weakest evidence in the system
  was labelled `mixed`;
- the watchdog pointed at `/api/status`, which builds the whole dashboard
  payload on the engine's event loop behind an 8s timeout — a busy tick
  would have answered 504 and had the add-on **restarted**, and restarts
  are precisely what corrupts this record.

Two more surfaced while wiring C2: VOID rows sat in the win-rate
**denominator**, and both close-alert paths formatted `exit_price`/
`r_multiple` with `:.2f`, which raises on the `None` a VOID carries —
inside the loop that closes every other trade.

Test count over the four commits: 591 → 694.

---

## 1. Grades

Graded against what a serious version of *this system* would look like,
not against a hobby project. A high finding count here reflects an
unusually inspectable codebase and a very thorough audit, not a bad one.

| Area | Grade | Why |
|---|---|---|
| Engineering craft (per-incident) | **A−** | Exception isolation, budget accounting, evidence-loss discipline, secret handling, the deferred/definitive contract. Genuinely excellent. |
| Maintenance, testing, packaging | **B−** | Six machine-enforced drift guards and a settings-coverage test most teams never write — but five *human-readable* config surfaces with zero guards, all rotted; CI runs `pytest` and nothing else. |
| Signal generation & entry | **B** | Seven independent gates, each with its live incident recorded. The entry path is the best-engineered part of the system. |
| Data ingestion & sourcing | **C+** | The EDGAR half is excellent and correctly conceived. The news half is ~200 chars of aggregator blurb feeding the most expensive judgement in the system, and they are treated as peers in the corroboration count. |
| Robustness & data durability | **C−** | Per-incident hardening is best-in-class; *systemic* durability is absent. One copy of an irreplaceable dataset, no backup, five loaders that treat "I cannot read this" as "start fresh". |
| Trade realism & exits | **C−** | No corporate-action handling of any kind. The exit is a calendar: at 50%/100% bands virtually every trade times out, so the exit — half of any strategy's P&L — contains zero information from the evidence machinery. |
| Evidence scoring (the aggregator) | **D+** | A defensible design on top of an indefensible aggregator: no accepted item can ever lower the score on any of its four terms, and it saturates at two items. |
| Measurement & statistical validity | **D+** | The *capture* design is right and unusually thoughtful. The *analysis* on top of it has a lookahead path, no market adjustment, no version filter, and no baseline. |
| Relationship graph & edge building | **D** | The load-bearing element of the entire thesis, and the least developed part of the system. 207 lines, add-only, no verification, no materiality, no aging, and `rel_type` never reaches the propagation math. |
| Strategy & economics | **C−** | The premise survived, at perhaps a quarter of its headline size, and the design targets the channel that survived worst. Capacity ceiling ~$1.5M. |

---

## 2. Root causes

The 127 findings are not 127 independent defects. Sorted by *what would
have had to be true for this not to happen*, they collapse into seven
causes, and those seven share one origin.

**RC1 — Absence of data is treated as a fact about the world.**
The missing rule is one sentence: *no destructive or classifying action
may be taken because a value is missing.* It is violated in both
directions — an API that fails to answer becomes "this ticker is dead"
(`news.py:260-266` → `universe_screen.py:86` → `engine.py:3302`), and a
file that cannot be read becomes "there was nothing here." Roughly 15
findings, including the most urgent one in the audit.

**RC2 — Stamped but never read: the system cannot analyse itself.**
The author repeatedly identifies the right discriminating variable,
writes an excellent docstring explaining why it matters, persists it — and
never builds the reader. `SCORING_VERSION`, `threshold_in_force`, strategy
generation, the five new synthesis columns, `is_propagated`, `rel_type`.
Everything needed to answer the system's central question is on disk, and
no code path asks. ~18 findings.

**RC3 — The score can only go up, and the one brake is not wired to the
gate.** No item the system accepts can lower the aggregate on any of its
four terms. The skeptic's proudest calibration feature — accept the real
but oversized fact with `adjusted_magnitude` scaled down — provably
*raises* the dossier's magnitude, because the multiplier keys on a source
count gated on confidence alone and never looks at the item's own
magnitude. Verified by executing the shipped `_aggregate`: twenty items
scaled down to magnitude 0.05 take a below-bar 0.225 thesis to 0.708.
Meanwhile the only downward force in the design, the Opus synthesis cap,
is not consulted on the merge firing path. ~22 findings. **This is the
most dangerous cause** — it is the only one that produces a confident,
plausible, wrong answer rather than a visible failure.

**RC4 — The edge has no sign, no size and no expiry.**
`rel_type` never enters the propagation math or either LLM prompt, though
both prompts promise the model it will be told the relationship type. The
academic result this system is named after is a *directional,
magnitude-bearing* claim. What the code implements is "adjacency implies
co-movement" — a competitor's good news propagates as good news. There is
no materiality (customer-concentration percentages are extracted and
discarded), no currency (a terminated relationship reads identically to a
live one), and no aging.

**RC5 — The stated invariant and the enforced invariant have diverged,
and only the machine-readable surfaces are guarded.** Six drift guards
protect `Settings` ↔ `config.yaml` — verified a perfect bijection. Zero
guards protect the README, DOCS.md, `.env.example`, the code's own block
comments, or the dashboard's rendered defaults, and all five have rotted.
One commit moving the strategy defaults left behind a `config.py` comment
saying "defaults to institutional" five lines above `= "retail"`.

**RC6 — The single-task engine.** One asyncio tick, every HTTP and LLM
call inline, no timeouts. Generates the largest raw finding count (~23)
and the smallest share of record corruption. Deliberately demoted: it
causes cost, latency and availability problems, not wrong answers.

**RC7 — The system ends at the signal.** Seven gates before entry; a
calendar after it. Exit logic, market mechanics (splits, borrow, ADV,
halts) and the analysis layer are all comparatively stubs.

### The cause behind the causes

**SmartBoi was debugged into existence rather than specified into
existence.** Every mechanism in it exists because something visibly broke;
almost nothing exists because a failure was reasoned about in advance.
`name_matches_ticker` exists because Advantest was recorded as ATRO. The
synthesis budget reservation exists because synthesis had never once run.
This predicts both lists with almost no exceptions: everything with a
visible symptom is guarded to a high standard, and everything silent,
slow, or only visible in aggregate is unguarded.

---

## 3. Critical findings

### C1 — A Finnhub outage silently deletes the runtime universe
`engine.py:3302`, `news.py:260-266`, `universe_screen.py:86`

`market_cap_musd` returns `None` on **any** `httpx.HTTPError` — connect
timeout, 401/403 on a revoked or over-quota key, 5xx, or a 429 surviving
three backoffs. `screen_universe` stores it verbatim. Then, once every 30
days:

```python
dead = [r.symbol for r in results if r.market_cap_musd is None]   # engine.py:3302
```

Every "dead" symbol present in `accepted_candidates` is deleted, the
universe is rebuilt without them, and `_archive_orphaned_dossiers()` moves
their dossiers out of the live directory. Nothing distinguishes "the API
said this ticker has no profile" from "the API did not answer," and there
is no sanity bound on the size of the prune. On the live board that is 144
symbols and their entire accumulated evidence. `data/dossiers_archived/`
has **no reader** anywhere in the codebase, so the deletion is permanent.

*Fix (2h):* a `lookup_failed` sentinel distinct from `None`, and a refusal
to prune when the dead set exceeds ~10% of the universe.

### C2 — No corporate-action handling anywhere; a reverse split fabricates a maximal win
`paper_journal.py:392-397, 501-508`

`stop_price` and `target_price` are frozen as absolute dollar levels at
open and compared forever against raw, unadjusted prices:

```python
hit_stop   = day_low  <= trade.stop_price
hit_target = day_high >= trade.target_price
```

`grep` over `src/` returns **zero** hits for split ratio, adjustment
factor, ex-date or any price-jump sanity check. Both price sources are
unadjusted (`whatToShow="TRADES"`; Finnhub `/quote` raw `c`/`h`/`l`). With
stops and targets at −50%/+100%, any split of 2:1 or greater in either
direction lands outside the band on the first bar after the ex-date: a
1-for-10 reverse split books a fabricated maximal WIN on a position that
did not move. Sub-$1 Nasdaq compliance reverse splits are routine in
exactly this universe.

*Fix (3h for detection):* flag and alert on any `|log(p/p_prev)|` beyond a
plausible session move; a daily invariant checker over open trades. Full
adjustment is a later, larger job — detection alone stops the record being
corrupted silently.

### C3 — Forward-return entry prices can predate the score they are joined to
`forward_returns.py:113`, `engine.py:1006-1043`

The snapshot pass and the price-mark pass are two independent schedulers,
each gated only on "≥86400s since *my* last run," with no time-of-day
anchor. `is_trading_day()` is a bare `weekday() < 5` in ET with no hour
bound, so after every weekend the mark pass re-anchors to ~00:00–01:00 ET
Monday and holds that hour all week. Finnhub `/quote` at 00:30 ET returns
the *previous* session's close, and the row is stamped with today's date.

So the entry price for a snapshot dated D can be the close of D−1, while
the score on that snapshot reflects every 8-K and news item ingested
through some hour of D. 8-Ks and earnings cluster after the close, and
this system's inputs are precisely 8-Ks and news. The dataset buys at the
last pre-announcement print and measures the announcement gap as forward
return — a systematic lookahead in the primary decision dataset.

*Fix (1–2 days):* anchor both passes to a fixed ET session time after the
close and key rows on `session_date`, not UTC wall clock.

### C4 — `SCORING_VERSION` is written at three sites and filtered at zero
*Verified by hand.* Written at `dossier.py:285`, `signals.py:114`,
`status.py:526`. Grepped across `forward_returns.py`, `event_study.py`,
`tools.py`, `exit_analysis.py` and `scripts/`: **no reader filters on it.**
The guard that exists specifically so incompatible scoring regimes are
never pooled does not guard anything. Every analysis silently pools v1
through v4. The same is true of `threshold_in_force` and strategy
generation.

This matters more than any individual bug, because 0.49.0 just bumped
`SCORING_VERSION` to 4 — correctly, for a real rules boundary — and
nothing will act on it.

*Fix (3h):* `scoring_version` / generation / `--since` filters at the
three join sites, and a boxed pre-registered decision gate at the top of
the forward-return report.

---

## 4. New findings from the empirical completeness pass

These came from *running* the system, not reading it, and none of the
fifteen dimension agents found them because each sits in a seam between
two dimensions.

### N1 — The rationing lottery: the record samples `DEFAULT_UNIVERSE[:30]`
**Arguably the single most important finding in this audit.**

Three facts nobody joined:

1. `Engine.symbol_list` is the universe list in declaration order. There
   is no shuffle, rotation, round-robin or cursor anywhere.
2. Both ingestion loops walk it in that order (`_poll_edgar:1125`,
   `_poll_news:1984`).
3. When the dossier budget is exhausted the item is *not* registered in
   the dedup index — it is retried on the next poll, which starts again at
   index 0.

LLM demand exceeds the $3.30/day budget by roughly 5×. So the budget is
consumed front-to-back from the same starting point every poll, every day.
Driving the real `UsageTracker` with shipped defaults over the real
140-symbol order:

| demand | served | cut lands at | ecosystems with zero |
|---|---|---|---|
| 420/day | 379 (90%) | index 126 | 1 of 9 |
| 980/day | 379 (39%) | index 54 | 6 of 9 |
| **1,820/day** | **379 (21%)** | **index 29** | **7 of 9** |
| 3,780/day | 379 (10%) | index 14 | 8 of 9 |

The economics angle independently measured live demand at 1,892 items/day
against 378 served — the third row. The ecosystem blocks in
`DEFAULT_UNIVERSE` are contiguous (semi_equipment 0–23, defense_tier2
24–46, … transport_logistics 128–139), so the cut is **exactly aligned
with the variable the strategy is trying to diversify**.

The consequence is the one the operator has been chasing. `universe.py`'s
own docstring records that "the book behaved like one correlated bet on AI
capex." The fix was five new, structurally better ecosystems — every one
appended after index 79, and therefore structurally unreachable. The
diversification shipped and cannot take effect. Nothing reports this:
there is no per-symbol or per-ecosystem "items scored today" counter
anywhere.

Second-order: deferred items are retried only while inside the ingestion
lookback (3 days news, 14 days EDGAR), so tail-of-list evidence is not
delayed, it is **discarded**. And because the censor is deterministic
rather than random, the missingness is non-ignorable — every bucket mean
and bootstrap CI in `forward_returns.py` is conditioned on "symbol index
< ~30," which correlates with market cap, ecosystem, news volume and beta
simultaneously. No amount of clustering fixes a non-random censor.

*Fix (4 lines):* a persisted rotating start offset converts a
deterministic censor into an unbiased one overnight. *Better (half a
day):* ecosystem round-robin plus an `items_scored_today_by_ecosystem`
heartbeat field.

### N2 — Auto-accept is a demand pump aimed at the starved end
All ~144 runtime-accepted symbols are **appended** after the 140 curated
entries. So auto-accept (a) roughly doubles daily LLM demand, moving the
rationing cut earlier for the curated core; (b) can never itself be
served, since every accepted symbol sits beyond index 140; and (c) still
costs a full EDGAR fetch every 900s and a Finnhub fetch every 3600s each —
~144 × 24 round-trips a day producing zero scored evidence. It spends the
extraction budget to discover symbols whose evidence the dossier budget
can never score. *Fix (1h):* insert accepted symbols by ecosystem rather
than appending, and gate the polls on remaining budget before the HTTP
fetch.

### N3 — The score has almost no dynamic range above the signal bar
Running the shipped `_aggregate` over N identical items:

```
dossier SCORE (confidence x magnitude), items at confidence 0.70
  item mag | S=1   S=2   S=3   S=4   S=6   S=8   S=12  S=20
    0.60   | 0.420 0.600 0.719 0.810 0.938 0.950 0.950 0.950
    0.70   | 0.490 0.700 0.839 0.900 0.950 0.950 0.950 0.950
    0.80   | 0.560 0.800 0.858 0.900 0.950 0.950 0.950 0.950
    0.90   | 0.630 0.800 0.858 0.900 0.950 0.950 0.950 0.950
```

At S≥6, every per-item magnitude from 0.60 to 0.90 gives the identical
0.950. At S≥4, 0.70/0.80/0.90 are all exactly 0.900. On a 3,000-row Monte
Carlo where a latent quality drives both scores and returns, 38.5% of the
entire trading region carries the single value 1.000.
`pearson_correlation` and `bucket_returns` are being asked to detect a
monotone relationship in a variable that is a point mass over a third of
its decision-relevant range. An end-to-end run reproduced it from the
shipped engine: two propagated items at 0.80/0.80 produced
`confidence 0.9, magnitude 1.0` — the ceiling, from the *minimum
admissible* evidence set. The red team showed junk can inflate the score;
the complementary and worse fact is that good evidence cannot
differentiate itself.

### N4 — Corrupt state can hard-crash the engine at startup
Every prior durability finding assumes loaders degrade to empty. A second
class raises out of `Engine.__init__`/`start()`, past `main._amain` (which
catches only `KeyboardInterrupt`), and the container exits. With
`boot: manual` and no `watchdog:` in the add-on manifest, it stays exited.
*Fix (20 min):* broaden three `except` clauses, add `isinstance(raw, dict)`
checks, set `boot: auto` and a watchdog URL.

### N5 — The account is EUR, every price is USD, and no FX rate exists
`trading_currency: str = "EUR"` is read at exactly two places, both as a
display label. `position_value = initial_trading_capital / slots` is EUR;
every price and P&L is USD. *Fix:* five minutes to relabel the account USD
(honest and free), or an hour to add EURUSD to the daily mark sweep.

### N6 — Paper-trade citations actively misattribute propagated evidence
An end-to-end reconstruction of a propagated LONG (INTC news → 0.92
customer edge → FORM) produced a `paper_trades` row whose citations are
two headlines *about Intel*, filed under a *FORM* trade, with no field
anywhere saying so. The record is not merely missing the edge — as written
it misleads any reader, including the three analysis scripts. *Fix: four
words* — add `origin_symbol` and `is_propagated` to the citation dict at
`engine.py:2716-2722`.

Also: `Settings` has zero field validators (all bounds live in the add-on
YAML, and both documented non-HA paths bypass them); three naive
`date.today()` calls in an otherwise strictly-UTC system; `avg_r` and
`avg_r_gross` averaged over different denominators, so the dashboard's
cost-drag figure is a difference of two samples.

### Verified clean — do not re-audit these
- **Timezone and DST, end to end.** Swept hourly across both 2026/2027
  transitions. `zoneinfo` used correctly; RTH is UTC 13–19 summer, 15–20
  winter; weekday boundary rolls at ET midnight in both regimes.
- **Month/quarter/year/leap edges.** Every date expression is `timedelta`-
  or ISO-based. Verified across 2028-02-29 and year boundaries.
- **Cold first run.** Real `Engine.start()` plus a full tick in an empty
  directory with no credentials: completed, logged four correct warnings,
  correctly refused to mark a zero-row snapshot pass done, wrote three
  files. Better than most systems manage.
- **Money representation.** No accumulation pathology; float error at
  ~3,900 calls/day is ~4e-3 cents.
- **Unicode — clean by accident.** No `encoding=` on any of 17 file-I/O
  sites; safe only because `json.dumps` defaults to `ensure_ascii=True`.
  Adding `ensure_ascii=False` to make dossiers readable would make the
  entire store locale-dependent overnight. Add `encoding="utf-8"` to all
  17 sites now, while it is free.

---

## 5. Strategy and economics reality

### The premise has decayed ~75–80%
- Cohen & Frazzini (JF 2008), 1981–2004: >150bp/month; 1.58% VW, t=3.79.
- Hou, Xue & Zhang (RFS 2020) replication with NYSE breakpoints and value
  weights: **0.79%/mo** — method correction alone is a 50% haircut.
- Pinchuk (2023), post-discovery 2005–2018: EW 1.5%→0.6%, VW 1.3%→0.6%,
  and the value-weighted version **loses significance**.
- McLean & Pontiff (JF 2016): −58% post-publication.
- Chen & Velikov (JFQA 2023), 204 anomalies: net of effective spreads the
  average earns **4bp/month**.

Compounded central estimate for 2026: **~33bp/month gross**, ≈4%/yr on a
long-short before any cost.

Three results do more damage than the decay numbers:

- **Ali & Hirshleifer (JFE 2020):** connected-firm momentum via *shared
  analyst coverage* earns 1.68%/mo (t=9.67) and **subsumes customer
  momentum entirely** — customer, industry, geographic and technology
  momentum alphas all go insignificant against it. The transmission
  channel is analysts. This universe screens for ≤10 analysts, several
  names at 0–2. *The system selects against the mechanism that survived.*
- **Burt & Hrdlicka (JFQA 2021):** much of economically-linked
  predictability is commonality-in-momentum, not information diffusion —
  it persists up to 10 years, inconsistent with a days-to-weeks lag story.
  This is exactly what the operator's own week-1 data showed.
- **Griffin, McInnis & Zhao (JAAF 2026):** PEAD's decline is driven by
  falling signal informativeness *specifically in smaller firms* — you
  cannot route around it by being uncrowded.

And the artifact the system is proudest of is the commoditised part:
FactSet Revere ships 1.57M curated supply relationships across 154,862
companies, with history to 2003, sitting in WRDS.

**The residual inefficiency is real but narrower than the design assumes.**
It is not "discover the link." It is "estimate the magnitude of a specific
novel event through a *quantified* disclosed revenue share, in a name too
small for a pod shop to size." Concrete implication: set an analyst
**floor** of ~3–4 alongside the ceiling of 10, and record analyst count on
every paper trade — that turns a philosophical argument into a
measurement. The five ecosystems added in July (auto, transport,
industrial, energy services, medtech) are structurally *better* than the
original four and should become the core, not the diversifier.

### Economics: the API bill is not the problem
- Spend is **$3.30/day, $100/month, $1,204/year** — and it is exactly the
  cap, because demand exceeds it ~5×. The cost model is a rationing
  constant, not a spend estimate.
- **Capacity:** median ADV across a 13-name sample of the tradeables is
  ~$2.1M, bottom quartile under $0.70M. At the 2%-of-ADV participation the
  config's own retail cost table assumes, deployed capital caps at
  **$630k–$1.3M**, hard ceiling ~$1.5M.
- **Friction:** 15 slots × 21-day holds ≈ 8× turnover/year × ~84bp blended
  round trip = **6.7% of deployed capital per year at zero alpha** — six
  to thirteen times the plausible net edge.
- **Time to an answer:** ~740–1,150 *effective* observations needed for a
  1%/trade edge. At ~195 episodes/year and realistic cross-sectional
  correlation, that is **3.8–5.9 years**, and 7–29 years if correlation is
  not removed. Against ~2 behaviour-changing commits/day. **The logic
  currently changes ~1,400× faster than the evidence accumulates.**

### The highest-value data source is free and not being read
**EDGAR full-text search.** The system reads only its own universe's
filings and asks an LLM to infer who else is connected. SEC disclosure
obligations run *upward* — a small supplier must disclose customer
concentration; a giant never discloses its small suppliers — which
`universe.py` already concedes in prose after the KLA extraction produced
only large-cap edges. EDGAR FTS inverts that asymmetry and returns a
**named, cited** edge instead of an inferred one. Free, keyless, JSON,
~40 requests/day against a 10 req/s budget.

Also: **`edgar_forms` contains neither 6-K nor 20-F**, while ASML and TSM
are polled hourly as anchors. Foreign private issuers file 6-K/20-F, so
those two can never produce a single filing — TSMC's monthly revenue 6-K
alone is 12 free, dated, fixed-schedule ecosystem datapoints a year,
currently discarded. **The fix is two lines and zero marginal API cost.**

Then, in order: USAspending FSRS sub-award data (free, names first-tier
subcontractors with dollar amounts — disclosed prime→sub edges); DoD daily
contract announcements (every business day 17:00 ET, ≥$7.5M, one LLM call
covers the whole feed; the host moved to war.gov in 2025); openFDA device
endpoints and the Federal Register API. Finnhub company news is the
weakest ingested source and is a candidate for replacement rather than
supplementation — keep the client for quotes and screening.

---

## 6. The plan

The bugs are not the bottleneck; the fix cadence is. Every finding falls
into one of two lanes, and **the lane, not the severity, determines when
it ships.**

- **Lane A (free, continuous):** cannot alter what the system decides.
  Backups, quarantine-on-corrupt, timeouts, watchdog, extra columns, extra
  marked symbols, everything in `forward_returns.py`. Ship these whenever.
- **Lane B (expensive, batched):** changes a decision. Every Lane B commit
  forks the forward record. **Two forks, ever.**

### Phase 0 — "Don't lose the asset" (weeks 1–2, ~2 days)
Off-box copy of the data today, before any deploy. `boot: auto` + a
watchdog on `/api/status`. Bind the dashboard to 127.0.0.1 (`host_network:
true` currently puts an unauthenticated dashboard with state-mutating
endpoints on the LAN, not just behind Ingress). Nightly tarball off the
existing scheduler. `src/smartboi/persist.py`: one atomic-write-with-fsync
and one quarantine-and-refuse loader, migrated across all five stores.
Fix C1's universe-wipe blast radius. Timeouts on all five
`AsyncAnthropic` constructions.

### Phase 1 — "Make the record readable" (weeks 3–7, ~5 days)
All Lane A, all unbackfillable, which is why it goes first.
Add IWM/SPY and one ETF per ecosystem to the daily mark sweep — **one
extra call per day, and the single highest-leverage item in the audit**:
market adjustment cuts residual σ from ~15% to ~10.5% and collapses
pairwise ρ from ~0.4 to ~0.05, taking the requirement from ~1,150
effective observations to ~196. Twelve discriminating columns on
`snapshot_dossier` (`propagated_share`, `max_rel_confidence`,
`dominant_rel_type`, …). Full evidence provenance on `PaperTrade`,
including `origin_symbol`. Reader-side `scoring_version` / generation /
`--since` filters. The rotating universe cursor (N1). Corporate-action
*detection*. A shadow symmetric skeptic that logs but never gates.

### Fork 1 — "Start the clock" (week 8, one commit)
Deliberately subtractive — it narrows the population rather than changing
scoring: `enable_ecosystem_propagation=false` (the red team executed the
real code: six items propagated on sector membership alone, no filing, no
disclosed link, score 0.527, fires at the 0.5 bar),
`auto_accept_anchors=false`, `enable_auto_supplier_research=false`.

### Phase 2 — "Test the premise before paying for it" (weeks 9–20)
Scoring path frozen. Ship Lane A freely; build Fork 2 on a branch. Five
cheap monthly measurements on data Phase 1 now captures — the first is
**propagated vs direct**: does evidence arriving over a graph edge predict
better, worse or the same as direct evidence on the same names? That is
the thesis, and today the system cannot answer it.

### Fork 2 — "The one fork" (week 21, one commit)
Everything Lane B that survived the gate, merged once: sign-aware
propagation (carry `rel_type` into both prompts and invert competitor);
the `_aggregate` split — corroboration raises *confidence* only, magnitude
becomes a precision-weighted combination bounded by the largest item;
synthesis cap consulted on the merge path; an INVALIDATED exit on
direction flip and keeping synthesis alive on open positions.

### Phase 4 — "Freeze and decide" (weeks 22–52)
Lane A only. Evaluate the **pre-registered** cell at day 90 and 180 — not
whichever of ~30 intervals looks best. Decide on the *panel* (score →
benchmark-relative forward return, rank IC, time-block CI, after cost),
not the trade log: at ~120 trades/year and ~18% per-trade σ, a 1% net edge
needs ~2,540 trades, i.e. 21 years. The panel needs twelve months.

### Stop doing
- Stop shipping behaviour changes as you find them. Two a day against a
  five-year sample is the defining problem of this project.
- Stop treating `paper_trades.jsonl` as the deliverable. Demote it to an
  execution-realism sanity check; promote the snapshot panel.
- Stop widening the universe. Auto-accept currently makes the curated core
  hungrier without feeding the periphery.
- Roughly 45 of the 127 findings never get fixed in this plan. That is the
  point.

### Monday morning
The first action is not a commit. **09:00 — get a copy of the only copy
off the box**, verify the tarball opens. Everything this quarter involves
deploying code to the single host holding an irreplaceable dataset with no
backup and five loaders that treat "I cannot read this" as "start fresh."

---

## 7. Counter-review: where this audit is wrong

Two of the five synthesis passes (the verdict and the devil's-advocate
pass) were lost to a session limit. This section is written by the lead
auditor and is therefore the least independently checked part of the
document — treat it accordingly.

**The 0% refutation rate is a warning sign.** Sixteen agents, 127 findings,
zero refuted. Adversarial verifiers that confirm everything they check are
not being adversarial. Severity was corrected downward in a number of
cases, which is some evidence they engaged, and the four highest-stakes
claims were re-verified by hand. But assume some MEDIUM-tier findings are
softer than stated.

**Do not act on the finding count.** 127 findings on 12.7k LOC reflects an
unusually inspectable codebase reviewed by twenty agents at high effort,
not a bad system. A typical solo project of this ambition would not
survive one of these dimensions.

**Fixes that would be a mistake:**
- *The three-task engine split.* RC6 is real but demoted; a technically
  strong operator will instinctively spend a week here. It buys latency
  and availability, not correctness, and it adds concurrency risk to a
  design that currently has none. After Fork 2, if at all.
- *Improving the skeptic.* The evidence says self-critique detection tops
  out near one error in three, and role-relabelling already captures most
  of the available gain. Make it a logged prior, not a gate; do not spend
  money making it smarter.
- *Optimising the dedup index, JSONL rotation, `usage.py` write
  amplification, `/api/status` blocking.* All real, all survivable for a
  year, all complexity added to a system whose complexity is itself a top
  finding.
- *Making dossier JSON human-readable with `ensure_ascii=False`.* Actively
  dangerous — see the unicode note in §4.

**Where the audit's framing may be wrong.** Every agent accepted the
system's goal on its own terms: build a forward record, then decide. Given
capacity of ~$1.5M and a premise that has decayed ~75–80%, the honest
alternative framing is that this is a **research and engineering project
whose output is knowledge and a reusable evidence pipeline**, not a
pre-trading validation exercise. Under that framing the priorities change:
the measurement plumbing (Phases 0–1) matters *more*, the trade-realism
work matters *less*, and the right success criterion is "did I learn
something publishable about cross-company information diffusion in
thinly-covered names" rather than "did I clear a Sharpe bar." That is a
legitimate and defensible project. It is worth deciding which one this is
before spending the next quarter.

---

## 8. What to protect

Do not refactor these away while fixing everything else.

- **Paper-only by construction.** Verified again: `prices.py` has no order
  methods; the journal is a dataclass log; nothing else touches a broker.
- **The definitive-vs-deferred evidence contract** (`engine.py:2246-2364`)
  — three-valued outcomes, `_pending_proposals` surviving a budget-deferred
  skeptic call, `would_allow` at selection and `record` only on a fresh
  handle. The best single piece of engineering in the tree.
- **Tick ordering.** The comment at `engine.py:929-954` documents three
  distinct trade-killing bugs and the fix is correct.
- **Daily passes scheduled off persisted wall-clock and gated on their own
  return value** — a day marked done with no rows is unbackfillable, and
  the code knows it.
- **Signal state persists before it publishes on every path.** No
  `signals.jsonl` row can exist without its episode key.
- **The forward-capture design.** `effective_sample_count`,
  `cluster_bootstrap_ci`, `ecosystem_benchmark_return` excluding the symbol
  from its own benchmark, cap-bucketed costs on both legs, per-record model
  stamping — written before any auditor asked.
- **The machine-readable drift guards.** Six between `Settings` and
  `config.yaml`, plus
  `test_every_non_secret_setting_is_either_reported_or_deliberately_omitted`.
  Extend this pattern to the prose surfaces rather than inventing a new one.
- **EDGAR depth.** Exhibits-first 8-K text, item-code expansion before a
  token is spent, structured Form 4 summaries, CIK liveness pruning.
- **The habit of recording the live incident that calibrated each
  constant.** It is why this audit could be precise.

---

## 9. Coverage and limits

- No runtime data was available: `data/` contains only `.gitkeep`. Live
  figures quoted here (144 accepted symbols, 30 open trades, spend totals,
  demand rates) come from `AUDIT-2026-08.md`'s diagnostics bundle and are
  **not independently verified**.
- The code pass ran against 0.48.0; a dedicated delta agent re-verified
  every affected claim against 0.49.0. 0.49.0 genuinely fixes the
  synthesis-persistence gap (all six terminal branches traced) and
  partially fixes the `seen_count` double-count — **partially**, because
  the guard keys on `document_url not in entry["sources"]` and `sources` is
  a 5-slot ring buffer, so above five distinct source filings the guard is
  a complete no-op (measured: `seen_count` 6→12→18→24 over three cycles).
- Two synthesis passes were lost to a session limit; §7 is
  correspondingly weaker than the rest.
- Nothing here was executed against a full year of data, and **it cannot
  be**: there is no replay path. No raw source text is ever cached, so a
  scoring change is not just un-backtestable against history, it is
  un-*re*-testable against the system's own past. If one thing gets built
  beyond this list, that is a strong candidate.
