# Independent verification of the shipped fixes — 2026-08-09 (follow-up to AUDIT-2026-08-FOLLOWUP)

Scope: an **independent** re-audit of the fixes that `AUDIT-2026-08-FOLLOWUP.md`
described. That document was written against the **0.49.0** tree and its fix
list was "implemented on this branch" — i.e. it verified its own work. This
pass checks that work a second time, from scratch, against the **merged,
released 0.50.0 tree** (SCORING_VERSION 5, PR #16), to answer two questions the
prior document could not answer about itself: *did the fixes actually ship in
the release,* and *do they hold up to fresh, adversarial reading rather than
the author's own?*

Method: four independent deep-dive agents, each given one subsystem and an
explicit brief to **distrust the changelog and read the code** — the
synthesis gate, the corroboration/saturation math, the budget/universe-rot
pair, and the robustness/hardening set. Every claim below carries a `file:line`
in the 0.50.0 tree and was re-read by hand. The full suite was run:
**639 tests pass** (`pytest -q`, ~10s). Nothing already documented in the
`AUDIT-2026-07*` / `AUDIT-2026-08*` files is re-litigated except to record
whether it shipped.

**Bottom line up front:** the release is sound. Every load-bearing finding in
FOLLOWUP-1 was a **real** defect, and every corresponding fix **shipped and is
mechanically correct** — the synthesis cap is on the trade-opening path, the
magnitude multiplier is bounded, ecosystem fan-out no longer mints source
slots, graduated tradeables auto-demote, and the durable-write / circuit-breaker
hardening is correct. This pass found **no new correctness bug**. It did find
**four places where FOLLOWUP-1's changelog claims slightly more than the code
delivers** — one of them load-bearing (the synthesis fail-open is *narrowed,
not eliminated*). Those are documented in §2 so the record is honest about the
residual, not so the fixes are in doubt.

---

## 1. Fix verification vs AUDIT-2026-08-FOLLOWUP

Confirmed against the 0.50.0 tree. "Real?" is whether the original finding was
a genuine defect; "Shipped & sound?" is whether the fix is present in the
release and correct on re-reading.

| Finding (FOLLOWUP-1) | Real? | Shipped & sound? | Evidence (0.50.0) |
|---|---|---|---|
| **§0 / 2.1.1 — synthesis absent from the trade-opening path** | ✅ yes | ✅ **sound** | `_cap_with_synthesis` (`engine.py:3187`) is called on the sole merge path at `engine.py:2461`, *after* `merge_evidence` (2456) and *before* `save` (2462) and `evaluate` (2471); veto zeroes both scores, trim is `min(...)`, 36h freshness gate, cap-never-lift. Verdict flag survives `_aggregate` (`dossier.py:512-594` never touches `already_priced_in`) so the cap re-applies; end-to-end test `test_a_synthesis_veto_survives_a_later_evidence_merge` (`test_engine.py:1814`) |
| **HIGH-1 — unbounded magnitude corroboration multiplier** | ✅ yes | ✅ **sound** | `doublings = min(_corroboration_doublings(count), MAX_CORROBORATION_DOUBLINGS)` (`dossier.py:550-551`) now feeds *both* bonuses: confidence `0.10·doublings` (552), magnitude `1.0 + 0.25·doublings` (584). `MAX_CORROBORATION_DOUBLINGS = 0.25/0.10 = 2.5`, so the magnitude multiplier is capped at ×1.625 (was unbounded in source count). `test_magnitude_corroboration_is_bounded_past_the_doublings_cap` |
| **HIGH-3 — ecosystem items mint independent-source slots** | ✅ yes | ✅ **sound** | `slot_bearing` drops `is_propagated ∧ relationship_confidence ≤ ECOSYSTEM_ASSOCIATION_CONFIDENCE` (`dossier.py:512-517`); `independent_source_count` derives from that set (518). Mass (`_side_mass`) is unfiltered, so ecosystem evidence still moves direction/contest. Engine stamps ecosystem links at exactly `0.25` (`engine.py:2201`); constant parity pinned by `test_engine.py:1766` |
| **HIGH-2 (new) — graduated tradeable never auto-demoted (universe rot)** | ✅ yes | ✅ **sound** | `_demote_graduated_tradeables` (`engine.py:3573`, called from the monthly screen) demotes a runtime-accepted tradeable to anchor when the screen's **freshly re-fetched** market data (`r.market_cap_musd`/`r.analyst_count`, 3608-3613) now recommends "anchor" — no dependence on an operator bounds change |
| **MED-5 — synthesis fails open on the call cap** | ✅ yes | ⚠️ **narrowed, not closed** (see §2.1) | `_reserved_calls_elsewhere` (`usage.py:155-174`) + guarded gate `available_calls = daily_call_budget − reserved` (201-203) closes the *call-starvation* vector; but a synthesis deferred for any *other* reason still evaluates uncapped on the decay path |
| **2.3 / MED — corrupt-file quarantine + fsync** | ✅ yes | ✅ **sound** | `atomic_write_json` fsyncs the tmp fd before rename and the parent-dir fd after (`state.py:47-69`); unreadable files quarantined to `<name>.corrupt-<ts>` with a WARNING (22-44); shared by `JsonState`, `DossierStore` (`dossier.py`), `RelationshipGraph` (`graph.py`) |
| **2.5 — persist retry caches across restart** | ✅ yes | ✅ **sound** | `retry_state.json` via `JsonState` (`engine.py:390`), load/persist at 2260-2287, wall-clock stamps (correct for surviving a restart) |
| **A4 — IB data-level circuit breaker** | ✅ yes | ✅ **sound** | 5 consecutive timeouts → 30-min skip to Finnhub, single post-cooldown probe, collapsed logging (`prices.py:67-68`, monotonic clock); resets on success |
| **SIGTERM handler** | ✅ yes | ✅ **sound** (untested — §2.3) | `loop.add_signal_handler(SIGTERM/SIGINT → task.cancel())` (`main.py:40-48`) so `run_forever`'s cleanup runs on a Docker/HA stop |
| **MED-1/2/3 — measurement de-biasing** | ✅ yes | ✅ **shipped** | symbol-equal-weighted headline (`forward_returns.py:242-243`, `per_symbol_breakdown` 269), synthesis-aware buckets, leverage disclosure (`status.py`) |
| **skeptic readout (built-but-dead → wired)** | ✅ yes | ✅ **shipped** | refutations logged to `logs/skeptic_refutations.jsonl` (`engine.py:2298`), `skeptic_report.py` + `scripts/analyze_skeptic.py` compute the rate/distribution |

All other FOLLOWUP-1 items (extraction done-marker 2.2, dated grader prompts,
widened near-dup window MED-4, 10-K exhibit order MED-edgar, EDGAR backoff,
edge upgrade-on-stronger + aging 2.4, runtime reset) are present in the
0.50.0 diff (`a010b44..c1076ef`) and covered by new tests.

---

## 2. Residuals the changelog understates

None of these is a regression or a broken fix. Each is a place where the
FOLLOWUP-1 changelog reads as "closed" while the code leaves a bounded gap
worth naming — the same honesty standard the series holds itself to.

### 2.1 MED-5 — the synthesis fail-open is narrowed, not eliminated

The call-count reservation is correct and closes the vector FOLLOWUP-1
actually named (dossier fan-out exhausting the shared call cap and starving
synthesis before it prices a token). But the underlying shape — *"a synthesis
that does not run leaves the raw aggregate, and `evaluate` fires on it
uncapped"* — is architectural, not a call-budget accident, and it persists on
two axes:

1. **Decay path, non-starvation deferral.** `_apply_synthesis` is
   "failure is a no-op, not a block" by design (`engine.py:3113`): a `None`
   verdict returns `False` (3136), no cap is written, and `evaluate` runs on
   the raw arithmetic (`engine.py:3086`). The reservation removes *one* cause
   of that `None` (call starvation); a genuine dollar-budget exhaustion, the
   synthesis category hitting its *own* 0.25 ceiling, or a transient API error
   still yields the same uncapped evaluation. This only bites at or above the
   synthesis floor (`confidence·magnitude ≥ threshold · synthesis_score_floor_pct`,
   `engine.py:3129-3131`) — below it a cap could not change the decision anyway
   — so the exposed band is *near-threshold scores whose synthesis deferred for
   a non-call reason.*
2. **Merge path, no fresh verdict to re-apply.** `_cap_with_synthesis`
   re-applies the *last persisted* verdict and is freshness-gated at 36h,
   no-op'ing when `synthesis_at` is missing or stale (`engine.py:3209-3217`).
   A thesis that climbs across the bar via intraday merges **before its first
   daily synthesis has ever run**, or whose last verdict is >36h old, is capped
   by nothing on the merge path. This is the exact "trades open minutes after an
   item lands; synthesis refreshes once a day" concern from FOLLOWUP-1 §0 — the
   cap shrinks the window (a fresh verdict now gets re-applied) but cannot
   eliminate it, because it can only re-apply a verdict that already exists.

Net: the fix is sound for what it claims; the residual is the gap between
"a fresh (<36h) verdict exists to re-apply" and "a thesis reaches the bar."
**Options** (in order of leverage, none shipped): treat a budget-deferred
synthesis on an above-floor, never-synthesized dossier as *"not yet eligible to
fire"* rather than *"fire uncapped"*; or emit a WARNING whenever `evaluate`
fires on a dossier with no fresh synthesis, so the forward record can at least
tag those rows. The latter is cheap and would make the residual *measurable*,
which is the series' usual first move.

### 2.2 "Ecosystem items → only decay mass" is slightly overstated

The HIGH-3 fix stops ecosystem-association items minting an *independent-source
slot* (`dossier.py:512-517`), which is the saturation-relevant claim and is
correct. But the base thesis is one item's `confidence·magnitude·weight²`
(`dossier.py:543`), and an ecosystem item is still a member of the `weighted`
set it is chosen from — so a lone, unusually strong ecosystem item *can* be
selected as `best` and set the base score, not merely "decay mass." Reaching a
signal from there still requires ≥2 genuine (non-ecosystem) slots to pass the
source gate (`signals.py`), so this is **not** a fan-out saturation path and
the fix's intent holds; the changelog phrasing just claims a hair more than the
mechanism.

### 2.3 The SIGTERM handler ships without a test

`main.py:40-48` is correct by inspection but is the one hardening change with
**no test**. It is also inherently awkward to unit-test (a real signal into an
asyncio loop). Worth a targeted test that installs the handler on a throwaway
loop and asserts the cancellation path; until then it is code-verified only.

### 2.4 The FOLLOWUP-1 document mixes two tree states

`AUDIT-2026-08-FOLLOWUP.md` is a 0.49.0 *findings* report with a 0.50.0 *"fixes
implemented"* preface bolted on top. A reader landing on it cold cannot easily
tell that most of its alarming findings section is already fixed in the tree
they are running — the "❌ not fixed" status table (its §1) describes 0.49.0 and
is now largely stale. This is a documentation-clarity issue, not a code one;
this file exists partly to state the current (0.50.0) status plainly. When the
next release rolls, FOLLOWUP-1's §1 table should be marked historical.

---

## 3. Still deliberately open (confirmed against 0.50.0)

Re-verified against the shipped config; all match FOLLOWUP-1's "still open"
list, so its account of its own deferrals is honest:

- **Config-only containment (A9.1)** — `enable_ecosystem_propagation`
  (`config.py:347`), `auto_accept_anchors`/`auto_accept_tradeables`
  (`config.py:512-513`) all still default `True`. Once saturation is contained
  in code (HIGH-1/HIGH-3, shipped), this is a measurement-window/spend choice,
  not a safety one — FOLLOWUP-1 characterizes it correctly.
- **Position-cap enforcement (A5)** — the *disclosure* ships
  (`peak_concurrent` vs `max_concurrent_positions`, with a candid docstring at
  `status.py:52-62`); *enforcement* at entry is still deferred to real order
  placement, as designed.
- **Dashboard LAN exposure (MED-6)** — `host_network: true`
  (`ha-addons/smartboi/config.yaml:18`) with Ingress; accepted by the operator,
  CSRF-guarded but not authenticated.
- Minor items (independence key for direct filings 2.6, cache-token pricing,
  gap-through-stop fill, `pyproject.toml` still `version = "0.1.0"` vs the
  0.50.0 add-on, the version-consistency guard) remain low-severity polish.

---

## 4. Re-confirmed excellent (this pass)

- **The durable-write path is textbook.** Right fds, right ordering (tmp-fsync
  before rename, dir-fsync after), best-effort dir-fsync, quarantine-not-wipe on
  an unreadable file. Two clocks chosen deliberately and correctly: monotonic
  for the circuit breaker, wall-clock for cross-restart retry state.
- **The synthesis cap is a genuinely good piece of design.** Cap-never-lift,
  freshness-gated, no new LLM call, and the surviving `already_priced_in` flag
  plus daily refresh make the verdict durable yet self-releasing. The
  docstrings at `engine.py:3098-3120` explain *why* it can only veto/trim,
  never raise — the system's ops-journal-in-comments discipline holds.
- **Test growth tracks the fixes.** 639 green, with load-bearing coverage for
  each money-path change (veto-survives-merge, bounded corroboration,
  pure-ecosystem-fanout-mints-no-slots, retry-state-survives-restart, the
  breaker state machine).

---

## 5. The standing limit: profitability still needs a live sample

Unchanged and unclosable by any code pass: the profitability verdict in
FOLLOWUP-1 §2 ("the system cannot yet tell whether the strategy is profitable;
discount the first apparent edge") rests on live figures — board counts, the
3,872-calls-vs-3,000-cap overspend, the "97.6% hit rate" example — that live in
a diagnostics bundle **not in the repo**. This pass could re-verify every code
*mechanism* those claims depend on, but not the numbers themselves. Feeding a
fresh `data/` + `logs/` sample to the next pass is the single step that would
turn "cannot tell yet" into a measured statement, and it is worth more than any
further code audit right now.

---

## 6. Method & coverage

Four independent subsystem agents (synthesis gate; corroboration/saturation;
budget + universe rot; robustness/hardening), each briefed to distrust the
changelog and read the 0.50.0 source line-by-line; the three load-bearing
findings (the merge-path cap, the MED-5 residual, the demotion path) were
hand-re-read; the full suite was executed (639 passed). This remains an
**AI-driven** verification of code and tests — not an external human review and
not a live-trading review. Its value is that it is *independent of the process
that wrote both the audit and the fixes*, and that it ran the tests rather than
trusting the "green" claim. The one thing it cannot substitute for is a live
sample (§5).
