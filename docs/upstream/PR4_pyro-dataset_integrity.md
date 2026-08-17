PR #4 — pyro-dataset
Title: test(integrity): fail fast on cross-class ID collisions (wildfire ∩ fp)

## Problem

`tests/test_data_leakage.py` covers split-level leakage thoroughly: the same
image or sequence appearing in two of train/val/test. But the tests are
parametrised **over** categories:

```python
SEQ_CATEGORIES = ["wildfire", "fp"]

@pytest.mark.parametrize("category", SEQ_CATEGORIES)
@pytest.mark.parametrize("dir_a,dir_b,name_a,name_b", SEQ_SPLIT_PAIRS)
def test_sequential_no_sequence_leakage(...):
    cat_a = dir_a / category
    cat_b = dir_b / category      # same category on both sides
```

Each class is checked separately against the splits. `wildfire` and `fp` are
never compared **to each other**.

That leaves a distinct failure open: the same sequence identifier present in
both classes. Nothing fails — each class is internally consistent, and no split
contains the sequence twice — but a real fire ends up trained as an example of
what must *not* be detected, degrading exactly the capability the dataset
exists to build.

The two checks look similar and do not overlap. A dataset can have zero split
leakage and still classify a sequence both ways.

## Proposed change

One additional test, and a reusable helper.

```python
def test_no_cross_class_collision():
    """A sequence cannot be both an example and a counter-example."""
    wildfire_ids = _all_sequence_ids("wildfire")
    fp_ids = _all_sequence_ids("fp")
    collisions = wildfire_ids & fp_ids
    assert not collisions, (
        f"{len(collisions)} sequence(s) present in both wildfire and fp: "
        f"{sorted(collisions)[:5]} — these would be trained as what must not be detected."
    )
```

The helper version in the reference implementation generalises to any number of
mutually exclusive classes and produces an actionable message that states the
**consequence**, not just the fact — a log line saying "duplicate id" gets
ignored; one saying "this fire will be trained as a false positive" does not.

The check is deliberately split-independent: a sequence that is a fire in train
and a false positive in test is just as corrupted as a collision inside one
split.

## Also included, if useful

Two smaller pieces from the same module, both optional:

- **`SplitLedger`** — freezes the id→split assignment and reports drift between
  two builds, distinguishing *added*, *removed* and **moved**. A moved sequence
  is the serious case: train→test invalidates comparison in both directions.
  This partly overlaps with the lockfile mechanism you have already introduced
  for the frozen test selection, so it may be redundant — happy to drop it.
- **`compare_builds` / `manifest_hash`** — build-twice-and-diff, to catch
  non-determinism (unsorted directory traversal, unpinned seed, environment
  dependence). Manifest keys are normalised so the hash does not itself depend
  on filesystem ordering.

If only the collision test is wanted, it stands alone in about fifteen lines.

## Shape of the patch

1. `tests/test_data_leakage.py`: one new test, no change to existing ones;
2. optionally `src/pyro_dataset/integrity.py` for the reusable helpers;
3. optionally wire `assert_no_class_collision` into the build stage so it fails
   the pipeline rather than a later test run.

Reference implementation, fully tested:
[`openvigie/dataintegrity.py`](https://github.com/doxav/OpenVigie/blob/main/src/openvigie/dataintegrity.py)
— Apache-2.0, same licence.

## Open questions for maintainers

- **Where does the canonical identifier live?** I assumed the sequence folder
  name. If the registry carries a more stable id, the check should use that
  instead — comparing folder names would miss a collision after a rename.
- **Is a cross-class collision ever legitimate?** I assumed never, but a
  sequence containing both a real fire and a later false positive episode might
  be a real case in your data — in which case the right fix is episode
  segmentation, not a collision test, and this PR is the wrong answer.
- **Should this fail the DVC stage or only the test suite?** Failing the build
  is stricter and catches it earlier; failing a test is less disruptive to
  adopt.

## Honest limitation

I could not run this against your actual data — I do not have DVC remote
access. The check is validated on synthetic registries only. Before merging it
would be worth running it once over the current dataset: if it fires, that is
itself the finding, and the threshold question above ("is a collision ever
legitimate?") becomes the first thing to settle.

## Note on scope

The audit that motivated this work also flagged test-set instability between
builds. Cloning the repository shows you have since addressed that with the
lockfile mechanism (`the lockfile IS the test FP selection`), so that part is
deliberately **not** proposed here. Only the cross-class collision, which
appears still open, is.
