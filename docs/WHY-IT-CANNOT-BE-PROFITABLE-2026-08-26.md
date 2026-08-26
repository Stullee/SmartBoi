# Why this cannot be profitable yet — 2026-08-26 (0.79.0)

Not a list of defects. One structural problem, in two halves, measured on the
2026-08-26 bundle. Both halves are independent of every bug fixed so far, and
neither is fixed by fixing more of them.

---

## Half 1: the gate is closed by construction

`_apply_synthesis` folds the whole-body verdict onto the dossier with `min()`,
so the synthesis-rated score is the score the signal bar sees. The bar is
**0.250** (`signal_confidence_threshold`).

Across **532 traced synthesis verdicts** (2026-08-14 → 08-26):

```
verdicts whose rated score reaches 0.250 :  0  of 532
highest rated score ever returned        :  0.210   (SPWR, 2026-08-16)
highest confidence ever returned         :  0.60
highest magnitude ever returned          :  0.40
median non-zero rated score              :  0.045
```

The ceiling of the pass is below the floor for action. **A dossier that has
been synthesis-judged can never signal**, no matter what the evidence says.

Which means every trade this system opens is one the gate never saw. Since the
`[UNJUDGED]` detector was added on 2026-08-15, **7 signals have fired and all 7
fired on raw arithmetic with no verdict** — at scores of 1.000, 0.938, 0.721,
0.552, 0.418, 0.410. Nothing synthesis has ever rated comes close to those,
because the two passes are not on the same scale:

| | per-item updater (haiku) | whole-body synthesis (opus-5) |
|---|---|---|
| n | 125 | 532 |
| median score | 0.000 | 0.045 |
| p90 score | 0.120 | 0.106 |
| **max score** | **0.488** | **0.210** |
| share ≥ 0.25 bar | 3% | **0%** |

The aggregate reaches 1.0 only by stacking corroboration multipliers across
items. Synthesis then re-rates the body on a scale that tops out at 0.21. The
bar was set against the first scale and is applied to the second.

So the funnel is: arithmetic inflates → synthesis correctly deflates → the
deflated number can never clear the bar → the only trades are the ones that
slipped through before synthesis ran. That is the whole explanation for
"nothing has opened in 5 days".

---

## Half 2: and the score is anti-predictive anyway

This is the half that matters, because fixing Half 1 alone would simply make
the system trade — and the names it would trade are the losing ones.

Joining `dossier_snapshots.jsonl` to `price_marks.jsonl`, forward return over
10 sessions, signed by the thesis direction, **symbol-equal-weighted** (one
vote per symbol, so one long-lived thesis cannot carry a bucket):

| score bucket | rows | symbols | mean 10d | hit rate |
|---|---|---|---|---|
| 0.00 – 0.10 | 123 | 31 | **+7.63%** | 71% |
| 0.10 – 0.20 | 65 | 22 | +6.44% | 59% |
| 0.20 – 0.40 | 147 | 36 | +2.61% | 56% |
| **0.40 – 1.00** | 114 | 28 | **−1.47%** | **43%** |

Monotone decreasing across every bucket. The universe baseline over the same
window is **+3.23%**, so the highest-conviction names underperform the
universe by about five points and are the only bucket that loses money — and
the only bucket that could ever clear the signal bar.

Two confounds checked and ruled out:

- **The outage.** All 449 rows predate 2026-08-21; the frozen-score window
  contributes zero joinable rows, so none of this is dead-score-vs-live-price.
- **Direction mix.** SHORT share by bucket is 17% / 11% / 15% / 14% — flat.
  This is not "the high bucket is short in an up-market".

### The mechanism

Suggestive, not proven — only 25 rows have both a trailing and a forward
window, so read the direction, not the magnitudes:

| score bucket | prior 10d | forward 10d |
|---|---|---|
| 0.10 – 0.20 | +11.8% | −17.3% |
| 0.20 – 0.40 | +25.3% | −10.8% |
| 0.40 – 1.00 | +4.0% | −6.4% |

Every bucket has a strongly positive move *behind* it and a negative move
ahead. The score's main amplifier is corroboration — more independent sources
saying the same thing. But N sources writing about something is the definition
of the market having seen it. **The score is a completed-move detector.**

This is why synthesis vetoes everything, and it means synthesis is the
component that is *working*. `already_priced_in` is detecting exactly this, and
the WOLF verdict says it in words: "entering short after a fully-absorbed 22%
drawdown on one restated fact is chasing." The broken component is the
arithmetic aggregate that keeps handing it chased moves.

---

## What follows

Do not lower the signal bar. That converts "never trades" into "trades the
−1.47% bucket", which is worse than doing nothing.

The three things worth doing, in order:

1. **Close the calibration loop before writing any more strategy code.** The
   system already captures the right raw material — daily score snapshots and
   daily price marks. Nothing reads them back to ask whether a score predicts
   anything. Until a bucket demonstrates positive out-of-sample expectancy,
   every threshold in the system is a guess.
2. **Test the inverted thesis.** The 0.00–0.10 bucket returned +7.63% at a 71%
   hit rate on 31 symbols. That is what the original premise actually predicts
   — get in while diffusion is incomplete, not after corroboration arrives. The
   current implementation rewards the opposite of its own thesis.
3. **Stop thresholding `confidence × magnitude`.** It is two unanchored
   LLM-elicited numbers multiplied together, produced by two passes that
   disagree with each other by 5x on the same evidence, compared against a
   hand-set constant. Nothing anywhere ties "confidence 0.6" to a 60% realized
   hit rate.

## Limits of this analysis

About one month of snapshots, 57 symbols, a single market regime (small caps
drifting up — the +3.23% baseline). The join is daily, so intraday timing is
invisible. The mechanism table is n=25. This is enough to say the current
decision variable does not work; it is not enough to say the inverted one does.
That is what step 1 is for.
