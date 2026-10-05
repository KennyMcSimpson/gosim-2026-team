# GOSIM v2 + Pi candidate - 2026-10-05

An isolated adaptation of `v2(jmk)/concentrated-20261005/project` from team
repository commit `1589f104cf2bda6dc5bd2df7d9c008caed32ce03`. The user reports
v2 as their best version; no new score comparison has been run.

The Python JSONL entry point, v2 numerical search, state, geometry, scoring,
calendar, CPU pacing and action validation remain in charge. A persistent Node
worker uses actual Pi agent-core/ai 1.0.3 for bounded tool-based advice on public
notices, feedback/requests and fault confirmation. This first adaptation covers
these existing model roles; it does not generate or reload strategy code.

The platform uses `python:3.12-slim` and `python3 -u agent.py`. Its build step
downloads checksum-pinned Node 22.20.0 to the writable project directory.
Pi dependencies are prebundled and checksum-verified; no npm install is needed
during platform preparation. No root filesystem installation is needed.
Linux container build and real-provider availability still need platform checks.

Runtime configuration:

| Variable | Meaning |
| --- | --- |
| `AGENT_MODEL_BACKEND=pi` | Default: Pi tool loop |
| `AGENT_MODEL_BACKEND=v2` | Original v2 HTTP client for comparison; key required |
| `AGENT_MODEL_BACKEND=disabled` | Numerical v2 planning without model advice |
| `OPENAI_API_KEY` or `KIMI_API_KEY` | Runtime-only credential |
| `OPENAI_BASE_URL` | OpenAI-compatible base; default Kimi Coding endpoint |
| `OPENAI_MODEL` | Model identifier; default `k3` |
| `PI_NODE_EXECUTABLE` | Optional local Node path for development |
| `AGENT_TRACE_PATH` | Optional runtime trace, outside the submitted ZIP |

Pi timeouts, missing keys, missing Node or failed model/tool calls return to the
v2 rule path. Model output cannot bypass Python freshness and action validation.
Multi-step Pi calls share the original run-wide attempt allowance and wall reserve.
Logs never use competition stdout. EOF, reinitialization and finish close the worker.

For local Windows use, install Node >=22.19 and run the Python entry point.
To rebuild after editing the worker, run `npm ci --include=dev --ignore-scripts`
and `node build_bundle.mjs` inside `pi_adapter`. Do not run the Linux build
script on Windows.

Package with `python3 pack_agent.py --out ../gosim-v2-pi-20261005.zip`.
The bundled Pi code and third-party licenses are included. Dependency directories,
Node binaries, tests, task data, logs and credentials are excluded.

This is an engineering candidate. Mock-provider and protocol tests cannot prove
real-model support, full-season budget compliance or better competition scores.
