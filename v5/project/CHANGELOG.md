# v5 official Python candidate 2026-10-06

## v5 changes

- Prioritize incomplete REQUIRED targets by their last public feasible night.
- Recall up to eight request anchors in deadline/reward order, including each
  target's shortest threshold-satisfying exposure.
- Rebuild a bounded recovery queue after Hard-mode `state_resync`; restore
  invalidated REQUIRED and incomplete request targets before search pruning.
- Make the LLM evidence chain explicit: `llm_submitted`, `llm_result`,
  `llm_applied`, `llm_fallback`, and `llm_skipped`. A successful local notice
  interpretation changes `state.extra_avoid` and records its planner consumer.
- Add reproducible synthetic and protocol smoke checks under `../results/`.

No formal score improvement is claimed until a hosted evaluation is run.

Derived from the published v3 after formal run 76c437d3 exhausted the CPU
budget on A/B/C. This is a policy change, with no verified new formal score.

- Cache static trigonometry, spatial tiles and per-anchor unit vectors.
- Use cheap recall estimates, then exact geometry and science evaluation.
- Bound refinement by the remaining real CPU budget; keep legal plans already
  evaluated and protect one science and one urgent request anchor.
- Price one decision in observing seconds as remaining observing time times
  measured decision CPU divided by remaining real CPU budget.
- Keep zero-science partial requests and a bounded virtual sequence of at most
  eight steps. When sequence proof is cut off and science is exhausted, execute
  a legal request-progress step and await real feedback.
- Restore the direct Python HTTP advisor for notice and feedback roles.
- Remove the external candidate-selector interface and its bridge module.
- Use the official Python entry, empty build and standard-library-only runtime;
  no Pi or Node dependency is present.

Public snapshots, synthetic request regressions, protocol/fiber smoke and
source hashes are in the parent experiment directory. Full-season CPU, live
model quality, Linux execution and formal improvement remain unverified.

## Previous science-repair changes

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
