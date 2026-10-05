# GOSIM concentrated candidate - 2026-10-05

Based on the strategy subtree from merged team PR #2, main
`8792dd2fc15fdaacf131a47c17ddb32594f3e63a`.

Upload this ZIP as a complete project. The archive root contains
`observer.project.json`; the platform runs `python3 -u agent.py` in
`python:3.12-slim`. Python standard library only; no installation or GPU needed.

Set the same model configuration used for the friend baseline on the platform:
`OPENAI_API_KEY` (or `KIMI_API_KEY`), `OPENAI_BASE_URL`, `OPENAI_MODEL`.
Defaults remain the friend's Kimi Coding endpoint and `k3`. A runtime API key
is required. No keys, public task inputs, hidden truth, or test fixtures are in
the package. Do not add credentials to files.

Changes: separate best-science and conservative completion ledgers with rollback;
joint fiber/program/exposure selection; request-window-aware scheduling with a
bounded two-step estimate; lazy public REQUIRED opportunity calendar; directional
quality estimates and explicit invalidation risk; cached uniformity and spatial
indices; reversible CPU pacing; asynchronous notice interpretation and feedback
adaptation model roles. Geometry supports any valid square grid, including
9, 16, 25 and 100 fibers.

Frozen initial heuristics: 20/50/80 empirical quality quantiles, 0.2/0.6/0.2 scenario
weights, 0.65 prior for unknown future sky, up to 28 planning questions spread
through the season, 64 total HTTP attempts, 8s attempt timeout, 18s question budget,
300s wall reserve. Calibration and quality inference are approximate. Rounded
score-only feedback cannot always identify completion factor exactly; its ledger
stores a conservative lower bound. The two-step estimate executes only its first
step and replans after actual feedback.

Validation is protocol/geometry and synthetic logic checking, with localhost
model mocks and a small public-input CPU diagnostic. No formal A-D or practice
scoring was run. Real model availability, hosted CPU consumption, online score
improvement and leaderboard position remain unverified.

Repackage with `python3 pack_agent.py --out ../gosim-concentrated-20261005.zip`.
The packager includes only source, the manifest and these documentation files.
