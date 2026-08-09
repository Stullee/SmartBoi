# Full system sweep — 2026-08-09 (follow-up-2 to AUDIT-2026-08)

Third audit cycle, against the **0.50.0** tree (HEAD `c1076ef`, PR #16 merged).
Live ~1 day since the runtime reset on 2026-08-08 (a SCORING_VERSION v4→v5
boundary). Method: **Phase 0** re-verified every prior finding across all five
audit docs against current code (6 adversarial verifiers, 38 ledger entries).
**Phase 1** was an exhaustive fresh 4-dimension pass — 8 diverse finders, each
finding refuted through three lenses (correctness / does-it-reproduce / cost),
majority-refute kills, then a completeness critic — yielding 49 verified
survivors. **Phase 2** grounds both against the live diagnostics bundle. Every
finding carries a `file:line` and a fix sketch. Forward-return rows are never
pooled across the SCORING_VERSION boundary in this analysis; where the *code*
does pool them, that is itself a finding (M1).

**Bottom line up front.** Cycle-2 (PR #16) was a genuinely strong release:
**~19 of the ~30 prior fixable items are confirmed-fixed and wired into the
live path** — the merge-path synthesis cap, the bounded magnitude multiplier,
ecosystem-items-as-mass-not-slots, corrupt-file quarantine+fsync, the IB
circuit breaker, SIGTERM, persisted retry state, the extraction done-marker,
edge upgrade-on-stronger, the symbol-weighted headline, and more. But the
**single most important asset in the system — the forward-return record — is
currently unreadable as a measure of the live strategy**: `scoring_version` is
stamped on every row and read by *nothing*, so the report pools v4 and v5, and
one day past the reset the only horizon-complete rows are *all pre-reset v4*
presented as the current record (M1, HIGH). The live symptom (near-zero
signal, 0 trades) is **not** a bug and **not** "the strategy is switched off":
the disclosed-edge mechanism is fully live; it is finding almost nothing
because (a) synthesis correctly vetoes saturated sector-beta theses as
already-priced-in, (b) the disclosed-edge graph is sparse (221/322 anchors
inert), and (c) IB is down so nothing can open regardless. The blunt verdict
(§4) is unchanged from cycle-2: **no edge can be claimed, and the first
apparent one should be discounted, not trusted.**

---

## Phase 0 — verification ledger (all prior findings vs current code)

Legend: ✅ confirmed-fixed · ◐ partial (fix real, residual gap) · ❌
claimed-but-missing · ⭘ still-open-as-claimed (deferred) · ⚠ regressed.

### Cycle-2 claims that shipped and hold up (✅)

| ID | Finding | Evidence (0.50.0) |
|---|---|---|
| 2.1.2 | Persist synthesis verdict every run | `engine.py:3084-3085` save-on-`changed or synthesized`; `_apply_synthesis` returns True whenever a verdict applied. |
| 2.1.3 | Snapshot records synthesis fields + capped score | `status.py:580-605`; capped `score=conf*mag` since the fields are mutated in place. This is what makes "27/48 vetoed to 0.000" visible live. |
| HIGH-1 | Bounded magnitude corroboration | `dossier.py:550-551` single `doublings=min(_corroboration_doublings, MAX_CORROBORATION_DOUBLINGS=2.5)` feeds BOTH bonuses; mag multiplier capped at 1.625. No residual unbounded path. |
| HIGH-3 | Ecosystem items → mass, not slots | `dossier.py:512-518` `slot_bearing` excludes `is_propagated ∧ rel_conf≤0.25`; still counted in `_side_mass`. `has_filing_evidence` gating (526-531) blocks a 0.25 ecosystem filing from relaxing the news-only bar. |
| 2.3 | Corrupt-file quarantine + fsync | `state.py:22-69` quarantine + double-fsync ordering; shared by `JsonState`/`DossierStore`/`RelationshipGraph`. |
| 2.5 | Persisted retry state | `retry_state.json` (`engine.py:390`), wall-clock stamps, 15-day TTL prune. **(residual: limiters not persisted — see C4)** |
| A4 | IB circuit breaker | `prices.py:67-104` threshold 5 / cooldown 1800s / recover-probe; per-symbol spam → one WARNING. |
| SIGTERM | Graceful stop | `main.py:40-48` installs SIGTERM+SIGINT → task cancel → `run_forever` finally cleanup. |
| price-marks-stall | Bounded by breaker | `engine.py:3266-3318`; first hung pass ~105s then breaker skips IB 30 min. **(residual: intermittent gateway — see C4)** |
| 2.2 | Extraction done-marker | `extracted_filings.json` checked before the paid `_extract_relationships` (`engine.py:1224-1230`), set only on success; defers re-charge scoring not extraction. |
| MED-1 | Symbol-weighted headline | `forward_returns.py:231-243` computes both; `_bucket_table` prints sym-wt as headline. **(but CI mismatched — see M4)** |
| A8 | SCORE_BUCKETS comment | `forward_returns.py:13-24` corrected; both 0.5 and 0.65 are clean bucket edges. |
| 2.7-date | Today's date in grader prompts | `dossier.py:811-812`, `skeptic.py:191-192`; live call sites default `now` to real UTC. |
| MED-4 | Near-dup window widened | `dedup.py:87` `_NEAR_DUP_LOOKBACK_DAYS=5` ≥ 3-day feed lookback; compares full window. |
| MED-edgar | 10-K/10-Q exhibit ordering | `edgar.py:572-585` gated on `8-K`; primary-first for other forms; tested. |
| EDGAR-backoff | 429/503 retry | `edgar.py:286-303` mirrors Finnhub client. |
| HIGH-2 | Universe-rot demotion | `_demote_graduated_tradeables` (`engine.py:3573-3632`) driven off the monthly screen's fresh market data, not the frozen bounds cache. |
| skeptic-readout | Refutation log + report | `skeptic_refutations.jsonl` + `skeptic_report.py` + `proposed_*` now read in 3 places. **(residual: version/window mixing — see M-skeptic)** |
| runtime-reset | Clean measurement window | `engine.py:3420-3472` archives open trades, resets dossiers to ACTIVE, clears synthesis fields, keeps evidence/graph/logs. |

### Cycle-2 claims that are partial — fix real, residual gap (◐)

- **2.1.1 / A1 merge-path synthesis cap — ◐ (residual LOW).** Core fix is real
  and on the live path: `signals.evaluate` (`signals.py:95-98`) gates on
  `dossier.confidence*magnitude`, exactly the fields `_cap_with_synthesis`
  (`engine.py:2461,3187-3224`) mutates. **Gap:** the cap is a *perishable
  re-application* honoured only <36h (`_SYNTHESIS_CAP_MAX_AGE_HOURS`). After a
  trade closes, `_reset_to_active` (`engine.py:2631-2637`) does **not** clear
  synthesis fields (contrast `reset_runtime_state:3457-3463`), and the decay
  pass skips synthesis for open-trade dossiers — so `synthesis_at` freezes for
  the whole hold, goes stale, and the first post-close merge fires on raw
  arithmetic until the next daily pass. Currently unexercised (0 open trades).
  See B-cluster (S1–S4) for the full theme.
- **MED-5 synthesis call-cap fail-open — ◐ (MEDIUM).** The call-count
  reservation is real and wired (`usage.py:155-174,201-203`). **But** the
  fail-open it targets is still reachable on the **dollar** axis under the live
  `$12.76 > $10` overrun: when synthesis is budget-refused, the *decay path*
  runs `evaluate()` on the raw uncapped score with **no `_cap_with_synthesis`
  fallback** (that fallback exists only on the merge path). Fix: give the decay
  path the same fallback (`engine.py:3086`), and stop CAT_DOSSIER overshooting
  the reserved boundary. See S2, O1.
- **2.4 edge upgrade/aging — ◐ (MEDIUM).** Upgrade-on-stronger + `extracted_at`
  refresh confirmed (`graph.py:95-111`); `stale_edges` computed
  (`status.py:293-306`). **Gaps:** (a) `stale_edges`/`edge_age_median` are
  **not printed** in the text diagnostics bundle (`tools.py:520-542`) — invisible
  to the CLI operator; (b) the O(edges²) I/O concern is **worse** now — every
  add/upgrade/re-stamp rewrites the whole `graph.json` + 2 fsyncs, and the new
  re-stamp branch turned the previously no-op re-confirmation into a full
  rewrite. Amplified by the rolling refresh re-reading unchanged 10-Ks.
- **MED-2 synthesis-aware buckets — ◐ (LOW).** Exclusion of `already_priced_in`
  rows works (`forward_returns.py:433-482`) but **4 of 5** snapshot synthesis
  columns are captured-but-unconsumed; it is a band-aid over the version-pooling
  root cause (M1), and a lapsed-cap re-fire (S4) can be mislabeled as a clean row.
- **MED-3 leverage disclosure — ◐ (LOW).** `peak_concurrent` shipped but is
  computed from **closed trades only** (`status.py:405-406`), so it *under*-discloses
  leverage exactly when live positions stack past the slot count.

### Prior claims that are wrong or never implemented (❌)

- **SCORING-VERSION downstream split — ❌ (HIGH).** `reset_runtime_state`'s
  docstring (`engine.py:3430-3432`) claims the version-stamped logs "split
  cleanly at the version boundary downstream, so old rows stay segregated."
  **No such splitter exists.** `scoring_version` is written (`status.py:580`,
  `signals.py:114`) and read by **no** analysis code — grep across
  `forward_returns.py`, `event_study.py`, `exit_analysis.py`, `tools.py`,
  `scripts/` returns zero reads. Elevated to fresh finding **M1**.

### Items deferred that remain open — and whether "deferred" still holds (⭘)

| ID | Finding | Still-deferred defensible? |
|---|---|---|
| A9.1 | Shipped defaults still ship the fan-out chain ON (`enable_ecosystem_propagation`, `auto_accept_*`, backfill, refresh, supplier-research all `True` in `config.py` + `config.yaml`) | **Weakly.** Saturation is now code-contained, but the *cost* half ships on and the live data shows it is waste. A fresh install re-saturates. Flip the cost-only toggles off by default (see Config §5). |
| A3 | Retroactive `is_common_equity` sweep never shipped (accept-time guard only, `engine.py:556`) | **No, less so now.** A pre-guard preferred/ADR whose cap screens "tradeable" stays a live trade target. Add the startup sweep. |
| 2.6 | `independence_key` collapses all same-form EDGAR filings to one slot (`dossier.py:209-240`) | **No, less so now.** It undercounts the *highest-quality* evidence class and now works in the *same suppression direction* as the over-veto (P2). |
| MED-6 | Dashboard LAN exposure: header-presence CSRF ≠ auth; destructive endpoints reachable by any LAN `curl` (`webapp.py:72-77`, `config.yaml:18`) | Only if LAN fully trusted. `runtime/reset` is also destructive but its docstring says otherwise. See O5. |
| A5 | No position-cap enforcement at entry (`engine.py:2868-2869`); disclosure-only | Defensible for a paper-measurement system; disclosure is partial (M7). |
| 2.7-cache | Cost meter ignores cache tokens (`usage.py:227-241`) | Bounded ~2-4% undercount; but it erodes the $-cap safety net that is *already* breached. See L7. |
| 2.7-gapfill | Gap-through-stop fill lacks session open (`prices.py:25-33`) | Latent (IB down); re-emerges on real intraday bars, directionally optimistic. See M6. |
| version-consistency | `pyproject` stuck at 0.1.0; `SMARTBOI_COMMIT` untied to version; `AlertSender`/accept-candidate thinly tested | Low-risk/latent; the live version path (`SMARTBOI_VERSION`) is guarded. See O8. |
| webapp-status-io | `/api/status` does unthreaded disk I/O on the loop every 10s/viewer; `wait_for(8s)` is false protection (no yield) | Low today; thread it for parity with the tool endpoints. |
| LOW | Refresh re-reads unchanged filings; anchor auto-accept skips `name_matches_ticker` | Wasteful; a mis-resolved anchor injects a wrong propagation edge (not merely wasted spend). |

---

## Phase 1 — ranked fresh findings

Each tagged **(Severity / Effort / Impact)**. Effort = implementation size.
Findings are grouped by theme; duplicates from multiple finders are merged.

### A. Measurement integrity — the forward record cannot currently be trusted

**M1 — `scoring_version` is write-only; the forward report, event study, and
exit report pool across the v4→v5 reset. (HIGH / low / high)**
`forward_returns.py:92-146,204-249,391-484`, `event_study.py` (same join),
`exit_analysis.py`, `scripts/analyze_forward_returns.py:64-93`, `status.py:580`,
`engine.py:3430-3432`. Nothing filters or splits on version. **Because the
reset was ~1 day ago and horizons are 5/20 trading days, every horizon-complete
forward-return row today is a pre-reset v4 snapshot** — so the current report is
a *pure pre-reset record labelled as the live v5 strategy*, and true
cross-version pooling begins silently the moment v5 rows mature.
`dedup_snapshots` (keeps first-seen by `(symbol,date)`) even prefers the stale
v4 row on a mixed day. **Fix:** carry `scoring_version` through
`compute_forward_return`; filter to `== SCORING_VERSION` (or group-by version,
headline = current) in `run_forward_returns`/`analyze_*`/`run_event_study`;
assert single-version input in `format_report`; correct the false docstring.

**M2 — generation segmentation is not scoring-version-aware. (MEDIUM / low /
medium)** `config.py:29` `_STRATEGY_PARAM_KEYS` = (stop, tp,
signal_confidence_threshold, tx_profile, drift, horizon) — **`SCORING_VERSION`
is absent.** The v4→v5 reset only forks the trade "generation" because
`signal_confidence_threshold` *coincidentally* moved 0.2→0.5 at the same time.
A future scoring-only change (no trade-param move) will pool old and new trades
into one generation silently — the same disease as M1, one layer down. This is
also why `HIGH-exit-generation-pool-3`'s praise of `status.py:497-522` ("segments
by strategy_key") is weaker than it reads: it segments by trade params, a proxy
for scoring version, not the version itself. **Fix:** add `SCORING_VERSION` to
`_STRATEGY_PARAM_KEYS` / `strategy_key`.

**M3 — the live cost profile is `retail`, understating friction 3-4× exactly
where the strategy trades, and it is the shipped default. (MEDIUM / low /
medium)** `config.py:413` `transaction_cost_profile = "retail"`. Traced:
commit `5a6b64d` (2026-08-07, *before* 0.49.0) flipped it `institutional →
retail` as part of the hold-to-horizon pivot — alongside threshold 0.65→0.5,
drift 5→12, stop 8→50, tp 16→100, **five loosenings in one commit, all toward a
better-looking record, none of them audited.** Retail buckets 15/35/75 bp/side
vs institutional 50/150/300 (`paper_journal.py:34-48`); on the grid the
break-even win rate falls **59% → 34%**. It is used both by live trades
(`engine.py:2859-2862`) and the displayed cost table (`tools.py:594-595`). It
contradicts (a) the inline comment right above it ("Only move this to retail if
the position size genuinely cannot move the book"), (b) `paper_journal.py:41-44`
("Institutional stays the DEFAULT"), and (c) the prior audit's explicit praise
of an institutional default — which was therefore **factually wrong at 0.49.0**.
The `tools.py:617-620` warning against retail self-suppresses under retail (all
retail buckets break even <55%), so the operator never sees it. **Fix:** default
`institutional`; if a permanently tiny book justifies retail, report the record
under *both* (`r_multiple_gross` is already stored). This is the single most
record-flattering setting in the system.

**M4 — bucket 90% CI is a row-weighted bootstrap printed beside a
symbol-weighted point estimate. (MEDIUM / low / medium)**
`forward_returns.py:189-194,383-387`. `cluster_bootstrap_ci` resamples symbols
but then pools all their rows and takes a flat row mean — the bootstrap of the
*row-weighted* mean — while the headline it sits next to is the *symbol-weighted*
mean. So the interval is dominated by the very long-lived thesis the symbol
weighting (MED-1) was introduced to neutralize, and need not even bracket the
point estimate beside it. **Fix:** inside the bootstrap, average per-symbol
means of the resampled clusters, so the CI matches the printed estimator.

**M5 — residual optimistic biases in the fill/censoring model (all lean toward
flattering the record). (LOW / mixed / low-now)** (i) gap-through-stop fills at
`min(stop, close)` not the gapped open (`paper_journal.py:520`; `PriceBar` has
no `open`, `prices.py:25-33`) — truncates fat left-tail losses to −1R; (ii)
delisting/acquisition mid-horizon is dropped as "unjoinable" and lumped with
"too recent" (`forward_returns.py:405-422`), so genuine (extreme-outcome)
censoring is invisible; (iii) **`market_hours.py:11-16` self-documented holiday
gap** — every US holiday is a weekday, so `is_trading_day`/`is_regular_trading_hours`
return True on holidays and there is no half-day (1pm close) handling, so an
entry can book at a holiday's stale prior-close stamped live. Latent while IB is
down and 0 trades are open, but every one re-emerges on the first real v5 trades.

**M6 — `peak_concurrent` disclosure counts closed trades only.** (LOW / low /
low) `status.py:405-406` — under-discloses live leverage exactly when positions
stack past the slot count. Fold `journal.open_trades` (as `[opened_at, now]`)
into the interval sweep.

### B. The synthesis gate is perishable arithmetic, not a durable verdict (one theme)

The cap that makes README point 6 true is real (2.1.1) but its enforcement
lives in mutable score fields re-derived on every merge, with a 36h clock, no
direction memory, and an asymmetric relationship to the cheap skeptic. Five
distinct mechanisms:

**S1 — veto/cap not durable; re-derived every merge. (MEDIUM / medium /
medium)** `dossier.py:602,616` (`_aggregate` recomputes raw conf/mag),
`engine.py:2461` (re-cap). Enforcement is a re-applied cap, not a status
`evaluate` respects, so any path that reaches `evaluate` without the fresh
re-cap fires uncapped.

**S2 — decay-path fail-open under budget exhaustion. (MEDIUM / low / medium)**
`engine.py:3086` runs `evaluate` on raw score when `_apply_synthesis` returns
False (budget-refused/failed) — no `_cap_with_synthesis` fallback on the decay
path, asymmetric with the merge path. Reachable live ($12.76>$10). Fix: call
`_cap_with_synthesis` before the decay-path `evaluate`.

**S3 — direction-flip: a prior-direction verdict is applied to a flipped
thesis. (MEDIUM / low / medium)** `_cap_with_synthesis` (`engine.py:3209-3224`)
has no `dossier.direction` check and stores no `synthesis_direction`; a LONG
`already_priced_in` veto within 36h zeroes a legitimately flipped SHORT (or a
stale LONG trim mis-constrains it). Fix: persist `synthesis_direction`, no-op
the cap on a mismatch, and clear synthesis fields whenever `_aggregate` flips
direction.

**S4 — cap goes stale / never runs → unsynthesized fire. (LOW / low / low-now)**
`engine.py:3060-3063` (decay skips open-trade dossiers), `3129-3131` (synthesis
floor 0.30; below it `_apply_synthesis` returns without refreshing
`synthesis_at` — `CODE-floor-skip-freezes-verdict-4`), `2631-2637`
(`_reset_to_active` leaves synthesis stale). A brand-new dossier that crosses
the 0.5 bar between two daily passes fires with `synthesis_at` empty → cap is a
no-op → first signal unsynthesized. Fix: clear synthesis on `_reset_to_active`;
treat a bar-crossing merge with no fresh (<36h) verdict as a hold, not a green
light.

**S5 — `distinct_fact_count` is computed but wired into no gate. (LOW / low /
medium)** `CODE-distinct-fact-count-unwired-1`. Synthesis' own "N items or N
facts" number — described as the most important judgement in the system and the
direct answer to the corroboration question — is stored and never read by
`_aggregate` or `evaluate`. Fold it into the effective source count so 21 items
behind 2 distinct facts corroborate like 2.

**S6 — model-tier asymmetry: Opus can only CAP; Haiku has unbackstopped VETO
over admission. (MEDIUM / medium / high)** `STRATEGY-cap-only-no-backstop-1` +
`STRATEGY-skeptic-refutes-propagated-3`. The expensive Opus synthesis can only
trim/veto *down*; evidence the cheap Haiku skeptic refutes is dropped before it
ever enters a dossier and synthesis can never restore it. The skeptic's
refute-on-uncertainty default is worst exactly for loosely-connected propagated
(second-order) evidence — the strategy's core signal. Live: all 8 logged
refutations are propagated. Fix: for propagated evidence, make the skeptic
advisory (trim, not drop) or upgrade it a tier; decide from the readout (below).

### C. LLM pipeline correctness & cost

**L1 — permanent errors masked as transient across all LLM call sites. (MEDIUM
/ low / high-if-hit)** `LLM-permanent-error-masked-as-transient-1`. Every call
site wraps `messages.create` in `except Exception → return None`, which the
engine reads as "transient, retry later." A permanent 400/auth/model-not-found
(e.g. a mistyped model id, or an API surface change) becomes a **silent
infinite retry that never scores evidence again** — the exact failure
`llm.py:5-11`'s own docstring warns about, unmitigated. Fix: classify
`BadRequestError`/`AuthenticationError`/`NotFoundError` as permanent → log
ERROR/alert and stop retrying that call shape.

**L2 — the trade-gating skeptic runs on non-reasoning Haiku and hard-refutes on
a truncated response. (MEDIUM / low / medium)** `skeptic.py:206,218-220`.
`effort="high"` is a no-op on Haiku 4.5 (no thinking). And an API *success* with
no tool block returns `{"refuted": True, adjusted→0}` → evidence **dropped**,
whereas an API *exception* returns None → **retried**. A no-verdict success
should retry, not refute (the updater/synthesizer don't hard-refute on
truncation). Fix: on a no-tool-use response, defer/retry, don't refute.

**L3 — SILENT CAP: synthesis input truncated to the newest 40 items while the
block is labelled with the full count. (MEDIUM / low / medium)**
`CODE-synthesis-digest-40-cap-1`. The synthesizer digest keeps only the newest
40 non-stale evidence items but the prompt still reports the full item count, so
Opus judges "are these N items N facts?" on a truncated view while being told it
sees all N. Flagged per the no-silent-caps rule. Fix: cap-and-say-so, or raise
the cap, or summarize the tail.

**L4 — extraction input is uncapped: primary 150k + up to 3×150k exhibits
(~600k chars) on Haiku, billed/commented as "150k". (MEDIUM / low / medium)**
`LLM-extraction-input-1` + `OPS-extraction-not-150k-capped-1` (merged);
`edgar.py:557-585`, `graph.py:181-218`. Extraction owns the input-token bill
(see cost anatomy). Re-truncate the extractor input to an aggregate cap and fix
the comment.

**L5 — non-time-critical extraction/backfill/research run synchronously at full
price. (MEDIUM / medium / medium)** `LLM-batch-extraction-2`; `graph.py`,
`research.py:209`. Extraction and research are explicitly *not* time-critical
(`usage.py:58-69`) yet run as live `messages.create` — the **Batches API is 50%
off** and fits their latency tolerance exactly. Largest single structural
spend-cut available.

**L6 — the same 10-K can be paid-extracted twice via independent poll vs
backfill markers. (LOW / low / low)** `OPS-double-extraction-1`; the poll path
(`extracted_filings.json`) and the backfill path (`backfill_state`) don't share
a marker.

**L7 — cache tokens unpriced → the dollar cap gates on a ~2-4% undercount. (LOW
/ low / low)** `usage.py:227-241`, `llm.py:96-98` (merged
`CODE-cache-token-metering-5` + `LLM-cache-token-undercount-1`). Bounded, but it
compounds the already-breached ceiling (O1). Pass
`cache_creation_input_tokens`/`cache_read_input_tokens` and price at 1.25×/0.1×.

*Cost anatomy (context, not a defect — `LLM-cost-anatomy-0`):* **dossier**
(updater+skeptic, uncapped) owns the dollars and calls — $6.46 / 1864 calls,
roughly doubled by the skeptic pass; **extraction** owns the input-token bill —
$5.28 on 99 calls (~53k tok/call, 150k-char filings on Haiku); synthesis is
cheap ($1.02/27). *Opus-5 synthesis EARNS its keep and should NOT be cut*
(`LLM-synthesis-keep-7`): 27 calls for the only whole-body priced-in judgement,
and it is what is correctly vetoing the saturated theses. The levers are the
skeptic (S6) and extraction (L4/L5), not synthesis.

### D. Budget & operability

**O1 — the "hard" dollar ceiling was breached live; the breach is invisible.
(MEDIUM / medium / high)** Total spend **$12.76 vs a $10 cap (+28%)**, extraction
**$5.28 vs its $3.50 category cap (+51%)**. `budget_remaining` is a *pre-call*
gate (`usage.py:181-225`) with no pre-charge estimate, and per-call cost is
variable and large (150k-token 10-K extractions; adaptive-thinking Opus), so the
ceiling is soft. Extraction is sequential (`engine.py:1168-1170`), so +51%
exceeds one call's slop — the runtime `llm_usage.json` is needed to fully
diagnose (overlapping pass or accounting gap). The breach is only logged at INFO
(`OPS-budget-soft-cap-unsurfaced-1`) — never WARN, never alerted. Fix: subtract
an input-size estimate (chars/4) before the gate; hard-refuse a category within
one worst-case call of its cap; WARN on any category or total overshoot.

**O2 — no watchdog alert for any of the three live failure modes. (MEDIUM /
medium / high)** `OPS-alert-blindspot-1`; `alerts.py`. Budget-over, IB-down, and
**zero-signal/zero-trades** all persisted for a day with nothing escalating
(webhook disabled, and even enabled it watches none of these). For an unattended
add-on these are the three states an operator most needs pushed. Fix: a small
watchdog that fires the alert channel on budget>cap, IB breaker open >N hours,
and no signal in >M days.

**O3 — the dashboard "IB price feed" indicator shows configuration, not
connectivity. (MEDIUM / low / medium)** `OPS-ib-cap-health-1`. A dead Gateway
reads green because the badge reflects `ENABLE_IB_PRICE_FEED`, not the breaker
state — so the operator has no surface telling them entries are silently
disabled. Fix: render `price_feed` breaker/last-success state.

**O4 — the monthly universe screen prunes/archives a *live* accepted symbol on a
transient Finnhub failure. (MEDIUM / medium / medium)** `OPS-prune-transient-1`.
`_prune_dead_symbols` acts on `market_cap is None`, which a rate-limit/timeout
also produces, so a transient outage during the monthly screen can archive a
real tradeable and its dossier. Fix: distinguish "no data" from "lookup failed"
(retry/skip on error, only prune on a confirmed empty result).

**O5 — destructive reset endpoints reachable unauthenticated on the LAN. (MEDIUM
/ low / medium)** `webapp.py:72-77,1182-1217`, `config.yaml:18`. `host_network`
+ `0.0.0.0:8100`; the `X-SmartBoi-Request` header is a CSRF defense (presence
only), not auth, so any LAN `curl` reaches `reset-accepted`, `runtime/reset`
(destructive to the live measurement window, though its docstring denies it),
`supplier-research` (real money), `rebuild-graph`. Fix: bind `127.0.0.1` behind
HA ingress, or require an `hmac.compare_digest` token on POSTs.

**O6 — the paper-journal open-state write is the one store cycle-2's durability
fix missed. (MEDIUM / low / medium)** `paper_journal.py:363-368,324-332`:
`write_text`+`replace` with no fsync, and a corrupt file returns `{}` with no
quarantine — unlike `state.py`. An unclean power-off can silently wipe all open
paper trades. Fix: route through `state.atomic_write_json` + `quarantine_corrupt_file`.

**O7 — IB-unreachable WARNING logged on every `ensure_connected` failure. (LOW /
low / low)** `OPS-ib-warn-not-ratelimited-1`; defeats the engine's rate-limited
warn. **O8 — `pyproject` version 0.1.0; dashboard hard-codes v0.44.0. (LOW /
low / low)** cosmetic version drift.

### E. Robustness / concurrency / coverage

**C3 — money-path test coverage gaps. (MEDIUM / low / medium)** Position sizing
(`engine.py:2868-2869`) and the open-path cost-bucket wiring
(`engine.py:2853-2875`, only `>0` asserted; the traded fixture symbol has no
market cap so bucket/profile/×2 are never exercised) have no engine-level test —
a regression dropping `/slots`, the retail profile, or the ×2 round-trip ships
green. Add engine tests through the signal→`_mark_and_execute` path with known
capital and a small-cap market cap.

**C1 — non-atomic close→reset can double-count an episode. (LOW / low / low)**
`engine.py:2960-2974`; a crash/SIGTERM between close-persist and
`_reset_to_active` leaves a SIGNALED dossier whose episode already closed, and a
second trade can open on the same episode. **C2 — `to_thread` diagnostics
iterates live mutable engine dicts. (LOW / low / low)** `webapp.py:1178`,
`tools.py:427,580` — `dictionary changed size during iteration` 500 or torn
counts (live-reachable via `dedup._seen` on any news poll). **C4 —
SlidingWindowLimiters not persisted / monotonic (`engine.py:350-362`)** →
restart resets the propagation/ecosystem fan-out cooldowns; the intermittent-IB
tail also isn't covered by the consecutive-only breaker (price-marks-stall
residual). Snapshot dicts before offload; persist limiter events.

### F. Strategy design

**P1 — no propagation edge is demonstrated as surviving containment; the
disclosed-edge graph is sparse. (MEDIUM / — / high)** `STRAT-edge-containment-verdict-1`
+ live graph health: 221/322 anchors inert (no disclosed edge to a tradeable), 5
tradeables (CVLG HURC INTT PLAB PLPC) carry a thesis with *no* graph edge at all
(so the cross-company mechanism — the actual edge — never fired for them), 9/54
tradeables disconnected. The strategy is, in practice, running substantially on
direct filings, not second-order inference, for a large slice of the book. This
is the signal bottleneck (see §3).

**P2 — the source-independence gate is thinner than it looks. (LOW / low /
low)** `STRAT-source-gate-collapse-1` + prior 2.6: `min_independent_sources=2`
is satisfiable by one own filing + one news item; and the same-form EDGAR
independence key (`dossier.py:240`) *undercounts* genuine multi-event filing
corroboration — a suppressor now working in the same direction as the over-veto.

**P3 — the 50%/100%/21-day grid is incoherent with a days-scale diffusion
thesis. (LOW / low / low)** `STRAT-stop-tp-horizon-1`. At 50% stop / 100% TP
over 21 days on small caps neither bound typically binds; exits are dominated by
the horizon close, so the 2:1 R apparatus (`trade_economics`) measures a grid
that rarely triggers. The diffusion thesis ("the market connects the link in
days-to-weeks") argues for a *shorter* horizon and tighter, ATR-scaled bounds.

**P4 — `has_disclosed_link_evidence` relaxes the source bar on a single
unverified Haiku edge-confidence ≥ 0.85. (LOW / low / low)**
`STRAT-disclosed-link-trust-1`. One extraction-time Haiku confidence number
decides which corroboration bar applies; a single hallucinated 0.85 edge lets a
news-only thesis clear the relaxed bar.

### Completeness-critic residue (uncovered surface, now checked)

- **`market_hours.py`** holiday/half-day gap → folded into M5.
- **`skeptic_report.py`** `refutation_rate = n_refuted/(n_accepted+n_refuted)`
  mixes a persistent append-only log (numerator) with live-only dossiers
  (denominator), with no shared window or version filter — so the rate biases
  upward over time and pools across the reset. Fold into M-skeptic/M1. Run
  `tools.run_skeptic_report` to get the *actual* refutation rate and re-scaling
  distribution — it was not in the bundle and is the decision-grade number for
  S6.
- **`ratelimit.py`** would_allow→record TOCTOU: safe only if callers are serial;
  verify no `await`-interleaved fan-out tasks pass the gate for the same key
  (relates to O1's overshoot).
- **HA packaging** (`_addon_options.py`/`addon_entrypoint.py`): split
  data/log dirs (`/config/smartboi_run` vs `/config/smartboi_logs`); confirm
  pydantic round-trips stringified bool/list options.
- **Currency overlay** examined and **benign**: EUR `position_value` × unitless
  return fraction, bps costs unitless — dimensionally correct, no FX bug.

---

## 3. The closest three levers to more / better signal

The live picture: the disclosed-edge mechanism **is running** (confirmed —
`_can_produce_evidence`, `engine.py:2037-2042`, and the live GOOGL/XOM/CAT
propagation cooldowns); `enable_ecosystem_propagation=False` disables only the
*guessed-sector* fan-out (correctly — it was the saturation noise). So the near-zero
signal is not "the strategy is off." It is a sparse graph + correct vetoes + a
dead price feed. The three highest-leverage moves, in order:

1. **Densify the disclosed-edge graph — the signal is gated by connectivity,
   not thresholds.** 221 inert anchors and 5 edge-less tradeables are latent
   capacity producing nothing. This is compatible with a frozen *tradeable*
   universe (anchors and edges never enter the trade cross-section), so it does
   **not** confound the measurement window: keep `auto_accept_tradeables=false`
   but let edge/anchor growth run — accept the pending anchor candidates that
   would create edges, and lean on `enable_auto_supplier_research`/graph-refresh
   to build disclosed customer/supplier edges for the inert anchors and the 5
   edge-less tradeables first. Add the anchor `name_matches_ticker` guard
   (`engine.py:1726`) before re-opening anchor growth so a mis-resolved ticker
   can't inject a wrong edge. *More genuine (disclosed-edge) signal, no
   measurement cost.*

2. **Stop the cheap skeptic from silently deleting the edge (S6), and measure
   it first.** Propagated second-order evidence is the strategy's entire signal,
   and the Haiku skeptic's unbackstopped refute-on-uncertainty is aimed straight
   at it (all 8 live refutations were propagated). Run `run_skeptic_report`
   *today* for the real refutation rate and re-scaling distribution (it was not
   in the bundle); then make propagated-evidence refutals *advisory* (trim, not
   drop) or upgrade the skeptic one tier — the containment fixes freed the budget
   to afford it. *Better (retained) signal — recovers real second-order evidence
   the current pipeline discards.*

3. **Make the record able to tell good signal from bad, then tune to it.** Right
   now you cannot: M1 (version pooling) means the forward report evaluates
   pre-reset rows, M3 (retail cost) flatters every trade, M4 (mismatched CI) hides
   single-thesis domination, and S5 (`distinct_fact_count` unwired) throws away
   the one number that separates "10 facts" from "1 fact ten times." Fix M1/M3/M4
   and wire S5, and the daily dossier×price-mark cross-section becomes a real
   threshold counterfactual — the only instrument that can point at *which*
   signals are worth more. *Better signal selection, by making the edge
   measurable at all.*

Note what is **not** on this list: re-enabling ecosystem propagation (it was
noise), lowering the signal threshold (fires more saturated theses synthesis
would veto anyway), or cutting Opus synthesis (it earns its keep). More signal
comes from a denser real graph and a skeptic that doesn't eat the edge — not
from loosening the gates.

---

## 4. Blunt verdict — profitability and live-readiness

**Profitability: no edge can be claimed, and the machinery cannot yet measure
one.** The current-generation (hold-to-horizon v0.50.0) record has **zero closed
trades**. The only closed history is 15 legacy trades: **40% win, −0.51 avg R,
CI 20–64%** — net-negative, and net-negative *even under the generous retail
cost assumption* (M3); under the institutional default it is worse. The
forward-return cross-section that is supposed to substitute for the thin trade
record is, one day past the reset, **entirely pre-reset v4 rows mislabelled as
the live strategy** (M1) — so the report a reader would consult to judge the edge
is currently measuring the *old* rules. Meanwhile synthesis is vetoing ~27/48
above-floor dossiers as already-priced-in, which — given they are saturated
AI-capex / defense / oilfield sector-beta theses — is very likely *correct*: the
honest reading is that after cycle-2 removed the fan-out inflation, **there is
very little un-priced-in second-order signal left in the current universe**, and
what remains is throttled further by a sparse graph (P1) and an edge-eating
skeptic (S6). The strategy thesis remains coherent; the demonstrated edge is
zero, and the first apparent one should be discounted.

**Live-readiness: not ready.** IB Gateway is down (`ConnectionRefused 4002`)
and the feed was then disabled, so **no position can open or mark regardless of
signal** — the "0 trades" is partly a dead price feed, not only weak signal, and
nothing alerts on it (O2, O3). The daily budget is **28% over its own $10 cap**
with the breach invisible (O1). The forward record is not trustworthy until M1
ships. None of these is catastrophic — it is paper-only by construction
(re-confirmed: no order path anywhere) and the data is durable — but it is a
system that currently cannot trade, cannot measure, and cannot tell its operator
any of that. Fix M1 (measurement), O1–O3 (budget/IB visibility + alerting), and
restart IB, and it is back to being a clean measurement instrument that is
honestly reporting "no edge yet."

---

## 5. Recommended configuration

You asked what settings I'd recommend. Split into **keep**, **change now**, and
**fix-in-code-then-decide**. Current live overrides are noted.

**Keep (these are the right calls for a clean measurement window):**
- `enable_ecosystem_propagation = false` (live: off). The guessed-sector fan-out
  was the saturation noise; off is correct. Do **not** turn it back on.
- `auto_accept_tradeables = false` (live: off). Freezing the *tradeable* set
  keeps the forward cross-section clean. Keep it off for the window.
- `signal_confidence_threshold = 0.5`, `min_independent_sources = 2 / 3
  news-only` — leave as-is; the problem is not the bar, it is graph density and
  the skeptic.
- Opus-5 synthesis — keep. It is cheap and it is doing the one job (veto
  priced-in theses) that the arithmetic cannot.

**Change now (config-only):**
- `transaction_cost_profile: retail → institutional` (or read the record under
  both). Retail understates cost 3-4× where you trade and flatters a
  net-negative record into looking near-breakeven (M3). The asymmetry argument in
  the code's own docstring is right: an understated cost manufactures an
  unrecoverable phantom edge.
- Reconsider `auto_accept_anchors = false → true` **specifically to densify the
  graph** (lever #1) — but only *after* the anchor `name_matches_ticker` guard
  ships (`engine.py:1726`), and understanding that with ecosystem propagation
  off, a new anchor only matters once it earns a *disclosed* edge, so this grows
  signal capacity without re-saturating scores. If you'd rather not touch
  auto-accept, instead lean on `enable_auto_supplier_research=true` (already on)
  and the graph refresh to build edges for the inert anchors.
- Treat `max_daily_usd = 10` as **~$13 effective** until O1/L7 ship — the
  ceiling is soft and currently breached. If $13/day is unacceptable, the real
  lever is the skeptic 2× cost (S6) and batchable extraction (L5), not lowering
  the cap (which just starves synthesis into the fail-open, S2).
- If IB is intended to stay down for now, set `enable_ib_price_feed = false` to
  stop the ERROR spam and make "no trades" an intentional, quiet state rather
  than a silent failure. Otherwise, restart/repair the Gateway on `127.0.0.1:4002`.

**Fix-in-code, then decide (not a toggle):**
- **M1 is the blocker on believing any forward number** — until
  `scoring_version` is filtered in the report, do not read the forward-return /
  event-study / exit output as evaluating the live v5 strategy.
- Flip the **shipped defaults** (`config.py` + `config.yaml`) for the cost-only
  fan-out toggles to `false` (A9.1) so a fresh install doesn't re-saturate, and
  extend the drift-guard test to cover them. This is for the repo, not your live
  instance (which already overrides them).

Net: your instinct to contain the fan-out was right, and cycle-2 made it stick.
The remaining moves are (1) let the *graph* grow while the *tradeable set* stays
frozen, (2) switch the cost profile back to honest, (3) make the record
version-aware, and (4) stop the skeptic and a soft budget from silently
distorting the two things — retained signal and spend — you most need to trust.
