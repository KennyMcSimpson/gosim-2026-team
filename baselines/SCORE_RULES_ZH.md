# Agent Observer 巡天评分规则说明

本文用于队内讨论和策略开发，依据平台公开的 `challenge-score-v3` 评分契约、公开入门包及其评分器整理。

官方页面：

- [平台文档](https://create.gosim.org/survey26/platform/docs)
- [平台规则](https://create.gosim.org/survey26/platform/rules)

正式运行时，应以初始化消息中发布的 `scoring_contract` 和当前场景的请求字段为准。不同场景的 tile 数量、请求数量、请求奖励和覆盖权重可能不同。

## 1. 观测天区的结构

巡天目录可以理解为：

```text
观测区域 region
    └── tile（一个可观测天区）
            └── scheduling_class：REQUIRED 或 FLEXIBLE
```

公开正式机制预览中，目录示例是 **8 个区域、每个区域 8 个 tile**。每个区域通常包含：

- 2 个 `REQUIRED` tile；
- 6 个 `FLEXIBLE` tile；
- 其中可能有一个 REQUIRED tile 只有较短的可观测窗口。

具体数量以当前场景目录为准，不能把公开预览的 64 个 tile 当成所有正式场景的固定数量。

`REQUIRED` 和 `FLEXIBLE` 的区别主要是最终任务约束，不是两套不同的科学观测公式。只要曝光合法并完成，二者都按同一套科学分公式计算；区别在于漏掉它们时的终局罚分不同。

## 2. 科学观测分的组成

总分中的科学部分来自合法完成的曝光。单个曝光段的基本公式是：

```text
A = instrument_efficiency × transparency × sky_quality
    / (seeing_arcsec × airmass)

A_used = A × lunar_quality_factor

曝光科学分
= tile_science_value
  × (segment_seconds / nominal_exptime_seconds)
  × A_used
  × (1 + program_bonus)
```

如果一次曝光跨越多个 slot，评分器会把它拆成多个曝光段分别计算，再累加。`tile_science_value` 是该 tile 中目标的科学权重总和，不同 tile 可以不同。

### 2.1 曝光完成比例

```text
segment_seconds / nominal_exptime_seconds
```

- `nominal_exptime_seconds` 是该 tile 要求的名义曝光时长，例如 900 秒；
- `segment_seconds` 是该段实际成功曝光的秒数；
- 正常完成整个曝光时，比例为 `1.0`。

曝光可能因天气恶化、天区高度跌出限制、观测窗口结束或夜晚结束而中断。当前规则中，**中断曝光的科学分按 0 处理**，不能简单按已完成比例领取部分科学分。

跨 slot 不一定会中断：只要仍在同一夜晚、几何窗口合法且天气可观测，曝光可以继续推进。

### 2.2 `A_used`

`A_used` 表示这段曝光的实际观测质量。

`A` 由以下因素组成：

- `instrument_efficiency`：仪器效率；
- `transparency`：大气透明度；
- `sky_quality`：天空质量；
- `seeing_arcsec`：视宁度，通常越小越好；
- `airmass`：大气质量，通常越小越好。

之后再乘 `lunar_quality_factor`，得到 `A_used`。月光、目标位置和月面几何会影响这个因子。

Agent 的公开预览通常看不到仪器效率，因此预览分与最终实际分可能有差异。正式比赛中，隐藏仪器效率、故障乘数和异常标签乘数都可能造成这种差异。

### 2.3 Program 修正

Program 由质量等级决定。质量等级使用**不包含仪器效率的质量估计**判定，因此 Agent 可以根据公开快照选择 Program：

| 质量等级 | 条件 | 应选 Program | 匹配奖励 |
|---|---:|---|---:|
| DARK | 质量 ≥ 0.65 | `DARK` | +25%，即乘 1.25 |
| BRIGHT | 0.40 ≤ 质量 < 0.65 | `BRIGHT` | +15%，即乘 1.15 |
| BACKUP | 质量 < 0.40 | `BACKUP` | +8%，即乘 1.08 |

选错 Program 通常仍可得到基础科学分，但失去对应的匹配奖励。

正式比赛的隐藏异常标签还会乘在 tile 的科学分上：

- `NOVA`：科学分乘 `1.5`；
- `Reddening`：科学分乘 `0.8`；
- 两种标签如果同时存在，可以叠加。

这两个乘数不会改变公开的 `tile_science_value`，而是在评分时影响实际观测分。

## 3. REQUIRED、FLEXIBLE 和 request 的任务约束

### 3.1 REQUIRED

完成 REQUIRED tile 后，照常获得该 tile 的科学分和可能的 Program 奖励。

如果巡天结束时仍有 REQUIRED tile 未完成：

```text
每个遗漏的 REQUIRED tile：−1000 分
```

因此，通常应先保证 REQUIRED 不漏，再优化高质量观测和覆盖均匀性。

### 3.2 FLEXIBLE

FLEXIBLE tile 也会产生正常科学分，但每个区域有最低完成配额。公开评分配置中的配额是：

```text
每个区域至少完成 4 个 FLEXIBLE tile
```

不足部分按以下方式扣分：

```text
每个区域每缺少 1 个：−100 分
```

这意味着不能只集中观测少数区域的高价值 tile，而完全忽略其他区域。

### 3.3 观测请求（request）

request 是运行期间发布的临时科学任务，包含：

- `request_id`；
- 可开始时间和截止时间；
- 要求观测的 tile；
- `ALL` 或 `AT_LEAST_N` 完成条件；
- `completion_reward`；
- `miss_penalty`。

满足请求后，奖励是科学分之外的额外加分。例如公开场景常见的是每个所需 tile 完成奖励 `+140`，但正式场景应以请求字段中的实际值为准。

请求必须在发布之后完成。请求发布之前拍过的同一个 tile，不能倒计入该请求。

请求在截止时间前未满足要求时，通常会扣请求自己的 `miss_penalty`。如果在整个截止窗口内根本不存在合法可观测机会，平台可能将其标记为 `excused_unobservable`，免除该罚分。

## 4. 覆盖均匀性奖励

正式大型巡天场景可能启用覆盖均匀性奖励，基本形式是：

```text
coverage_bonus
= coverage_bonus_weight
  × base_science
  × coverage_evenness
```

`coverage_evenness` 衡量已完成 tile 在各个区域之间是否均匀，评分器使用类似 Jain fairness 的均匀度指标：

- 各区域完成数量越均衡，指标越高；
- 只集中在少数区域，指标越低；
- 它通常在最终结算时计算。

公开练习场景可能把 `coverage_bonus_weight` 设为 `0`，此时没有这项奖励。正式场景是否启用以及权重是多少，以初始化时发布的评分配置为准。

## 5. 异常报告规则

正式比赛可能允许 Agent 在不占用观测时间的情况下提交报告：

```json
{"kind": "NOVA", "tile_id": "T00001"}
{"kind": "Reddening", "tile_id": "T00001"}
{"kind": "Instrument_Failure"}
```

### 5.1 NOVA 和 Reddening

它们是隐藏的 tile 异常标签，不是要求 Agent 修改数据或“修复”天区。Agent 的任务是根据公开公式预测分与实际完成分之间的偏差，判断是否可能存在异常并报告类型。

- 正确报告 NOVA 或 Reddening：`+100`；
- 错误报告：`−150`；
- 同一 tile、同一种标签只结算首次报告；
- NOVA 和 Reddening 可以分别报告。

Reddening 的分数乘数是 `0.8`，所以它会降低该 tile 的科学分；正确报告获得的 `+100` 是独立的报告奖励，不会恢复被降低的科学分。

### 5.2 Instrument_Failure

仪器故障是平台模拟的隐藏事件，可能影响全部天空或某些区域，并使实际仪器效率下降。它通常不提前出现在普通天气预报中。

- 在存在尚未确认的活动故障时报告：正确；
- 正确报告后，平台通常约一个模拟日后发布 `fault_status`；
- 维修时间在公开预览配置中约为两个模拟日；
- 故障报告本身不是让 Agent 自动等待两天；维修期间仍可根据故障范围观测未受影响区域，或评估受影响区域是否仍值得观测；
- 已确认且正在维修中的故障重复报告通常是中性结果；
- 没有活动故障时报告属于误报，每次正确报告之间有一次免费误报额度，之后每次误报通常扣 `100`。

## 6. 练习场景和正式比赛场景的区别

| 项目 | 练习场景 | 正式比赛场景 |
|---|---|---|
| 场景数据 | 公开，可用于本地复现 | 关键真值隐藏 |
| 天气真值 | 公开场景文件通常包含天气数据 | Agent只能获得平台发布的天气快照和预测 |
| NOVA | 不启用 | 启用隐藏标签 |
| Reddening | 不启用 | 启用隐藏标签 |
| Instrument Failure | 不启用 | 可能发生，且通常不在预报中出现 |
| 实际分反馈 `tile_last_finished` | 通常不提供 | 提供最近一次完成曝光的实际分 |
| `fault_status` | 不提供 | 正确故障报告后延迟发布 |
| 报告通道 | 不接受或不结算 | 支持 NOVA、Reddening、Instrument_Failure |
| 重复观测 | 已完成 tile 再观测通常非法 | 合法，最终按该 tile 历次观测最高分计入 |
| 覆盖均匀性 | 通常关闭或权重为 0 | 正式大型场景可能启用 |
| REQUIRED/FLEXIBLE | 有 | 有 |
| request | 有公开请求 | 有正式场景发布的请求 |
| 天气和事件文件 | 可公开获取 | 不直接提供给 Agent |
| 评分复现 | 可用公开数据本地重放 | 只能依据运行时评分契约和反馈进行决策 |

两套规则中都存在基础科学分、Program 奖励、REQUIRED/FLEXIBLE 约束和 request；正式比赛是在此基础上增加隐藏异常、反馈、报告、重复观测和覆盖均匀性机制。

## 7. Q&A

### Q1：REQUIRED、FLEXIBLE 和 request 是不是另外一套评分？

是。科学分公式主要评价“这次曝光本身的质量”；REQUIRED、FLEXIBLE 和 request 评价的是“最终任务是否完成”。一条合法曝光可以同时获得科学分、Program 奖励和 request 完成奖励，最终再扣任务遗漏罚分。

### Q2：每个 tile 观测后是不是都有同样的科学分？

不是。公式相同，但 `tile_science_value`、曝光时长、天气、几何条件、月光、仪器效率和正式比赛中的隐藏标签都可能不同。

### Q3：曝光完成一半是不是得到一半科学分？

当前配置不是这样。完成比例会进入曝光公式，但如果整个曝光没有成功完成，`interrupted_exposure_science_score` 为 `0`，因此中断曝光通常不产生科学分。

### Q4：Reddening 为什么乘 0.8 还要识别？

因为它是一个需要从分数偏差中发现的隐藏异常。识别成功会获得独立的 `+100` 报告奖励，但不会恢复该 tile 被降低的科学分。

### Q5：正确报告 Instrument_Failure 后，接下来两天是不是只能 wait？

不是。故障有影响范围和效率乘数，可能只影响部分区域。维修期间应查看 `fault_status`，继续观测未受影响且仍有价值的候选；只有没有合法或值得观测的候选时才等待。

### Q6：公开练习赛的高分能否代表正式比赛成绩？

不能直接代表。练习赛主要验证调度和协议；正式比赛还包含隐藏异常、仪器故障、重复观测、反馈和覆盖均匀性。练习成绩只能说明策略在公开场景上的基础调度能力。

### Q7：最终应该优先追求什么？

建议优先级是：

1. 避免不可观测时强行观测和非法 action；
2. 完成所有 REQUIRED tile；
3. 满足各区域 FLEXIBLE 配额；
4. 在合法窗口中匹配正确 Program；
5. 完成高价值 request；
6. 保持区域覆盖均匀；
7. 用实际分反馈谨慎判断隐藏异常，避免无证据的错误报告。

