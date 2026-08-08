"""Detects price discontinuities that are corporate actions rather than
returns, so neither the trade record nor the forward panel books them as P&L.

Nothing in this system adjusts for splits. `PaperTrade` freezes
`entry_price`, `stop_price` and `target_price` as absolute dollar levels at
open and compares them forever against raw, unadjusted prices
(`whatToShow="TRADES"` from IB, Finnhub `/quote`'s raw `c`/`h`/`l`). With
the shipped -50%/+100% bands, ANY split of 2:1 or greater in either
direction lands outside the band on the first print after the ex-date:

  - a 1-for-10 reverse split multiplies the print by 10, so `day_high >=
    target_price` and the trade books a maximal WIN on a position that did
    not move;
  - a 2:1 forward split halves it and books a stop-out.

Sub-$1 Nasdaq compliance reverse splits are routine in precisely this
universe -- thinly-covered small caps -- so this is a recurring fabrication,
not a tail risk.

The panel is hit harder than the trade log, and it is the dataset that
reaches significance first. `compute_forward_return` divides two raw marks,
so one 1-for-10 reverse split inside a window produces a +900% observation
that swamps every bucket mean and every bootstrap CI it lands in.

WHAT THIS DOES NOT DO: adjust. Adjustment needs a split-ratio feed this
system does not have, and guessing a ratio wrong corrupts the record in a
new way. This refuses to score what it cannot trust, and says so loudly.

## Telling a split from a real move

The naive test -- "flag anything above N%" -- is wrong here, and wrong in
the direction that matters. A thinly-covered small cap doubling on a
contract award or buyout is exactly the event this whole system exists to
catch. Voiding those would bias the record AGAINST the strategy while
fixing a bias in the other direction.

The discriminator is that a split lands on an EXACT simple ratio and a real
move does not. A 1-for-10 reverse split multiplies the price by almost
precisely 10 (times that day's genuine move); a buyout pop lands at 2.31x
or 1.87x -- an arbitrary number. So:

  - a large jump close to a simple split ratio  -> treat as a corporate
    action: refuse to resolve stops/targets against it, alert, and void the
    trade rather than scoring it;
  - a large jump at an arbitrary ratio          -> treat as a real return:
    score it normally, but alert, because it is also what a bad tick looks
    like and the operator should see it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Ratios a US-listed split actually uses. Forward splits divide the price
# (2:1 -> 0.5), reverse splits multiply it (1-for-10 -> 10.0); both
# directions are generated from this list.
# 3-for-2 and 5-for-2 splits exist but are deliberately absent: their
# ratios (1.5, 2.5) sit deep inside the range of ordinary small-cap moves,
# so "detecting" them means mislabelling real returns. They also cannot
# fabricate a WIN or a stop-out on their own, because they do not carry the
# price across the -50%/+100% band.
_SPLIT_NUMERATORS = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0,
                     12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 100.0)

# Below this, a move is ordinary market behaviour and not worth inspecting.
# ln(2) means "doubled, or halved". A US equity can genuinely do that in one
# session, which is why crossing this floor only starts the question rather
# than answering it.
JUMP_FLOOR_LOG = math.log(2.0)

# Beyond this, no real single-session move exists. Nothing US-listed
# quadruples or loses three quarters of its value in one session without a
# corporate action behind it, so above this the exact ratio stops mattering
# and everything is treated as an action.
CERTAIN_ACTION_LOG = math.log(4.0)

# Inside the ambiguous zone (2x-4x, or 0.25x-0.5x) a real move and a split
# genuinely overlap, so only a CLOSE match to an exact ratio counts. 7%
# absorbs an ordinary session on top of the split without swallowing
# arbitrary moves: a genuine 2.31x buyout pop stays 2.31x, while a 1-for-2
# reverse on a flat day is 2.00x.
#
# The narrow band is the right trade-off because of where the damage is.
# A fabricated WIN or stop-out happens exactly when the ratio carries the
# price across the -50%/+100% band -- i.e. at 2.0 and 0.5, which is
# precisely where exact-ratio matching is sharpest. A 1-for-2 reverse on a
# -10% day (1.8x) is missed, and misses nothing: at 1.8x the price never
# reaches the target, so nothing is fabricated.
SPLIT_TOLERANCE = 0.07


def _split_candidates() -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for n in _SPLIT_NUMERATORS:
        label = f"{n:g}".rstrip("0").rstrip(".") if n % 1 else f"{int(n)}"
        out.append((n, f"1-for-{label} reverse split"))
        out.append((1.0 / n, f"{label}-for-1 forward split"))
    return out


@dataclass(frozen=True)
class PriceJump:
    """A move large enough that it may not be a return at all."""

    reference_price: float
    current_price: float
    ratio: float
    # True when the ratio is close to an exact split ratio, i.e. this is
    # probably a corporate action and NOT a return.
    is_split_like: bool
    # Human-readable guess, e.g. "1-for-10 reverse split". Empty when the
    # jump is large but lands nowhere near a split ratio.
    likely_action: str

    @property
    def pct(self) -> float:
        return (self.ratio - 1.0) * 100.0

    def describe(self) -> str:
        if self.is_split_like:
            return (f"{self.reference_price:.4g} -> {self.current_price:.4g} "
                    f"({self.ratio:.3g}x) matches a {self.likely_action}")
        return (f"{self.reference_price:.4g} -> {self.current_price:.4g} "
                f"({self.pct:+.0f}%) is not near any split ratio")


def classify_price_jump(reference_price: float | None, current_price: float | None) -> PriceJump | None:
    """None when the move is ordinary (or unmeasurable). A PriceJump when it
    is big enough to be worth deciding about; read `is_split_like` to decide
    which kind of big."""
    if not reference_price or not current_price or reference_price <= 0 or current_price <= 0:
        return None
    ratio = current_price / reference_price
    log_move = abs(math.log(ratio))
    if log_move < JUMP_FLOOR_LOG:
        return None
    nearest, label = min(
        _split_candidates(), key=lambda pair: abs(math.log(ratio / pair[0])),
    )
    if log_move >= CERTAIN_ACTION_LOG:
        # No exact-ratio test: past 4x there is no competing explanation, and
        # demanding a clean match would let an odd ratio (a 1-for-8 on a
        # heavy day, a stale reference price) through as a "real return".
        return PriceJump(reference_price, current_price, ratio, True, label)
    if abs(ratio - nearest) <= SPLIT_TOLERANCE * nearest:
        return PriceJump(reference_price, current_price, ratio, True, label)
    return PriceJump(reference_price, current_price, ratio, False, "")
