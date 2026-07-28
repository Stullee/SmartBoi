"""Pure "does score predict forward returns" math -- the offline analysis
behind scripts/analyze_forward_returns.py. No network, no engine
dependency: everything here operates on already-loaded rows from
logs/dossier_snapshots.jsonl (status.py's snapshot_dossier) and
logs/price_marks.jsonl (engine.py's _run_daily_price_marks), both
append-only daily logs captured for exactly this purpose -- see README's
"Forward-validation data capture"."""
from __future__ import annotations

from datetime import date, timedelta

# Score buckets: is the forward-return relationship monotonic across them?
# A real edge should show higher buckets outperforming lower ones; if it
# doesn't, raising the signal threshold is not obviously the fix either.
SCORE_BUCKETS = ((0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 1.01))


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
        "entry_date": entry_date,
        "entry_price": entry_price,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "horizon_days": horizon_days,
        "signed_return_pct": signed_pct,
    }


def bucket_returns(rows: list[dict]) -> list[dict]:
    """Mean signed forward return and hit-rate per score bucket -- is the
    relationship monotonic (higher score, better forward return)? Buckets
    with zero rows are omitted rather than shown as a misleading 0.0."""
    grouped: dict[str, list[float]] = {}
    for r in rows:
        grouped.setdefault(_bucket_label(r["score"]), []).append(r["signed_return_pct"])
    out = []
    for lo, hi in SCORE_BUCKETS:
        label = _bucket_label(lo)
        vals = grouped.get(label)
        if not vals:
            continue
        out.append({
            "bucket": label,
            "count": len(vals),
            "mean_return_pct": sum(vals) / len(vals),
            "hit_rate": sum(1 for v in vals if v > 0) / len(vals),
        })
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
) -> float | None:
    """Mean RAW (unsigned, price-only) return of every symbol in
    `ecosystem` over [entry_date, entry_date + horizon_days], built from
    every symbol price_marks tracks -- not just symbols that happen to
    have a dossier. That distinction matters: an ecosystem with only one
    dossier would otherwise have its "benchmark" computed from that same
    dossier's own return, making alpha exactly 0 by construction rather
    than a real measurement (confirmed live: IESC, the only grid_datacenter
    dossier, showed 0.00 mean alpha for exactly this reason). Returns None
    if no symbol in the ecosystem has both an entry- and exit-date price."""
    returns = []
    for symbol, eco in ecosystem_by_symbol.items():
        if eco != ecosystem:
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
    cache: dict[tuple[str, str, int], float | None] = {}
    out = []
    for r in rows:
        eco = ecosystem_by_symbol.get(r["symbol"], "?")
        cache_key = (eco, r["entry_date"], r["horizon_days"])
        if cache_key not in cache:
            cache[cache_key] = ecosystem_benchmark_return(
                price_marks, r["entry_date"], r["horizon_days"], eco, ecosystem_by_symbol, max_lookahead_days
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


def format_report(
    horizon_days: int,
    rows: list[dict],
    price_marks: dict[str, dict[str, float]],
    ecosystem_by_symbol: dict[str, str],
) -> str:
    """One horizon's full report as plain text: bucket table (raw and
    benchmark-relative), correlation, overall hit-rate, per-symbol
    breakdown. Returns a one-line "no data" message instead of empty
    tables when there's nothing joinable for this horizon yet -- forward
    data takes horizon_days to even exist."""
    lines = [f"=== Forward returns, {horizon_days}-day horizon ({len(rows)} joined snapshot(s)) ==="]
    if not rows:
        lines.append("No joinable snapshot/price-mark pairs yet for this horizon.")
        return "\n".join(lines)

    lines.append("")
    lines.append("-- By score bucket (raw, signed in thesis direction) --")
    lines.append(f"{'Bucket':<14}{'Count':<8}{'Mean Return %':<16}{'Hit Rate':<10}")
    for b in bucket_returns(rows):
        lines.append(f"{b['bucket']:<14}{b['count']:<8}{b['mean_return_pct']:<16.2f}{b['hit_rate'] * 100:<9.1f}%")

    corr = pearson_correlation([r["score"] for r in rows], [r["signed_return_pct"] for r in rows])
    lines.append("")
    lines.append(f"Score vs. signed forward return correlation: {corr:.3f}" if corr is not None else "Score vs. signed forward return correlation: n/a (insufficient data)")
    overall_hit_rate = sum(1 for r in rows if r["signed_return_pct"] > 0) / len(rows)
    lines.append(f"Overall hit rate: {overall_hit_rate * 100:.1f}% ({len(rows)} theses)")

    bench_rows = benchmark_relative_returns(rows, price_marks, ecosystem_by_symbol)
    benchmarked = [r for r in bench_rows if r["benchmark_relative_pct"] is not None]
    unbenchmarked = len(bench_rows) - len(benchmarked)
    lines.append("")
    lines.append("-- By score bucket (benchmark-relative: minus own ecosystem's mean RAW return, sign-matched to direction) --")
    if unbenchmarked:
        lines.append(f"({unbenchmarked} row(s) skipped -- no other priced symbol in their ecosystem over this window)")
    bench_for_bucket = [{**r, "signed_return_pct": r["benchmark_relative_pct"]} for r in benchmarked]
    lines.append(f"{'Bucket':<14}{'Count':<8}{'Mean Alpha %':<16}{'Hit Rate':<10}")
    for b in bucket_returns(bench_for_bucket):
        lines.append(f"{b['bucket']:<14}{b['count']:<8}{b['mean_return_pct']:<16.2f}{b['hit_rate'] * 100:<9.1f}%")

    lines.append("")
    lines.append("-- Per-symbol breakdown (worst first) --")
    lines.append(f"{'Symbol':<8}{'Count':<8}{'Mean Return %':<16}")
    for s in per_symbol_breakdown(rows):
        lines.append(f"{s['symbol']:<8}{s['count']:<8}{s['mean_return_pct']:<16.2f}")

    return "\n".join(lines)
