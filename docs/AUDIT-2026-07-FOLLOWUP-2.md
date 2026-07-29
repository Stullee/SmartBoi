# 0.22.0 — why the first signal never became a trade

On 2026-07-28 at 19:15 UTC this system produced its first signal ever:
DCO LONG, confidence 0.62, magnitude 0.40, two independent sources. By the
next morning's diagnostics the dossier was back to `ACTIVE` with an empty
`Signaled @`, and zero paper trades had opened.

The bundle could not say why. That is the finding this release is about.

## What was actually wrong

**1. The bundle never printed the decisions ledger.** `decisions.jsonl`
was built in 0.21.0 specifically to record what the engine DID with a
signal episode — opened, drift-skipped, expired — and `run_diagnostics`
printed only its first and last timestamp. The Event-study button reads
it, but that report needs price marks at both ends of a horizon and says
"not enough data yet" for anything recent, which is exactly when the
question gets asked. So the one-button bundle showed a signal firing, zero
trades, and nothing in between.

**2. Every expiry reason was the same sentence.** `_try_open_from_signal`
logged `"thesis no longer qualifies at entry time"` for two distinct
failures (below the score bar / flipped direction), with no numbers. Even
with the ledger printed, that row explains nothing.

**3. Entries were evaluated on the marking clock.** `PRICE_POLL_INTERVAL_SEC`
defaults to 6 hours, which is right for marking open positions and wrong
for confirming an entry. A signal firing at 15:15 ET is next looked at
somewhere in the following six hours — usually after the close, where
there is no entry price — while the daily decay pass and any newly merged
evidence can expire it in the meantime. A marginal signal (DCO's score was
0.248) can die without an entry ever being attempted.

**4. The decay pass ran on a process-local timer.** `_last_decay_pass` was
a `time.monotonic()` marker, so it reset to "due immediately" on every
restart. That was harmless when the pass only re-scored dossiers; it is
not harmless now that the pass EXPIRES a `SIGNALED`-but-unopened dossier
that has slipped below the bar. On a deployment that rebuilds several
times a day, each restart was another chance to kill a pending signal
before any price poll had looked at it. (Same bug class as the duplicated
daily snapshots fixed earlier — that fix just never reached this pass.)

Which of 3/4 killed DCO specifically is not recoverable after the fact:
the expiry predates the ledger being printable and its reason string was
the generic one. Both are fixed so the next one is answerable.

## Fixed in this release

| Area | Change |
| --- | --- |
| Diagnostics | New **Signal episodes** section: one row per episode (collapsed from re-logs) with its ledger outcome and reason. New **Entry pipeline** section: price-feed state, what is waiting for an entry, both poll cadences, deadline and drift bound |
| Diagnostics | Episodes with no ledger row are called out explicitly rather than looking like clean signals |
| Expiry reasons | `_below_bar_reason` reports which gate failed and by how much (`sources 2/3 (news-only bar)`, `score 0.240 < 0.250`), used by all three expiry sites; a direction flip is now its own reason |
| Entry cadence | New `SIGNAL_ENTRY_POLL_INTERVAL_SEC` (900s): prices poll at this cadence while any entry is pending, at `PRICE_POLL_INTERVAL_SEC` otherwise. Tracked by a flag set when a dossier signals and recomputed by `_mark_and_execute`, so no extra dossier reads per tick |
| Decay pass | Now scheduled off persisted wall-clock (`_daily_pass_due("decay_pass")`), so a restart cannot re-run it and re-expire a pending signal |
| Diagnostics settings | `transaction_cost_bps_per_side` and `min_independent_sources_news_only` were both added to `Settings` and silently missing from the print allow-list. Both added, along with `edgar_forms`, `enable_*_ingestion`, `enable_universe_autoscreen` and the new interval — and a test now fails when any future setting is neither reported nor explicitly listed as omitted |
| Lender filter | Bank debt is disclosed in a dozen near-synonyms and the phrase list caught only some. M&T Bank reached the graph as a "supplier" to Taylor Devices off a *demand line of credit*. Added `line of credit`, `loan agreement`, `promissory note`, `notes payable`, `bank facilit`, `financing agreement`, `mortgage`, `borrower` |
| Biography edges | 8-K item 5.02 officer appointments name a string of well-known former employers, and extraction read a CV line as a business relationship: EPAC→ITW, EPAC→GE, VVX→RTX, NCSM→APO all confirmed live. These are worse than useless — they feed an unrelated mega-cap's news into a small-cap dossier. New `_is_biography_relationship`, applied to every `rel_type` (a CV line gets labelled customer/supplier/competitor essentially at random), with a deliberately narrow phrase list so a genuine disclosure using "served" survives |
| Dead symbols | The monthly screen skipped anchors entirely, so a dead anchor (BMWYY/VLKAY/HYMTF — OTC ADR lines no source covers) was polled forever with nothing noticing. Anchors are now screened for **liveness only** (still exempt from the thin-coverage bounds). Runtime-accepted symbols with no market data at all are **removed automatically** and their dossiers archived; curated symbols are reported in the bundle instead, since a curated list is a deliberate choice and deleting from it would be un-undoable from the dashboard |

333 tests pass (up from 320), including new coverage for each item above.

## Still outstanding

Unchanged from the previous follow-up, minus item 3 (curated screen results
are now surfaced in the bundle):

1. **Keep the capture running, uninterrupted.** Still the #1 lever. Every
   missed day is unbackfillable.
2. **Freeze the tradeable universe for the 90-day window**:
   `AUTO_ACCEPT_TRADEABLES=false`, consider `AUTO_ACCEPT_MAX_PER_DAY=2`.
3. **8-K press-release exhibits** (Exhibit 99.1) as evidence text — the
   primary document is often boilerplate while the substance is in the
   exhibit.
4. **Dashboard binds 0.0.0.0 with no auth**: fine behind HA Ingress, do not
   port-forward; the accept-candidate endpoint is mutable.
5. **`ib_async` log filter** must be attached at handler level, not logger
   level — child-logger records (`ib_async.wrapper`) bypass a filter set on
   the parent, which is why two previous attempts had no effect.
6. **Decision gate at day 90**: positive benchmark-relative ≥0.65-bucket
   mean at the 20-day horizon, CI excluding zero, surviving the bucketed
   friction — then build sizing. Otherwise the next lever is evidence
   quality, not threshold tuning.

## Configuration note

`SIGNAL_ENTRY_POLL_INTERVAL_SEC` is a new add-on option and defaults to
900s. Home Assistant add-on options do **not** inherit repository defaults
for an existing install — check the add-on's configuration tab after
rebuilding, along with `max_horizon_days`, which should read 21.
