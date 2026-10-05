# v2 Pi adaptation - 2026-10-05

- Preserve the exact v2 numerical strategy and protocol validation modules.
- Replace the default model client with a persistent Pi worker and bounded tools.
- Keep the original model client selectable for comparison.
- Allow deterministic startup without a model key in Pi/disabled modes.
- Close model workers on EOF, finish and reinitialization.
- Prebundle locked Pi dependencies and install only pinned Node at preparation.
- Package explicit runtime source files; exclude installed dependencies and data.

Model/tool behavior changes. Numerical policy preservation only applies when
advice is disabled or unavailable. No competition score improvement is claimed.
