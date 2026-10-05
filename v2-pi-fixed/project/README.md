# v2-pi-fixed

Based on the original v2 at team commit `1589f104cf2bda6dc5bd2df7d9c008caed32ce03`.
Default `AGENT_MODEL_BACKEND=pi-fixed` restores v2's asynchronous notice and feedback
advisor roles, using the Pi 1.0.3 single-turn tool bridge. Python owns the original
numerical search, geometry, feedback, clock and action submission. Candidate
replacement and its extra search collection are off by default.

Feedback freshness uses request identity, season quarter, invalidation state,
hit-rate band and a bounded quality drift. Each advisor role gets its own share
of the 28-question seasonal allowance. Applied settings are recorded in stderr.
Model fault confirmation can defer once; renewed multi-night evidence after
cooldown proceeds through the original report gate. This changes report behavior
and its real-model false-report rate is unverified. The `v2` comparison backend
retains the original advisor freshness, quota and fault-veto semantics.

`v2` selects the original HTTP provider path; `disabled` makes no model calls.
`pi-review` opts into experimental candidate review. Review rejects reductions
in estimated science/utility rates, REQUIRED counts, request reward or validity.
Its candidates and tradeoffs are logged to stderr. These estimates do not prove
final-score improvement. The original two scored Pi packages remain archived.

Set `OPENAI_BASE_URL`, `OPENAI_MODEL` and a runtime `OPENAI_API_KEY` or
`KIMI_API_KEY` on the platform. No credential belongs in this ZIP.
Build pins Node 22.20.0 for Linux x86_64 and checks the bundled worker hash.
Package with `python3 pack_agent.py`; upload the resulting ZIP as a complete project.

This is a regression repair candidate. Engineering checks and mock-provider
integration are not online scoring; real-model and hidden-score gains are unverified.
