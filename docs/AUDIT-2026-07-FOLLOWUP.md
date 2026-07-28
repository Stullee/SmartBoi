# Audit follow-up — 2026-07-28 (v0.21.0)

Same-day follow-up to `AUDIT-2026-07.md`. That audit's fixes shipped in
0.20.0; its section 2 ("what still holds back usable results") listed the
non-bug work that matters most for a monetizable track record. This release
implements the three highest-impact items from that list, plus corroboration
hardening. 319 tests green (302 → 319).

## Shipped in this release

1. **Decisions ledger + signal-episode event study** (audit item #4 — the
   biggest gap). Every `trade_opened` / `drift_skip` / `signal_expired` now
   writes an episode-keyed row to `logs/decisions.jsonl` with the price at
   decision time. `scripts/analyze_signal_events.py` (and the dashboard's
   "Signal event study" button) joins signals + decisions + price marks and
   scores each outcome group's forward return from the fire date — including
   the drift-blocked-vs-opened comparison that finally makes the
   entry-timing guards falsifiable. Until now, a skipped or expired signal
   left only a log line: there was literally no way to learn whether
   `MAX_FAVORABLE_DRIFT_PCT` was saving you from chases or costing you
   trades.

2. **Corroboration hardening** (audit item #5). Reworded wire copy could
   satisfy the 2-source gate as two "independent" publishers. Now: (a) a
   token-overlap near-duplicate check collapses lightly reworded republishes
   (same symbol, same/previous UTC day) before they burn an LLM call or a
   source slot — conservative threshold, opposite stories stay distinct; and
   (b) news-only dossiers need `MIN_INDEPENDENT_SOURCES_NEWS_ONLY=3`
   publishers, while any agreeing SEC-filing evidence (a primary disclosure,
   immune to rewording) restores the normal bar. Expect somewhat fewer,
   better-corroborated signals from news-heavy dossiers.

3. **Market-cap-bucketed transaction costs + borrow realism** (audit item
   #3). Flat 25bp/side → 50bp/side ≥$1B, 150bp $300M–1B, 300bp <$300M
   (middle bucket when no cap is fetchable; the configured flat value is a
   floor, never a ceiling). Each trade records the market cap its bucket
   came from. SHORTs in sub-$500M/unknown-cap names carry `assumes_borrow`,
   and stats report avg R with and without them. **Paper R-multiples will
   drop vs. 0.20.0 — the new numbers are the honest ones.**

Also fixed: the 0.20.0 release bumped `config.yaml` to 0.20.0 but left the
add-on Dockerfile's `SMARTBOI_VERSION` at 0.19.0, so diagnostics bundles
misreported their version.

## Still outstanding (in priority order)

1. **Keep the capture running, uninterrupted** (audit #1). Still the #1
   lever; nothing below matters until weeks of snapshots/marks/decisions
   exist. Every missed day is unbackfillable.
2. **Freeze the tradeable universe for the 90-day window** (audit #2):
   `AUTO_ACCEPT_TRADEABLES=false`, consider `AUTO_ACCEPT_MAX_PER_DAY=2`.
3. **Curated-symbol screen results are still flag-only** (audit #6): a
   delisted/acquired curated name keeps accruing dossiers and spend; surface
   the flag on the dashboard at minimum.
4. **8-K press-release exhibits** (Exhibit 99.1) as evidence text (audit
   #8) — the primary document is often boilerplate while the substance is in
   the exhibit.
5. **Dashboard binds 0.0.0.0 with no auth** (audit #8): fine behind HA
   Ingress, do not port-forward; the accept-candidate endpoint is mutable.
6. **Decision gate at day 90** (audit #7): positive benchmark-relative
   ≥0.65-bucket mean at the 20-day horizon, CI excluding zero, surviving the
   (now bucketed) friction — then build sizing. Otherwise the next lever is
   evidence quality, not threshold tuning.

## Note on this pass's scope

The fresh-eyes multi-agent sweep planned for this follow-up was cut short by
a usage budget: two of six subsystem reviewers (both engine halves) ran but
were stopped before reporting. The implemented items above were verified by
direct code reading and by 319 passing tests, including new engine-level
lifecycle tests for every ledger hook. A future pass should still fresh-eyes
`edgar.py` (Form 4 parsing), `prices.py`, and the HA add-on's data
persistence across restarts — the forward dataset's durability depends on
the last one.
