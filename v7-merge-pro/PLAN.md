# GOSIM 巡天智能体 v7：merge-pro 最终方案

日期：2026-10-07（北京时间）  
状态：`design-final / implementation-complete / engineering-verified / effect-unverified`  
仓库发布目录：`v7-merge-pro/`；ZIP、源码、调研快照和验证回执均在该目录。
目标：以官方 `python-pro` 为数值规划主干，迁入现有候选的配置、请求和恢复设计，再按照 Week4.2 调研的监督边界做在线适配。完整 v7 已实现并打包；工程通过不等于提分或泛化通过。

## 1. 结论

v7 不从 `base-adaptive-v1` 继续堆模块，也不把官方 pro 当成只能参考的黑盒。代码主干采用官方仓库 `examples/python-pro` 的联合规划器；官方 `pro-log-reader` 分支的值班留言读取器作为第一类 Agent 输入。现有基座中的配置驱动、状态撤销、请求完整奖励和证据门作为迁入层，防止 pro 示例中的固定常数和卡片假设直接进入新版本。

最终结构是：

固定的是规划器结构、合法性、计分规则和更新幅度；动态的是公开天气/值班事实、Pro 自身的 band/offset 估计和少量 science 残差参数。保留 science、REQUIRED、request、recovery 四种价值通道作“类 MoE”专家，Agent 根据当前证据调整候选条件与风险，数值规划器负责统一比较。它是冻结规划基座上的任务级在线适配，不是神经 MoE，也不把黑盒 API 调用称为 LLM 权重训练。

```text
官方 pro 数值主干
  ├─ 几何、光纤、指向偏差、program band、故障诊断、pace
  ├─ gain - lambda * exposure_time 联合候选搜索
  └─ 从当前公开 scoring / instrument / survey 读取门槛和资源
        ↓
Operations Agent（真实作用）
  ├─ 每条长 observation_request.reason 只读一次
  ├─ 抽取带原文证据和时间窗的 closure / sector avoid / instrument issue
  ├─ 每夜或语义事件变化时给出 bounded profile
  └─ 规则层决定 wait、方位降权、是否进入 report 证据链
        ↓
Merge 保护层
  ├─ 请求奖励按完整请求核算，不把目标单点分数冒充请求收益
  ├─ REQUIRED 使用公开 completion threshold，不使用固定 0.5 / 80
  ├─ 所有报告经过故障/天气/地震区分和 false-report 预算
  └─ 无 key、超时、无效 JSON、证据过期时回退官方数值主干
        ↓
Merge-pro 在线适配层
  ├─ 低维、可撤销的 selected-exposure positive-fraction 校准
  ├─ 正分比例只作诊断；公开 science best-max 增量作决策监督
  ├─ 同条件足量支持、预序误差改善后，限幅修正 science 分项
  ├─ 有限连续情景短窗重排，当前动作执行后下一轮重算
  └─ 事实记忆按有效期/更正复用；无完整轨迹证据的策略记忆不赋权
```

回答“是不是在官方 Pro 上改”：是。它已经把联合搜索、光纤几何、offset 学习、band 拟合和故障诊断连在一起，适合作为完整可回退基座。团队旧版本用于提供配置化、请求整体奖励、撤销恢复和证据门的设计，不能凭旧卡高分把整套特化策略覆盖到 Pro 上。

这是一版完整 v7：官方数值主干、Agent 语义事实与夜间判断、结果驱动适配、短窗调度在同一实现内接入，不交付中间 Merge 版本。在线支持门、CPU 截止和回退是该算法的运行规则，不是需要用户逐轮选择的研发阶段。任何模块都不能改计分系数、目标门槛、动作合法性或把文字判断写成已完成观测。

## 2. 已核实依据

### 官方 pro

官方仓库 [main 的 Python Pro](https://github.com/gosimfoundation/hackathon-survey26/tree/373d94f32598073b4eff31c15794fcd3474722ae/examples/python-pro) 为 `373d94f32598073b4eff31c15794fcd3474722ae`，已有规划主干。[pro-log-reader 分支](https://github.com/gosimfoundation/hackathon-survey26/tree/ab27e5ef32f0a834b054d855cf9706b44502204b/examples/python-pro) 为 `ab27e5ef32f0a834b054d855cf9706b44502204b`，不是 main 的祖先；它新增的值班读取器尚未合入 main。此处版本为 2026-10-07 核查快照。

官方 pro 的确定性主体包括：

- 一次联合搜索同时选择 pointing、fiber assignment、duration 和 program，并用 `gain - lambda * T` 计价。
- 用饱和命中估计 program band，把天气造成的 quality 下降和 instrument fault 分开。
- 从命中/落空学习固定 pointing offset。
- 用 season plan、REQUIRED 概率和 request bonus 做候选排序。
- 用 fair-clock 的自身 CPU 成本调整搜索级别。
- 模型只在夜间计划、故障复核和付费 report 确认等窄职责中使用；模型失败时数值规则继续运行。

`pro-log-reader` 新增的明确作用是：读取 `active_requests` 与新 `observation_request` 的 `request_id`、`reason`、`issued_at_utc`，让模型提取带时间窗的全站关闭、方位避让和仪器问题，再由确定性规则转成 wait、降权或 report 证据。它不是完整 action 生成器。

迁入时必须修正的源码风险：REQUIRED 使用固定 `0.5/80`，request 用 `3*reward/remaining` 分摊，搜索含 `/16` 和固定光纤编号；reader 的更正没有充分撤回，仪器留言可直接 report，最长等待达 240 秒。这些是源码事实，不是对其线上成绩的归因。完整分支差异为 25 文件，其中三套 examples 相关差异为 21 文件。

### 当前项目的教训

- `base-adaptive-v1` 默认 `S-shadow`，策略强度和预测影响均为零；因此 Agent 不能改变动作是设计结果，不是模型能力结论。
- `S-F/S-PF` 的预测到动作融合尚未接入；仅登记预测和实际权重字段不能证明策略影响。
- 旧批次有 API 失败后规则回退、`llm_calls=0`、模型标签与真实调用不一致的情况；v7 必须报告成功、失败、回退和动作变化四本账。
- A1–D1 是固定公开输入但未来天气、事件和故障隐藏；当前规则确认每卡 900 标准化 CPU 秒并另有 30 分钟墙钟，超级榜为 `0.2 * sum(A-D) + 0.8 * sum(A1-D1)`。
- 朋友 v5 的固定 REQUIRED 数、永久 abandoned、强制 PROTECT 排序有泛化风险，不能原样迁入。旧结果包与某个源码 tip 尚未完全绑定，也没有本轮与官方 Pro 的新配对实验，因此“最好”只指已有开发结果。

### Week4.2 调研

依据：[Week4.2 调研增补](RESEARCH.md)。优先研究小型锚定结果校准和连贯情景短窗调度；条件提案记忆等前两者有完整轨迹证据后再接。TTT/LoRA、JEPA 和 bandit 暂缓，因为缺少参数访问、任务专属转移数据或可信动作奖励与覆盖条件。

其中 [ORCA](https://arxiv.org/html/2606.14222v2) 支持“冻结基座、小残差按真实结果更新”的思路；[JitRL](https://proceedings.mlr.press/v306/li26cv.html) 提供候选重排启发。它们不直接证明 GOSIM 效用或计时优势，不能把这些方法名称当成已实现的 TTT。

## 3. v7 的组件边界

### 3.1 数值主干：`OfficialProPlanner`

来源：官方 `examples/python-pro`。迁入时只做接口适配和配置化修复，不重写其几何核心。

必须改为公开配置驱动的字段：

- `required_threshold`、`required_penalty`、`completion_reward`、false report penalty/free allowance。
- `grid.n`、fiber pitch、曝光最小/最大值和实际 `scoring.program`。
- 公开 night calendar、deadline、request target set 和 remaining count。

报告间隔、单次模型等待、重试和并发队列属于防阻塞的工程参数；官方若无对应字段，就不称为“公开配置”。它们与官方计分配置分别记录。模型调用总数、阶段调用数和累计等待时长不设自创额度。

固定数值只能保留为明确的 fallback，并在 trace 中注明来源；卡名、target ID 和开发卡条件不能进入策略分支。

### 3.2 Operations Agent：`OperationsAdvisor`

保留 Pro 的 `night_plan` 和 `fault_review` 两种默认启用的大模型职责；新增 Operations 是第三种职责。无 key / 平台 no-model 模式运行完整数值基座，并明确标为规则模式。代码有两个阶段不等于平台已认定合规，验收须核实各阶段实际调用和输出用途。

输入：当前公开 bulletin/forecast、active requests、长 `reason`、issued time、当前 UTC、剩余 wall time。  
输出：结构化事实和可选 profile，不直接输出比赛 action。

每个事实必须包含：`source_id`、`issued_at`、`information_cutoff`、`scope`、`direction`、`start_utc`、`end_utc`、`quote` 的哈希/短引用、`status`。原文只在进程内用于校验，不写 trace。

允许的 scope：

- `closure`：整个站点在指定时间窗关闭，才允许提前 wait。
- `avoid`：指定方位/全方位在时间窗内不宜观测，只降低对应候选权重。
- `instrument`：值班记录明确说仪器异常，进入 report evidence；天气、地震和模糊担忧不能直接触发 report。

事实按 `issued_at` 排序应用；后来的同 scope/direction 更正可以撤回早期事实；过期事实自动失效；无证据、quote 不是新留言原文子串、时区不明确或模型超时则不应用。quote 一致只证明可追溯，不证明模型解释正确；无法确定更正范围时保留为未接受建议。

instrument 输出只进入统一报告入口，不直接返回 `report`。该入口检查新鲜证据、此前正确报告/已消费证据、误报资源、间隔、天气与地震解释，并保留付费确认。明确的当期仪器事实可以构成故障证据；模糊推测和天气留言不能获得同等权限。

调用策略：每条 request 留言一次，按来源 ID/issued/hash 去重；每夜最多一次 night plan 和 fault review。按用户要求，API/Token 额度视为充足，**没有自创的总调用次数、分阶段调用额度或累计等待配额**。共享单 worker、有界队列只保护程序资源；单问题截止 45 秒，429/5xx 有界重试；主循环单次等待最多 8 秒，并根据平台实际剩余 wall time 缩减。没有 key 时跳过模型，确定性 pro 继续工作。单请求超时、重试和队列长度是防阻塞参数，不是模型额度；赛题 CPU/墙钟硬限制仍以官方实际 payload 为准。

### 3.3 结果校准器：`OutcomeCalibrator`

这是黑盒在线校准，不是 LLM 权重 TTT。

对每个已执行曝光只登记一条冻结记录：

```text
(action_index, issued_at, end_time, base_probability, features, input_hash)
```

`y = positive_score_count / assigned_count` 只在结果完整、公开、可关联且未被撤销时成熟。它是所选曝光的正分比例，受天气、几何和仪器共同影响，不能命名为纯天气概率，也不能直接代替 REQUIRED completion probability。

最终正分诊断采用同 epoch 的 Beta(1,1) 全局收缩比例作锚，在 direction/program 条件至少 8 条成熟曝光时做向该锚收缩的条件比例估计。早期研究原型的 12 参数 logit 残差不作为最终实现描述。概率锚点始终对应同一监督对象，不能拿 Pro 的 quality scale 或 band 数值当概率；曝光时长和分配数作为下述 science 适配的条件桶。

预测在输出动作时冻结，下一轮收到完整公开结果后才更新；`state_resync` 或 revised 结果按记录重放，无法逐条回撤就重置可信统计。支持不足、未知标签、未覆盖条件和标签不完整回退 base；每卡重置。

正分比例校准器只做 shadow 诊断，**不乘到 science gain**。它不能映射到加权 best-max 效用，也没有未选候选的反事实标签。Brier/log loss 改善只允许预测结论。

动作适配由单独的 `ScienceGainCalibrator` 完成。输出曝光前冻结基座估计和每个已分配目标的旧 best score；完整公开结果成熟后构造：

```text
y_science = sum_over_assigned_targets max(0, public_hit_score - old_best_score)
```

未命中目标取 0；标签是这次曝光的公开科学分增量，不混入 REQUIRED、request、均匀度或 report。用小型、有收缩的残差模型修正同一量，预测倍率限于 `0.8–1.2`。同 program/direction/曝光桶/分配数桶至少 8 条成熟记录，先预测后更新的误差优于冻结基座，且当前条件在支持范围内，才允许倍率影响候选 science 分项；否则倍率为 1。每次调整相对当前 Pro 估计，不永久累乘。更新与更正可撤销，每卡重置。

这补上了监督对象到效用的对应关系，但仍只观测被执行动作的结果，不能宣称证明了未选动作的反事实效果或隐藏泛化。同预算完整轨迹消融负责检验实际收益；运行中的小样本门不是科学有效性的证明。

原来的“Agent 独立预测天气”需求保留为独立研究接口：冻结 horizon/cutoff 和预测，只有以后公开的、可匹配天气/公告证据才能作标签。没有公告不等于晴天，曝光正分比例不等于天气真值。首版不得用不可见天气给自己打分，更不得因为自评预测更好就自动提高修改权重。

### 3.4 连贯短窗调度：`ShortHorizonReranker`

只在有限 beam 中比较当前动作和最多一个后续动作。天气路径使用预先冻结的压力情景，不当作真实概率：当前持续、偏差不利、偏差有利。每个后续动作重新计算开始时刻、目标高度、月亮、光纤可行性、曝光时长和 deadline。

重排条件：

- 当前动作合法且立即 utility 不明显下降。
- 请求奖励按完整 request 一次计入，并且 deadline 内的目标集合满足 completion threshold。
- REQUIRED 使用公开配置；同一目标的 best-max 不能重复计分。
- `time.process_time()` 到搜索截止就回退当前官方 pro 方案。
- 当前已有完整 request two-step 计划时，短窗模块不能把它替换成没有证明的普通 action。

首版保留最多 4 个候选，压力尺度固定为 `1.0 / 0.8 / 1.1`，最多比较两次曝光；先保证首步即时效用至少达到基座的 `0.98`，再按三条连贯路径的最坏总效用重排。每轮最多使用 `0.04` 实际 CPU 秒且不超过估计单轮 CPU 配额的 `12%`；预算紧张时直接用基座动作。这些参数在本轮冻结，不根据某张卡临时改动。

短窗模块不能把“多情景模拟”写成天气预测，也不能把额外搜索 CPU 当作算法收益。它的结果只在同预算完整轨迹中检验。

### 3.5 条件提案记忆

只保存结构化的公开条件、提案参数相对当前 base 的变化、证据引用、版本、TTL、执行和完整轨迹回执。提案状态分 `proposed / executed / supported / expired / retracted`。每卡在线状态清空；开发阶段记忆在验证前冻结；失败案例保留，不能只存高分案例。

v7 直接复用当卡仍有效的公开语义事实，减少重复解读；它是事实 ledger。跨轨迹“策略提案记忆”只保留 schema 和审计，只有 `supported` 且绑定匹配完整轨迹回执的条目才可进入有限先验。LLM 自评或一次曝光正分不算 supported；目前没有这样的策略条目，所以不能捏造一个策略库为其赋权。

## 4. Agent 为什么这次会产生实际影响

已确认原因是默认 shadow 的影响权重为零，以及预测到动作尚未接入。另一个条件性风险是异步回答要求 context signature 严格匹配；合成夜间推进可改变签名，但没有真实运行 trace 证明拒绝频率，不能说它是普遍失效原因。v7 使用按事实/夜次/有效期校验的稳定职责：

```text
长值班留言 -> OperationsAdvisor -> 有时间窗事实 ledger
      -> 官方 pro 的 wait / 方位权重 / report evidence

成熟曝光结果 -> ScienceGainCalibrator -> 有支持、限幅的science增量修正
      -> 连续情景短窗 -> 候选排序 -> 下一轮动作

同一结果 -> PositiveFractionCalibrator -> 独立预测诊断
```

各阶段分别记录 `attempts/success/accepted/rejected/fallback`、来源哈希、状态/时间版本和参数变化。`model_calls > 0` 只证明请求发出，`accepted > 0` 只证明通过校验。动作差异必须来自同状态关闭该影响的可复核对照或完整轨迹消融；参数变化不能填成已测 `action_diff`。收益只由同预算完整轨迹比较证明。

## 5. 一次交付的 v7

只交付 `v7 merge-pro` 一个候选，内部同时包含：

1. 官方 Pro 联合规划器以及公开配置、完整 request reward、撤销恢复修正。
2. 默认启用的 `night_plan / fault_review / Operations`，共享客户端、防阻塞超时、失败回退和逐阶段审计；默认不限模型调用次数。
3. ScienceGainCalibrator 的在线残差更新及条件支持门；正分比例只作独立 shadow 诊断。
4. 连贯短窗重排、best-max 去重、请求整体奖励、实时几何复算和严格 CPU 回退。
5. 当前卡的有界事实记忆；未获完整轨迹证据的跨轨迹策略条目不赋权。

默认有模型时窄职责接入，无模型时保留完整数值主干、公开结果适配和短窗能力；不会把“永久 shadow”设为整个系统的默认模式。校准条件支持不足只关闭该项倍率，Operations 与短窗仍各自工作。

验证使用同一份源码的开关做消融，不需要迭代交付多版：官方 Pro、v7全体、禁用Agent应用、禁用gain校准、禁用短窗。预测proper loss、动作变化、科学/REQUIRED/request分项与整季净分分别报告，不能把组合提升全部归因于Agent或TTT。

Agent 权重不能因为它自己的解释或预测更好自动增加。它的事实权限由公开来源和 TTL 决定；数值适配倍率由真实曝光结果与支持门决定；总系统有效性由完整轨迹决定。这三条证据各自负责自己的权限。

## 6. 验收合同

| 层级 | 比较 | 主要端点 | 通过条件 | 失败处理 |
| --- | --- | --- | --- | --- |
| 协议 | keyless v7 vs official pro | 合法 JSONL、无 agent_error、完整结束 | 9/16/25/100光纤合同与完整结束通过 | 数值基座 |
| Agent 输入 | mock note / real public note | quote 校验、时间窗、更正和过期 | 无假事实、无天气误报 instrument、无重复调用 | 禁用该事实，继续规则 |
| LLM 职责 | night_plan / fault_review / Operations | 逐阶段成功、接受、用途与回退 | 有 key 默认启用前两阶段；无 key 规则模式协议通过 | 数值基座 |
| 预测 | global shrinkage vs calibrator on same exposure labels | Brier/log loss、按曝光等权、撤销重放 | 只作预测诊断；不自动解锁动作 | calibration-shadow |
| 科学增量适配 | frozen base gain vs bounded residual | 同曝光平方损失、分桶支持、撤销、OOD | 运行支持门只准许局部倍率；策略结论仍需整季差值 | science倍率=1 |
| 动作 | same budget paired trajectories | action diff、CPU、wall、REQUIRED、request、science | 预先冻结门槛下净收益不低于 comparator 且无预算回归 | 保留 pro action |
| 泛化 | unseen generated stress families | 卡级差异、最坏路径、退出原因 | 不依赖卡名/target ID，坏天气不灾难性留债 | 关闭失效增量 |
| 外部 | official current A-D/A1-D1 or hidden E-H | 官方回执、版本、终止原因 | 只报告真实回执；本地不能替代线上 | 不做线上结论 |

运行参数已如上冻结；整季效果验收采用同预算基座对照、逐卡差值和失败卡全保留，不把任意提升百分比冒充已测成功线。新增模块若挤掉完整运行、增加错误/超时或无法撤销标签，立即回退。收益若仅出现在已调参卡上，只报告开发集现象。

## 7. 不进入 v7 的方向

- LLM LoRA、模型权重 TTT：当前模型是黑盒 API，且没有专门预训练和安全恢复边界。
- JEPA：没有视觉/结构化转移训练集和与 score 对齐的辅助监督。
- Bandit/EXP4/CBwK：没有可用 propensity、动作覆盖和可对齐即时净奖励。
- 逐 exposure 自评 reward 的 JitRL：官方代码的优势重排结构可参考，但当前不能把 LLM 自评当作 GOSIM 净收益。
- 按 A/B/C/D 或 A1/B1/C1/D1 写分支：公开卡固定但 hidden E–H 存在，卡名分支会直接放大过拟合风险。

## 8. 交付与证据边界

计划文档、官方源码审计、调研增补和 v7 候选源码分别保存。官方源码的 `pro` 分数是示例包自报/参考，不等于我们的 v7 结果；`base-adaptive-v1` 的 33 个测试和 keyless L1–L4 回放也不构成 v7 收益。v7 必须有自己的源码哈希、ZIP 哈希、测试回执、逐卡完整轨迹和模型调用/回退报告。

| 产物 | 位置 | 2026-10-07 当前证据 |
| --- | --- | --- |
| 原冻结基座 | `experiments/base-adaptive-20261006/worktree/candidates/base-adaptive-v1/project` | 已有工程和无模型本地证据；未作为 v7 实测 |
| 官方来源副本 | `experiments/official-pro-audit-20261007/upstream` | 固定在 `ab27e5e`，主代理已读关键源码 |
| A/B/Operations 研究原型 | `experiments/merge-20261007` | 独立模块测试；尚未接官方 Pro，更无策略增益证据 |
| v7 实现 | `experiments/v7-merge-pro-20261007/project` | 全部模块接入，56 项测试通过；实际 ZIP 解压后的四种光纤协议复验通过 |
| v7 交付包 | `experiments/v7-merge-pro-20261007/v7-merge-pro.zip` | 59,938 bytes，九个运行文件；未上传或评分 |
| v7 复验回执 | `experiments/v7-merge-pro-20261007/verification.json` | 源码/ZIP 哈希、测试与解压协议结果；真实模型调用和评分轨迹均为 0 |

普通子代理均请求 `gpt-6-luna/max`；applied-routing 遥测不可见，记 `ROUTING_POLICY_VIOLATION`。关键采用结论由主代理复核，子代理报告不自动升级为已核事实。

本轮根代理于 2026-10-07 01:35（北京时间）运行 `verify_v7.py`，56 项测试通过，实际 ZIP 解压后的 9/16/25/100 光纤协议均返回合法 observe 并产生 finish audit。测试覆盖模型尝试超过 60 次、260 条 Operations 留言、队列恢复、迟到更正、撤销反馈与短窗 CPU 回退。ZIP SHA-256 为 `c995cb8fff57f7fe3fb6b7086658a98b5dc758981c05f25903518c1409e03428`；运行源码集合 SHA-256 为 `f76cc7852722c94c4f97a52f68abb1d9ed2e75f6d6e63cfb985fcfa09c4be0f1`。

有限缓存并不是调用额度：完成留言保留最近 32 条、去重 key 保留最近 1024 条，事件和活动事实各最多 256 条。极旧 key 被淘汰后再次出现可能重解读；缓存容量与模型次数分别记录，不将有限缓存宣称为完整长期记忆。

当前可称为 `engineering-verified / awaiting-independent-evidence`。在没有同预算配对轨迹和独立条件前，不能称为 SOTA、已超过官方 pro 或已解决过拟合。完整卡 CPU、真实模型接受率和净分仍未测。

### 来源与下一步

- 原完整版方法与朋友 v5 审阅属于本地工作区研究材料，本仓库只发布本 v7 方案及其必要的调研快照。
- [官网规则](https://create.gosim.org/survey26/platform/rules)、[公告](https://create.gosim.org/survey26/platform/announcements/)、[公开来源核查回执](SOURCE_VERIFICATION.json)。规则和公告中的计时/榜单口径冲突沿用调研快照记录，不静默选择一种解释。
- 本轮一次完成 v7 接线、合同测试和独立候选包；已发布到 `KennyMcSimpson/gosim-2026-team` 的 `v7-merge-pro/` 目录。此次 GitHub 发布不等于赛事平台上传、线上评分或隐藏评测；这些仍需单独执行并记录回执。
