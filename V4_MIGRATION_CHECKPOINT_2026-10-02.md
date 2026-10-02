# GOSIM v4 迁移检查点与讲座收获

日期：2026-10-02（北京时间）

这份文件是团队仓库的开发参考，不是参赛提交说明，也不是 v4 API 的替代规范。正式实现以前，官网 Rules/Docs、资源页下载的 v4 starter/card、`initialize` 实际 payload 和参赛页检查结果优先。

## 结论先说

截至 2026-10-02，`observer-practice-app` 已加入与 v3 练习主线隔离的本地 v4 contract harness，并有 `compileall`、v4 regression test 和 deterministic smoke 证据。它不是完整 official parity：`gamma`/`delta` 的 `stress`/`earthquake`/`state_resync`、官方 exact `skymath` 几何、lunar scorer、公开资源的 fiber 数量冲突和 hidden truth 仍待核对。

本地 harness 使用独立的 v4 协议、几何、评分、工作流、demo fixture 和最小 Agent；`observer.project.json` 及公开 v3 练习入口没有切换。讲座启发式曾短暂接入默认 v3 Agent，得到的公开场景分数只是策略实验；该代码已经撤回。讲座收获保留为研发参考，不写入 v3 模拟器，v3 主线未改变。

当前本地证据包括 v4 模块的 `compileall`、`tests/test_v4_regression.py` 回归测试，以及 `scripts/smoke_v4.py` 对 `scenarios/v4-demo` 的 deterministic smoke；smoke 产物记录了正常结束、5 个动作、4 个目标和已完成的 demo request。这些证据只覆盖本地 fixture 的契约回归，不等于官方卡、官方 scorer、云端运行或正式/隐藏成绩。

## 已核对的 v4 规则边界

截至 2026-10-02，官网规则页面显示：

- 练习卡为 α、β、γ、δ；线上比赛卡为 A、B、C、D；赛后隐藏卡为 E、F、G、H。
- v4 动作包括 `observe`、`wait`、`report`、`finish`。
- `observe` 同时涉及天空指向、逐根光纤的目标分配、60–3600 秒曝光和 `DARK`/`BRIGHT`/`BACKUP` 程序。
- 光纤布局和数量以每张卡的配置及 `initialize` 实际 payload 为准；公开资源存在 fiber 数量冲突，页面列出的数量与本地 fixture 都不能直接当作最终真值，仍待核对。
- 目标只有在落入被分配光纤的 cell、且整个曝光期间高度角不低于 30° 时才计分；评分还涉及亮度、曝光、sky quality、science weight 和程序加成。
- v4 线上每次评测运行 A–D 四张卡，每张卡 900 秒；线上结束后，主办方在 E–H 上对最终版本各评测一次，最终成绩取四张隐藏卡成绩的算术平均。
- 规则页面明确区分 v3 练习 CSV 与 v4 任务卡；v3 练习场景不能作为 v4 兼容性证明。

这些是规则页面层面的迁移输入；v4 starter/card 的具体文件哈希、完整 JSON schema、异常语义、平台运行细节、官方 exact `skymath` 几何、lunar scorer 以及 `gamma`/`delta` 的 `stress`/`earthquake`/`state_resync`，仍需从资源页和实际接口逐项核对；hidden truth 当前不可得。

## v3 与 v4 的差异

| 方面 | 当前本地模拟器 | v4 目标实现 |
|---|---|---|
| 场景 | `dev-fortnight`、`dev-reference` 公开 v3；另有隔离的本地 v4 demo fixture | α–δ 练习卡，之后 A–D/E–H |
| 协议 | v3 snapshot、预排 tile candidate；v4 harness 独立处理 v4 contract | `initialize` 下发 fiber config、target/catalog 和 v4 合约 |
| 观测动作 | 选择现成 `tile_id`/`program`/`request_id` | 指向、逐 fiber 分配目标、曝光秒数、程序联合决策 |
| 动作集合 | 主要是 `observe`、`wait` | `observe`、`wait`、`report`、`finish` |
| 评分 | 本地 `challenge-score-v3` | v4 target/fiber/cell/sky/exposure/program/coverage 等规则 |
| 证据 | v3 可本地复现；v4 有 compile/test/smoke 的 fixture 证据 | 仍需 v4 starter/card、平台实际 payload、官方 geometry/scorer 和 hidden truth 复核 |

## 当前 v4 证据边界

- v4 harness 是 `observer-practice-app` 内与 v3 入口隔离的本地 contract regression 入口；它不改变 `observer.project.json`、v3 协议或 v3 scorer。
- 本地 `v4-demo` fixture 证明协议、动作、fiber cell、30° 高度角、曝光边界、评分流程和结束流程可以在本地回归；它不是官方 α–δ、A–D 或 E–H 任务卡。
- `gamma`/`delta` 的 `stress`/`earthquake`/`state_resync`、官方 exact `skymath` 几何、lunar scorer、公开资源 fiber 数量冲突和 hidden truth 均属于待核对项；因此当前结论是“有隔离 harness 和本地证据”，不是“完成 official parity”。

## 讲座留下的可迁移认识

这些认识应进入后续策略设计，而不是直接改写官方模拟器：

1. 优化对象是整个巡天的科学产出，不是单个目标的局部得分。
2. 指向、目标选择、光纤分配、曝光、程序和顺序是组合决策。
3. 目标类别、亮度、seeing、transparency、sky background、月光和高度角共同改变收益。
4. 指向、读出、换仪器、故障、烟雾、地震和卫星过境属于调度风险，不能凭讲座口头描述擅自写成 scorer 常数。
5. AI 应负责受约束的计划与适应；最终动作必须经过官方 schema、几何、时间和风险检查。

## 本次实验记录与处理

临时 lecture-aware 策略在公开 v3 上得到：

| 场景 | 原 v3 基线 | 临时策略 | 处理结论 |
|---|---:|---:|---|
| `dev-fortnight` | 6512.721299 | 6526.194418 | 仅作策略实验记录，不改官方基线 |
| `dev-reference` | 12287.478365 | 12298.666905 | 仅作策略实验记录，不改官方基线 |

分数变化说明候选排序会影响公开 v3 结果；它不说明 overhead、目标类别偏好或高度角权重是官方 v3 行为。因此这部分实现已从模拟器撤回。

## 正式开发顺序

1. 从官网资源页取得 v4 starter/card，记录下载日期、文件哈希和目录结构。
2. 只读重建 v4 `initialize`、decision request、response、`finish` grace period 和错误语义。
3. 保留现有隔离的 v4 contract harness 作为本地回归入口；待官方 starter/card 和实际 payload 核对后，再补齐协议、fiber geometry、target legality、时间推进和 scorer；不修改 v3 回归线。
4. 现有最小 fixture 已覆盖 cell 合法性、30° 高度角、曝光边界、程序加成、重复目标、`report`、`finish` 和异常输入；仍需用官方卡补做 parity 核对。
5. 先让确定性 v4 baseline 与官方公开卡逐项对齐，再把讲座认识作为可替换策略层。
6. 分别报告 simulator parity、baseline score、策略实验 score 和正式/隐藏未知项，不把它们混为一类。

## 公开来源

- [官方 Rules](https://create.gosim.org/survey26/platform/rules)
- [官方 Docs](https://create.gosim.org/survey26/platform/docs)
- [官方 Resources](https://create.gosim.org/survey26/platform/resources)
- [本地模拟器仓库](https://github.com/KennyMcSimpson/gosim-agentic-observer)
