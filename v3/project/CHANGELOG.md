# Science-repair candidate 2026-10-05

One candidate derived from merged team PR #2 and the concentrated release, repaired
after inspection of the user's v1 public run feedback. Original inputs are
preserved separately. Formal improvement remains to be measured on the platform.

- Recall fields by achievable single-exposure science and spatial density.
- Prefer greater single-exposure utility within a 3% rate tolerance.
- Aggregate sky, direction and program-band feedback by exposure.
- Estimate instrument efficiency from feedback instead of a fixed 0.95 divisor.
- Reset stale efficiency/fault evidence after repair or resync.
- Expire zero-score direction masks and clear them on bulletin transitions.
- Preserve reachable request anchors through fiber and pointing truncation.
- Retain bounded request lookahead and broader representative fibers in fast mode.
- Search second-step request targets after subtracting predicted first-step hits;
  restore public request state before returning and wait for real feedback.
- Include every request's threshold in exposure-duration candidates and derive
  missing remaining counts from the published completion requirement.

- Jointly search assignments, duration thresholds and DARK/BRIGHT/BACKUP.
- Count request reward once per satisfied request; require full exposure inside its
  issue/deadline window; reactivate previously saturated request targets.
- Recover surviving science maxima and conservative completion bounds after resync.
- Bound REQUIRED calendar construction, cache uniformity by RA band and avoid
  repeated spatial-index key construction.
- Apply event-driven notice and feedback model outputs off the decision path.
- Keep fault reporting evidence checks and a reserved confirmation-call budget.
- Ship an explicit source/documentation allowlist; no task data or credentials.

This changes the policy; it is not a lossless speed-only patch. Online gains and
SOTA are not claimed. Diagnostic artifacts and tests live outside the upload ZIP.
