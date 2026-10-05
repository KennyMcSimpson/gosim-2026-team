# GOSIM 2026 Team

This public repository keeps the official GOSIM Agentic Observer examples,
the team's Python baseline, and candidate strategies for online trials.

## 最新候选：集中升级版（2026-10-05）

基于朋友已合并的 PR #2，一版集中更新联合光纤/曝光搜索、请求完成奖励、
REQUIRED 截止风险、作废回滚、CPU 预算和异步模型反馈。

- [下载可上传 ZIP](candidates/concentrated-20261005/gosim-concentrated-20261005.zip)
- [中文更新说明、使用方式和验证范围](candidates/concentrated-20261005/README.md)
- [对应源码](candidates/concentrated-20261005/project/)

13 项合成逻辑检查及 9/16/25/100 光纤的解压协议检查通过；尚未运行正式评分，
线上提升待验证。上传时沿用原平台模型配置。

## Current archive

- [Official examples and training files](training/official-examples/gosim-observer-examples/)
- [Original archive SHA-256](training/official-examples/gosim-observer-examples.zip.sha256)
- [Source and provenance](training/official-examples/SOURCE.md)

The extracted directory contains the organizer-provided Python, TypeScript, and Rust
examples, the public L1-L4 local cards and truth files, documentation, and the
official local runner. Use its `LICENSE.md` for attribution and
license terms.

## Python baseline

[Team Python baseline source](baselines/python-agent-baseline-unmodified/)
includes the strategy changes merged in [PR #2](https://github.com/KennyMcSimpson/gosim-2026-team/pull/2).
Its directory retains the historical `unmodified` name. See
[baseline provenance](baselines/README.md) for the earlier official packaging record.

## Archive helper

[scripts/fetch-official-examples.sh](scripts/fetch-official-examples.sh) is the
earlier pinned-ZIP extraction helper. PR #2 replaced the committed ZIP with
extracted files, so use the directory linked above directly. The helper downloads
the pinned archive when it is missing; `REFETCH=1` also forces a temporary download.

Do not commit API keys, `.env` files, passwords, private submissions, run logs,
or generated experiment outputs here.
