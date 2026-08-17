PR #1 — pyro-api
Title: fix(sequences): replace first-match-wins association with a scored best-match

## Problem

A detection is currently attached to a sequence by walking candidates in
recency order and stopping at the first one whose bbox overlaps, using a
boolean test:

```python
candidate_sequences = await sequences.fetch_all(
    ..., order_by="last_seen_at", order_desc=True,
)
for seq in candidate_sequences:
    last_bbox = await _get_last_bbox_for_sequence(detections, seq.id)
    if last_bbox is not None and _bboxes_overlap(last_bbox, det_bbox, settings.SEQUENCE_BBOX_TOLERANCE):
        matched_sequence = seq
        break
```

Three properties compound here:

1. **candidates are ordered by recency**, which has nothing to do with spatial
   quality;
2. **the test is boolean** — `_bboxes_overlap` accepts even a negative gap up
   to `SEQUENCE_BBOX_TOLERANCE`, so a one-pixel touch counts as much as a
   perfect overlap;
3. **a sequence's spatial identity is its last bbox**
   (`_get_last_bbox_for_sequence`), so a single outlier box moves it durably.

With `SEQUENCE_RELAXATION_SECONDS = 7200`, a chance intersection can also link
episodes two hours apart.

**Observed consequence**: two fires on the same pose, one abnormally large box
on the first, and that box absorbs the other fire's detections. The theft is
self-reinforcing — the stealing sequence keeps growing, so it overlaps even
more — and the operator ends up seeing one fire's imagery at another fire's
triangulated position.

## Proposed change

Keep the shape of the existing code (one detection at a time, no schema or DB
change). Replace the boolean early-exit with a scored best-match:

| Decision | Before | After |
|---|---|---|
| Order | recency | all candidates evaluated |
| Comparison | boolean overlap | continuous quality (IoU, centre distance, size ratio, time gap) |
| Choice | first that passes | best score |
| Tie | first seen | **explicit refusal** → new sequence |
| Sequence identity | last bbox | **median** of the last N bboxes |
| Giant box | accepted | rejected above an area-ratio guard |
| Time gap | up to 2 h | episode split |

The guiding rule when uncertain is **open a new sequence**. A duplicate costs
the operator one check; a wrong association shows them a fire at the wrong
place.

## Evidence

Replayed side by side on identical data
([OpenVigie test](https://github.com/doxav/OpenVigie/blob/main/tests/test_upstream_contributions.py)):

```
detection clearly belonging to A: (0.11, 0.11, 0.18, 0.18)
  current logic  -> B        <- theft
  proposed logic -> A | best score 0.778
    A: quality=0.778 iou=0.581 rejected=None
    B: quality=0.000 iou=0.000 rejected=centre moved by 0.60: too fast
```

The median reference also makes the theft recoverable: in this scenario B's
`last_box` covers >90 % of the frame while its median reference box stays
under 5 %.

## Shape of the patch

1. new pure module `app/services/association.py` (no DB, no I/O — directly
   unit-testable);
2. in `detections.py`, replace the `for … break` loop with "score all
   candidates, keep the best, refuse on tie";
3. expose thresholds in `config.py`, defaulting to today's behaviour so
   nothing changes until they are enabled;
4. add the replay test.

Reference implementation, fully tested and dependency-free (NumPy only):
[`openvigie/association.py`](https://github.com/doxav/OpenVigie/blob/main/src/openvigie/association.py)
— Apache-2.0, same licence as pyro-api, so it can be vendored or adapted
freely.

## Open questions for maintainers

- **Which thresholds do you want configurable vs hard-coded?** I defaulted to
  `max_gap_s=900`, `max_area_ratio=8.0`, `ambiguity_margin=0.05`, but these
  are engineering judgement, not measurements — they should be calibrated on
  your annotated sequences before becoming defaults.
- **Is the median reference box acceptable given your query patterns?** It
  needs the last N bboxes rather than just the latest, which changes
  `_get_last_bbox_for_sequence` into a small-window fetch.
- **Should tie refusal create a new sequence, or flag for human arbitration?**
  I chose the former as the safer default, but you have the operational
  context to judge.

## Honest limitation

This was validated on a **reconstructed** scenario, not replayed against real
production sequences — I don't have access to that data. Before merging I'd
want the replay run against the actual incident, which would also calibrate
the thresholds. Happy to adapt the implementation to whatever that shows.
