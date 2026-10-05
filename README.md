# GOSIM 2026 Team Training Archive

This public repository keeps the current official GOSIM Agentic Observer example
archive and an unmodified Python baseline for team use.

## Current archive

- [Official example and training archive](training/official-examples/gosim-observer-examples.zip)
- [SHA-256](training/official-examples/gosim-observer-examples.zip.sha256)
- [Source and provenance](training/official-examples/SOURCE.md)

The archive contains the organizer-provided Python, TypeScript, and Rust
examples, the public L1-L4 local cards and truth files, documentation, and the
official local runner. Use the archive's own `LICENSE.md` for attribution and
license terms.

## Python baseline

[Download the unmodified Python project ZIP](baselines/python-agent-baseline-unmodified.zip).
It is repackaged from the current official archive with `observer.project.json`
at the ZIP root. See [baseline provenance](baselines/README.md) for its source,
checksum, and API configuration requirements.

## Extract the examples

Run with Bash (Git Bash on Windows), `unzip`, and `sha256sum` or `shasum`:

```bash
bash scripts/fetch-official-examples.sh
```

The helper verifies the pinned archive and extracts it to
`.cache/gosim-observer-examples`. Pass a different destination as its first
argument. Existing destinations are never replaced. `REFETCH=1` downloads a
temporary copy from the official release and requires the same pinned checksum;
it does not update the committed archive when the upstream asset changes.

Do not commit API keys, `.env` files, passwords, private submissions, run logs,
or generated experiment outputs here.
