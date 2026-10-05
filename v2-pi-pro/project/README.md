# GOSIM v2-pi-pro - 2026-10-06

Based on the original v2 at team commit
`1589f104cf2bda6dc5bd2df7d9c008caed32ce03`.

Python owns the JSONL-v4 protocol, geometry, progress, scoring, numerical search,
CPU pacing, action validation and final submission. Pi agent-core/ai 1.0.3
reviews a diverse frontier of already-computed plans at the current decision.
One bounded tool turn submits a candidate ID, decision token and enumerated
tradeoff. It cannot invent coordinates, assignments or executable code.

Candidates expose science gain, REQUIRED completion and final opportunity,
request reward/deadline, validity risk, uniformity, program, pointing and duration.
Replacements need a numerical reason and an estimated rate of at least 85% of
the original v2 plan; opportunity/uniformity choices need 98%. These thresholds
limit estimated regret, not actual score loss. Unknown IDs, stale state, illegal
geometry, missing keys, failed tools or timeouts return the original v2 choice.

The original v2 ranking remains the baseline. Extra frontier representatives
come from plans already evaluated during its search. No hidden truth is read.
Current directional notices are interpreted synchronously as a second Pi role.
Global asynchronous priority/risk advice is removed from the default path.

At most 24 plan reviews and 8 notice interpretations are released gradually
across the season; each question allows one model attempt and one tool call,
with an 8-second ceiling and 300-second wall reserve. Fault confirmation retains
the original multi-night evidence gate. Model-call limits remain 64 per card.

| Runtime Variable | Meaning |
| --- | --- |
| `AGENT_MODEL_BACKEND=pi-pro` | Default Pi candidate review |
| `AGENT_MODEL_BACKEND=v2` | Original HTTP client/advisor for comparison |
| `AGENT_MODEL_BACKEND=disabled` | Original numerical v2 behavior without models |
| `OPENAI_API_KEY` or `KIMI_API_KEY` | Runtime-only key |
| `OPENAI_BASE_URL`, `OPENAI_MODEL` | Provider and model; default Kimi Coding `k3` |
| `PI_NODE_EXECUTABLE` | Optional local Node >=22.19 path |
| `AGENT_TRACE_PATH` | Optional JSONL diagnostics outside submission ZIP |

Build downloads checksum-pinned Linux x86_64 Node 22.20.0 in the project
directory, respecting `HTTPS_PROXY`. Pi dependencies and licenses are bundled;
platform build does not run npm. Rebuild locally with `npm ci --ignore-scripts`
then `node build_bundle.mjs` in `pi_adapter`.

Package: `python3 pack_agent.py --out ../v2-pi-pro-20261006.zip`.
Use the ZIP as a complete project. Credentials, tests, logs, data, node_modules
and runtime binaries are excluded. Competition stdout contains only protocol.

Synthetic provider tests prove tool/action integration, not real-model
reliability or score improvement. Compare with original v2 under identical
provider settings and record score components, termination, accepted plan
changes and model latency.
