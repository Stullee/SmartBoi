"""Pure "does score predict forward returns" math -- the offline analysis
behind scripts/analyze_forward_returns.py. No network, no engine
dependency: everything here operates on already-loaded rows from
logs/dossier_snapshots.jsonl (status.py's snapshot_dossier) and
logs/price_marks.jsonl (engine.py's _run_daily_price_marks), both
append-only daily logs captured for exactly this purpose -- see README's
"Forward-validation data capture"."""
from __future__ import annotations

import random
from datetime import date, timedelta

# Score buckets: is the forward-return relationship monotonic across them?
# A real edge should show higher buckets outperforming lower ones; if it
# doesn't, raising the signal threshold is not obviously the fix either.
# The single most important question this report answers is "does the region
# that actually trades (>= the signal threshold) beat the region just below
# it?", and a bucket straddling the threshold structurally could not answer
# it. Both 0.5 and 0.65 are bucket EDGES so the split is clean whatever the
# configured SIGNAL_CONFIDENCE_THRESHOLD happens to be (the shipped default is
# 0.5; it has also run at 0.65). Do not read either edge as "the threshold" --
# the bar a given row actually cleared is stamped on the signal row itself
# (see signals.SignalEvent.threshold_in_force), not inferred from these edges.
SCORE_BUCKETS = ((0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 1.01))


def _bucket_label(score: float) -> str:
    for lo, hi in SCORE_BUCKETS:
        if lo <= score < hi:
            return f">= {lo:.2f}" if hi > 1.0 else f"[{lo:.2f}, {hi:.2f})"
    return "?"


def dedup_snapshots(rows: list[dict]) -> list[dict]:
    """Collapses to one row per (symbol, snapshot date). A restart used to
    make engine.py write a full duplicate dossier_snapshots.jsonl batch
    (see _daily_pass_due) -- several restarts on one day means several
    byte-identical rows per symbol for that day, each one otherwise
    counted as an independent observation and silently inflating a score
    bucket's sample size (and skewing its mean) by however many times the
    engine happened to restart that day. The fix in engine.py stops this
    from happening going forward, but logs captured before that fix
    already have the duplicates baked in, so this is applied unconditionally
    regardless of when the log was written. Keeps the first-seen row for a
    given (symbol, date) -- duplicates are byte-identical in practice, so
    which one survives doesn't change the result, only the count."""
    seen: set[tuple[str, str]] = set()
    out = []
    for row in rows:
        symbol = row.get("symbol")
        snapshotted_at = row.get("snapshotted_at") or ""
        if not symbol or not snapshotted_at:
            continue
        key = (symbol, snapshotted_at[:10])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def price_marks_by_symbol(rows: list[dict]) -> dict[str, dict[str, float]]:
    """symbol -> {date (YYYY-MM-DD): price}. Last mark wins for a given
    day if there were somehow more than one (shouldn't happen -- price
    marks are written at most once a day per symbol -- but this must never
    crash on real log data)."""
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        symbol = row.get("symbol")
        price = row.get("price")
        marked_at = row.get("marked_at") or ""
        if not symbol or price is None or not marked_at:
            continue
        out.setdefault(symbol, {})[marked_at[:10]] = price
    return out


def _price_on_or_after(marks: dict[str, float], target_date: str, max_lookahead_days: int = 5) -> tuple[str, float] | None:
    """The price mark ON target_date, or the nearest one within
    max_lookahead_days AFTER it -- the IB feed can be briefly unreachable
    on any given day (see README's entry-timing guards), so an exact-date
    lookup alone would silently drop an otherwise-good row over one missed
    poll."""
    d = date.fromisoformat(target_date)
    for offset in range(max_lookahead_days + 1):
        candidate = (d + timedelta(days=offset)).isoformat()
        if candidate in marks:
            return candidate, marks[candidate]
    return None


def compute_forward_return(
    snapshot: dict,
    price_marks: dict[str, dict[str, float]],
    horizon_days: int,
    max_lookahead_days: int = 5,
) -> dict | None:
    """One dossier snapshot -> its signed forward return over horizon_days,
    or None if either endpoint price isn't available (a NONE-direction
    snapshot has no thesis to score; a gap in price_marks near either date
    beyond max_lookahead_days leaves it unjoinable). Signed in the THESIS
    direction -- LONG: +price move is a win, SHORT: -price move is a win --
    so a positive signed_return_pct always means "the thesis was right so
    far," regardless of direction, and buckets/correlations downstream
    don't need to special-case direction again."""
    direction = snapshot.get("direction")
    if direction not in ("LONG", "SHORT"):
        return None
    symbol = snapshot.get("symbol")
    marks = price_marks.get(symbol)
    if not marks:
        return None
    snapshotted_at = snapshot.get("snapshotted_at") or ""
    if not snapshotted_at:
        return None
    entry = _price_on_or_after(marks, snapshotted_at[:10], max_lookahead_days)
    if entry is None:
        return None
    entry_date, entry_price = entry
    if entry_price == 0:
        return None
    target_date = (date.fromisoformat(entry_date) + timedelta(days=horizon_days)).isoformat()
    exit_ = _price_on_or_after(marks, target_date, max_lookahead_days)
    if exit_ is None:
        return None
    exit_date, exit_price = exit_
    raw_pct = (exit_price - entry_price) / entry_price * 100.0
    signed_pct = raw_pct if direction == "LONG" else -raw_pct
    return {
        "symbol": symbol,
        "direction": direction,
        "score": snapshot.get("score", 0.0),
        # Carried through so the report can separate snapshots the daily
        # synthesis flagged as already-priced-in (a veto) from clean ones. It
        # matters most for pre-SCORING_VERSION-5 rows, where the merge path did
        # not yet re-apply the synthesis cap, so a vetoed dossier could be
        # snapshotted at its UNCAPPED arithmetic score and land in the top
        # bucket -- exactly the region the report exists to evaluate.
        "already_priced_in": bool(snapshot.get("already_priced_in")),
        "entry_date": entry_date,
        "entry_price": entry_price,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "horizon_days": horizon_days,
        "signed_return_pct": signed_pct,
    }


def effective_sample_count(rows: list[dict], horizon_days: int) -> int:
    """A defensible EFFECTIVE observation count: per symbol, only rows
    whose entry dates are at least horizon_days apart (greedy, in date
    order) count -- consecutive daily snapshots of one persistent thesis
    have almost-fully-overlapping forward windows and are close to copies
    of a single data point, so the raw row count wildly overstates how
    much independent evidence exists. This is what keeps a '58% hit rate
    over 1,300 rows' headline from being read as decision-grade when it
    rests on a few dozen genuinely independent thesis-windows."""
    by_symbol: dict[str, list[str]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r["entry_date"])
    n_eff = 0
    for dates in by_symbol.values():
        last_counted: date | None = None
        for d in sorted(dates):
            current = date.fromisoformat(d)
            if last_counted is None or (current - last_counted).days >= horizon_days:
                n_eff += 1
                last_counted = current
    return n_eff


def cluster_bootstrap_ci(
    values_by_symbol: dict[str, list[float]],
    n_boot: int = 2000,
    confidence: float = 0.90,
    seed: int = 42,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI for the mean, resampling SYMBOLS (clusters)
    with replacement rather than individual rows -- rows within a symbol
    are heavily serially correlated (overlapping forward windows), so a
    row-level bootstrap would be far too confident. None when there are
    fewer than 2 symbols: a single cluster has no between-cluster
    variation to estimate from, and printing a CI would be fiction."""
    symbols = sorted(values_by_symbol)
    if len(symbols) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample: list[float] = []
        for _ in symbols:
            sample.extend(values_by_symbol[rng.choice(symbols)])
        if sample:
            means.append(sum(sample) / len(sample))
    if not means:
        return None
    means.sort()
    tail = (1.0 - confidence) / 2
    lo_idx = int(tail * len(means))
    hi_idx = min(len(means) - 1, int((1.0 - tail) * len(means)))
    return means[lo_idx], means[hi_idx]


def bucket_returns(rows: list[dict], horizon_days: int | None = None) -> list[dict]:
    """Mean signed forward return and hit-rate per score bucket -- is the
    relationship monotonic (higher score, better forward return)? Buckets
    with zero rows are omitted rather than shown as a misleading 0.0.
    When horizon_days is given, each bucket also carries the number of
    distinct symbols, the non-overlapping effective sample count, and a
    symbol-clustered bootstrap CI for the mean -- the raw row count alone
    badly overstates the evidence (see effective_sample_count)."""
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(_bucket_label(r["score"]), []).append(r)
    out = []
    for lo, hi in SCORE_BUCKETS:
        label = _bucket_label(lo)
        bucket_rows = grouped.get(label)
        if not bucket_rows:
            continue
        vals = [r["signed_return_pct"] for r in bucket_rows]
        # SYMBOL-equal-weighted stats alongside the row-weighted ones. A single
        # thesis that persists is snapshotted once per DAY with almost-fully-
        # overlapping forward windows, so the row-weighted mean/hit-rate can be
        # driven almost entirely by whichever name stayed in the bucket
        # longest -- a "97% hit rate" that is one winning thesis counted 40
        # times. Averaging per-symbol means (one vote per symbol) is the
        # headline the report should show, and it matches the CI, which is
        # already bootstrapped over symbols. Row-weighted figures are kept for
        # any programmatic consumer.
        by_symbol: dict[str, list[float]] = {}
        for r in bucket_rows:
            by_symbol.setdefault(r.get("symbol", ""), []).append(r["signed_return_pct"])
        sym_means = [sum(v) / len(v) for v in by_symbol.values()]
        sym_hits = [sum(1 for x in v if x > 0) / len(v) for v in by_symbol.values()]
        entry = {
            "bucket": label,
            "count": len(vals),
            "n_symbols": len(by_symbol),
            "mean_return_pct": sum(vals) / len(vals),
            "hit_rate": sum(1 for v in vals if v > 0) / len(vals),
            "mean_return_pct_symbol_weighted": sum(sym_means) / len(sym_means),
            "hit_rate_symbol_weighted": sum(sym_hits) / len(sym_hits),
        }
        if horizon_days is not None:
            entry["n_effective"] = effective_sample_count(bucket_rows, horizon_days)
            entry["ci_90"] = cluster_bootstrap_ci(by_symbol)
        out.append(entry)
    return out


def pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """None (not 0.0) when there's too little data or no variance to
    compute a meaningful correlation -- 0.0 would misleadingly read as
    "confirmed no relationship" rather than "couldn't tell"."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def per_symbol_breakdown(rows: list[dict]) -> list[dict]:
    """Worst mean forward return first -- the biggest losers are what a
    reviewer wants to see immediately, not scrolled past."""
    grouped: dict[str, list[float]] = {}
    for r in rows:
        grouped.setdefault(r["symbol"], []).append(r["signed_return_pct"])
    out = [
        {"symbol": s, "count": len(vals), "mean_return_pct": sum(vals) / len(vals)}
        for s, vals in grouped.items()
    ]
    out.sort(key=lambda r: r["mean_return_pct"])
    return out


def ecosystem_benchmark_return(
    price_marks: dict[str, dict[str, float]],
    entry_date: str,
    horizon_days: int,
    ecosystem: str,
    ecosystem_by_symbol: dict[str, str],
    max_lookahead_days: int = 5,
    exclude_symbol: str = "",
) -> float | None:
    """Mean RAW (unsigned, price-only) return of every symbol in
    `ecosystem` over [entry_date, entry_date + horizon_days], built from
    every symbol price_marks tracks -- not just symbols that happen to
    have a dossier. That distinction matters: an ecosystem with only one
    dossier would otherwise have its "benchmark" computed from that same
    dossier's own return, making alpha exactly 0 by construction rather
    than a real measurement (confirmed live: IESC, the only grid_datacenter
    dossier, showed 0.00 mean alpha for exactly this reason).

    `exclude_symbol` should be the symbol being benchmarked: including a
    stock in its own benchmark shrinks every measured alpha toward zero by
    ~1/N (and to exactly zero when it's the only priced member) -- the
    benchmark must be "the rest of the sector", not "the sector including
    the pick". Returns None if no OTHER symbol in the ecosystem has both
    an entry- and exit-date price -- those rows are reported as
    unbenchmarkable rather than given a fabricated 0.00 alpha."""
    returns = []
    for symbol, eco in ecosystem_by_symbol.items():
        if eco != ecosystem or symbol == exclude_symbol:
            continue
        marks = price_marks.get(symbol)
        if not marks:
            continue
        entry = _price_on_or_after(marks, entry_date, max_lookahead_days)
        if entry is None:
            continue
        entry_found_date, entry_price = entry
        if entry_price == 0:
            continue
        target_date = (date.fromisoformat(entry_found_date) + timedelta(days=horizon_days)).isoformat()
        exit_ = _price_on_or_after(marks, target_date, max_lookahead_days)
        if exit_ is None:
            continue
        _, exit_price = exit_
        returns.append((exit_price - entry_price) / entry_price * 100.0)
    if not returns:
        return None
    return sum(returns) / len(returns)


def benchmark_relative_returns(
    rows: list[dict],
    price_marks: dict[str, dict[str, float]],
    ecosystem_by_symbol: dict[str, str],
    max_lookahead_days: int = 5,
) -> list[dict]:
    """Each row's signed return minus its own ecosystem's mean RAW return
    over the same [entry_date, entry_date + horizon] window (sign-matched
    to the row's own direction: a LONG compares against the raw ecosystem
    return directly, a SHORT against its negation, so a positive result
    always means "beat what a symmetric bet on the whole sector would
    have returned") -- separates alpha (the pick itself) from sector beta
    (the whole ecosystem moved). A LONG book losing because the
    semiconductor SECTOR fell is a different failure than the individual
    picks being wrong, and only this view tells them apart. Unclassified
    symbols (not in ecosystem_by_symbol) fall into a "?" bucket rather
    than being dropped; rows whose ecosystem has no benchmarkable price
    data get benchmark_relative_pct=None rather than a misleading 0.0."""
    cache: dict[tuple[str, str, str, int], float | None] = {}
    out = []
    for r in rows:
        eco = ecosystem_by_symbol.get(r["symbol"], "?")
        cache_key = (r["symbol"], eco, r["entry_date"], r["horizon_days"])
        if cache_key not in cache:
            cache[cache_key] = ecosystem_benchmark_return(
                price_marks, r["entry_date"], r["horizon_days"], eco, ecosystem_by_symbol,
                max_lookahead_days, exclude_symbol=r["symbol"],
            )
        raw_benchmark = cache[cache_key]
        if raw_benchmark is None:
            signed_benchmark = None
            relative = None
        else:
            signed_benchmark = raw_benchmark if r["direction"] == "LONG" else -raw_benchmark
            relative = r["signed_return_pct"] - signed_benchmark
        out.append({**r, "ecosystem": eco, "ecosystem_benchmark_pct": signed_benchmark, "benchmark_relative_pct": relative})
    return out


def _bucket_table(rows: list[dict], horizon_days: int, value_header: str) -> list[str]:
    # Mean/Hit are SYMBOL-equal-weighted (one vote per symbol), which matches
    # the symbol-clustered CI and cannot be dominated by one long-lived thesis
    # -- see bucket_returns. The row-weighted mean is available programmatically
    # but deliberately not the headline.
    lines = [
        f"{'Bucket':<14}{'Rows':<7}{'Syms':<6}{'N_eff':<7}{value_header + ' (sym-wt)':<18}"
        f"{'90% CI':<20}{'Hit% (sym-wt)':<14}"
    ]
    for b in bucket_returns(rows, horizon_days):
        ci = b.get("ci_90")
        ci_text = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a (1 sym)"
        lines.append(
            f"{b['bucket']:<14}{b['count']:<7}{b.get('n_symbols', 0):<6}{b.get('n_effective', 0):<7}"
            f"{b['mean_return_pct_symbol_weighted']:<18.2f}{ci_text:<20}"
            f"{b['hit_rate_symbol_weighted'] * 100:<13.1f}%"
        )
    return lines


def format_report(
    horizon_days: int,
    rows: list[dict],
    price_marks: dict[str, dict[str, float]],
    ecosystem_by_symbol: dict[str, str],
    attempted: int | None = None,
) -> str:
    """One horizon's full report as plain text: bucket table (raw and
    benchmark-relative), correlation, overall hit-rate, per-symbol
    breakdown. Returns a one-line "no data" message instead of empty
    tables when there's nothing joinable for this horizon yet -- forward
    data takes horizon_days to even exist.

    `attempted` is the number of direction-bearing snapshots the join was
    attempted for -- reporting joined-of-attempted makes silently dropped
    rows (symbol left the universe mid-horizon, price marks stopped,
    horizon not yet elapsed) visible instead of quietly censored, since a
    delisting/acquisition mid-horizon is disproportionately an EXTREME
    outcome and dropping those biases every statistic here.

    Row counts vs N_eff: one thesis that persists for weeks contributes a
    row per DAY with almost fully overlapping forward windows -- close to
    copies of one observation. N_eff counts non-overlapping windows per
    symbol (see effective_sample_count) and the CI is bootstrapped over
    SYMBOLS, so the printed uncertainty reflects the genuinely independent
    evidence, not the inflated row count."""
    joined_note = (
        f"{len(rows)} joined snapshot(s)"
        if attempted is None
        else f"{len(rows)} of {attempted} direction-bearing snapshot(s) joined; "
             f"{attempted - len(rows)} unjoinable (no entry/exit price mark -- too recent, or marks stopped)"
    )
    lines = [f"=== Forward returns, {horizon_days}-day horizon ({joined_note}) ==="]
    if not rows:
        lines.append("No joinable snapshot/price-mark pairs yet for this horizon.")
        return "\n".join(lines)

    # Snapshots the daily synthesis vetoed as already-priced-in are excluded
    # from the tables below and reported separately: for pre-SCORING_VERSION-5
    # rows their `score` is the UNCAPPED arithmetic, so they contaminate the
    # top bucket -- the exact region this report exists to evaluate -- with
    # snapshots the strategy's own whole-body pass would not have traded.
    clean_rows = [r for r in rows if not r.get("already_priced_in")]
    vetoed_rows = [r for r in rows if r.get("already_priced_in")]
    if vetoed_rows:
        vetoed_mean = sum(r["signed_return_pct"] for r in vetoed_rows) / len(vetoed_rows)
        lines.append(
            f"({len(vetoed_rows)} joined snapshot(s) excluded from the tables below: synthesis "
            f"flagged them already-priced-in. Their mean forward return: {vetoed_mean:+.2f}%.)"
        )
    if not clean_rows:
        lines.append("No non-vetoed snapshots to analyse for this horizon yet.")
        return "\n".join(lines)

    lines.append("")
    lines.append("-- By score bucket (raw, signed in thesis direction) --")
    lines.extend(_bucket_table(clean_rows, horizon_days, "Mean Return %"))
    lines.append("(Mean%/Hit% are symbol-equal-weighted -- one vote per symbol -- so one long-lived "
                 "thesis can't dominate them; row counts are shown for context.)")

    corr = pearson_correlation([r["score"] for r in clean_rows], [r["signed_return_pct"] for r in clean_rows])
    lines.append("")
    lines.append(f"Score vs. signed forward return correlation: {corr:.3f}" if corr is not None else "Score vs. signed forward return correlation: n/a (insufficient data)")
    lines.append("(row-level, so overlapping windows inflate it -- trust the bucket CIs above over this number)")
    row_hit_rate = sum(1 for r in clean_rows if r["signed_return_pct"] > 0) / len(clean_rows)
    by_symbol_hits = {}
    for r in clean_rows:
        by_symbol_hits.setdefault(r["symbol"], []).append(1 if r["signed_return_pct"] > 0 else 0)
    sym_hit_rate = sum(sum(v) / len(v) for v in by_symbol_hits.values()) / len(by_symbol_hits)
    n_eff_total = effective_sample_count(clean_rows, horizon_days)
    lines.append(
        f"Overall hit rate: {sym_hit_rate * 100:.1f}% symbol-weighted "
        f"({row_hit_rate * 100:.1f}% row-weighted; {len(clean_rows)} rows across "
        f"{len(by_symbol_hits)} symbols, ~{n_eff_total} independent thesis-windows)"
    )

    bench_rows = benchmark_relative_returns(clean_rows, price_marks, ecosystem_by_symbol)
    benchmarked = [r for r in bench_rows if r["benchmark_relative_pct"] is not None]
    unbenchmarked = len(bench_rows) - len(benchmarked)
    lines.append("")
    lines.append("-- By score bucket (benchmark-relative: minus the REST of own ecosystem's mean RAW return, sign-matched to direction) --")
    if unbenchmarked:
        lines.append(f"({unbenchmarked} row(s) skipped -- no other priced symbol in their ecosystem over this window)")
    bench_for_bucket = [{**r, "signed_return_pct": r["benchmark_relative_pct"]} for r in benchmarked]
    if bench_for_bucket:
        lines.extend(_bucket_table(bench_for_bucket, horizon_days, "Mean Alpha %"))

    lines.append("")
    lines.append("-- Per-symbol breakdown (worst first) --")
    lines.append(f"{'Symbol':<8}{'Count':<8}{'Mean Return %':<16}")
    for s in per_symbol_breakdown(clean_rows):
        lines.append(f"{s['symbol']:<8}{s['count']:<8}{s['mean_return_pct']:<16.2f}")

    return "\n".join(lines)
