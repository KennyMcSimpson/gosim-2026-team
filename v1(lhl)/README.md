# Python baseline provenance

Current source is [python-agent-baseline-unmodified/](python-agent-baseline-unmodified/).
[PR #2](https://github.com/KennyMcSimpson/gosim-2026-team/pull/2) replaced the earlier
ZIP with an extracted project and changed its strategy. The directory keeps its
historical name; the packaging record below describes the earlier unmodified
official ZIP, which is no longer present at the current main revision.

The 2026-10-05 concentrated upgrade is a separate
[candidate with source, upload ZIP and change notes](../candidates/concentrated-20261005/README.md).

## Historical official ZIP (2026-10-04)

`python-agent-baseline-unmodified.zip` contains all 19 files from the `python/`
directory of the current [official examples archive](../training/official-examples/SOURCE.md).
Only the ZIP layout was changed: `observer.project.json` is at its root, ready
for complete-project upload. The source files are unchanged.

| Field | Value |
| --- | --- |
| Packaged | 2026-10-04 (Beijing time) |
| Source archive SHA-256 | `0b553877d9163a41f7fb170a3a6d8ccbf7327799fa0f51829db06e34a1c15397` |
| Baseline ZIP SHA-256 | `b643f56aa8a6674268556396eac8240f3ea3e73a054108f7f203d08e2cf2d9e1` |
| Baseline ZIP size | 49,489 bytes |
| Manifest | `observer-project-v1`, `jsonl-v4`, `python:3.12-slim` |
| Run command | `python3 -u agent.py` |

The official agent requires `OPENAI_API_KEY` (or `KIMI_API_KEY`) at startup.
Configure that key, `OPENAI_BASE_URL`, `OPENAI_MODEL`, and the provider domain in
the platform's Keys and network settings. Defaults are Kimi Coding Plan and
model `k3`. The ZIP contains only an empty `.env.example`, no credentials.

This keeps PR [#1](https://github.com/KennyMcSimpson/gosim-2026-team/pull/1)'s useful
baseline packaging idea, using the current official source instead of its older
ZIP. Packaging and byte equality were checked; no API-backed evaluation or
competition score is claimed here.

Attribution: [GOSIM 2026 Agentic Observer Hackathon](https://create.gosim.org/survey26/).
Organizer-provided examples use [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/),
as stated in the full official archive's `LICENSE.md`.
