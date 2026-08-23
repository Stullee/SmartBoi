"""Per-symbol volatility, and the thresholds that should scale with it.

Three numbers in this system decide "has the move already happened", and all
three were single constants applied identically across the whole universe:

    engine._VETO_FALSIFICATION_DRIFT_PCT   8%   refutes an already-priced-in verdict
    settings.max_favorable_drift_pct      12%   refuses an entry that has run
    settings.stop_loss_pct / take_profit  50/100  the exit grid

That universe spans SIF at a $164M cap with no analyst coverage and DCO at
$2.6B with nine. A move that is three ordinary sessions for one is a week and
a half for the other, so a single percentage cannot mean the same thing on
both -- it is simultaneously too tight on the noisy name (refusing entries
that never had an information content) and too loose on the quiet one
(admitting moves that were genuinely the repricing this system exists to get
ahead of). engine.py's own comment concedes the point while picking 8%:
"3-5% daily vol is ordinary in this universe" is a volatility estimate,
hardcoded, at one value, for forty names.

This module supplies the estimate those constants were standing in for.

WHY TRUE RANGE AND NOT CLOSE-TO-CLOSE. The risk in a thin name is mostly
overnight: a 424B5 prices after the bell and the stock opens 20% lower having
"moved" nothing intraday. True range counts the gap; a high-minus-low does
not, and a close-to-close series computed from logs/price_marks.jsonl cannot
see either extreme. That is the same argument bars.py's docstring makes for
why the marks log is the wrong instrument for anything about what actually
happened to a price, and it applies here unchanged.

WHY WILDER SMOOTHING AND NOT A FLAT MEAN. A simple N-day mean drops a day
entirely once it ages past the window, so a single violent session enters and
later leaves the estimate as a step change -- and a threshold derived from it
steps with it. The EWMA decays that day's contribution instead, which is what
keeps a resolved threshold from moving several percentage points because one
old bar rolled off the back.

WHY PERCENT OF CLOSE. KLXE trades near $2 and PLPC near $284. An absolute
range is not comparable across them, and every threshold this feeds is a
percentage.

NO NETWORK, NO STATE, NO CLOCK. Pure functions over already-fetched bars, so
this is unit-testable on synthetic series exactly like backtest.py, and the
engine can compute a value once a day and cache it rather than reaching for a
provider inside an entry decision."""
from __future__ import annotations

from smartboi.bars import DailyBar

# The ATR (as % of close) at which the configured flat thresholds apply
# UNCHANGED. Everything here scales a threshold by atr_pct / this, so a name
# sitting exactly here keeps the behaviour the system has today.
#
# 4.0 is the midpoint of the "3-5% daily vol is ordinary in this universe"
# range that engine.py used to justify its 8% constant. Choosing it that way
# is deliberate: it makes this change a REDISTRIBUTION of the existing
# thresholds across the universe rather than a loosening or a tightening of
# them. The median name keeps its 12% drift gate; the noisy one gets more
# room and the quiet one less, which is the entire point.
REFERENCE_ATR_PCT = 4.0

# How far a resolved threshold may travel from its configured value, as a
# multiple of it. Both ends are load-bearing.
#
# The ceiling stops a halted, gapping or near-untradeable name from voting
# itself an enormous allowance: at 2x, a 12% drift gate can reach 24% and no
# further, however wild the tape gets. Without it a name whose ATR is 20%
# would be handed a 60% gate, which is not a gate.
#
# The floor stops the opposite failure, which is easier to overlook: a very
# quiet name would resolve to a threshold so tight that ordinary noise trips
# it, and the gate would refuse every entry it ever saw. A refused entry is
# invisible in the paper record -- it never becomes a trade -- so this failure
# mode is silent, which is exactly why it gets a clamp rather than a comment.
MIN_SCALE = 0.5
MAX_SCALE = 2.0

# Bars needed before an ATR is reported at all. Below this the estimate is
# dominated by whichever few sessions happen to be in hand, and a threshold
# derived from three bars is worse than the flat constant it replaced --
# it carries the same authority while being noise. The caller gets None and
# is expected to fall back to the configured value, NOT to a guess.
MIN_BARS = 15

# Wilder's default. Long enough to survive a single violent session, short
# enough to notice a regime change within a thesis horizon (max 21 days).
DEFAULT_LOOKBACK = 20


def true_range(prev_close: float, bar: DailyBar) -> float:
    """Wilder's true range: the session's extent including any overnight gap.

    max(high-low, |high-prev_close|, |low-prev_close|). The second and third
    terms are what make this different from a bare high-minus-low, and they
    are the terms that matter in this universe -- a stock that gaps 20% and
    then trades in a 2% band had a 20% session, not a 2% one."""
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def atr_pct(bars: list[DailyBar], lookback: int = DEFAULT_LOOKBACK) -> float | None:
    """Wilder-smoothed ATR over the most recent `lookback` sessions, as a
    percent of the latest close. None when there is not enough history (see
    MIN_BARS) or the data cannot support the calculation.

    `bars` is oldest-first, the order bars.py returns. The whole series is
    read -- pass as much history as is cached. More history does not make the
    reading older (the effective memory stays ~`lookback` sessions); it only
    dilutes the seed.

    None rather than 0.0 for the degenerate cases, because 0.0 is a VALID
    reading (a halted name that did not move) and would scale every threshold
    it touched to the floor. The two must not be confused: one means "this
    name is quiet", the other means "we do not know"."""
    if lookback < 1 or len(bars) < MIN_BARS:
        return None
    ranges = [
        true_range(prev.close, bar)
        for prev, bar in zip(bars, bars[1:])
    ]
    if not ranges:
        return None

    # Wilder smoothing over the WHOLE series, seeded on the simple mean of the
    # first `lookback` ranges. Written as an explicit recurrence rather than a
    # library call because this file deliberately has no numpy/pandas
    # dependency -- requirements.txt carries no scientific stack at all.
    #
    # Seeding on a mean rather than on ranges[0] is not a detail. With
    # alpha = 1/20, a single-bar seed still carries 0.95**19 = 38% of the
    # answer after a full lookback of smoothing, so one arbitrary session --
    # whichever happened to be first in the fetched window -- would dominate a
    # threshold that decides whether money is committed. Averaging the seed
    # period removes that, and running the recurrence over all available
    # history rather than a tail slice dilutes it to nothing: after 200 bars
    # the seed retains 0.003% of the weight.
    #
    # The estimate's effective memory is ~`lookback` sessions regardless of
    # how much history is passed, which is the property that matters -- a long
    # series makes the seed irrelevant, it does not make the reading stale.
    seed_n = min(lookback, len(ranges))
    smoothed = sum(ranges[:seed_n]) / seed_n
    alpha = 1.0 / lookback
    for value in ranges[seed_n:]:
        smoothed = smoothed + alpha * (value - smoothed)

    latest_close = bars[-1].close
    if latest_close <= 0:
        return None
    return smoothed / latest_close * 100.0


def volatility_scale(atr_percent: float | None) -> float:
    """How far this name's thresholds should sit from their configured values,
    as a multiple: 1.0 for a reference-volatility name, clamped to
    [MIN_SCALE, MAX_SCALE].

    Returns exactly 1.0 for an unknown ATR, so a caller that does not check
    for None still gets the system's existing behaviour rather than an
    accidental widening."""
    if atr_percent is None or atr_percent <= 0:
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, atr_percent / REFERENCE_ATR_PCT))


def scaled_threshold(configured_pct: float, atr_percent: float | None) -> float:
    """A configured percentage threshold, resized for this name's volatility.

    The single entry point callers should use. `configured_pct` keeps its
    existing meaning as the value for a reference-volatility name, so the
    setting on the dashboard still describes the typical case and an unknown
    ATR reproduces today's behaviour exactly."""
    return configured_pct * volatility_scale(atr_percent)
