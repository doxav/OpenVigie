PR #3 — pyro-eval
Title: feat(metrics): stratified recall, FP/camera/day and a non-regression gate

Hello 👋 — I've been building [OpenVigie](https://github.com/doxav/OpenVigie),
a watch-tower detection toolkit, and kept running into the same evaluation
questions you've already thought about. Rather than reinvent them badly on my
side, here's what I ended up with, offered upstream.

**I read the code first, so let me start with what you already have**, because
my first draft of this PR got two things wrong:

- sequence-level precision / recall / F1 — already there;
- `avg_detection_delay` — already there too.

So this isn't "you have no operational metrics". It's four specific gaps I hit,
in decreasing order of how much they bit me.

---

## 1. Aggregate recall hides where the system fails

Global recall is dominated by the easy cases — large, close plumes — which are
also the least urgent. A model can improve its headline number while losing
exactly the capability the system exists for.

Computed with the proposed module on synthetic sequences:

```
baseline  : global recall 0.60 | worst stratum 0.60
candidate : global recall 0.60 | worst stratum 0.20

2 blocking regressions:
  - [stratum_recall] plume_size_px [0, 20) px    : recall down 0.400
  - [stratum_recall] distance_m [7000, 15000) m  : recall down 0.400
```

Identical aggregate recall. Small distant plumes — early detections, the whole
point — down from 0.60 to 0.20. No aggregate ranking catches that, which is why
`stratified_recall()` keeps `n` visible per stratum: a recall of 1.0 over two
sequences means nothing, and hiding the count would be worse than showing it.

## 2. Average delay is the wrong statistic

`avg_detection_delay` exists, but the distribution is strongly skewed. A system
with an excellent mean that takes an hour one time in ten isn't dependable, and
the mean won't say so. `detection_delays()` reports **median and p90**, and
counts undetected fires separately rather than imputing a value — imputing
would quietly distort the very statistic you're reading.

## 3. FPR isn't comparable across datasets, nor readable as workload

`fpr_to_fp_per_camera_per_day(fpr, frames_per_day)` normalises it:

```
frames per camera per day at 30 s cadence : 2880
  FPR 0.05 -> 144 raw activations / camera / day
  budget of 1 alert/day -> max FPR 0.000347
```

**Important caveat, and I got this wrong at first**: that is *not*
operator-visible alerts. Your predictor smooths over `nb_consecutive_frames`
with a majority vote on spatially-consistent boxes plus hysteresis, which
removes most of these before they ever become alerts. What the conversion
actually measures is the **input load on that temporal filter**.

I still think it's worth reporting, because the filter's protection is very
uneven:

```
window of 8, majority vote:
  FP present in  5% of frames -> survives  0.04%   (flicker: crushed)
  FP present in 20% of frames -> survives  5.6%
  FP present in 80% of frames -> survives 99.0%    (persistence: untouched)
```

Temporal smoothing is near-perfect against erratic artefacts and near-useless
against **persistent** ones — fog banks, industrial plumes, dust, stationary
cloud. Which are exactly the false positives that cost operator time. So a
given FPR isn't reassuring on the grounds that a temporal filter follows it:
what matters is the *persistent fraction*, which FPR alone doesn't tell you.

`temporal_suppression_factor(raw, observed)` makes the dependency explicit — a
factor of 1440 means the filter absorbs 99.93 % of activations, and apparent
performance rests on it rather than on the detector. Worth watching rather than
celebrating.

## 4. Nothing blocks a regression

`release_gate(baseline, candidate)` refuses a version that regresses on **any**
stratum, even if its aggregate improves. Tolerances are deliberately asymmetric
— extra false-positive load is manageable, a recall or delay regression isn't.
Strata below `min_stratum_size` aren't opposable, because blocking a release on
a recall computed over two sequences would destroy trust in the gate itself.

Plus `pareto_front()` / `select_under_budget()`, because there's no single best
setting: lowering a threshold buys recall and costs false positives. Exposing
the non-dominated set leaves the trade-off to whoever bears its consequences. A
change like "FP 114→86, FN 1→2" is a *move along the front* — neither an
improvement nor a regression — and current reporting can't express that.

---

## Shape of the patch

Purely additive; nothing existing is removed or changed.

1. new module `pyro_eval/opmetrics.py` — NumPy + stdlib only, no new dependency;
2. `evaluation.py` prints the operational block alongside the existing one;
3. optional `--baseline <run>` on `run_evaluation.py` to run the gate in CI;
4. tests.

Reference implementation, 76 tests:
[`openvigie/opmetrics.py`](https://github.com/doxav/OpenVigie/blob/main/src/openvigie/opmetrics.py)
— Apache-2.0, same licence, free to vendor, adapt or cherry-pick. If only the
stratified recall and the gate are of interest, they stand alone.

## Questions I genuinely don't know the answer to

- **What's your real cadence?** The FP/day conversion is only as good as
  `frames_per_camera_per_day`, and it presumably varies per deployment.
- **Which strata matter to you?** I implemented size and distance because
  they're derivable from annotations. Visibility is supported but needs a
  field. If your annotations carry something better, the binning is
  parameterised.
- **Gate blocking, or report-only?** I'd start report-only for a season,
  collect what it *would* have blocked, then decide. Blocking on thresholds
  nobody has calibrated seems premature.
- **How do you define ignition for TTD?** Mine is ignition → first alert. If
  yours differs, no number is comparable until that's aligned.

## Honest limitation

Validated on synthetic outcomes — I have no access to your evaluation data, so
the thresholds in `GateConfig` are engineering judgement, not measurement. What
I can vouch for is that the metrics compute what they claim. Whether the
defaults suit your deployments is exactly where I'd want your input, and I'm
happy to be told the framing is wrong — I've already had to correct it once
after reading your code more carefully.
