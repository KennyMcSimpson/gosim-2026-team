# Candidate 2026-10-05

One combined candidate, derived from merged team PR #2. The original snapshot is
preserved separately. No upload, final-version selection, push or merge was done.

- Jointly search assignments, duration thresholds and DARK/BRIGHT/BACKUP.
- Count request reward once per satisfied request; require full exposure inside its
  issue/deadline window; reactivate previously saturated request targets.
- Recover surviving science maxima and conservative completion bounds after resync.
- Bound REQUIRED calendar construction, cache uniformity by RA band and avoid
  repeated spatial-index key construction.
- Apply event-driven notice and feedback model outputs off the decision path.
- Keep fault reporting evidence checks and a reserved confirmation-call budget.
- Add a capped RA-band deficit hint for ordinary science candidates; REQUIRED and
  active-request candidates retain independent hard-task protection.
- Record submitted/succeeded/parsed/applied/fresh/changed advisor telemetry and
  move fault-report confirmation to the background advisor with rule fallback.
- Ship an explicit source/documentation allowlist; no task data or credentials.

This changes the policy; it is not a lossless speed-only patch. Online gains and
SOTA are not claimed. Diagnostic artifacts and tests live outside the upload ZIP.
