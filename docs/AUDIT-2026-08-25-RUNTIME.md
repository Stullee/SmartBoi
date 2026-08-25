# Runtime analysis — 2026-08-25 (0.76.0, bundle `smartboidiagnostics20260825T182521Z`)

Scope: the two-day runtime window the bundle was cut for, read against the
retained log (2026-08-10 11:23 → 2026-08-25 18:24), 59 dossiers, 1,265 graph
edges, 681 signal rows, 1,181 decisions, 5,513 price marks and 586 LLM traces.
Uptime at capture 2d 13h; `restarts/24h 0`.

Method: eleven subsystem lanes read the live 0.76.0 source line-by-line against
the bundle, followed by adversarial refuters that tried to **kill** each
finding. The sweep was cut short for budget, so refutation completed for six
lanes (LLM layer, evidence scoring, synthesis gate, signal→entry, paper P&L,
graph) and did not run for five (ingestion, cost, ops, measurement, strategy).
Findings from the five unrefuted lanes are marked **[unverified]** — they carry
`file:line` citations but nobody tried to break them. Everything else either
survived refutation or was verified by hand here; refuters killed four findings
outright and corrected the numbers on roughly a third of the rest.

---

## Bottom line up front

The evidence engine has been dead since **2026-08-21 19:49 UTC** — 3 days 22
hours at capture — and every other number in this bundle has to be read through
that. A rebuild resolved `anthropic>=0.40` to 1.0.0, which removed `temperature`
from `AsyncMessages.create()`, so every Haiku call site raised `TypeError` in the
client before a request was sent: 109,092 failures, zero evidence merges since
2026-08-22. Synthesis (Opus-5, which sends no temperature) kept running, so
every health surface stayed green and 0.76.0 shipped on 08-23 into a dead
engine. That is fixed and pushed. Underneath it, three things are structurally
wrong and were wrong before the outage: the **-50%/+100% grid is unreachable**
(0 of 413 realized 21-day windows in this universe touched either level, so the
stop and target are decorative and every trade is a forced horizon exit),
**competitor evidence has no sign** (158 competitor-edge records scored 66%
LONG — a competitor's good news is being written down as bullish), and the
**graph refresh deletes an extraction marker before re-extracting**, which
manufactured 36 of the 37 "never extracted" backlog including all four open
positions.

---

## 1. The outage

**Root cause.** `anthropic` 1.0.0 removed `temperature` (and `top_p`/`top_k`)
from `AsyncMessages.create()`. Confirmed by introspecting the wheel:

```
version: 1.0.0
has temperature: False      has output_config: True      has thinking: True
```

`requirements.txt:5` and `pyproject.toml` pinned only `anthropic>=0.40`, and
`ha-addons/smartboi/Dockerfile:21` installs with a bare
`pip install "smartboi @ git+…"` — the app commit is pinned to a SHA with ten
lines of commentary about cache correctness, and its dependencies are then
resolved live from PyPI. `llm.py:60` marks `claude-haiku-4-5` temperature-capable,
so `request_kwargs` added `temperature=0` to every Haiku call.

A fresh `pip install -e ".[dev]"` in a clean container today resolves to 1.0.0
and reproduces the failure exactly.

**Timeline.**

| when | what |
|---|---|
| 2026-08-21 19:31 | last successful dossier LLM call (`llm_trace.jsonl`) |
| 2026-08-21 19:47:38 | SIGTERM restart into 0.75.0 — the rebuild that moved the SDK |
| 2026-08-21 19:49:55 | first `unexpected keyword argument 'temperature'` (`smartboi.log.4:18292`) |
| 2026-08-23 04:39:18 | rebuild to 0.76.0 — still broken, nobody noticed |
| 2026-08-25 18:24 | still failing at capture |

**Blast radius.** 109,092 failed calls. Zero evidence merges after
2026-08-21T18:28:48 (max `merged_at` across all 59 dossiers). All 220 decisions
logged in the outage window are `below_bar` — no `trade_opened`, no
`below_bar_on_merge`, because both require a merge. Dead: dossier updater,
skeptic, relationship extraction, research. Alive: synthesis.

**Why it ran for four days.** Three independent failures of escalation:

1. **The breaker could not see it.** `usage.note_failure` (`usage.py:207`) is
   the right mechanism and is already wired into all five call sites — but
   `permanent_failure_reason` matched only four billing/auth substrings
   (`llm.py:177-182`), so a `TypeError` read as transient. `llm_usage.json`
   still says `breaker_reason: ""` after 109,092 permanent failures. The
   docstring at `usage.py:214` describes this exact bug costing "11,893
   identical billing failures in two hours". The same shape recurred.
2. **Nothing counts failures.** Every bound in the system — call cap, USD cap,
   category share — is incremented in `record()`, reachable only *after* the
   `try` block returns. A 100%-failure loop spends nothing, so it is invisible
   to the only gates that could stop it, by construction.
3. **The health surfaces report configuration, not health.** `webapp.py:1740`
   is literally `"anthropic": engine.updater is not None`, and `engine.py:1057`
   constructs the updater on nothing more than a non-empty API key. The light is
   green whether the engine scored a thousand items or zero. The diagnostics
   printed the clearest symptom in the bundle — `dossier $0.00 0 calls` on the
   highest-volume category — under a footer explaining that as normal.

**Fixed and pushed** (`1340205`, 1091 tests green):

- `temperature` now travels in `extra_body`. The API still accepts it on Haiku;
  the SDK just stopped naming it. `extra_body` is merged into the body verbatim
  and exists across both SDK generations. Verified by binding the emitted kwargs
  against the real installed signature for all 11 models in `_CAPABILITIES`.
- `anthropic` pinned `>=1.0,<2` in `requirements.txt` and `pyproject.toml`.
- A client-side `TypeError` is classified permanent, by **type**, not by message
  text. It now halts the day the way an exhausted balance does. Rate limits,
  connection resets and genuine per-request 400s stay transient, as before.
- `test_every_emitted_kwarg_is_accepted_by_the_INSTALLED_sdk` introspects
  `AsyncMessages.create` and asserts every kwarg `request_kwargs` can emit is
  accepted. **It fails on the tree as it stood.** The reason 1,087 green tests
  caught nothing is that every one of them fakes the client and asserts our
  *model* of the API, never the signature of the SDK actually installed.

Still open from this incident: there is no lockfile or `constraints.txt`, and
nothing anywhere records the installed SDK version — not the diagnostics, not
the logs, not `model_provenance.json`. Add `anthropic.__version__` to the
diagnostics header.

---

## 2. What the outage revealed

Freezing the evidence body accidentally ran a controlled experiment.

**Verdict churn on a frozen board.** Comparing synthesis verdicts across
2026-08-22 → 08-25 over 484 traces, 39 of the 43 symbols judged on ≥2 of those
days returned a different `(direction, already_priced_in, distinct_fact_count,
confidence, magnitude)`. AGEN's direction went SHORT → SHORT → NONE → SHORT.
BKTI — an open position — flipped `already_priced_in` True→False→True→False on
four consecutive days.

The honest reading, after refutation: the *evidence* is frozen but the prompt is
not — it carries a daily price block and today's date, which are legitimate
inputs. A stricter test isolating genuinely identical prompts puts pure sampling
variance at **~5% on direction and ~2.5% on the priced-in veto**, not 90%. So
this is mostly a rational response to a moving price, not a coin flip. What
remains is still a real design gap: `_apply_synthesis` (`engine.py:4447`)
**overwrites** `confidence` and `magnitude` with the verdict and treats
`already_priced_in` as a hard veto, and it re-derives that veto daily from a
moving input with no hysteresis. A name's tradeability oscillates day to day
with no mechanism damping it.

**The zeros are vetoes, not decay.** 34 of 59 dossiers read score 0.000. Every
one has a *non-zero* arithmetic underneath — INTT 0.795, ESOA 0.792, KULR 0.500,
LMB 0.431. The board is not decayed; it is hot underneath and being actively
zeroed. `_below_bar_reason` (`engine.py:3843`) computes its explanation from
`confidence * magnitude` — the already-zeroed fields — and never inspects
`already_priced_in` or `synthesis_direction`, so every log line, expiry reason
and `decisions.jsonl` row blames decay for a synthesis veto. That mislabelling
is why the episode table reads like a decayed board.

---

## 3. Critical

**3.1 The stop/target grid is unreachable — the strategy is a blind 21-day hold.**
`config.py:536-537` sets stop 50% / take 100%; `paper_journal.py:472` sizes them
as `entry*0.5` / `entry*2.0`. Across all 66 tradeables, **0 of 413 realized
21-day windows touched either level**. All four open positions confirm it: 1R is
50% of entry, so AOSL at -19.8% books as -0.396R and BKTI at -1.1% as -0.022R.
The stop never protects and the target never pays; every trade exits at horizon
at whatever the market is. The 50%/100% choice was deliberate (commit `5a6b64d`),
so the defect is narrower than "wrong parameters": the diagnostics still prints
stop/target break-even hit rates for legs that are ~0% reachable, and no module
anywhere reconciles the printed model with the realized one.

**3.2 Competitor evidence has no sign.** `DossierUpdater.propose_update`
(`dossier.py:1334-1338`) never passes `relationship_type` into the prompt. So
when a competitor's good news propagates, nothing tells the model that good news
for a competitor is typically *bad* for the holder. Measured: 158 competitor-edge
evidence records, **66% scored LONG**. Competitors are 528 of 1,265 edges — the
single largest edge class. Refutation trimmed one claim: the free-text
`relationship_note` *is* rendered (`dossier.py:1356`) and usually names the
relation in words, so the model is not sign-blind, only unprompted. The 66%
number stands.

**3.3 The graph refresh destroys extraction markers.** `_run_graph_refresh`
(`engine.py:2886`) unconditionally deletes a symbol's extraction record *before*
re-extraction and only rewrites it on success (`engine.py:28xx`). With extraction
dead since 08-21, every refreshed symbol lost its marker and gained nothing.
This **manufactured 36 of the 37 "never extracted" backlog** the diagnostics
reports — including all four open paper positions. The re-queue is logged
(`engine.py:2890`), the deletion is not.

**3.4 The first signal after a merge is never synthesis-judged.** [survived refutation]
`_maybe_resynthesize` returns immediately unless a verdict is already fresh
(`engine.py:4663`), so a dossier crossing the bar for the first time fires on
raw arithmetic with no whole-body check. Instrumentation added 2026-08-15
(`_synthesis_bypassed`) caught 6 of 6 signals since it existed; the 21 signals
before that carry no instrumentation, so the 100% figure covers only the
instrumented window.

**3.5 Entry prices and validation marks come from different feeds, unrecorded.** [unverified]
Entries price off IB; marks fall back to Finnhub whenever the breaker is open
(402 passes). `price_marks.jsonl` has no source field. The measured disagreement
between the two feeds averages **7.0%** — the same size as the entire original
8% stop budget — and nothing reconciles them. This is the mechanism behind two
other unverified measurement findings: the forward-return join uses different
date calendars on its two halves (entry price a median 24.1h later than the
score it joins to), and nothing excludes the dead-score outage window from the
forward buckets, so 37 zero-score rows per trading day are flowing into the one
bucket whose CI excludes zero.

**3.6 The score may be anti-predictive.** [unverified — confirm before acting]
The strategy lane reports that the system's own forward-validation output shows
the dossier score anti-predictive at every horizon, and that the live 0.25 entry
bar selects into precisely the buckets with negative alpha and sub-break-even
hit rates. This is the single highest-stakes claim in the sweep and it is the
one that did not get refuted. Re-run the forward report with the outage window
excluded (3.5) before believing or dismissing it.

---

## 4. High

- **Stale veto lapses and the arithmetic re-fires.** The synthesis cap expires at
  36h (`engine.py:283`) and `_apply_synthesis` only re-judges above a 0.150 floor
  (`engine.py:4478`). UFPT was vetoed `already_priced_in` on 2026-08-10 09:54;
  the cap lapsed; on 08-13 the raw arithmetic (0.292) cleared the 0.250 bar and
  opened a position at 329.11, three days after the system's own final gate said
  the move was already priced. It is 305.39 today and its verdict is 368 hours
  stale. Refutation correctly notes the lock is not permanent — any merge lifting
  the arithmetic back over 0.150 restores re-judgement — and only 3 dossiers are
  currently stale. Latent trap, not an active fire.
- **Nothing exits a vetoed thesis.** `_close_if_thesis_flipped` (`engine.py:3863`)
  fires only on a direction flip that clears the bar. AOSL is open at -19.8% with
  `already_priced_in: true` and synthesis confidence 0.0 as of 08-25 11:30.
  Entry requires surviving synthesis; staying in does not.
- **The exit side has no opening blackout.** `paper_journal.py:577` guards
  same-day and out-of-hours but has no minutes-into-session test, while entries
  got a 15-minute blackout in 0.76.0. Exits can still book against the opening
  print that entries are now refused on.
- **Signalled episodes are destroyed by universe churn.** Archiving a symbol
  (SCE-PN, SCRNY, TCPA on 08-11; EMBC, MTW on 08-15; ELUT 08-17; AGEN, LONA
  08-18; INVZ 08-22) discards its live SIGNALED episode with no ledger row.
- **232 evidence items exceed the 21-day horizon clamp and never decay.**
  `engine.py:3349` clamps at merge time; `DossierStore.load` does not, and there
  is no prune path. 139 items at 30d, 82 at 45d, 10 at **180d** — every one
  merged on or before 2026-08-10. Their stale cutoffs run 30–360 days against a
  strategy whose max hold is 21. 13 dossiers are inflated by it today; SIF by 47%.
- **The corroboration ceiling is inert on exactly the fan-out it was written for.**
  `MAX_CORROBORATION_DOUBLINGS` resolves to 2.5, and `2**2.5 = 5.657`, so any
  source count ≥6 saturates. DCO's 20 sources and 6 distinct facts produce an
  identical bonus. Inert on 21 of 46 synthesised dossiers, including all four
  widest bodies.
- **Synthesis sees only the last 40 items but is told the full count.**
  `dossier.py:1576` renders `live[-40:]`; `dossier.py:1616` labels the block with
  `len(dossier.evidence)`. 126 items across 7 dossiers are never shown (DCO hides
  56 of 96), and the model answers `distinct_fact_count` over the number it was
  told, not the number it saw.
- **The reported R is not a common unit.** The headline "40% win rate, avg R
  -0.51" was made on the retired 8%/16% grid; 1R there is 6.25× smaller than 1R
  now. The generation key (`config.py:29-45`) maps a missing signature to
  `legacy`, and the pooled line will silently average numbers 6.25× apart. Today
  pooled equals legacy exactly, so the contamination is latent.
- **Every break-even figure assumes the stop holds.** `paper_journal.py:145-159`
  computes break-even from `net_r(stop)`. The realized loss leg is **-1.94R**, so
  the printed 37% break-even bar should read **54%**. `exit_analysis.reward_risk`
  already computes the realized version; the diagnostics prints only the model.
- **Log flood is destroying the baseline.** Burn went 0.026 → 0.199 MB/h (6.3×).
  One repeated string is 66.9% of the retained 26.8 MB. The 30 MB window has
  collapsed from ~40 days to ~6.4 days, and the last pre-outage baseline rotates
  out within about 16 hours of capture. No dedup or rate-limit on repeated
  identical warnings.
- **The IB breaker cannot heal itself.** [unverified] 347 opens at a 30.6-minute
  median cadence, 82.5% duty cycle since 08-24; nothing in the code ever forces a
  reconnect, so it reopens indefinitely.
- **The DoD contract feed has never worked.** 234 requests, **234 HTTP 403s**,
  zero items, ever. `dod_contracts.py:219` documents a previous round of this
  ("159 requests, 159 rejections") and the browser User-Agent added to fix it
  (`dod_contracts.py:248`) did not change the outcome. defense_tier2 is the
  second-largest trade generator (11 of 49 positions) and its dedicated catalyst
  feed contributes nothing.
- **8-K exhibit fetching has produced zero exhibits.** [unverified] The dossier
  engine is being handed "the substance is in the attached exhibit" followed by
  cover-page boilerplate. This is the feature the README calls the system's
  edge over readers who only parse the primary document.
- **Half the graph cannot propagate.** [unverified] 634 of 1,265 edges (50.1%)
  have an endpoint outside the universe. `graph_audit.py`'s report sort order has
  no entry for `ORPHANED_EDGE`, so those findings are silently dropped from the
  printed audit (they do survive in `by_kind`).
- **`max_concurrent_positions=15` is never enforced.** [unverified] It is only a
  sizing divisor; the book has run 34 positions — 227% gross on a EUR 5,000
  account — with an effective breadth of about 3.8 independent bets.

---

## 5. Medium and below (summary)

Evidence/scoring: `relationship_type` empty on 763 of 1,023 propagated items, so
the SCORING_VERSION 6 competitor tightening never fires on the edges it targets.
The news-only bar of 3 sources is in force for only 12 of 59 dossiers.
Cross-source dedup is clean on rewordings but blind to the same event told
differently — 29 same-day cross-publisher pairs sit just under the Jaccard
threshold and each mints a separate "independent" slot.

Cost [unverified]: the daily pass re-runs Opus-5 on every eligible dossier with
no premise check even though `synthesis_keys` is persisted for exactly that
comparison — $6.36 burned on the frozen board, and ~79% waste even when healthy.
Supplier research silently runs on `claude-opus-5` (there is no `research_model`
setting) with 4 calls plus 6 unmetered server-side web searches behind one budget
check. `budget_remaining()` writes to disk and consumes the breaker's recovery
probe token, and the diagnostics renderer calls it four times per run.

Ops [unverified]: `dedup.py` is the one persisted store the 2026-08 durability
fix never reached — no fsync, no quarantine — and it rewrites 3 MB per
fingerprint (~1.8 GB/day at pre-outage rates) onto an SD card.
`UnicodeDecodeError` escapes every quarantine handler. The ib_async benign-error
log filter has never suppressed a record: it is attached to the parent logger,
and Python does not run ancestor filters during propagation.

Graph: a malformed extraction response is recorded as a *successful* backfill —
5 anchors carry permanent done-markers for 10-Ks that produced nothing. This is
documented and intentional (`graph.py:266-274`), but two of the five are now on
the inert-anchor skip list. 86 edges point at financial institutions (20 are BNY
debt-trustee links) and 13 are executive-biography or board-seat edges — all
banned by the current prompt, none removable, because no filter is ever re-run
over `graph.json`.

Strategy: medtech, the "deliberately uncorrelated" sleeve, is genuinely
uncorrelated but generates 1.9% of signal volume — signal rate tracks anchor
*news volume*, not link strength. The book is 42 LONG / 7 SHORT with 61% of
positions in three ecosystems that are all the same capex cycle.
AUDIT-2026-08-FOLLOWUP's HIGH-2 (universe rot) is listed as shipped but catches
0 of the 6 real cases — all six graduated tradeables are curated in
`universe.py`, which the demotion pass skips by construction.

---

## 6. What the record can and cannot tell you

It cannot tell you whether the strategy works. n=15 closed trades, 95% CI
20–64% on the win rate, which spans everything from "broken" to "good". Worse,
the 15 are the survivors of a reset: `archive_open_trades`
(`paper_journal.py:403`) documents that a single reset on 2026-08-09 dropped 30
of the 49 positions opened, and that "the 19 survivors had a median hold of one
session; the 30 archived took a median of nine to ten sessions and seventeen
never resolved at all." For a days-to-weeks repricing strategy, that removed
precisely the observations the premise is about. The archiving fix is in the
tree and is forward-looking — `paper_trades.jsonl` today is
`{'LOSS': 9, 'WIN': 6}` with **zero ARCHIVED rows** — so the 30 are still absent
from the denominator, and the diagnostics still prints "win rate 40%" without
saying so.

Add to that: eight scoring versions and a signal bar that moved 0.500 → 0.300 →
0.250 inside the measurement window, all pooled onto one score axis; and 9 of
the 15 closed trades were opened inside the entry blackout 0.76.0 now refuses —
carrying 5 of the 6 wins.

The forward-validation capture is nonetheless the best-built part of this system
and it is close to trustworthy. Fix the join calendar and exclude the outage
window, and it starts answering the question. Until then, treat every
performance number in the dump as provisional.

---

## 7. Ranked fix list

| # | fix | effort | why here |
|---|---|---|---|
| 1 | **Rebuild the add-on** onto `1340205` | S | the engine is dead right now; everything else is downstream |
| 2 | Exclude the outage window from forward buckets; record the price source on every mark | S | otherwise the next report is poisoned by 4 days of dead-score-vs-live-price rows (3.5) |
| 3 | Pass `relationship_type` into the dossier prompt | S | 528 competitor edges currently score good news as bullish (3.2) |
| 4 | Delete the extraction marker *after* success, not before | S | one-line; stops manufacturing the backlog (3.3) |
| 5 | Re-run the forward report and settle 3.6 | S | decides whether anything below matters |
| 6 | Clamp `horizon_days` on load; prune items past the clamp | S | 232 immortal items, SIF inflated 47% |
| 7 | Add a "thesis vetoed" arm to the exit path | M | AOSL is riding a thesis the system says is void |
| 8 | Print realized break-even alongside modelled; stop printing unreachable-leg stats | M | the 37% bar should read 54% |
| 9 | Gate `_apply_synthesis` on `synthesis_keys` unchanged; add veto hysteresis | M | removes daily oscillation and ~79% of Opus spend |
| 10 | Re-derive dashboard lights from `last_success_at` per category | M | the breaker fix covers permanent errors; this covers the transient-looking 100%-failure case |
| 11 | Rate-limit repeated identical warnings | S | stops the log eating its own history |
| 12 | Replace or remove the DoD feed | M | 234/234 blocked; it is pure noise today |
| 13 | Revisit the grid, or stop describing it as stop/target | L | 0 of 413 windows reached either leg |

Items 1–6 are all small and all independent. Nothing above depends on the
strategy question in 3.6 except item 13.

---

## 8. Checked and clean

- **Graph structure.** 1,265 edges, zero self-edges, zero duplicate
  `(from, to, type)`, 674 at confidence ≥0.9. Only 1.8% below 0.5. The junk
  classes named above are real but small.
- **Price-mark session filing.** I suspected an off-by-one on the 5,025 of 5,513
  marks (91%) written before the `session` field existed. It is correctly
  handled: `forward_returns.py:96` and `backtest.py:299` both fall back to
  `session_for_quote(captured)`, and the docstring cites the exact figure
  (3,992 of 5,025 one session late) as the bug it fixes.
- **Snapshot/price-mark capture skew** was 11.7 hours backwards for 12
  consecutive days (08-03 → 08-14) and was fixed from 08-17 onward.
- **The synthesis gate is the best-reasoned component in the system.** Its 100%
  veto/trim rate is not a broken gate — read the WOLF verdict at
  `llm_trace.jsonl` 2026-08-25T11:36: it correctly identifies that 3 of 5 items
  are one restated fact and that a -21% move over 08-14 → 08-24 already absorbed
  it. **Do not loosen this gate.** The 8.4× ARITH/RATED median gap is the
  aggregator being wrong, not synthesis.
- **The daily USD budget is not binding.** One day only (2026-08-10) hit it, 12
  times, all in `research` — the lowest-priority category, which is the intended
  order.
- **Four findings were killed by refutation** and are not reported: an
  entry-guard unreachability claim and an `entry_attempts` claim, both
  contradicted by the lines they cited; a fact-key "two rulebooks" claim whose
  numbers reproduced but whose conclusion did not follow; and the original
  failure-classifier finding, which is dead because the fix shipped mid-sweep.
