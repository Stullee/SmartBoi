# Data & signal-generation research — 2026-08-10

Scope: a targeted research pass on one question — how to bring in more
real, high-quality evidence that legitimately produces more signals,
without violating the system's design premises. Not a general audit.
Method: nine parallel research agents (two independent funnel diagnoses,
one graph/anchor analysis, one config-lever analysis, one pipeline
plug-point map, and four data-source landscape sweeps), each reading the
live source at `claude/smartboi-data-signals-k7pqth` (HEAD `742e137`,
0.53.0, `SCORING_VERSION = 5`). Every load-bearing claim below was
re-verified by hand against the tree or recomputed from the runtime logs
before being written down.

**Nothing here is implemented.** This is research and a plan.

---

## Data caveats — read before quoting any number

1. **Only 2026-08-08 and 2026-08-09 are representative.** The sv4→sv5
   scoring change landed 2026-08-09 and dossiers were zeroed. Everything
   older reflects a regime that no longer exists and was excluded.
2. **The sv5 window is one day.** One daily snapshot (2026-08-09T16:00),
   five signal rows, three symbols (DCO, ASYS, SIF). Several conclusions
   below are directional, and are labelled where so.
3. **Both representative days are a weekend.** 2026-08-08 is a Saturday,
   2026-08-09 a Sunday. No filings land; `price_marks.jsonl` has zero rows
   for both days and jumps 08-07 → 08-10. This *strengthens* attribution of
   the collapse to the rules change (near-zero evidence arrival in the
   window) but means no market data exists inside it.
4. **`data/graph.json` was unavailable.** `data/` is empty and gitignored on
   this checkout. All graph claims are reconstructed from `universe.py`,
   `engine.py`'s seeding/extraction paths, relationship confidences quoted
   verbatim in live paper-trade theses, and snapshot-derived proxies. Every
   such claim is labelled a proxy. **No claim below is a graph read.**
5. **Data-source verification was crippled by the network policy.** This
   session's egress proxy 403s at CONNECT for effectively every external
   host — `sec.gov`, `efts.sec.gov`, `api.usaspending.gov`, `api.fda.gov`,
   `globenewswire.com`, `war.gov`, `finra.org`, `roic.ai`. Confirmed by
   direct test: both `curl` and `WebFetch` fail; only server-side
   `WebSearch` works. **No agent fetched a single endpoint.** Every cost,
   rate limit, and coverage figure in §3 comes from search results
   retrieved 2026-08-10 or from official API contracts mirrored on GitHub.
   **Every RECOMMEND on an external source is contingent on one live probe
   from a host with open egress.** Prices that could not be confirmed say
   UNKNOWN and were not invented.
6. **Budget figures use `max_daily_usd = 10`**, the operator's live value,
   not the shipped `3.30` (`config.py:284`).

---

## 0. Bottom line up front

**This is a CONVERSION problem, not an INTAKE problem, and the two seed
ideas are aimed at the wrong stage of the funnel.**

On 2026-08-09, across all 48 dossiers: **zero fail on the min-independent-
sources bar alone.** Not one, on either representative day, at either bar
assumption. The gate that "more sources" relieves is not currently binding
on a single dossier. The board breaks down as 0 passing, 0 failing on
sources only, 35 failing on threshold only, 13 failing both.

What *is* binding is a single thing:

> **52 of 52 persisted synthesis verdicts in the entire snapshot record are
> `already_priced_in: true`. Nine distinct synthesis timestamps, 35
> symbols. Zero trims. Zero clean passes. Not one row anywhere with
> `synthesis_confidence > 0`.** (Recomputed by hand from
> `dossier_snapshots.jsonl`.)

That is not a filter with a high rejection rate. It is an off switch that
costs an Opus call. On 08-09 it converted 27 live candidates into 27 exact
zeros in one pass and took the board from 33 SIGNALED to 0, ceiling 0.279
against a 0.500 bar.

**The root cause is a prompt that asks a question whose honest answer is
always yes.** `dossier.py:894-901` defines the veto as true when "the
evidence is old, **the story was widely covered when it broke**, or it
merely confirms what was already known." Anchors are selected precisely
*for* being large and heavily covered (`universe.py`). Every propagated
thesis this system generates originates in a widely-covered anchor fact —
that is the premise of the strategy, not a disqualifier. The synthesizer
is answering correctly and the question is wrong. The system prompt three
lines up asks the *right* question — "has the market already made this
**connection**?" (`dossier.py:930-931`) — but the tool-schema description
broadens it into a disjunction where the "widely covered" clause alone
suffices, and that clause fires every time.

At `max_daily_usd = 10` the synthesis pool is $2.50/day, comfortably above
the ~$0.84–3.02 those 27 Opus calls cost. **The pass had budget, ran, and
vetoed everything.** Budget starvation is excluded as an explanation.

### The two seed ideas, unhedged

**Idea #1 — more news sources beyond Finnhub: NOT WORTH DOING as stated.
Worth doing in a narrow, different form.**

Zero dossiers are source-blocked today. Worse, 19 of the 21 sub-threshold
dossiers **cannot reach 0.50 at any source count**, because both
corroboration bonuses cap at `MAX_CORROBORATION_DOUBLINGS` ≈ 5.66 sources
(`dossier.py:322`, `571-573`, `604-606`). HTLD and GTX are already at the
cap; infinite extra corroboration moves them by exactly zero. Adding
publishers relieves a gate that is not binding, to feed a score that is
capped, on dossiers that are zeroed downstream.

The narrow form that *is* worth doing: **primary, orthogonal source
TYPES** — not more reporters. Those matter for a different reason (§2.1).

**Idea #2 — more anchors per tradeable: NOT WORTH DOING as stated. Worth
doing as edge growth, in 4 of 9 ecosystems only.**

221 of 322 anchors carry no edge to any tradeable (`engine.py:3550-3551`).
Only 1 of the 5 `SEED_RELATIONSHIPS` is live. And the claim at
`universe.py:36-39` that an edgeless anchor "costs zero LLM calls" is **no
longer true** — with `enable_ecosystem_propagation = True`
(`config.py:347`) it falls through to ecosystem fan-out at 0.25
confidence, which post-sv5 buys decay mass and **zero** source slots. It is
now expensive and pointless rather than free and pointless.

### Fix order

Conversion first, and within conversion the veto first. Everything else is
unmeasurable until it lands, because the veto zeroes whatever the other
levers produce.

1. Make the synthesis veto falsifiable (R1–R3). Free. No new data.
2. Fix the slot accounting the veto currently masks (R4). Free.
3. Then, and only then, add primary orthogonal source types (R5–R9).

---

## 1. Funnel diagnosis — where evidence actually dies

### 1.1 The gate table, 2026-08-09 (sv5, n=48)

Recomputed directly from `dossier_snapshots.jsonl` against
`signals.evaluate` (`signals.py:59-115`), bar = 2:

| Outcome | Count |
|---|---|
| **Pass `evaluate()`** | **0** |
| Fail **sources only** | **0** |
| Fail **threshold only** | 35 |
| Fail **both** | 13 |
| direction = NONE | 0 (41 LONG / 7 SHORT) |

Of the 48, **27 carry `already_priced_in: true` and score exactly 0.000** —
all stamped `synthesis_at = 2026-08-09T09:49`, one pass. Of those 27, only
**one** is also below the source bar. The other 26 would clear
corroboration if the score were not zeroed.

The surviving score distribution is censored by construction: synthesis
only runs above `signal_confidence_threshold × synthesis_score_floor_pct`
= 0.5 × 0.6 = **0.30** (`engine.py:3254-3256`, `config.py:245`), and
everything above that floor was vetoed to zero. Max non-vetoed score
0.2785 (INTT). "Mean 0.062, max 0.279" is not arithmetic degradation — it
is the sub-floor tail, all that survives.

### 1.2 Control: 2026-08-08 (sv4, n=48)

23 dossiers score ≥ 0.5. 25 carry `already_priced_in: true` **and still
score high** (mean 0.628, max 1.000) — with `synthesis_at` stamps from
2026-07-30 through 08-07, never 08-08. That is the pre-sv5 merge-path
bypass visible in the raw data: `_aggregate` recomputed from scratch and
nothing re-applied the verdict.

### 1.3 Attributing the 23 → 0 collapse

Three sv5 changes are candidates. They are not equally responsible.

**(b) bounded magnitude multiplier — third-order.** Removes exactly one
dossier from the passing cohort (THRM, 0.525 → 0.450).

**(c) ecosystem items mint no slots — enormous in the source count,
small in the score.** Universe-wide `independent_source_count` went
**442 → 187 (−57.7%)**, 29 symbols down, **zero up**. Baseline churn
08-07 → 08-08 was +5.8% with no symbol decreasing, so this is
unambiguously the rule change (`dossier.py:533-539`), not decay.

But the score effect is far smaller, because the bonus is logarithmic and
capped. The only clean natural experiment — matched sv4/sv5 rows on the
same day, hours apart:

| symbol | sv4 score (slots) | sv5 score (slots) | Δscore |
|---|---|---|---|
| DCO | 0.939 (17) | 0.940 (15) | +0.001 |
| ASYS | 1.000 (21) | 0.980 (5) | −0.020 |
| SIF | 0.931 (5) | 0.813 (3) | −0.118 |

**ASYS lost 76% of its slots and 2% of its score.**

**(a) the merge-path synthesis re-cap — everything else.** The two changes
the question names are not what moved the board. `engine._cap_with_synthesis`
(`engine.py:3312-3349`, called at `engine.py:2530`) correctly closed a
bypass — and thereby revealed that synthesis rejects everything the
arithmetic promotes.

Change (c) also separates the graph exactly along the disclosed-link line.
Annihilated: TCPA 26→0, INTT 22→1, MTRX 16→1, PLPC 20→2, CVV 15→1,
AOSL 11→1, PLAB 9→0. Untouched: THRM 12→12, PUMP 14→14, DCO 16→15,
VVX 12→11, NCSM 12→11 — precisely the 0.95-confidence 10-K edges
(THRM→GM, PUMP→XOM, DCO→LMT/RTX).

### 1.4 The second structural ceiling

Independent of the veto, the score is anchored on **one** item
(`dossier.py:564-565`, `604-606`); everything accumulated enters only
through `doublings`, capped at 2.5. At the cap, uncontested, full weight:

```
score = (best.c + 0.25) × best.m × 1.625
```

For a representative tier-2 item (c=0.60, m=0.35) that is **0.483 — below
the 0.5 bar, at any source count.** Six sources and twenty-seven sources
produce the identical number. Break-even is `m ≥ 0.3077/(c + 0.25)`; at
c=0.60 the best single item needs magnitude ≥ 0.362, and the rubric puts
tier 2 at 0.20–0.50 (`dossier.py:686-694`). **The lower half of tier-2
evidence can never signal, however much corroboration arrives.**

This is a second, independent reason more sources do not help.

### 1.5 What is not the problem

- **Decay is not starving anything.** Source counts were monotonically
  non-decreasing for all 48 symbols over 08-01 → 08-08. Not one dossier
  lost a source in eight days. A horizon-21 item holds full weight for
  three weeks and contributes mass for six (`dossier.py:387-413`),
  outliving the 16–21 day trades it justifies by 2×.
- **The news-only elevated bar cost nothing** on either day. Only three
  signal rows carried `min_sources_in_force = 3` (TCPA, INTT, CDRE) and
  **all three cleared it anyway**.
- **The contested discount is not binding.** Exactly one dossier on the
  last full sv4 board matches "low confidence, high source count" (GTX,
  0.22 at 7 slots).
- **Reach is broadly fine.** 40 of 43 default tradeables have a dossier
  (93%); only MLAB, MVST and NVX have never formed one. Zero dossiers have
  zero evidence.

### 1.6 Stages the logs cannot see, and the counters that already exist

Ingestion volume, dedup collapse, propagation reach, skeptic
refutation/rescaling and merge volume are all **UNKNOWN** from the four
supplied logs. They are not unknown to the running system:

| Stage | Counter that already exists | Where |
|---|---|---|
| Skeptic refutation/rescaling | `logs/skeptic_refutations.jsonl` (written, not supplied) | `engine.py:2355-2372` |
| Signal → trade conversion | `logs/decisions.jsonl` (written, not supplied) | `signals.py:145-173` |
| Ingestion volume + dedup | `len(dedup._seen)` + publisher histogram, computed live, never persisted | `tools.py:425-435` |
| Propagation reach | `gather_coverage` — tradeables connected, anchors live/inert | `status.py:133-180` |
| Per-category spend | `usage.by_category` calls/$/EXHAUSTED | `tools.py:437-461` |
| **Which gate a dossier failed** | `_below_bar_reason` — computes exactly the §1.1 table | `engine.py:2793-2813` |

`_below_bar_reason` is the important one: it is reachable **only** from
`_expire_signal` (`engine.py:2557`, `2871`, `3216`), so a dossier that
never signalled produces no record of which gate it failed. On 08-09 that
is 48 of 48, silently. `_log_heartbeat` (`engine.py:989-996`) carries no
ingestion, dedup, merge, skeptic or synthesis counters.

Two fields absent from `snapshot_dossier` (`status.py:599-639`) would have
made this entire analysis a query instead of a reconstruction: the
**pre-veto arithmetic score** (the veto zeroes confidence, magnitude,
`synthesis_confidence` *and* `synthesis_magnitude` at `engine.py:3291-3294`,
so it is unrecoverable) and the two backing flags that decide the source
bar.

### 1.7 Would more signals have been profitable?

n = 15, all opened under sv3, all closed, none since 2026-08-06. The
regime that produced them no longer exists. With that caveat:

| Metric | Value |
|---|---|
| 6 WIN / 9 LOSS | 40% |
| mean `r_multiple_gross` | **−0.094 R** |
| mean `r_multiple` (net) | **−0.505 R** |
| mean cost drag | +0.411 R/trade |
| t-statistic (net) | −1.08 — not distinguishable from zero |

**The gross edge is a coin flip and costs turn it negative.** Scaling
signal count scales a negative expectancy. Cost is the mechanism and it
bites exactly where this universe hunts: the sub-$300M bucket (600bp)
needs a 58.6% hit rate to break even and delivered 33%; the >$1B bucket
(100bp) is the only profitable one at +0.248 R mean.

Both scoring inputs are *inverted* in this sample: confidence 0.60–0.75
returned +0.866 R while 0.75–1.01 returned −1.044 R; source count 3–5
returned −0.353 R while 6+ returned −0.659 R. n is far too small to
conclude anything, but nothing here supports "more signals would have
made money."

---

## 2. The two seed ideas, judged

### 2.1 More news sources beyond Finnhub — NOT WORTH DOING as stated

Evidence against, in order of force:

1. **Zero dossiers are source-blocked** on either representative day (§1.1).
2. **19 of 21 sub-threshold dossiers are unreachable by corroboration** at
   any source count (§1.4).
3. **The corroboration bonus caps at ~5.66 sources** (`dossier.py:322`).
4. **In the trade record, more sources associated with worse net R** (§1.7).
5. **There is no budget headroom.** At `max_daily_usd = 10` measured demand
   is $10.01/day against a $10.00 ceiling — dossier $6.47 + extraction
   $3.54 (`config.py:315-316`). Extraction is already at its 0.35 share and
   deferring. Added intake displaces existing evidence rather than adding
   to it, on arrival order rather than value, and deferred items age out of
   the lookback (news 3 days, `config.py:189`) and are **dropped silently
   and permanently**.

**What the shortage actually is.** Finnhub sells two news products:
`/company-news` (free, what `news.py` uses) and `/press-releases` — sourced
from BusinessWire, ACCESSWIRE, GlobeNewswire, Newsfile and PRNewswire —
which is **Enterprise-only** [SEARCH-UNDATED]. The system's own
measurement corroborates from the other side: six publisher names for the
whole universe, one aggregator ("Yahoo") at ~69% (`dossier.py:230-240`).
**You are being served Yahoo's re-syndication of the wires, not the
wires.**

So the narrow, defensible version of idea #1 is **new primary source
TYPES**, and the reason is specific: change (c) deliberately removed the
cheap sector-correlation slots, and those should not come back. What can
legitimately replace them is evidence that is independent *by
construction* — a government award, a regulator's record, a filing type
not currently read. Each such type mints a real slot. **More reporters do
not.**

### 2.2 More anchors per tradeable — NOT WORTH DOING as stated; narrow win as edge growth

Counts from `universe.py`: 140 specs, 43 tradeable, 97 anchor, 9
ecosystems, **5** `SEED_RELATIONSHIPS`.

**Only 1 of the 5 seeds is live.** `UCTT→AMAT`, `UCTT→LRCX`, `ICHR→AMAT`
and `ICHR→LRCX` are dead: both filers were demoted to anchors in the
2026-07 refresh, making them anchor→anchor edges, and `_process_evidence`
skips `signal_source_only` targets (`engine.py:2213-2215`). Hand-seeding
currently contributes exactly one live edge (`DCO→RTX`) to the whole
system.

**Anchor growth already ran to its conclusion, live.** The measurements,
in order: 26 of 130 anchors live (2026-07-29, `README.md:712`, explicitly
pre-fix); 104 of 130 with no edge to a tradeable (`engine.py:2085-2089`);
**221 of 322** (`engine.py:3550-3551`, freshest in-code). The situation got
absolutely worse while marginally improving in ratio. The countermeasure
already shipped: `auto_accept_anchors` now defaults **False**
(`config.py:517`), with a connectivity reconcile that prunes inert
runtime-accepted anchors (`engine.py:3545-3640`).

**Where edge growth pays.** Retention of slot-bearing sources across the
08-09 cut, per ecosystem — a proxy for "fed by real edges" vs "fed by 0.25
fan-out":

| ecosystem | trd | slots 08-08→08-09 | retained | verdict |
|---|---|---|---|---|
| transport_logistics | 5 | 16 → 16 | 100% | **Strong** — carriers disclose shipper concentration (`ULH→F` 25%) |
| medtech_supply | 2 | 6 → 6 | 100% | Real but tiny (`UFPT→SYK` 21.5%) |
| battery_storage | 4 | 9 → 9 | 100% | Misleading — base of 9 across 4 names, none ever signalled |
| energy_services | 4 | 38 → 37 | 97% | **Strong** (`PUMP→XOM` 24.9%, `→OXY` 13.7%) |
| auto_supply | 4 | 30 → 29 | 96% | **Strongest** — every tradeable edge-fed, every edge quantified |
| defense_tier2 | 6 | 46 → 37 | 80% | **Strong** (`DCO→BA` "13% revenue exposure") |
| industrial_machinery | 5 | 21 → 6 | 28% | Weak — no single named OEM to disclose |
| grid_datacenter | 6 | 125 → 20 | 16% | **Zero edge-fed tradeables** |
| semi_equipment | 4 | 63 → 7 | 11% | **Zero edge-fed tradeables, 20 anchors for 4 names** |

**58% of all corroboration in the system was ecosystem fan-out, not graph
edges** (442 → 187 slots).

`grid_datacenter` and `semi_equipment` are the clean refutation of idea #2
as stated. Twelve and twenty anchors respectively, zero edge-fed
tradeables. MSFT/GOOGL/AMZN/META never contract with a $350M mechanical
contractor — the real counterparties are utilities and EPCs. Adding a 13th
hyperscaler to a group whose existing five have produced zero disclosed
edges cannot help. `semi_equipment`'s counterparties are fabs and OSATs
that are frequently foreign, private, or reached through distributors —
a disclosure-regime problem no feed solves.

**The disclosure asymmetry is why.** SEC obligations run *upward*: a small
supplier must disclose customer concentration as a material risk; a giant
never discloses its small suppliers. `universe.py:43-51` records the live
proof — extracting KLA's own 10-K produced only `KLAC→TSM/AMAT/ONTO`, all
large caps, not one small supplier. Every quantified edge reconstructed
from live theses has a **small cap as `from_symbol`**; zero have a giant.

**Verdict: treat the anchor list as a recognition dictionary, not a
discovery instrument.** Its right size is "the set of counterparties our
tradeables actually disclose" — which the connectivity reconcile already
computes from data you own.

---

## 3. Ranked recommendations

Ranked by expected signal-lift per unit of integration effort. R1–R4 are
free, require no new data, and are prerequisites for everything after.

### R1. Make the `already_priced_in` veto falsifiable by price — **effort S**

**What.** The veto asserts a testable claim: the market has already
absorbed this. If the price has since moved *against* the thesis, the tape
has refuted it — whatever the market absorbed, it was not this thesis at
this price, and the entry is better than when the verdict was made.

Snapshot the price at verdict time in `_apply_synthesis` beside
`engine.py:3264`, reusing the already-fallback-hardened `_price_bar`
(`engine.py:2576`) exactly as `_capture_inception` does
(`engine.py:2709-2736`). Test it in `_cap_with_synthesis`, replacing the
bare `if dossier.already_priced_in:` at `engine.py:3344`, using the
existing `signals.favorable_drift_pct` (`signals.py:118-128`). No price →
verdict stands (fail-safe direction is correct).

**Expected lift.** 27 dossiers currently sit at exactly 0.000 on a verdict
nothing in the system can contradict. Grep confirms `already_priced_in` is
read at `engine.py:3283`, `engine.py:3344`, `forward_returns.py:433-434`,
`paper_journal.py:441`, `status.py:638` — **no price is compared to it
anywhere**. A join of the 52 verdicts against `price_marks.jsonl`
(forward return signed in the thesis direction) shows individual verdicts
falsified violently: ULH vetoed 08-03 LONG → +36.1% at +1 mark, +50.0% at
+5; PLPC vetoed 08-03 LONG → +32.0% at +3; BWEN vetoed 08-04 LONG →
+22.3%; SCRNY +9.9%. In the other direction, "already absorbed" was
asserted about theses the market had moved hard *against*: LMB (LONG,
−32% over the prior week), RDW (SHORT, +42.5% against).

*Honest reading:* aggregate means across 12–24 overlapping windows do not
prove the veto is wrong on average, and medians at +1/+3 are slightly
negative. What is established is that **individual verdicts are refutable
and nothing refutes them.**

**Where.** `engine.py:3264` (write), `engine.py:3344` (test).
**Risk.** Veto lifted on a falling stock, thesis re-fires into a
downtrend. Contained by three guards that all still apply: the arithmetic
must still clear 0.5 (`signals.py:97`), the entry drift gate
(`config.py:424`), and the next daily synthesis.
**Two-week test.** Count of verdicts lifted by price falsification, and
their forward return vs the verdicts that stood.

### R2. Re-synthesize when the evidence body materially changes — **effort S-M**

**What.** Persist the set of `independence_key` values present at verdict
time; in `_cap_with_synthesis`, if ≥2 *new* keys have arrived since, mark
the verdict stale and skip the cap pending re-judgement. Pair with a
bounded immediate re-synthesis (cap ~5 Opus calls/day) so the path does not
fail open.

**Why it cannot be gamed — this is the load-bearing point.** The trigger
counts new `independence_key` values, not new items. Ecosystem-association
items mint no key (`dossier.py:533-539`), so a fan-out burst of 30
correlated macro items produces **zero** new keys and cannot invalidate
anything. Three Yahoo articles about one story produce one key, and dedup
drops two before they arrive. The invalidation trigger reuses exactly the
accounting change (c) hardened.

**Correction to a natural assumption.** The veto is *not* architecturally
sticky. `_decay_one` calls `recompute_decay` first (`engine.py:3173`),
which rebuilds confidence/magnitude from raw evidence and erases the
previous day's zeroes, and only then tests the synthesis floor. Verified
in the snapshot series: VVX 0.000 → 0.921 next day; NCSM, RJET, BWEN, INTT
all show the same one-cycle escape. **The escape hatch exists — it just
never opens, because the verdict is always the same.** The freshness gate
(`_SYNTHESIS_CAP_MAX_AGE_HOURS = 36.0`, `engine.py:200`) should stay at 36.

**Where.** `engine.py:3312` (read), `engine.py:3264` (write),
`engine.py:2530` (bounded re-synthesis).
**Risk.** Extra Opus spend, bounded at ~$0.50/day.

### R3. Stop destroying the synthesis pass's own output — **effort S, zero behaviour change**

`_apply_synthesis` hard-zeroes `synthesis_confidence` and
`synthesis_magnitude` on the veto path (`engine.py:3291-3292`) *before*
persisting. The record therefore contains **no information at all** about
what the most expensive pass in the system actually thought — all 52
verdicts show `synthesis_confidence ∈ {0.0}`. You cannot distinguish "0.9
confidence but priced in" from "0.05 and priced in", ever. The veto branch
returns before those fields are read, so storing the real values is safe.

Also add to `snapshot_dossier` (`status.py:599-639`): the pre-veto
arithmetic score, `has_filing_evidence`, `has_disclosed_link_evidence`, and
`min_sources_required`. And call `_below_bar_reason` (`engine.py:2793-2813`)
on every `evaluate() → None`, not only on expiry, appending to
`decisions.jsonl`. Without these, §4's remaining levers cannot be sized.

### R4. Give ecosystem associations one collective slot, not zero — **effort S**

**What.** All ecosystem-association items on the agreeing side collapse to
**exactly one** slot keyed on the ecosystem, regardless of item count,
origin count, publisher count or day count. Replaces the current binary
exclusion at `dossier.py:533-539`.

**Guardrail-3 compliance is arithmetic, not argued:** the ecosystem class
contributes `min(1, |eco|)` to `independent_source_count`. Constant in
volume by construction. Doubling the item count changes nothing.

**Why 1 rather than 0.** An ecosystem link *is* one real piece of
information — "this sector is repricing" — and the honest accounting of one
piece of information is one slot. It cannot single-handedly qualify a
dossier (1 < `min_independent_sources` = 2), which is exactly the stated
design intent at `engine.py:188-191`: "it can raise a thesis but can never
single-handedly qualify one." Today's rule implements "cannot raise a
thesis at all."

**Expected lift — measured, and small; do not oversell it.** On the 08-09
board this moves 7 dossiers from 1→2 slots and 5 from 0→1. Best case is
INTT: 0.796/0.350 → 0.896/0.4375 = 0.392, **still under 0.5. Zero
immediate new signals.** The value is removing a permanent structural zero
for names whose only propagation path is the ecosystem edge.

**REJECT the tempting variant:** one slot per distinct *origin company*.
NVDA + AMAT + LRCX reporting one capex story would be three origins, three
slots — rebuilding the saturation bug with extra steps.

### R5. Add NT 10-K / NT 10-Q, S-3/S-1, 20-F/40-F/6-K, Form 25/15 to `edgar_forms` — **effort S**

**The best value-per-line-of-code in this report.** `edgar_forms`
(`config.py:161`) is currently
`"8-K,8-K/A,10-K,10-Q,4,SC 13D,SC 13D/A,424B5,424B3"`. `_poll_edgar`
already fetches each symbol's submissions payload regardless of form
filter (`engine.py:1207`) and `fetch_evidence_text` already handles the
non-8-K path (`edgar.py:580-585`). Marginal cost is HTTP plus LLM calls
for *matched* filings only.

- **NT 10-K / NT 10-Q** — a late-filing notice is one of the cleanest
  small-cap short catalysts in existence, and it skews *toward* thin caps
  (large caps rarely file 12b-25). Event-study literature reports 5-day CAR
  of −2.93% (NT 10-Q) / −1.96% (NT 10-K) with continued drift [SECONDARY].
  Reaction is conditional on the *reason given* — boilerplate negative, a
  legitimate stated reason positive — which is exactly what an LLM reading
  the text is good at. **~0.01 items/day.**
- **S-3 / S-3/A / S-1** (exclude **S-3ASR**, WKSI-only and routine) — the
  shelf *registration*, weeks-to-months ahead of the 424B5 takedown already
  ingested. Adds the leading indicator to the confirming one on the
  README's own "cleanest SHORT catalyst" thesis. **~0.04 items/day.**
- **20-F / 40-F / 6-K** — **this closes a live hole.** NVX (NOVONIX,
  `universe.py:307`) is a *tradeable* — and `universe.py:23` names it one
  of only two symbols still passing thin-coverage screening — but it files
  20-F/6-K, neither of which is in `edgar_forms`. **Its dossier can
  currently never receive a single filing-evidence item and its annual
  report can never mint a graph edge.** Same for anchors CAMT, TSM, ASML,
  MGA. TSMC files monthly net revenue on a 6-K — a monthly, primary,
  quantitative, directional datapoint for the whole `semi_equipment`
  ecosystem. **~0.4-0.7 items/day.**
- **Form 25 / 25-NSE / 15-12B / 15-12G** — defensive. `_is_unknown_to_edgar`
  (`engine.py:1217`) only prunes a symbol after SEC's ticker file drops it,
  which lags the delisting. A Form 25 is 10 days' warning. **<0.01/day.**

**Caveat on 6-K.** Ship 20-F/40-F first (1/yr each, zero risk). Tradeable
6-K is *direct* evidence and therefore gets per-day independence keys; an
ASX cross-filer pushing 30-60 6-Ks/year would mint a fresh slot each time.
Gate behind a per-form daily counter, or strip the per-day component for
`"SEC EDGAR (6-K)"`.

### R6. DoD daily contract announcements — **effort M**

`war.gov/News/Contracts/`, all DoD awards ≥$7.5M (DFARS 205.303),
published 5pm ET each business day. Free, no auth. **~0-3 matched
items/business day.**

**Why this and not USAspending.** Both the government and orthogonal
agents independently rejected USAspending for defense on the same hard
fact: **DoD awards are withheld from FPDS/USAspending/SAM for 90 days** —
the SAM Contract Awards API contract states it outright. That is ~6× past
the 14-day stale floor in `dossier.evidence_is_stale`; the evidence would
be born aged out. The daily announcements carry no such hold.

**~70% of the value is propagated.** LMT/RTX/NOC/GD/BA appear most
business days, and `DCO→LMT` sits at 0.95 disclosed confidence. This
attacks the exact documented stall: DCO at 17 agreeing items, zero
opposing, counting 2 sources.

Also note: **FPDS-NG's ATOM feed no longer exists** — FPDS.gov was
decommissioned 2026-02-24 and folded into SAM.gov. Any design referencing
it is dead on arrival.

**Risk.** Name matching is the ATRO/Advantest failure mode in its worst
form — announcements use legal entity names ("Ducommun LaBarge
Technologies Inc., Tulsa, Oklahoma", "Vertex Aerospace LLC" for V2X), and
"Vertex" collides with Vertex Pharmaceuticals. Requires a hand-reviewed
alias table, word-boundary matching only, never fuzzy. Also: many
"awards" are IDIQ *ceilings* or modifications, not new revenue — pass the
raw text verbatim so the skeptic can catch it. **Gate anchor-side items on
a value floor (~$100M)** or fan-out will dominate the budget.

### R7. Federal Register API — **effort S-M**

`federalregister.gov/api/v1/documents.json`, **no API key, free**.
**0-2 items/day** when scoped to 5-8 hand-curated saved searches.

Verified relevance to specific tickers: BWEN (utility-scale wind tower
AD/CVD proceedings name Broadwind as a Wind Tower Trade Coalition member);
**HDSN** (EPA AIM Act HFC allowance allocation notices — entity-specific,
the single biggest driver of Hudson's refrigerant economics, and HDSN is
an existing SHORT dossier); AOSL (BIS Entity List actions); auto_supply
(FMVSS); semi_equipment (BIS export controls).

**It does partly activate the unused `regulator` edge type**, but only via
a convention: a rule is not a symbol, so propagation needs **synthetic
origin symbols** (`BIS`, `EPA`) added as `signal_source_only` pseudo-members
with hand-seeded `regulator` edges. Seed those at **0.6-0.8, deliberately
below `DISCLOSED_LINK_CONFIDENCE`** so a sector-wide rule does not buy a
corroboration discount.

**REJECT any "watch the Federal Register broadly" design** — ~200
documents/business day unfiltered is a disqualifying firehose.

### R8. EDGAR full-text search — as a *candidate* source only — **effort M**

`efts.sec.gov/LATEST/search-index`, no auth, free, coverage from 2001.
Confirmed not implemented (no hits for `efts.sec.gov` in `src/`).

**Why it matters:** it is the only mechanism that inverts the disclosure
asymmetry — asking "which *other* filers name our anchor" surfaces exactly
the thinly-covered supplier population the strategy wants.

**Why it produces zero evidence items, and why that is the argument rather
than a dodge:** if the filer is already in the universe, `_poll_edgar`
already fetched that 10-K and ran extraction on it. If the filer is *not*
in the universe, there is no dossier to write to. **A hit is a candidate,
not evidence.** Route to `research.merge_into_candidates()`
(`research.py:277`), never to `_process_evidence`, mirroring the discipline
already stated at `research.py:22-38`: "Research decides where to look;
EDGAR still decides what is true." Do **not** increment `seen_count` —
`auto_accept_tradeables` with `min_seen_count=2` (`config.py:518-522`)
means an accidental double-increment auto-adds a *trade target*.

**Constraint found:** EDGAR FTS has **no proximity operator**. Quoted
phrases and implicit AND only. So the query is document-level AND at
EDGAR, then a **local regex proximity pass** (anchor name within ±300
chars of "accounted for" / "of our net sales") over the fetched text. Also
one hit per *document*, not per filing — dedupe on `_source.adsh`. And
EFTS reportedly answers rate-exceed with **403, and returns sporadic
500s**; `edgar.py:295` retries only 429/503, so an EFTS client must widen
that set or silently drop hits.

**~14 HTTP requests/day** on a weekly anchor rotation, **0 LLM calls.**

### R9. openFDA recalls, SEC enforcement RSS, SAM exclusions, Reg SHO — **effort S each**

Cheap short-side tripwires. The SHORT book currently rests almost entirely
on 424B5.

- **openFDA** `/device/recall` and `/device/enforcement` (free key; 240/min,
  120,000/day with key). **Recalls only — skip 510(k)**: micro-caps
  press-release every clearance, so it is redundant with news, and the
  endpoint updates monthly. Covers IRMD and MLAB; **not UFPT** (a CDMO —
  its customers file the recalls). Collapse enforcement batches on
  `recall_number` before scoring or it becomes a firehose. Exclude Mesa
  Biotech records (divested to Thermo Fisher 2021, not MLAB).
- **SEC litigation-release RSS** (`sec.gov/about/rss-feeds`) — free, ~0/day
  for this universe, top-tier catalyst when it fires.
- **SAM.gov exclusions** — debarment ends federal revenue. Expected ~0/year,
  but one request/day fits even the 10/day unregistered tier.
- **Reg SHO threshold lists** (`nasdaqtrader.com/dynamic/symdir/regsho/`) —
  free daily text files. Upgrades `paper_journal`'s `assumes_borrow` from a
  market-cap *proxy* to the actual observable, making the dashboard's
  with/without-borrow R split mean something.

### R10. Earnings-call transcripts — **effort M, gated**

The only unambiguously PRIMARY_ORTHOGONAL news-adjacent source: management
forward statements are the *source text* a wire release paraphrases, and
the Q&A names customers, programs and design wins that appear in no
filing — precisely the relationship-graph input the system is starved of.
~0.55 items/day mean, clustered post-quarter.

**Gate before writing code:** probe free-tier coverage against TAYD, SIF,
RFL, ESOA, NCSM, BKTI. Micro-caps are exactly where transcript vendors
thin out. **If coverage is <80% of the 43 tradeables, reject outright** —
partial coverage silently biases the engine toward the covered subset.
Prices for every paid tier are **UNKNOWN**.

Declare `source_type="transcript"` (not `"news"`) — this is the one place
the `dossier.py:548` behaviour is *wanted*, and **`source_name` must embed
the quarter** or two transcripts collapse to one slot.

---

## 4. Config / threshold levers

Every lever below either counts genuinely independent evidence the current
accounting wrongly discards, or lets genuinely new evidence escape a stale
verdict. None lowers the bar.

| Lever | Current | Proposed | Where | Verdict |
|---|---|---|---|---|
| Price-falsification of the veto | none | 5% adverse move invalidates | `engine.py:3344` | RECOMMEND (R1) |
| New-distinct-source invalidation | none | 2 new keys | `engine.py:3312` | RECOMMEND (R2) |
| Store real synthesis conf/mag on veto | zeroed | keep | `engine.py:3291-3292` | RECOMMEND (R3) |
| Ecosystem slot policy | 0 slots | collective cap of 1 | `dossier.py:533-539` | RECOMMEND (R4) |
| `min(S, distinct_fact_count)` | S only | tighten by certified facts | `dossier.py:571-572` | RECOMMEND |
| Freshness gate | 36h | **keep 36h** | `engine.py:200` | KEEP |
| `_MIN_STALE_DAYS` | 14 | 21 (= `max_horizon_days`) | `dossier.py:207` | CONDITIONAL, low priority |
| Split `has_filing_evidence` from the 0.85 gate | ≥0.85 | `> ECOSYSTEM_ASSOCIATION_CONFIDENCE` | `dossier.py:547-552` | CONDITIONAL — ship R3 diagnostics first |
| Fact-certified doubling ceiling 2.5 → 3.0 | 2.5 flat | 3.0 when a fresh verdict certifies ≥8 facts | `dossier.py:322` | **CONDITIONAL — flagged, see below** |

**`distinct_fact_count` is computed, persisted (`dossier.py:150`, written
`engine.py:3274`), and never read by `_aggregate` or `evaluate`.** It is
the one number produced by the only pass that can see overlap. Using it as
`min(independent_source_count, distinct_fact_count)` is **pure
tightening** — the observed distribution is `{1:2, 2:10, 3:11, 4:16, 5:5,
6:6, 8:2}`, so a fan-out dossier at 27 slots / 2 facts drops from 2.5
doublings to 1.0. Ship that half alone, first.

Raising the ceiling to 3.0 for fact-certified dossiers is the **only lever
in this report that violates the "synthesis caps, never lifts" principle**
(`dossier.py:963-967`, `engine.py:3227-3236`), and it is flagged as such
rather than smuggled in. It is also the only lever that moves a fresh,
uncontested tier-2 thesis over 0.5 (0.483 → 0.551 at 8 certified facts).
Operator's call.

### Independence accounting as new source types arrive

Today `independence_key` (`dossier.py:219-261`) makes **the reporter the
identity**. Three outlets = three slots. That does not survive adding
primary feeds. The rule should key on the **primary fact**, not the
reporter — a `primary_fact_key` field on `EvidenceRecord`, consumed by
`independence_key`, with the existing logic as fallback.

| Source | `primary_fact_key` | Mints a slot? |
|---|---|---|
| SEC filing | `edgar:{accession}` | yes, 1 per accession |
| Gov award | `usaspending:{piid}` / DoD article+awardee | yes, 1 per award |
| FDA action | `fda:{recall_number}` | yes |
| Exchange notice | `{exchange}:{notice_id}` | yes |
| Patent grant | — | **no — mass only** |
| Issuer press release | inherited from the matching accession | **no — collapses** |
| Third-party news | derived, else `source_name` | yes, if it does not collapse |

**Three hazards that must be closed before any of this ships:**

1. **The press-wire ↔ EX-99.1 double-count.** Verified: a wire copy of a
   release already read as an 8-K exhibit mints a *second* independent
   source. Different fingerprint namespaces (`filing:SYMBOL:accession` at
   `engine.py:1259` vs `SYMBOL:headline:date` at `engine.py:2124`),
   structurally invisible to `find_near_duplicate` (which scans only the
   `f"{symbol}:"` prefix, `dedup.py:162-163`), different `independence_key`.
   Two slots for one press release satisfies `min_independent_sources=2` on
   its own. There is **no content hash, body digest, or cross-source alias
   table anywhere in `dedup.py`**. The 8-K's headline is synthesised from
   form + date + item codes (`engine.py:1288-1291`), so even a headline
   cross-check has nothing to match — but `fetch_evidence_text` already
   fetches the exhibit, so the release's own title is in hand.
2. **`source_type` is a literal string test.** `has_filing_evidence` is
   `e.source_type != "news"` (`dossier.py:548`). Any new source declaring
   anything other than the exact string `"news"` is silently treated as
   primary-disclosure backing and **drops the news-only bar from 3 to 2**.
   For a government record or a transcript that is correct *by design* —
   but it must be a deliberate per-source decision, not an accident.
3. **The direct-evidence loophole.** The ecosystem slot guard only fires on
   *propagated* evidence (`relationship_confidence is not None`). Feeding a
   sector-wide series in as **direct** evidence per ticker
   (`origin_symbol=PLAB, relationship_confidence=None`) routes around the
   guard entirely and mints a real slot on every ticker for one macro
   fact. **Any proposal that loops an industry series over tickers is doing
   this. Reject on sight.**

**Correction on per-day slot granularity.** `independence_key` returns the
**propagated branch first** (`dossier.py:249`), so propagated evidence
never reaches the `startswith("SEC EDGAR")` line. All 97 anchors are
`signal_source_only`, and an anchor is never its own target
(`engine.py:2207-2208`), so every anchor-origin item keys as
`LMT|DoD Contracts` — **one slot per anchor, forever**. The per-day
mechanic applies only to the 43 tradeables' own direct filings. Widening
the prefix test to a tuple therefore helps direct evidence only; it does
nothing for the propagated channel that carries ~70% of R6's value.

---

## 5. What NOT to do

**Levers that are just lowering the bar** — all REJECT: dropping
`signal_confidence_threshold` (`config.py:361`), dropping
`min_independent_sources` (`config.py:362`) or
`min_independent_sources_news_only` (`config.py:370`), lowering
`MIN_SOURCE_CONTRIBUTION` (`dossier.py:270`, undoes the skeptic), softening
`contest_factor` (`dossier.py:578`, the only defence against trading a
51/49 thesis as conviction, and n=1 evidence it binds), raising
`_STALE_HORIZON_MULTIPLE` or `_DECAY_FLOOR` (keeps dead evidence alive and
contradicts the literature `config.py:373-380` itself cites), raising
`ECOSYSTEM_ASSOCIATION_CONFIDENCE` above 0.25 or lowering
`DISCLOSED_LINK_CONFIDENCE` below 0.85 (reclassifies weak links as strong).

**Disabling or thresholding the synthesis veto** — REJECT. It is the only
pass that can answer "are these 20 items one fact counted 20 times." Make
it falsifiable (R1/R2); do not delete it.

**Sources rejected, with the reason:**

| Source | Reason |
|---|---|
| Alpha Vantage, Marketaux, NewsAPI, NewsData, GDELT, Tiingo, EODHD, FMP, Polygon/Massive, Benzinga direct, Alpaca, Yahoo/Google per-ticker RSS, StockTitan, Seeking Alpha, Investing.com, Nasdaq per-ticker RSS | Guardrail 3 — the same wire copy Finnhub already serves. Several also fail on cost (NewsAPI $449/mo, commercial use barred on free) or rate limit (Alpha Vantage 25 req/day, Marketaux 100/day) or freshness (NewsData 12h delayed; Google News median item age ~6.6 days) |
| Company IR RSS / IR scraping | ~140 bespoke scrapers that fail **silently** (empty list → no warning → dossier goes quiet), and the incremental content over the already-ingested 8-K EX-99.1 path is *non-material* PR |
| USAspending for defense | 90-day DoD OPSEC hold — evidence born aged out. Civilian half (RDW/NASA, BKTI/USDA, WLDN/DOE, battery DOE grants) is CONDITIONAL and usable |
| USAspending sub-awards | 30-60 day FSRS reporting lag on top of the prime-side hold; GAO documents ~26% duplicates and 91% of STTR sub-awards unreported |
| FPDS-NG ATOM | **Does not exist** — decommissioned 2026-02-24 |
| Bill-of-lading (ImportGenius $125-899/mo, Panjiva/Datamyne UNKNOWN) | US waterborne imports only; ~5 clean tickers of ~40 (LMB/MTRX/ESOA/WLDN are domestic contractors, defense tier-2 is domestic by DFARS); and a BOL is a **record, not an event** — you need month-over-month deltas from the expensive tier, arriving 1-4 weeks late |
| 13F | 45-day statutory lag, long-only, needs a CUSIP bridge the system lacks |
| Form 144 | Redundant with Form 4 at a 21-day horizon (leads 2-5 days), and many/year × per-day keying **manufactures corroboration slots** |
| Forms 3/5, SC 13G, DEF 14A, EX-21, Form D/Reg A | No transaction / passive index crossings / governance boilerplate / lists subsidiaries not counterparties / dominated on timing by 8-K Item 3.02 |
| USPTO patents | A grant is a status change, not a fact with direction and horizon |
| FINRA daily short volume | Off-exchange only, dominated by MM hedging, offsetting buys invisible — FINRA's own guide warns against exactly this use. Cheap and wrong |
| SEC fails-to-deliver | Semi-monthly, 2-4 weeks stale, CUSIP-keyed; dominated by the daily ticker-keyed Reg SHO list |
| Interconnection queues, EIA-860, PUC dockets, FMCSA census, DAT/Cass, Baker Hughes, SEMI/SIA | Ecosystem-level or a **level not an event** — fails both decisive tests, mints zero slots, costs LLM calls |
| Job postings | Micro-cap industrials are not on Greenhouse/Lever; a posting count is a level |
| FCC, EPA ECHO, FERC, NRC, ITC EDIS, SEDAR+/RNS/TDnet | No usable API, no universe exposure, or paid/scrape-hostile — and every foreign counterparty that matters already files 20-F/40-F/6-K on EDGAR for free |
| EDGAR daily-index / RSS as *detection* | Daily index rebuilds ~10pm ET; structured RSS is 10-min 06:00-22:00 ET. Both **slower** than the existing 900s poll |

---

## 6. Sequencing

**The budget decides the order.** At `max_daily_usd = 10`, measured demand
is $10.01/day against a $10.00 ceiling — dossier $6.47 + extraction $3.54
(`config.py:315-316`), with extraction already at its 0.35 share and
deferring. There is **no headroom**. Any new source displaces existing
evidence unless something is cut first.

1. **R1–R3 (free, no new data).** Make the veto falsifiable and its output
   observable. Until this lands, no other change can be measured — the veto
   zeroes whatever they produce. Nothing else should ship first.
2. **R4 + `min(S, distinct_fact_count)` (free).** Fix the slot accounting
   the veto currently masks. Note the sequencing dependency: today only 1
   of the 27 vetoed dossiers is source-blocked, so the slot rule blocks
   nothing — **fix the veto and 13 of 48 immediately become source-blocked.**
   R4 is a latent prerequisite, not a present one.
3. **Cut ecosystem fan-out to free budget.** The ecosystem path alone
   demands ~1,892 (item,target) pairs/day at its cooldown-limited ceiling.
   Post-sv5 that spend buys decay mass and zero slots. Consider
   `enable_ecosystem_propagation = false` for `grid_datacenter` and
   `semi_equipment` specifically — those two generate 188 of the 255 lost
   slots. **This is what pays for step 4.**
4. **R5 (one config string).** Near-zero volume, closes the NVX hole,
   adds two short catalysts.
5. **Close the duplicate-collapse hazard** (§4, hazard 1) — a hard
   prerequisite for any press-release or wire source.
6. **R6–R9.** Combined ~2-6 direct items/day. Gate anchor-side items on a
   value floor or fan-out dominates.
7. **R10** only after the coverage probe.

**Would an intake increase be actively harmful today?** Yes, before step 3.
When the dossier budget exhausts, evidence is deferred, and the retry
vehicle is the item reappearing in a later poll — anything still unscored
when it ages out (news 3 days, EDGAR 14) is dropped silently and forever.
`_poll_news` walks `symbol_list` in fixed order (`engine.py:454-460`,
`2105`), so starvation is **deterministic and positional**: the same tail
of the universe starves every day, invisibly.

**Also worth fixing, found en route:** `.env.example` has drifted
materially from `config.py` — it ships `SIGNAL_CONFIDENCE_THRESHOLD=0.65`
(code 0.5), `MAX_HORIZON_DAYS=56` (code 21), `STOP_LOSS_PCT`/
`TAKE_PROFIT_PCT` 8/16 (code 50/100), `MAX_FAVORABLE_DRIFT_PCT=5.0` (code
12.0), and omits `MAX_DAILY_USD` and every budget share entirely. Four of
those are in `_STRATEGY_PARAM_KEYS` (`config.py:29-36`), so a `.env`-based
deploy forks the forward record into a different generation silently. The
live deployment is unaffected — every signal row stamps
`threshold_in_force=0.5`, so it runs the add-on's `config.yaml`.
Regenerate `.env.example` from `Settings` in CI.

---

## 7. Method, coverage, and what this pass did NOT verify

**Method.** Nine parallel agents on the live tree: two independent funnel
diagnoses (one from the runtime logs, one from source), a graph/anchor
analysis, a config-lever analysis, a plug-point map, and four source
sweeps. The two funnel agents reached the same verdict by different
routes. Three agents independently identified the same two integration
preconditions (per-source slot accounting; cross-source double-counting),
which is the main reason those are stated as fact rather than as one
agent's theory. The single most load-bearing claim — 52/52 vetoes — was
recomputed by hand, as was the 0.483 score ceiling, the `edgar_forms`
contents, the `independence_key` branch order, and NVX's tradeable status.

**Not verified, and material:**

- **No external endpoint was fetched.** §3's costs, rate limits and
  coverage claims are search-sourced. Every source RECOMMEND needs one live
  probe before code is written. Specifically unprobed: whether roic.ai's
  free tier covers our micro-caps; whether EDGAR FTS hit volume is
  tractable; whether the wire RSS feeds exist at the URLs cited; whether
  `war.gov`'s contracts RSS URL is what the agent inferred.
- **`data/graph.json` was unavailable.** Every graph number is a proxy —
  chiefly the 08-08→08-09 slot collapse read as "corroboration supplied by
  fan-out rather than edges." The 221/322 inert-anchor figure comes from a
  code comment (`engine.py:3550-3551`), not a live read.
- **The sv5 window is one weekend day.** Any claim about sv5 *rates* rather
  than sv5 *mechanics* is directional. The mechanics (which line zeroes
  which field) are code-verified and do not depend on the window.
- **Ingestion, dedup, propagation, skeptic and merge volumes are UNKNOWN.**
  Three logs the engine already writes (`skeptic_refutations.jsonl`,
  `decisions.jsonl`, `universe_screen.jsonl`) were not supplied. Requesting
  them costs nothing and would fill two dead funnel stages outright.
- **The `already_priced_in` falsification test is suggestive, not
  conclusive.** n=12-24 with overlapping windows and negative medians at
  short horizons. It establishes that individual verdicts are refutable,
  not that the veto is wrong on average.
- **The trade record cannot support any profitability claim.** n=15, all
  from an extinct regime, t=−1.08.
- **Not re-audited:** the entry-timing guards, the paper-trade journal, the
  measurement stack, hardening, and everything else covered by
  `AUDIT-2026-08*.md`. This pass looked only at evidence intake and signal
  conversion.
