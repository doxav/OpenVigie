PR #2 — pyro-engine
Title: feat(health): detect a stuck PTZ head and stale frames via per-pose fingerprints

## Problem

Two failure modes make a camera **look healthy while it has stopped watching
anything**. A blind spot nobody can see is worse than an outright outage,
because nobody goes and fixes it.

### A stuck PTZ head

Presets are commanded, the head does not move — seized mechanics, unpowered
motor, silently refused command — and the camera keeps returning perfectly
valid images. The stream responds, frames are sharp, inference runs. Nothing
signals the fault, but every pose shows the same scene.

Downstream: azimuths attached to detections are wrong (`resolve_cone` uses
`pose.azimuth`, which no longer matches what the camera sees), alerts duplicate
pose after pose, and coverage silently collapses to one direction.

### An offline camera reported alive

`SystemController._safe_get_latest_image` returns whatever the camera API
hands back, with no freshness check:

```python
def _safe_get_latest_image(self, ip: str, pose: int) -> Optional[Image.Image]:
    try:
        return self.camera_api_client.get_latest_image(ip, pose)
    ...
```

If the source serves the last successful frame from a cache, a disconnected
camera can appear active indefinitely.

## Proposed change

A small, purely observational module — no change to detection behaviour.

**Per-pose perceptual fingerprint (dHash, NumPy only).** If two poses meant to
look in different directions produce near-identical images, the head did not
move.

dHash is chosen for a specific trade-off: it compares each pixel to its
neighbour, so **an affine change in brightness leaves the fingerprint exactly
unchanged** (asserted in tests) — a passing cloud does not raise a false
hardware alarm — while remaining sensitive to landscape structure. Cost is a
few hundred operations per frame.

**Capture timestamp + TTL.** Stamp the *capture*, not the read, then apply an
explicit validity window. A frame older than its TTL is reported stale
regardless of whether the request succeeded.

```python
reg = PoseFingerprintRegistry("cam-1", ttl_s=900.0)
reg.record("P0", frame, captured_at=stamp)   # after each capture
report = reg.report()                         # at each heartbeat
report.status    # ok | stuck | stale | degraded
report.message   # actionable explanation
```

`drift_since()` additionally measures gradual framing drift — wind,
maintenance, a mount that shifted — *before* it becomes an outright collision.

## Shape of the patch

1. new pure module `pyroengine/posehealth.py` (no new dependency — NumPy is
   already present);
2. one `record()` call after each successful capture in
   `SystemController.inference_loop`;
3. one `report()` attached to the heartbeat payload;
4. collision threshold and TTL in configuration.

Reference implementation, fully tested:
[`openvigie/posehealth.py`](https://github.com/doxav/OpenVigie/blob/main/src/openvigie/posehealth.py)
— Apache-2.0, same licence as pyro-engine.

## Parameter that needs field calibration

`collision_threshold` (0.92 by default) depends on the landscape: a very
uniform horizon — sea, plain, fog — naturally brings two neighbouring
directions' fingerprints closer and needs a higher threshold. That is why it is
exposed rather than hard-coded, and it is the first thing to measure on a real
site.

If useful, the same registry can report the fingerprint distance between
consecutive visits to the *same* pose, which gives a cheap continuous drift
signal for the PTZ repeatability issue discussed in
[pyro-engine #397](https://github.com/pyronear/pyro-engine/issues/397).

## Open questions for maintainers

- **Where do you want the health loop to live?** The audit notes that during a
  live stream, inference and some heartbeats are suspended, which makes real
  health ambiguous. A fingerprint check independent of the inference loop would
  address that, but it touches your streaming state machine — your call.
- **Does the camera API expose a true capture timestamp?** If
  `get_latest_image` can return one, the TTL check becomes exact; otherwise it
  only catches the case where the engine itself stops polling.
- **Should a `stuck` verdict stop patrol / raise an alert, or only annotate the
  heartbeat?** I kept it purely observational so the PR cannot degrade
  detection, but you may want it to act.

## Honest limitation

Validated on synthetic landscapes only. The threshold is a starting point, not
a measurement — a real deployment with a genuinely stuck head, or a foggy
uniform horizon, is what would confirm or move it. I'd rather ship it as
observational-only first and let the data decide.
