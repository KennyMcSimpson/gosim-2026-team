# GOSIM v4 迁移检查点与讲座收获

日期：2026-10-02（北京时间）

这份文件是团队仓库的开发参考，不是参赛提交说明，也不是 v4 API 的替代规范。正式实现以前，官网 Rules/Docs、资源页下载的 v4 starter/card、`initialize` 实际 payload 和参赛页检查结果优先。

## 结论先说

官网当前规则已经切到 v4 任务卡叙述，但本地 `gosim-agentic-observer` **还没有实现 v4**。它仍是公开 v3 练习模拟器，核心代码使用 `challenge-score-v3`、`tile_id`/`region_id`/`request_id`、预排合法候选以及 v3 的 `observe`/`wait` 行为。

本次曾把讲座启发式接入默认 v3 Agent，得到了一组更高的公开场景分数；这只是策略实验，不是 v4 迁移，也不能证明模拟器更接近官方。该代码已经撤回，模拟器恢复到原来的 v3 代码线。

## 已核对的 v4 规则边界

截至 2026-10-02，官网规则页面显示：

- 练习卡为 α、β、γ、δ；线上比赛卡为 A、B、C、D；赛后隐藏卡为 E、F、G、H。
- v4 动作包括 `observe`、`wait`、`report`、`finish`。
- `observe` 同时涉及天空指向、逐根光纤的目标分配、60–3600 秒曝光和 `DARK`/`BRIGHT`/`BACKUP` 程序。
- 光纤布局和数量以每张卡的配置为准，并在 `initialize` 中下发；当前规则页面列出的练习卡光纤数量为 16、25、9、100。
- 目标只有在落入被分配光纤的 cell、且整个曝光期间高度角不低于 30° 时才计分；评分还涉及亮度、曝光、sky quality、science weight 和程序加成。
- v4 线上每次评测运行 A–D 四张卡，每张卡 900 秒；线上结束后，主办方在 E–H 上对最终版本各评测一次，最终成绩取四张隐藏卡成绩的算术平均。
- 规则页面明确区分 v3 练习 CSV 与 v4 任务卡；v3 练习场景不能作为 v4 兼容性证明。

这些是规则页面层面的迁移输入；v4 starter/card 的具体文件哈希、完整 JSON schema、异常语义和平台运行细节，在本地正式实现前仍需从资源页下载并逐项核对。

## v3 与 v4 的差异

| 方面 | 当前本地模拟器 | v4 目标实现 |
|---|---|---|
| 场景 | `dev-fortnight`、`dev-reference` 公开 v3 | α–δ 练习卡，之后 A–D/E–H |
| 协议 | v3 snapshot、预排 tile candidate | `initialize` 下发 fiber config、target/catalog 和 v4 合约 |
| 观测动作 | 选择现成 `tile_id`/`program`/`request_id` | 指向、逐 fiber 分配目标、曝光秒数、程序联合决策 |
| 动作集合 | 主要是 `observe`、`wait` | `observe`、`wait`、`report`、`finish` |
| 评分 | 本地 `challenge-score-v3` | v4 target/fiber/cell/sky/exposure/program/coverage 等规则 |
| 证据 | 可本地复现公开输入 | 需要 v4 starter/card 和平台实际 payload 复核 |

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
3. 在独立的 v4 模块或分支实现协议、fiber geometry、target legality、时间推进和 scorer；不修改 v3 回归线。
4. 用最小 fixture 覆盖 cell 合法性、30° 高度角、曝光边界、程序加成、重复目标、`report`、`finish` 和异常输入。
5. 先让确定性 v4 baseline 与官方公开卡逐项对齐，再把讲座认识作为可替换策略层。
6. 分别报告 simulator parity、baseline score、策略实验 score 和正式/隐藏未知项，不把它们混为一类。

## 公开来源

- [官方 Rules](https://create.gosim.org/survey26/platform/rules)
- [官方 Docs](https://create.gosim.org/survey26/platform/docs)
- [官方 Resources](https://create.gosim.org/survey26/platform/resources)
- [本地模拟器仓库](https://github.com/KennyMcSimpson/gosim-agentic-observer)
