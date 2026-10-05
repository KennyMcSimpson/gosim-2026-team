# GOSIM science-repair candidate - 2026-10-05

Based on merged team PR #2 (main `8792dd2fc15fdaacf131a47c17ddb32594f3e63a`)
and the concentrated candidate, revised using the user's v1 public run feedback.
One generic policy; no card IDs, fixed months, or target IDs in the strategy.

Upload this ZIP as a complete project. The archive root contains
`observer.project.json`; the platform runs `python3 -u agent.py` in
`python:3.12-slim`. Python standard library only; no installation or GPU needed.

Set the same model configuration used for the friend baseline on the platform:
`OPENAI_API_KEY` (or `KIMI_API_KEY`), `OPENAI_BASE_URL`, `OPENAI_MODEL`.
Defaults remain the friend's Kimi Coding endpoint and `k3`. A runtime API key
is required. No keys, public task inputs, hidden truth, or test fixtures are in
the package. Do not add credentials to files.

Science is the best valid single-exposure score per target. Prefer a more complete
exposure within a 3% utility-rate tolerance; recall fields using achievable science
and spatial density. Aggregate feedback per exposure before learning sky/fault
evidence. Separate instrument-scaled exposure quality from program-band quality,
estimate efficiency from feedback, reset stale evidence after repair/resync, and
expire empirical zero-score direction masks.

Recall a reachable target for each active request before anchor truncation and
preserve it through fiber and pointing beams. Retain window/deadline checks and
bounded two-step request planning in fast mode. Keep the separate science and
completion ledgers, rollback, REQUIRED calendar and uniformity accounting.
Second-step recall temporarily subtracts predicted first-step request hits;
only actual feedback updates the real exposure and completion ledgers.
Geometry supports valid square grids, including 9, 16, 25 and 100 fibers.

Frozen initial heuristics: 20/50/80 empirical quality quantiles, 0.2/0.6/0.2 scenario
weights, 0.65 prior for unknown future sky, up to 28 planning questions spread
through the season, 64 total HTTP attempts, 8s attempt timeout, 18s question budget,
300s wall reserve. Calibration and quality inference are approximate. Rounded
score-only feedback cannot always identify completion factor exactly; its ledger
stores a conservative lower bound. The two-step estimate executes only its first
step and replans after actual feedback.

Validation uses synthetic logic checks, public geometry/protocol, localhost model
mocks, a small public-input CPU diagnostic and sampled replay of the user's v1
public feedback. Tests and replay data stay outside the ZIP. Replay does not
produce a new formal score or read truth. Real model availability, hosted CPU
consumption, online score improvement and leaderboard position remain unverified.

Repackage with `python3 pack_agent.py --out ../gosim-science-repair-20261005.zip`.
The packager includes only source, the manifest and these documentation files.
