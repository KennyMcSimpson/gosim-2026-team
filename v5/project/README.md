# GOSIM v5 official Python agent - 2026-10-06

Based on the organizer's Python JSONL-v4 architecture, the v4-official
baseline, and the isolated recovery fixes. This is an improved team policy,
not an unchanged organizer baseline.

The platform runs `python3 -u agent.py` in `python:3.12-slim`. `build` is empty.
Only the Python standard library is required. No Pi, Node, worker subprocess,
external candidate selector, runtime download, or dependency installation is
used. `AGENT_MODEL_BACKEND` and `PI_ENABLED` have no effect on this version.

Keep the team's `OPENAI_BASE_URL`, `OPENAI_MODEL`, and runtime
`OPENAI_API_KEY` (or `KIMI_API_KEY`) settings. The original defaults are the
Kimi Coding endpoint and `k3`; the platform-saved service should override them
when appropriate. A runtime key is required and must never be stored in a file.

Two event-driven model roles run through Python `urllib` and an asynchronous
thread: public notice interpretation and feedback-based priority/risk advice.
Python retains numerical search, state, geometry, request accounting and final
action validation. Model errors retain deterministic planning. Model replies
must match the current public context before applying.

## Changes from v4-official

- Cache static sky geometry and spatial groups.
- Rank incomplete REQUIRED targets by their public feasible-night deadline and
  retain a bounded REQUIRED recall lane.
- Sort active requests by deadline/reward, preserve up to eight request anchors,
  and include every feasible target's shortest threshold-satisfying exposure.
- After `state_resync`, rebuild a bounded recovery queue for invalidated
  REQUIRED/request targets and restore those targets before candidate pruning.
- Charge decision cost in observing seconds: remaining observing seconds times
  average decision CPU divided by remaining real CPU budget.
- Recall saturated science targets when a fresh request requires them.
- Retain zero-science partial request progress under a search cutoff; examine
  at most eight virtual request steps and execute only the first step.
- Update score/completion ledgers only from actual feedback, including rollback.
- Record model submission, result, application, fallback and skip decisions in
  the optional JSONL trace. A valid local notice interpretation changes
  `state.extra_avoid`; `llm_applied` identifies `Planner._direction_factor` as
  its consumer.

These are policy changes, not a lossless speed patch. Previous single-exposure
science, exposure-level feedback aggregation, efficiency inference, REQUIRED
calendar and uniformity handling are retained.

## Validation and limits

Regression checks cover REQUIRED deadline recall, request target recall, short
threshold exposures, resync recovery, LLM success/fallback evidence and the
official JSONL-v4 entry point. Inputs, tests and receipts stay outside the
upload ZIP; no credentials or task/truth data are included.

No new official evaluation or real-provider call has established a score gain.
Full-season hosted CPU completion remains unverified; short REQUIRED exposures
can still create many decisions. Local timing and predicted utility are proxies.
See the parent implementation and validation records for exact evidence.

Repackage: `python3 pack_agent.py --out ../v5.zip`.
