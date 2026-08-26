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
vote per symbol, so one long-lived thesis cannot carry a bucket).

**Corrected 2026-08-26, after building the measurement properly.** The first
pass at this used an ad-hoc join and overstated both the effect size and its
monotonicity. Run through the real pipeline — `dedup_snapshots`,
session-correct price marks, synthesis-vetoed rows excluded — the effects are
smaller and the shape is not a clean staircase. What survives is the
conclusion, not the drama:

| variable | high-minus-low spread | verdict |
|---|---|---|
| score (confidence × magnitude) | −1.49 pts | anti-predictive |
| confidence alone | −1.34 pts | anti-predictive |
| magnitude alone | −2.72 pts | anti-predictive |
| **independent source count** | **−3.87 pts** | **anti-predictive** |

Baseline across every joined row: **+2.68%** over 10 sessions, 58% hit, 65
symbols. Read every band as excess over that.

Not one of the four variables a threshold could be set on earns it. The
corroboration count is the worst of them, and it is the one the strategy is
built on.

| source count | rows | symbols | mean 10d | hit rate |
|---|---|---|---|---|
| **1** | 213 | 51 | **+4.40%** | 63% |
| 2 | 72 | 21 | −1.59% | 38% |
| 3–4 | 109 | 34 | −1.96% | 43% |
| 5+ | 245 | 37 | +0.53% | 51% |

The single-source band is the only one that beats the baseline. It is not a
monotone decline — 5+ recovers to roughly flat — so the honest statement is
narrower than "more evidence is worse": **the one band that beats the universe
is the one `min_independent_sources = 2` structurally refuses to trade.**

Two confounds checked and ruled out:

- **The outage.** Every joined row predates 2026-08-21; the frozen-score window
  contributes zero joinable rows at a 10-session horizon, so none of this is
  dead-score-vs-live-price.
- **Direction mix.** SHORT share by band runs 11–17% throughout — flat. This is
  not "the high band is short in an up-market".

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

Do not lower the signal bar. Every variable it could be set on is
anti-predictive, so lowering it converts "never trades" into "trades the bands
that lose money" — worse than doing nothing.

Three things, in order:

1. **DONE — the calibration loop is closed.** `forward_returns.calibrate` now
   buckets by every captured decision variable and reports the high-minus-low
   spread, and the diagnostics dump prints it without being asked. The next
   threshold argument is settled by a table rather than an opinion. That is the
   fix that had to come first, because everything below is a hypothesis until
   something can measure it.
2. **Test the inverted thesis, on more data.** The single-source band is the
   only one beating the universe (+4.40% vs +2.68%, 51 symbols). That is what
   the original premise actually predicts — enter while diffusion is
   incomplete, not after corroboration has arrived. But one month in one
   regime is not enough to invert a strategy on, and flipping
   `min_independent_sources` on this evidence would repeat the original mistake
   in the opposite direction. Let the capture run and watch the spread.
3. **Stop thresholding `confidence × magnitude`.** Two unanchored
   LLM-elicited numbers multiplied together, produced by two passes that
   disagree with each other by 5x on the same evidence, compared against a
   hand-set constant. Nothing anywhere ties "confidence 0.6" to a 60% realized
   hit rate — and the calibration table now says so out loud.

## Limits of this analysis

About one month of snapshots, 65 symbols, a single market regime (small caps
drifting up — the +2.68% baseline). The join is daily, so intraday timing is
invisible. The mechanism table is n=25 and is suggestive only. Bands are 21–51
symbols, and the source-count relationship is not monotone.

This is enough to say no current decision variable earns its threshold. It is
**not** enough to say the inverted one does. Step 1 exists so that question
gets answered by accumulation rather than by argument.
