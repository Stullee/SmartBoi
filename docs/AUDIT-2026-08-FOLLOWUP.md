# Full system sweep — 2026-08-08 (follow-up to AUDIT-2026-08)

Scope: a fresh, whole-system multi-agent audit of the **0.49.0** tree,
run in two parts as the request asked. **Part 1** re-verifies every
fixable finding in `AUDIT-2026-08.md` (2026-07 → 0.47.0) against the
current code, to see what the two intervening releases (0.48.0, 0.49.0)
actually fixed. **Part 2** is an independent fresh audit of all eight
subsystems — codebase, functionality, projected profitability, hardening,
relationship-graph edge building, news processing/sourcing, and system
maintenance — looking for what the prior passes missed.

Method: nine deep-dive agents (one fix-verifier + eight subsystem
auditors) reading the live source line-by-line, followed by adversarial
verifiers that tried to **refute** every high/critical finding against the
code before it was allowed to stand. The three highest-stakes new findings
(the unbounded magnitude multiplier, the universe-rot reconcile guard, the
synthesis call-cap fail-open) were then re-checked by hand against the
source. Every claim below carries a `file:line` in the 0.49.0 tree.
**591 tests green** at the time of review (`pytest -q`). Nothing already
found in `AUDIT-2026-07*.md` or `AUDIT-2026-08.md` is re-reported except to
mark whether it was fixed.

Bottom line up front: **of ~18 distinct fixable items in AUDIT-2026-08,
about two-and-a-half shipped** — synthesis-verdict *persistence* (2.1.2),
the *snapshot columns* (2.1.3), and the pyyaml/schema-drift dev-dep — plus
per-trade traceability (an "add next" item, not a finding). The single
load-bearing gap (2.1.1, the merge-path synthesis cap) was **deliberately
deferred**, and every money/robustness fix and the whole config-only
containment are **untouched**. The fresh audit then found that the same
gap has more faces than the prior pass realized: the pass that is supposed
to be the system's final safety check is bypassed on the path that
actually opens trades, through at least four independent mechanisms.

---

## Update — fixes implemented on this branch (2026-08-08)

Implemented on `claude/full-system-audit-46xlq3` (616 tests green).

**Synthesis-gate + saturation (SCORING_VERSION 4→5):**

- **Merge-path synthesis cap (2.1.1 / A1)** — `engine._cap_with_synthesis`
  re-applies the persisted synthesis verdict (veto or trim) after every
  evidence merge, so a synthesis-blocked thesis no longer re-fires on the raw
  arithmetic. Cheap (no new LLM call), freshness-gated at 36h, cap-never-lift.
- **Bounded magnitude corroboration (HIGH-1)** — the magnitude multiplier is
  capped at the same doublings as the confidence bonus
  (`MAX_CORROBORATION_DOUBLINGS`), so fan-out mass can no longer saturate the
  score.
- **Ecosystem items → mass, not slots (HIGH-3 / A2)** — ecosystem-association
  evidence (relationship confidence ≤ `ECOSYSTEM_ASSOCIATION_CONFIDENCE`) no
  longer mints an independent-source slot, only decay mass.
- **Synthesis call-cap reservation (MED-5)** — the reservation now protects
  synthesis's *calls*, not just its dollars, so fan-out can't starve the
  whole-body cap into failing open.
- **Universe-rot demotion (HIGH-2 new)** — the monthly screen now demotes a
  runtime-accepted tradeable that has graduated past the cap ceiling to an
  anchor, driven off the screen's own re-fetched market data.

**Robustness / hardening:**

- **Corrupt-file quarantine + `fsync` (2.3 + MED)** — `state.py` now writes
  durably (`fsync` of file and dir before/after rename) and QUARANTINES an
  unreadable file (`<name>.corrupt-<ts>` + loud warning) instead of silently
  starting fresh and letting the next save clobber it. Shared by `JsonState`,
  `DossierStore` and `RelationshipGraph`.
- **Persisted retry state (2.5)** — `_handled_outcomes` / `_pending_proposals`
  now persist to `retry_state.json` with wall-clock stamps, so a restart can
  no longer forget a refuted marker and let a second nondeterministic skeptic
  run accept what the first refuted.
- **IB circuit breaker (A4)** — `prices.py` opens a data-level breaker after N
  consecutive call timeouts (skips IB → Finnhub for 30 min, one probe to
  recover), and collapses the per-symbol traceback spam into one summary
  WARNING. Removes the ~10.5-min-per-cycle / ~73-min whole-universe stall
  under a half-dead Gateway.
- **SIGTERM handler** — `main.py` translates SIGTERM/SIGINT into task
  cancellation so `run_forever`'s cleanup runs on a normal HA/Docker stop.

**Measurement de-biasing (makes the forward record trustworthy):**

- **Symbol-equal-weighted headline (MED-1)** — `bucket_returns` now reports
  symbol-weighted mean/hit-rate (one vote per symbol, matching the
  symbol-clustered CI) as the headline, so one long-lived thesis can't drive a
  "97% hit rate"; the row-weighted figure is kept but labelled.
- **Synthesis-aware forward buckets (MED-2)** — `compute_forward_return`
  carries `already_priced_in`, and `format_report` excludes synthesis-vetoed
  snapshots from the tables (reporting them separately), so pre-v5 uncapped
  vetoed scores can't contaminate the top bucket.
- **Leverage disclosure (MED-3 / A5)** — `PaperTradeStats` now surfaces
  `peak_concurrent` vs `max_concurrent_positions`, and the dashboard flags the
  currency equity as levered when peak concurrency exceeded the slot count.
- **SCORE_BUCKETS comment (A8)** — corrected the stale "0.65 is the default
  threshold" note (the shipped default is 0.5; both are bucket edges).

**News processing & evidence quality:**

- **Today's date in the grader prompts (2.7)** — `DossierUpdater.propose_update`
  and `Skeptic.review` now prepend `Today: <date>` (the synthesizer already
  had it), so the "old/already-priced-in news treated as new" judgment is
  computable against the evidence's published date instead of the model
  anchoring "now" to its training cutoff.
- **Widened near-dup window (MED-4)** — `dedup.find_near_duplicate` compares
  against the last `_NEAR_DUP_LOOKBACK_DAYS` (5) rather than same/previous day
  only, so a reworded republish 2+ days later (weekend syndication) can no
  longer buy a second "independent" source.
- **10-K/10-Q exhibit ordering (MED-edgar)** — `fetch_evidence_text` now leads
  with the primary document for 10-K/10-Q/424B5/SC 13D (it is the substance),
  exhibits-first only for 8-Ks, so head+tail truncation can't drop a filing's
  own MD&A/customer-concentration disclosures behind a routine EX-10 contract.
- **EDGAR 429/503 backoff** — `EdgarClient._throttled_get` now retries a
  transient SEC response with backoff, mirroring the Finnhub client.

**Cost:**

- **Extraction done-marker (2.2)** — a persisted per-accession
  `extracted_filings` marker is checked before the paid relationship
  extraction and set after it runs, so a filing whose dossier scoring defers
  on an exhausted budget retries only its scoring, not the ~150k-char
  extraction call it already paid for.

**Graph edge lifecycle & operability:**

- **Edge upgrade-on-stronger + aging (2.4)** — `graph.add` now supersedes a
  weaker edge when a stronger disclosure arrives (so a 0.55 passing-mention no
  longer permanently blocks a 0.95 quantified-concentration edge below the
  disclosed-link bar), and refreshes `extracted_at` on every re-confirmation;
  `gather_graph_health` surfaces `stale_edges` (not re-confirmed in ~120 days).
- **Runtime reset (`reset_runtime_state` / "Reset signals & trades" button)** —
  archives all open paper trades and resets dossiers to ACTIVE for a clean
  measurement window after a scoring-rules change, keeping evidence, the graph,
  and the version-stamped forward logs.

**Instrumentation:**

- **Skeptic-effect readout (§3.2 / the LLM-budget agent's key finding)** — the
  engine now logs every skeptic refutation to `logs/skeptic_refutations.jsonl`
  (they previously left no record, so the refutation rate was unmeasurable),
  and a new pure module `skeptic_report.py` + `tools.run_skeptic_report` +
  `scripts/analyze_skeptic.py` compute the refutation rate, the up/down/
  unchanged re-scaling distribution, and mean confidence/magnitude deltas —
  overall, by direct-vs-propagated, and by model. This is the data needed to
  decide the skeptic's model tier (previously "built but dead" instrumentation).

Still open (each a deliberate decision or low-severity polish): the config-only
containment defaults (A9.1 — the code now contains the *saturation*
mechanically, so this is a measurement-window/spend choice, not a safety one),
position-cap *enforcement* (A5 — the disclosure ships; enforcement stays
deferred to real order placement per the documented design), the dashboard LAN
exposure (accepted by the operator), the direct-filing independence key (2.6),
cache-token pricing and the gap-through-stop fill (2.7, both minor), a
dashboard button for the skeptic readout (runnable via tool/script today), and
the version-consistency guard. The findings below are the full audit as first
written.

---

## 0. The one thing to take away

**The daily synthesis verdict — the system's most expensive, most
carefully-prompted judgment, the only pass that can answer "are these 21
items 21 facts or one fact restated" and "has the market already made this
connection" — does not gate the trades that open.** Six findings from four
different agents converge on this single point:

1. The **merge path never consults synthesis** (`engine.py:2356-2369`;
   `signals.evaluate` at `signals.py:59-115` reads only
   confidence/magnitude/source-count, never `synthesis_confidence` or
   `already_priced_in`). This is prior finding **2.1.1**, confirmed
   deliberately still-open by the PR#15 commit message. Trades open on the
   merge path minutes after an item lands; synthesis only refreshes once a
   day. *(confirmed by adversarial verifier)*
2. A synthesis **veto is erased by the very next merge**: `merge_evidence`
   → `_aggregate` recomputes confidence/magnitude from raw arithmetic
   (`dossier.py:495,522`), wiping the zeroed cap; `already_priced_in` stays
   `True` on the dossier but nothing reads it again. A thesis vetoed
   Monday re-fires the instant one headline merges Tuesday. *(confirmed)*
3. The **arithmetic aggregate saturates**: the magnitude corroboration
   multiplier is **unbounded** while the confidence one is capped, so
   fan-out mass alone converts a sub-threshold thesis into a maxed-out one
   — exactly the error synthesis exists to catch, on exactly the path it is
   bypassed (§3, HIGH-1). *(confirmed)*
4. On heavy fan-out days synthesis **fails open**: its budget reservation
   protects dollars but **not the call cap** that fan-out exhausts, so on
   the busiest evidence days the cap silently does not run and signals fire
   uncapped (§3, HIGH-5). *(hand-verified)*
5. Ecosystem-propagated items **inflate the very source count** synthesis
   would discount, and are counted as independent corroboration (§3,
   HIGH-3). *(confirmed)*
6. The **forward record cannot see any of this**: the snapshot now stores
   the synthesis columns (2.1.3, fixed) but the forward-return report never
   reads them, so uncapped merge-path snapshots pool into the top score
   bucket — the exact region the report exists to evaluate (§3, MED-2).

This is not six problems. It is one problem — *the safety pass is not on
the trade-opening path* — with six faces, and it is the highest-leverage
thing in the system to fix. It interacts with, and is amplified by, the
**universe saturation** (A2) which is also entirely un-contained: every
default still ships the full fan-out chain on.

---

## 1. Fix status vs AUDIT-2026-08

`config.py` has **no diff at all** between the audited tree (261d129) and
HEAD, so none of the recommended flag flips landed. The changes that did
ship are confined to `dossier.py`/`engine.py`/`status.py`/`paper_journal.py`
(PR#14, PR#15).

| # | Finding | Status | Evidence (0.49.0) |
|---|---------|--------|-------------------|
| 2.1.1 | Merge path applies the synthesis cap | ❌ **not fixed** (deliberately deferred) | `engine.py:2356-2369`, `signals.py:59-115` — evaluate has no synthesis input |
| 2.1.2 | Persist synthesis verdict after every run | ✅ **fixed** | `engine.py:2962-2980` saves on `changed or synthesized`; `c43becf` |
| 2.1.3 | Record synthesis fields + capped score in snapshot | ✅ **fixed** | `status.py:531-554`; SCORING_VERSION 3→4 `dossier.py:285-296` |
| 2.2 | Per-accession "extracted" marker before re-extracting | ❌ not fixed | `engine.py:1189-1237` — extract before score, fp registered only on definitive handling; 833c4af is candidate-count dedup, not an extraction marker |
| 2.3 | Corrupt-file quarantine + warn | ❌ not fixed | `dossier.py:166-168`, `graph.py:43-50`, `state.py:15-19` — all still silent start-fresh (state.py doesn't even log) |
| 2.4 | Graph edges upgrade-on-stronger & age | ❌ not fixed | `graph.py:73-88` — `add()` returns False on an existing edge without touching confidence/`extracted_at` |
| 2.5 | Persist retry caches across restarts | ❌ not fixed | `engine.py:355,365`, limiters `331-343` — all in-memory |
| 2.6 | Independence key too blunt for direct filings | ❌ not fixed | `dossier.py:236-238`; source_name = `SEC EDGAR (<form>)` collapses all one-form filings to one slot |
| 2.7-date | Today's date in updater/skeptic prompts | ❌ not fixed | `dossier.py:742-747`, `skeptic.py:185-191` have no reference date (synthesizer does, `dossier.py:920`) |
| 2.7-cache | Cost meter ignores cache tokens | ❌ not fixed | `usage.py:200` prices input/output only (impact genuinely small — see §3 MED-6 note) |
| 2.7-gapfill | Gap-through-stop fills lack session open | ❌ not fixed | `prices.py:24-32` PriceBar has no `open`; `paper_journal.py:504` |
| 2.7-neardup | Near-dup window same/previous UTC day | ❌ not fixed | `dedup.py:141-144` — window (2d) is *narrower than* the 3d feed lookback (§3 MED-4) |
| 2.7-autoaccept | Auto-accept vs frozen-universe tension | ❌ not fixed | `config.py:504,527` unchanged |
| A1 | Synthesis bypass firing live | ⚠️ mechanism **intact** | The trade-gating path is byte-for-byte the 2.1.1 bypass; only the record side (2.1.2/2.1.3) changed → bypass is now *visible* but not *stopped* |
| A2 | Universe + ecosystem fan-out saturation | ⚠️ mechanism **intact** | Every link present; all defaults on (`config.py:347,504,512,527`) |
| A3 | Retroactive `is_common_equity` sweep | ◐ **partial** | Accept-time guard ships (`engine.py:530`); the *retroactive sweep* to demote pre-guard preferreds/ADRs is absent |
| A4 | IB data-level circuit breaker | ❌ not fixed | `prices.py:53` per-call timeout only; no consecutive-failure breaker anywhere |
| A5 | Position-cap enforcement at entry | ❌ not fixed | `engine.py:2763-2764` sizes at capital/slots with no open-count check |
| A6 | Budget run-rate vs target | ◐ **partial** | Hard $ ceiling now real (`usage.py:183-186`); but the saturation drivers that blew the target are all still on (see A2) |
| A7 | Publisher-identity thinness | ◐ partial / obviated | URL-domain fix landed (`engine.py:2048`); the *conclusion* stands — ~6 names, one aggregator ~69% |
| A8 | Stale SCORE_BUCKETS comment | ❌ not fixed | `forward_returns.py:16` still says "0.65 … matches the default"; default is 0.5 |
| A9.1 | Config-only containment (anchors off, ecosystem off) | ❌ **not applied** | Highest-leverage, zero-code item; not shipped in code or add-on defaults |

**Schema-drift / pyyaml** (from the pyproject note): ✅ fixed (`ebd4021`);
the six-plus guards now actually execute in CI.

---

## 2. Projected profitability — the honest verdict

The measurement stack is the **most intellectually honest part of the
system** and mostly earns the prior audit's praise: symbol-clustered
bootstrap CIs, non-overlapping `N_eff`, `None`-not-`0.0` discipline,
self-excluding ecosystem benchmarks, generation stamping so a new strategy
is never pooled with an old one, and `trade_economics`
(`paper_journal.py:108-151`) which computes and *prints* that the nominal
2:1 grid is really +1.19R/−1.72R at 600bp round-trip and needs a **59% hit
rate to break even** (verified). The cost model defaults to the *expensive*
institutional bucket on the correct principle that an understated cost
manufactures an unrecoverable phantom edge.

But three residual measurement biases **all lean the same way — toward
flattering the record**, and they compound with the strategy dynamics
above:

- **Row-weighted point estimates** (§3 MED-1): the mean/hit-rate a human
  reads is dominated by whichever thesis persisted longest, because each
  thesis is snapshotted daily with overlapping forward windows. A "97.6%
  hit rate" can be one winning thesis counted 40 times. The `N_eff` and CI
  columns flag the thinness but sit *next to* an uncorrected point
  estimate.
- **Uncapped merge snapshots in the top bucket** (§3 MED-2): the region
  "that actually trades" is contaminated by snapshots synthesis would have
  vetoed.
- **Currency P&L on a book that exceeded its own capital** (§3 MED-3):
  with no position cap (A5), the prior board's 30-open-on-15-slots is 2×
  the modeled capital, and the equity line presents that leveraged return
  as a single-account figure.

Add the optimistic gap-through-stop fill (2.7, unfixed) and delisting
censoring that drops disproportionately extreme outcomes.

**Verdict:** the strategy thesis (trade the diffusion lag before the market
connects a disclosed second-order link) is coherent, and the machinery to
answer "is it profitable" rigorously *exists*. But as wired, the system
systematically opens trades on saturated, most-likely-already-priced-in
fan-out theses that synthesis would have blocked, and measures their
returns against biased-optimistic point estimates. **The honest reading is:
the system cannot yet tell whether the strategy is profitable, and the
first apparent edge should be discounted, not trusted.** Fixing the
synthesis bypass and the three measurement biases is what turns the forward
record into a number worth betting on.

---

## 3. New findings (not in the prior audits), ranked

Severity reflects impact on money-at-risk, correctness, or the integrity
of the forward record (the one asset the system exists to build). "✔
verified" marks a finding an adversarial agent independently confirmed
against the code; "✔ hand-checked" marks one I re-read personally.

### HIGH

**HIGH-1 — The magnitude corroboration multiplier is unbounded; fan-out
mass saturates the score.** `dossier.py:520-522`. ✔ verified, ✔ hand-checked.
The confidence bonus is capped (`min(MAX_CONFIDENCE_CORROBORATION_BONUS,
step·doublings)`, line 487-488) but the magnitude bonus is
`1.0 + MAGNITUDE_CORROBORATION_STEP·doublings` with **no cap other than the
final `min(1.0,…)`** — so at S=8 sources ×1.75, S=16 ×2.0, S≈21 ×2.1. A
single tier-2 best item (confidence 0.7, magnitude 0.5) plus fan-out lands
at confidence 0.95 / magnitude ~1.0 → score ~0.95 against a 0.5 bar, built
from individually-weak, sector-correlated items the code itself notes "are
not fully independent." `distinct_fact_count` — computed by synthesis for
exactly this — is never read by `_aggregate` or `evaluate`. The comment at
517-519 says dedup protects this, but that only stops *syndication of one
story*; correlated-but-distinct sector items (NVDA/AMAT/LRCX) get distinct
keys and each add a doubling. **Fix:** cap the magnitude multiplier at the
same doublings that cap the confidence bonus (or ~1.5–1.75), and/or fold
`distinct_fact_count` into the effective source count so 21 items = 2 facts
corroborate like 2.

**HIGH-2 — Universe rot: a graduated tradeable is never auto-demoted.**
`engine.py:3200` + `1590-1593`. ✔ verified, ✔ hand-checked. NEW.
`_reconcile_accepted_types` demotes only when a candidate's
`recommended_as == "anchor"`, but that flag is refreshed **only when the
static universe bounds change** (`entry.get("recommendation_bounds") !=
bounds`, line 1592). In a steady-state deployment the bounds never change,
so the market-cap/analyst re-fetch never runs and the recommendation is
frozen at acceptance time — a company whose cap later balloons past the
$5B ceiling keeps trading forever. The code comment (1582-1589) states this
drift detection is "the reconcile pass's entire documented purpose"; the
guard defeats exactly that purpose. The monthly `screen_universe` *does*
re-fetch and compute `still_fits`, but `_prune_dead_symbols` acts only on
`market_cap is None` (`engine.py:3347`), so a graduated or sub-floor name
is logged and ignored. This silently reintroduces the correlated-book
failure the 2026-07 redesign was built to escape — and those names enter
the forward record as if they were thin-coverage diffusion plays. **Fix:**
drive demotion off the monthly `screen_universe` `still_fits=False` result
(for runtime-accepted tradeables), or re-fetch on an age cadence
independent of bounds changes.

**HIGH-3 — Ecosystem-propagated (0.25) items still count as independent
corroboration slots.** `dossier.py:236-238,458-459`; `engine.py:2143-2158`.
✔ verified. A2's recommended containment (ecosystem items add *mass* but
not *slots*) was not implemented. `independence_key` returns
`origin_symbol|source_name` for any propagated item, and `_aggregate`
counts it toward `independent_source_count` and the log2 corroboration
bonus whenever `confidence·weight ≥ 0.15`. `relationship_confidence=0.25`
is passed to the LLM only as prose, never as a hard multiplier. Because the
key is the origin *anchor*, one correlated AI-capex macro story fanned in
from three anchors becomes three "independent sources"; uniformly-positive
sector news means the contest discount never engages. This is the engine
that pins dossiers at ~1.0 from weak fanned-in items. The one related fix
that *did* ship (has_filing_evidence gating, `dossier.py:460-472`) only
stops these items relaxing the news-only bar 3→2 — it does not stop them
claiming slots. **Fix:** exclude ecosystem-fallback items (by a dedicated
flag or `relationship_confidence ≤ ECOSYSTEM_LINK_CONFIDENCE`) from the
independence-key set so they add magnitude/decay mass but never a source
slot.

### MEDIUM (high-confidence; not verify-gated only because rated medium)

**MED-5 (listed first for weight) — Synthesis fails open on the call cap.**
`usage.py:175-176`; `engine.py:3025-3026,2984-2988`. ✔ hand-checked.
`budget_remaining()` checks the total **call** cap *first*
(`if calls >= daily_call_budget: return False`) with no per-category
reservation; the synthesis reservation feeds only the **dollar** gate
(`_reserved_elsewhere`, `usage.py:184-186`). Dossier fan-out is exactly
what exhausts the call budget (prior A6: 3,872 calls vs a 3,000 cap). When
synthesis is call-starved, `synthesize` returns None, `_apply_synthesis`
returns False, and `evaluate()` runs on the raw uncapped score in the same
pass — the whole-body safety cap silently fails open on the busiest
evidence days. **Fix:** add a call-count reservation mirroring the dollar
one, or treat a budget-deferred synthesis as "not yet eligible to fire"
rather than "fire uncapped," and warn when a signal fires on an
un-synthesized dossier.

**MED (skeptic) — The trade-gating skeptic is on the cheapest model, its
false-refutations are permanent, unbackstopped, and unmeasured.**
`config.py:235`; `engine.py:2328,2353-2354`. The model-tiering argument is
sound for *acceptance* errors (synthesis can cap those) but blind to
*refutation* errors: evidence the Haiku skeptic wrongly refutes is dropped
before it ever enters the dossier, and synthesis only ever caps *down*,
never restores. The skeptic prompt biases toward refusal when unsure, and
propagated second-order evidence — the core of the strategy — is exactly
the loosely-connected case a weak model reads worst. Worse, the
instrumentation to detect this is **built but dead**:
`proposed_confidence`/`proposed_magnitude` are written on every record
(`engine.py:2353-2354`) and read *nowhere*; refutations are never
persisted. **Fix:** persist refutations and wire a skeptic-effect readout
(refutation rate, adjustment distribution, direct vs propagated, by model);
then make the skeptic tier a data-driven decision. Given synthesis cannot
backstop refutations, a one-tier skeptic upgrade is likely the
highest-value model spend — *after* the readout exists.

**MED-1 — Bucket means/hit-rates are row-weighted.** `forward_returns.py:
211-216,407-412`. See §2. **Fix:** report a symbol-equal-weighted headline
(mean of per-symbol means), or de-overlap rows to one per non-overlapping
window; at minimum label the figure "row-weighted."

**MED-2 — Forward-return buckets ignore the synthesis columns.**
`forward_returns.py:89-136,194-226`. The columns PR#15 added
(`status.py:547-548`) are never consumed; uncapped merge-path snapshots
pool into the top bucket. **Fix:** carry `already_priced_in`/`synthesis_at`
onto the row and either exclude vetoed snapshots from the primary table or
split each bucket into synthesis-clean vs vetoed-but-uncapped.

**MED-3 — Currency/equity overlay reports returns on a book that exceeded
its capital.** `status.py:44-50`; `engine.py:2763-2764`. See §2. **Fix:**
add a peak-concurrency-vs-slots warning and/or a concurrency-capped
counterfactual equity; don't present `initial_capital + realized_pnl` as an
achievable single-account return when concurrency breached the slot count.

**MED-4 — News-only corroboration defeatable by multi-day reworded
syndication.** `dedup.py:141-144`. The near-dup window is same/previous UTC
day (2d) but `recent_news` pulls `news_lookback_days=3`, so there is a
guaranteed ≥1-day band where a reworded republish is in the feed but
invisible to dedup and scores as a fresh independent source — and with
publisher identity thin, reaching 3 distinct publishers *tends to require*
syndication. **Fix:** widen the near-dup comparison to the full lookback
window, and/or add a body/summary shingle check.

**MED-6 — `host_network:true` exposes the no-auth write surface to the
LAN.** `ha-addons/smartboi/config.yaml:18` + `webapp.py:1217` +
`config.py:537`. The dashboard binds `0.0.0.0:8100` on the host LAN (not
just behind HA Ingress), and the only guard on POSTs is the
`X-SmartBoi-Request` header — a CSRF defense, **not authentication**: any
scripted LAN client (curl) can set it and reach `reset-accepted`
(destructive), `supplier-research` (a web-search Claude call — real money),
or `rebuild-graph` (~one LLM call per tradeable). Paper-only bounds the
blast radius, but budget is real money and reset is destructive. **Fix:**
prefer Ingress-only exposure (bind 127.0.0.1 / drop `host_network` if the
IB gateway allows), or add a shared-secret token on the POST endpoints; at
minimum document the exposure.

**MED (edgar) — 10-K/10-Q mislabeled as a "cover document," exhibits
ordered first.** `edgar.py:538-555`. Exhibits-first is correct for 8-Ks
but is applied to *every* form; for a 10-K/10-Q the primary document *is*
the substance, yet it is appended last, labeled "Filing cover document,"
and the 12k head+tail truncation can then drop the filing's own MD&A /
customer-concentration disclosures into the omitted middle. No test covers
a 10-K with an EX-10/EX-99 exhibit. **Fix:** gate exhibits-first on form
type (8-K → exhibits first; 10-K/10-Q/424B5/13D → primary first).

**MED (robustness) — `JsonState` has no `fsync`; the atomic-write guarantee
fails on power loss.** `state.py:33-37`. `tmp.write_text` + `tmp.replace`
with no fsync: on an unclean power-off (the characteristic HA-on-a-Pi/SD
crash) the rename can persist while the data blocks don't, leaving a
truncated file that then hits the silent-wipe path (2.3). The docstring's
"a crash mid-write can never leave a corrupt file" is true for a process
crash, false for power loss. Highest-value victims: `periodic_pass_state`
(wipe → every daily pass re-fires → duplicate snapshot/mark batches + extra
signal-expiry chances) and `accepted_candidates` (wipe → silent revert to
DEFAULT_UNIVERSE). **Fix:** `flush`+`os.fsync` the tmp file and the
directory before/after replace; pair with the 2.3 quarantine+log fix and a
daily `.bak` rotation of `data/`.

**MED (robustness) — Daily price-marks can stall a single tick ~73 min
under a half-dead IB Gateway with no Finnhub.** `engine.py:3160-3162`. In
an IB-enabled / Finnhub-absent config, `_run_daily_price_marks` falls to a
*sequential* `last_bars` over the whole 209-symbol universe; each symbol
under a hung farm hits the 20s timeout + 1s gap → ~73 min of single-task
engine stall, with no circuit breaker to short-circuit IB. A larger,
un-audited cousin of A4. **Fix:** the A4 data-level circuit breaker (also
fixes the marking-poll stall), plus a bounded-concurrency price fetch.

### LOW (worth a line each)

- **Rolling graph refresh spends the scarcest budget re-reading unchanged
  filings** it structurally cannot improve, because `graph.add` can't
  upgrade an edge (2.4). `engine.py:1857-1912`. Gate the refresh on the
  2.4 upgrade fix, or skip symbols whose most-recent 10-K accession is
  unchanged since last backfill (already stored).
- **`graph.add` rewrites the whole `graph.json` per edge** → O(edges²) I/O
  during backfill bursts. `graph.py:67-71`. Add a batch/deferred-save path.
- **Relationship extraction is billed as ~150k chars but can get ~600k**
  (primary + 3 exhibits, each capped at 150k). `graph.py:181-189`,
  `edgar.py:465,580`. Re-truncate the extractor input; fix the comment.
- **`EdgarClient` has no 429/503 backoff** (unlike the Finnhub client), and
  the first-run ticker-map raise is outside the per-symbol try in
  `_poll_edgar`. `edgar.py:280-288`; `engine.py:1126`.
- **SIGTERM is unhandled** → the graceful-shutdown/`finally` cleanup is dead
  code under Docker/HA (which stop with SIGTERM, not SIGINT).
  `main.py:32-40`. Install an asyncio SIGTERM handler.
- **Half-dead IB emits a full traceback per symbol per poll** instead of one
  summary WARNING (A4 asked for this). `prices.py:147-148`.
- **Anchor auto-accept skips `name_matches_ticker`** → a misresolved ticker
  becomes a live (mis-sourced) propagation origin. `engine.py:1726-1728`.
  Part of A2; apply the name-match to anchors too.
- **`guess_ecosystem` does no membership validation** before assigning a
  propagation-eligible sector. `universe_screen.py:182-193`. Require a
  *curated* ecosystem before an anchor is fan-out-eligible.
- **Version-consistency test gives false confidence**: nothing ties
  `SMARTBOI_COMMIT` to the declared version, so a future release could ship
  old code reporting a new version with all tests green.
  `Dockerfile:21,26`. Also `pyproject.toml` still says `version = "0.1.0"`.
- **Untested security boundaries**: `handle_accept_candidate` validation is
  exercised only at the CSRF-403 level (`webapp.py:1018-1055`), and
  `AlertSender`'s webhook-secret redaction — the module's whole purpose —
  has no test at all (`alerts.py:32-56`). A regression re-leaks a credential
  into the diagnostics bundle silently.
- **`/api/status` does unthreaded disk I/O on the event loop** (reads
  `paper_trades.jsonl` twice) every 10s per viewer. `webapp.py:950-997`.
  Offload with `asyncio.to_thread` and read once.
- **`engine.py` is a 3372-line god-module** holding the snapshot, synthesis
  cap, entry pipeline, backfill, graph rebuild, and candidate lifecycle —
  where future regressions to the money paths will hide.

---

## 4. What is genuinely excellent (confirmed — keep doing this)

The fresh pass confirms the prior audits' praise and adds a few:

- **Paper-only by construction** holds up: no order methods anywhere;
  `prices.py` is read-only.
- **Forward-record discipline**: `SCORING_VERSION` (now 4), per-row
  `threshold_in_force`/`min_sources_in_force`, generation stamping, the
  institutional-cost default with a printed 59%-breakeven, self-excluding
  ecosystem benchmarks, `assumes_borrow` flagging, symbol-clustered
  bootstrap CIs that return `None` rather than fabricate one. Publication-
  grade experimental hygiene.
- **The budget layer**: a hard dollar ceiling priced from an explicit
  table with unknown-models-priced-at-maximum, and the genuinely sharp
  *reservation-vs-ceiling* insight. This closes A6's overspend at the
  accounting layer (the saturation that *drove* the overspend is the part
  still open).
- **Restart/idempotency care**: daily passes scheduled off a *persisted*
  wall clock, per-tick exception isolation so one bad tick can't kill the
  loop, every IB call timeout-wrapped, `would_allow`/`record` split so a
  budget-deferred retry never double-charges, merge idempotency via
  persisted-dossier `has_evidence`.
- **Edge-building craft**: the pending-edges promotion
  (`engine.py:588-634`) that closes the inert-anchor hole at zero extra
  cost, three-layer `rel_type` validation with self-healing on load, the
  lender/biography domain filters, tradeables-before-anchors backfill
  ordering, `name_matches_ticker` (Advantest→ATRO).
- **News/EDGAR ingestion**: exhibits-first 8-K assembly (the difference
  between "news exists" and reading it), zip-padded column indexing,
  structured Form 4 summaries, Finnhub 429-backoff + `/search`
  circuit-breaker + token/webhook redaction + quote-band clamping.
- **Dashboard security posture** (within the LAN caveat of MED-6):
  genuinely read-mostly, app-wide CSRF middleware with sound CORS-preflight
  reasoning, no XSS path found under a full trace, an allow-list (not
  deny-list) diagnostics redaction. The prior audit's flagged `tools.py`
  "subprocess handling" is a **non-issue** — there is no subprocess/eval
  anywhere in `src/`.
- **The docstrings remain the system's ops journal** — nearly every
  constant carries the live incident that calibrated it. This is worth more
  than any single feature and should be preserved through any refactor.

---

## 5. Prioritized action list

**Today, config-only (highest leverage, zero code — still unshipped from
AUDIT-2026-08):** set `enable_ecosystem_propagation=false` and
`auto_accept_anchors=false` (or auto-accept off) until classification is
trustworthy; this collapses most of the saturation and the overspend before
any code ships. Repair/restart the IB Gateway.

**Then, code — in order of leverage:**

1. **Put synthesis on the trade-opening path** (§0, HIGH-2/2.1.1). Route the
   merge path through `_apply_synthesis`, or have `evaluate()` honor the
   persisted `already_priced_in` and `min(confidence, synthesis_confidence)`
   until a fresh synthesis supersedes them. Clear `already_priced_in` in
   `_aggregate` so it can never linger unenforced.
2. **Bound the magnitude multiplier and gate on `distinct_fact_count`**
   (HIGH-1). This is the saturation fix and is independent of #1.
3. **Fix the synthesis call-cap fail-open** (MED-5) — a call-count
   reservation, so #1 and #2 can't be silently skipped on the busiest days.
4. **Contain corroboration** (HIGH-3 + A2): ecosystem items → mass only, no
   source slots; anchor `name_matches_ticker`; require a curated ecosystem
   before fan-out.
5. **Stop universe rot** (HIGH-2 new): drive demotion off the monthly
   screen's `still_fits`, and add A3's retroactive `is_common_equity` sweep.
6. **De-bias the measurement** (MED-1/2/3): symbol-weighted headline,
   synthesis-aware buckets, leverage-disclosed currency equity.
7. **Enforce (or explicitly counterfactual) the position cap** (A5).
8. **Robustness**: IB circuit breaker + summary WARNING (A4 + price-marks
   stall), persist retry caches (2.5), corrupt-file quarantine + `fsync` +
   `.bak` (2.3 + new), SIGTERM handler.
9. **Skeptic**: persist refutations + wire the readout, then decide the tier
   (MED-skeptic).
10. **Smaller**: extraction done-marker (2.2), today's-date in the two
    prompts (2.7), widen the near-dup window (MED-4), 10-K exhibit ordering
    (MED-edgar), the two security-boundary tests, EDGAR backoff, the version
    guard, and begin carving up `engine.py`.

---

## 6. Method & coverage

Eight subsystem agents (synthesis/signal-gate, graph edges, news/sourcing,
universe/corroboration, profitability/measurement, LLM/budget/skeptic,
robustness/ops, dashboard/maintenance) plus a dedicated fix-verifier, each
reading the live 0.49.0 source; adversarial verifiers refuted every
high/critical finding against the code; the three load-bearing new findings
were hand-re-checked. Live-only claims from AUDIT-2026-08's diagnostics
bundle (board counts, IB failure logs, spend totals, fingerprint
distributions) could not be re-measured from the repo and are marked
"mechanism intact" where the underlying code path is unchanged — feeding a
fresh diagnostics bundle to the next pass would turn those back into
measured facts. `prices.py` internals, the measurement math, and the
dashboard write surface were re-verified this pass; a live board /
`data/` + `logs/` sample is the main thing that would sharpen the
profitability verdict from "cannot tell yet" into a measured statement.
