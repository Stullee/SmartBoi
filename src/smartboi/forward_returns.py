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
# The 0.65 boundary deliberately matches the default
# SIGNAL_CONFIDENCE_THRESHOLD: the single most important question this
# report answers is "does the region that actually trades (>= threshold)
# beat the region just below it?", and a bucket straddling the threshold
# (the old 0.5-1.01 top bucket) structurally could not answer it.
SCORE_BUCKETS = ((0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 1.01))


# The small-cap market index, not the large-cap one: this universe is
# small/mid caps, so IWM is the beta they actually carry. SPY is marked too
# and can be swapped in here, but IWM is the honest default.
MARKET_BENCHMARK = "IWM"


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
        session = snapshot_session(row)
        if not symbol or not session:
            continue
        key = (symbol, session)
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
        # session_date is the real key: it says which SESSION this close
        # belongs to. marked_at is when the row happened to be written, and
        # the two genuinely differ -- a mark taken at 16:20 ET Friday, or on
        # the Saturday after, is Friday's close either way. Rows written
        # before the session anchor existed have no session_date, so they
        # fall back to the old wall-clock key; that is also exactly the
        # population carrying the lookahead the anchor fixed, which is why
        # scoring_version and --since exist to exclude them.
        key = row.get("session_date") or (row.get("marked_at") or "")[:10]
        if not symbol or price is None or not key:
            continue
        out.setdefault(symbol, {})[key] = price
    return out


def snapshot_session(snapshot: dict) -> str:
    """The session a dossier snapshot describes, falling back to its write
    timestamp for rows predating the session anchor."""
    return snapshot.get("session_date") or (snapshot.get("snapshotted_at") or "")[:10]


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
    session = snapshot_session(snapshot)
    if not session:
        return None
    entry = _price_on_or_after(marks, session, max_lookahead_days)
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
        entry = {
            "bucket": label,
            "count": len(vals),
            "mean_return_pct": sum(vals) / len(vals),
            "hit_rate": sum(1 for v in vals if v > 0) / len(vals),
        }
        if horizon_days is not None:
            by_symbol: dict[str, list[float]] = {}
            for r in bucket_rows:
                by_symbol.setdefault(r["symbol"], []).append(r["signed_return_pct"])
            entry["n_symbols"] = len(by_symbol)
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


def market_benchmark_return(
    price_marks: dict[str, dict[str, float]],
    entry_date: str,
    horizon_days: int,
    benchmark_symbol: str,
    max_lookahead_days: int = 5,
) -> float | None:
    """Raw (unsigned) return of one benchmark ETF over the same window.

    Distinct from ecosystem_benchmark_return, and the distinction is the
    whole point. The ecosystem benchmark averages this universe's OWN
    members, so it answers "did this name beat its peers" -- useful, but it
    cannot separate alpha from beta, because every peer carries the same
    market exposure. An index ETF can. With nine ecosystems that are largely
    one bet on the AI/electrification capex cycle, that separation is the
    difference between a measurable edge and a leveraged position on the
    small-cap market."""
    marks = price_marks.get(benchmark_symbol)
    if not marks:
        return None
    entry = _price_on_or_after(marks, entry_date, max_lookahead_days)
    if entry is None or entry[1] == 0:
        return None
    entry_used, entry_price = entry
    target = (date.fromisoformat(entry_used) + timedelta(days=horizon_days)).isoformat()
    exit_ = _price_on_or_after(marks, target, max_lookahead_days)
    if exit_ is None:
        return None
    return (exit_[1] - entry_price) / entry_price * 100.0


def filter_by_scoring_version(
    rows: list[dict], scoring_version: int | None = None, since: str = "",
) -> list[dict]:
    """Restrict snapshots to one scoring regime, and optionally to rows on
    or after a session date.

    This function is the missing half of a guard that already existed.
    `SCORING_VERSION` was stamped on every snapshot and signal row, with a
    docstring explaining precisely why pooling across a rules change is
    dishonest -- and NOTHING in any reader ever filtered on it. It was
    written at three sites and read at zero, so every report silently pooled
    v1 through v4. A version stamp nothing filters on is a comment.

    Defaults to the CURRENT scoring version, so the default report answers
    "what is the edge under the rules in force" rather than "what is the
    average of four incompatible rule sets". Pass scoring_version=None to
    deliberately pool everything."""
    out = []
    for row in rows:
        if scoring_version is not None and row.get("scoring_version") != scoring_version:
            continue
        if since and snapshot_session(row) < since:
            continue
        out.append(row)
    return out


def benchmark_relative_returns(
    rows: list[dict],
    price_marks: dict[str, dict[str, float]],
    ecosystem_by_symbol: dict[str, str],
    max_lookahead_days: int = 5,
    market_symbol: str = MARKET_BENCHMARK,
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
    market_cache: dict[tuple[str, str, int], float | None] = {}
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
        row = {**r, "ecosystem": eco, "ecosystem_benchmark_pct": signed_benchmark,
               "benchmark_relative_pct": relative}
        # Market-relative, in addition to peer-relative. Sign-matched the
        # same way, so positive always means "beat the benchmark in the
        # direction the thesis was taking".
        market_key = (market_symbol, r["entry_date"], r["horizon_days"])
        if market_key not in market_cache:
            market_cache[market_key] = market_benchmark_return(
                price_marks, r["entry_date"], r["horizon_days"], market_symbol, max_lookahead_days,
            )
        raw_market = market_cache[market_key]
        if raw_market is None:
            row["market_benchmark_pct"] = None
            row["market_relative_pct"] = None
        else:
            signed_market = raw_market if r["direction"] == "LONG" else -raw_market
            row["market_benchmark_pct"] = signed_market
            row["market_relative_pct"] = r["signed_return_pct"] - signed_market
        out.append(row)
    return out


def _bucket_table(rows: list[dict], horizon_days: int, value_header: str) -> list[str]:
    lines = [
        f"{'Bucket':<14}{'Rows':<7}{'Syms':<6}{'N_eff':<7}{value_header:<15}{'90% CI':<20}{'Hit Rate':<10}"
    ]
    for b in bucket_returns(rows, horizon_days):
        ci = b.get("ci_90")
        ci_text = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a (1 sym)"
        lines.append(
            f"{b['bucket']:<14}{b['count']:<7}{b.get('n_symbols', 0):<6}{b.get('n_effective', 0):<7}"
            f"{b['mean_return_pct']:<15.2f}{ci_text:<20}{b['hit_rate'] * 100:<9.1f}%"
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

    lines.append("")
    lines.append("-- By score bucket (raw, signed in thesis direction) --")
    lines.extend(_bucket_table(rows, horizon_days, "Mean Return %"))

    corr = pearson_correlation([r["score"] for r in rows], [r["signed_return_pct"] for r in rows])
    lines.append("")
    lines.append(f"Score vs. signed forward return correlation: {corr:.3f}" if corr is not None else "Score vs. signed forward return correlation: n/a (insufficient data)")
    lines.append("(row-level, so overlapping windows inflate it -- trust the bucket CIs above over this number)")
    overall_hit_rate = sum(1 for r in rows if r["signed_return_pct"] > 0) / len(rows)
    n_eff_total = effective_sample_count(rows, horizon_days)
    lines.append(
        f"Overall hit rate: {overall_hit_rate * 100:.1f}% "
        f"({len(rows)} rows, ~{n_eff_total} independent thesis-windows)"
    )

    bench_rows = benchmark_relative_returns(rows, price_marks, ecosystem_by_symbol)
    benchmarked = [r for r in bench_rows if r["benchmark_relative_pct"] is not None]
    unbenchmarked = len(bench_rows) - len(benchmarked)
    lines.append("")
    lines.append("-- By score bucket (benchmark-relative: minus the REST of own ecosystem's mean RAW return, sign-matched to direction) --")
    if unbenchmarked:
        lines.append(f"({unbenchmarked} row(s) skipped -- no other priced symbol in their ecosystem over this window)")
    bench_for_bucket = [{**r, "signed_return_pct": r["benchmark_relative_pct"]} for r in benchmarked]
    if bench_for_bucket:
        lines.extend(_bucket_table(bench_for_bucket, horizon_days, "Mean Alpha %"))

    # Market-relative, and this is the table to read. The ecosystem
    # benchmark above compares a name against its own peers, all of which
    # carry the same market exposure, so it cannot separate alpha from beta.
    # IWM can, and doing so is what takes the observations needed to detect
    # a 1%/trade edge from roughly a thousand down to a couple of hundred.
    market_rows = [r for r in bench_rows if r.get("market_relative_pct") is not None]
    lines.append("")
    lines.append(f"-- By score bucket (market-relative: minus {MARKET_BENCHMARK}, sign-matched to direction) --")
    if not market_rows:
        lines.append(
            f"({MARKET_BENCHMARK} has no price marks over these windows yet. It is marked daily "
            "alongside the universe from the first tick after this shipped, so this table fills "
            "in one horizon from then -- it cannot be backfilled.)"
        )
    else:
        skipped = len(bench_rows) - len(market_rows)
        if skipped:
            lines.append(f"({skipped} row(s) skipped -- no {MARKET_BENCHMARK} mark over their window)")
        lines.extend(_bucket_table(
            [{**r, "signed_return_pct": r["market_relative_pct"]} for r in market_rows],
            horizon_days, "Mean Alpha %",
        ))

    lines.append("")
    lines.append("-- Per-symbol breakdown (worst first) --")
    lines.append(f"{'Symbol':<8}{'Count':<8}{'Mean Return %':<16}")
    for s in per_symbol_breakdown(rows):
        lines.append(f"{s['symbol']:<8}{s['count']:<8}{s['mean_return_pct']:<16.2f}")

    return "\n".join(lines)
