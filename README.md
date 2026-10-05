# GOSIM 2026 Team

This public repository keeps the official GOSIM Agentic Observer examples,
the team's Python baseline, and candidate strategies for online trials.

## 最新候选：v4 官方 Python 架构版（2026-10-06）

本次按 Kenny 的要求回到官方 Python 架构，完全不使用 Pi/Node。
修复 v3 计算预算、决策成本、饱和目标请求召回和部分请求过滤问题。
保留直接 HTTP 的公告解释和反馈建议两个模型角色。

- [下载 v4.zip，完整项目上传](v4-official/v4.zip)
- [使用方式、修复与验证](v4-official/README.md)
- [v3 与 v4-pi 的失败原因和证据边界](FAILURE_ANALYSIS.md)
- [对应源码](v4-official/project/)

最终 ZIP 的回归与四光纤配置协议验证通过；正式增分、整季预算和 SOTA 待实测。
v3 正式回执 A/B/C 提前耗尽 CPU，均分由旧结果 24940.80 降为 17054.15。
历史 v4-pi 保留归档，用户报告 Pi 效果不好，但缺这个具体 ZIP 的正式分项回执。

## v2 的 Pi 适配归档与修复（2026-10-06）

用户指定原 v2 为迁移基线。两版 Pi 的线上结果未达预期，已保留原包并记录失败。
新修复候选恢复 v2 模型角色，默认关闭动作替换，修复建议过期和故障重复否决。
工程验证通过；新版本线上提分仍未验证。

| Pi 版本 | 状态 | 下载与说明 |
| --- | --- | --- |
| v2-pi | 历史原包；平台版本名 v1-pi；均分 24493.83。 | [ZIP](v2-pi/v2-pi.zip) / [说明](v2-pi/README.md) |
| v2-pi-pro | 历史原包；平台版本名 v2-pi-new；均分 23800.89。 | [ZIP](v2-pi-pro/v2-pi-pro.zip) / [说明](v2-pi-pro/README.md) |
| v2-pi-fixed | 回归修复候选；尚无新线上分数。 | [ZIP](v2-pi-fixed/v2-pi-fixed.zip) / [源码与说明](v2-pi-fixed/README.md) |

[失败分项、故障修复差异及未验证边界](PI_FAILURE_ANALYSIS.md)。
上述两次均分是 Pi 之间的对照，不是用户指定原 v2 的实测对照。

## 历史候选：v3 科学收益修正版（2026-10-05）

基于 team PR #2 和 v2 集中版，针对 v1 平台回执修正单次曝光收益、反馈学习、
请求目标召回和两步规划。只提供一个通用候选。

- [下载 v3.zip，作为完整项目上传](v3/v3.zip)
- [中文更新说明、使用方式和验证范围](v3/README.md)
- [v3 对应源码](v3/project/)
- [验证摘要与 SHA-256](v3/VALIDATION.json)

18 项合成逻辑检查、请求专项回归、公开反馈抽样回放及 ZIP 解压后的
9/16/25/100 光纤协议复验通过。正式线上增分、隐藏泛化和 SOTA 待平台实测。
模型配置沿用原设置；可直接使用本目录已验证的 ZIP。

## 版本导航

| 版本 | 内容 | 入口 |
| --- | --- | --- |
| v4-official | 当前推荐，官方 Python 架构，无 Pi；预算与请求修复。 | [说明](v4-official/README.md) / [ZIP](v4-official/v4.zip) / [源码](v4-official/project/) |
| v4-pi | 历史 Pi 候选，原包冻结；此次不再推荐。 | [说明](v4-pi/README.md) / [ZIP](v4-pi/v4-pi.zip) / [源码](v4-pi/project/) |
| v3 | 正式评测退步，保留追溯；A/B/C CPU 耗尽。 | [说明](v3/README.md) / [ZIP](v3/v3.zip) / [失败分析](FAILURE_ANALYSIS.md) |
| v2(jmk) | 前一版集中升级候选。 | [说明](v2(jmk)/concentrated-20261005/README.md) |
| v1(lhl) | team PR #2 合并的朋友基线。 | [来源](v1(lhl)/README.md) / [源码](v1(lhl)/python-agent-baseline-unmodified/) |

## 前一版：集中升级版（2026-10-05）

基于朋友已合并的 PR #2，一版集中更新联合光纤/曝光搜索、请求完成奖励、
REQUIRED 截止风险、作废回滚、CPU 预算和异步模型反馈。

- [下载可上传 ZIP](v2(jmk)/concentrated-20261005/gosim-concentrated-20261005.zip)
- [中文更新说明、使用方式和验证范围](v2(jmk)/concentrated-20261005/README.md)
- [对应源码](v2(jmk)/concentrated-20261005/project/)

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

[Team Python baseline source](v1(lhl)/python-agent-baseline-unmodified/)
includes the strategy changes merged in [PR #2](https://github.com/KennyMcSimpson/gosim-2026-team/pull/2).
Its directory retains the historical `unmodified` name. See
[baseline provenance](v1(lhl)/README.md) for the earlier official packaging record.

## Archive helper

[scripts/fetch-official-examples.sh](scripts/fetch-official-examples.sh) is the
earlier pinned-ZIP extraction helper. PR #2 replaced the committed ZIP with
extracted files, so use the directory linked above directly. The helper downloads
the pinned archive when it is missing; `REFETCH=1` also forces a temporary download.

Do not commit API keys, `.env` files, passwords, private submissions, run logs,
or generated experiment outputs here.
