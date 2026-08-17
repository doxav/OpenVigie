PR #3 — pyro-eval
Title: feat(metrics): operational KPIs (FP/camera/day, TTD, stratified recall) and a non-regression release gate

## Problem

`pyro-eval` currently reports precision, recall, F1 and ROC/AUC, at image and
sequence level. Those are the right metrics for comparing detectors in the
abstract. They are the wrong metrics for deciding whether a model can be
deployed, for two reasons.

### F1 weights a background false positive like a missed fire

The two have neither the same cost nor the same frequency. A missed plume can
cost a forest; a false positive costs an operator one check. And a benchmark
roughly balances background and smoke, while a camera in the field produces
**thousands of background frames per fire**.

The practical consequence is a number that is missing from the current report:

```
frames per camera per day at 30 s cadence : 2880

  FPR 0.050  ->    144 false alerts / camera / day
  FPR 0.171  ->    492 false alerts / camera / day
  FPR 0.470  ->   1354 false alerts / camera / day

  budget of 1 FP/day  ->  max FPR 0.000347
```

An FPR that looks unremarkable on a benchmark is unmanageable once multiplied
by real frame counts. Several models in the repository's own history sit in
that range — a high-recall model at FPR 0.47, and an augmentation experiment
that moved FPR from 0.065 to 0.171. Reported as FPR they look like tuning
choices; reported as FP/camera/day they are go/no-go decisions.

### Aggregate recall hides where the system actually fails

Global recall is dominated by the easy cases — large, close plumes — which are
also the least urgent. A model can improve its headline number while losing
exactly the capability the system exists for.

Concrete example, computed on synthetic sequences with the proposed module:

```
baseline  : global recall 0.60 | worst stratum 0.60
candidate : global recall 0.60 | worst stratum 0.20

2 blocking regressions:
  - [stratum_recall] plume_size_px [0, 20) px  : recall down 0.400
  - [stratum_recall] distance_m [7000, 15000) m : recall down 0.400
```

Identical aggregate recall. Small distant plumes — early detections, the whole
point — down from 0.60 to 0.20. No aggregate ranking catches this.

This matches the repository's own observation that two more recent production
models scored below the v6 baseline on the shared benchmark.

## Proposed change

An additive module. Nothing existing is removed — precision/recall/F1/ROC stay
useful for model comparison; these metrics answer a different question.

**Operational metrics**

- `fp_per_camera_per_day` — the number an operations service can actually
  discuss, and the one a duty officer feels;
- `time_to_detect` — median **and p90**, because the tail is what loses a
  forest: a system with an excellent median that takes an hour one time in ten
  is not dependable;
- `stratified_recall` — by plume size, distance and visibility, with the
  stratum count kept visible (a recall of 1.0 over two sequences means
  nothing, and hiding `n` would be worse than showing it).

**Conversion helpers**

- `fpr_to_fp_per_camera_per_day(fpr, frames_per_day)`;
- `fp_budget_to_max_fpr(budget, frames_per_day)` — choosing a threshold from
  what the service accepts, rather than from where the ROC curve looks nice.

**Release gate**

`release_gate(baseline, candidate)` refuses a version that regresses on **any**
stratum, even if its aggregate improves. Tolerances are deliberately
asymmetric: some extra false-positive load is manageable, a recall or delay
regression is not. Strata below `min_stratum_size` are not opposable — blocking
a release on a recall computed over two sequences would destroy trust in the
gate itself.

**Pareto front**

`pareto_front()` and `select_under_budget()`. There is no single best setting:
lowering a threshold buys recall and costs false positives. Exposing the
non-dominated set leaves the trade-off to whoever bears its consequences,
instead of freezing it into one ranking. This directly supports the
recall/FP/delay trade-off already documented in the temporal model work
(FP 114→86, FN 1→2, median trigger 1→3 frames) — that change is a *move along
the front*, not an improvement or a regression, and the current reporting
cannot express that.

## Shape of the patch

1. new pure module `pyro_eval/opmetrics.py` (NumPy only, no new dependency);
2. `evaluation.py` reports the operational block alongside the existing one;
3. optional `--baseline <run>` on `run_evaluation.py` to run the gate in CI;
4. tests.

Reference implementation, fully tested (67 tests):
[`openvigie/opmetrics.py`](https://github.com/doxav/OpenVigie/blob/main/src/openvigie/opmetrics.py)
— Apache-2.0, same licence, free to vendor or adapt.

## Open questions for maintainers

- **What are your real cadence figures?** `frames_per_camera_per_day` defaults
  to a 30 s pose cadence; the FP/day conversion is only as good as that input,
  and it likely differs per deployment.
- **Which strata matter most to you?** I implemented size and distance because
  they are derivable from the annotations; visibility is supported but needs a
  field. If your annotations carry something better, the binning is
  parameterised.
- **Should the gate block CI, or only report?** I would start with report-only
  for a season, collect what it would have blocked, then decide — blocking on
  thresholds nobody has calibrated yet would be premature.
- **Do you already have a preferred TTD definition?** Mine is
  ignition → first alert, with undetected fires counted separately rather than
  imputed, since imputing would distort the median. If your annotations define
  ignition differently, this needs aligning before any number is comparable.

## Honest limitation

The thresholds in `GateConfig` are engineering judgement, not measurements.
They should be calibrated on your annotated sequences before being used to
block anything. And the whole module has been validated on synthetic outcomes
only — I have no access to your evaluation data. What I can vouch for is that
the metrics compute what they claim; whether the defaults are right for your
deployments is exactly what I would want your input on.
