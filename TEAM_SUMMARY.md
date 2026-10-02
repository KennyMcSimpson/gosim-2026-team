# 2026-10-02 讲座与赛事消息摘要

这份记录供团队共享今天的讲座要点、公开赛题变化和下一步安排。它不是官方 API 规范；实现时以官网 Rules/Docs、最新 starter/card 和参赛页实际行为为准。

## 今日讲座要点

- **目标不是单点最优。** 光谱巡天要在有限观测时间内获得更多、更有价值且质量足够的星系光谱；单个目标曝光更久不一定带来整个巡天的最优结果。
- **观测是组合调度。** 望远镜指向、目标选择、光纤分配、曝光时间、程序和顺序需要联合决策。
- **目标和条件不均匀。** BGS、ELG、LRG、QSO、STAR 的亮度与科学价值不同；seeing、transparency、sky background、月光和高度角共同影响可观测性。
- **要算上 overhead 和风险。** 指向、读出、换仪器、故障、烟雾、地震和卫星过境都会占用时间或破坏曝光，不能只按当前目标分数贪心。
- **AI 的作用是实时整合信息。** 最终目标是更稳定地利用观测窗口，而不是让模型直接生成未经约束的低层动作。

## 今天确认的赛题消息

- 官网 Rules/Docs 已切到 **v4** 任务卡叙述：练习卡为 α/β/γ/δ，正式卡为 A/B/C/D，隐藏卡为 E/F/G/H。
- v4 动作包含 `observe`、`wait`、`report`、`finish`；`observe` 涉及望远镜指向、光纤目标分配、60–3600 秒曝光和 DARK/BRIGHT/BACKUP 程序。
- v4 约束和评分还涉及光纤 cell 合法性、至少 30° 高度角、sky quality、程序 bonus、最佳曝光、required targets、RA 分带均匀覆盖、故障报告和限时请求。
- 正式阶段只收完整项目；每批运行 A、B、C、D 四张卡，每张卡 900 秒，每队每天 10 批。线上结束后，主办方在隐藏 E、F、G、H 上各运行最终版本一次，最终成绩取四张隐藏卡成绩的算术平均；这些仍不是本地公开 v3 的可验证结果。
- 公开 `current_competition` 仍为 `practice`；登录态 prepare、v4 云端 smoke、正式 A–D 和隐藏成绩仍未验证。

## 对团队方案的建议

1. 先做一份 v3/v4 差异表，确认 `initialize`、fiber config、target schema 和 action schema；不要把旧公开练习器直接宣传为 v4 兼容。
2. 保留公开 v3 场景作为回归基线，同时建立最小 v4 协议 harness，先测动作合法性、光纤分配、曝光范围和结束流程。
3. 继续采用“高层计划 + 确定性校验 + 快速回退”的结构；模型负责排序/建议，最终动作必须由本地约束层生成或放行。
4. 调度特征优先加入目标类别/亮度、airmass/高度角、seeing、透明度、月光/天光背景、曝光收益、overhead、required/request 和覆盖均匀性。
5. 在真实 key 或云端评测前先完成登录态 Participate prepare/interface check；正式和隐藏结果在验证前不要写进宣传或 README。

## 模拟器当前进展（2026-10-02）

应用仓库已把讲座中的可验证调度启发式落到公开 v3 selector：在不改变官方 scorer 和 JSONL 协议的前提下，确定性排序会把指向/读出 overhead、目标类别与天气质量匹配、高度角、airmass 和窗口余量纳入规划；它仍不会生成 v4 光纤动作。

- `dev-fortnight` 本机完整运行：`survey_complete`，593 个动作，总分 `6526.194418`。
- `dev-reference` 本机完整运行：`survey_complete`，7943 个动作，总分 `12298.666905`。
- 单元测试 4 项与 `compileall` 通过；上述结果只是仓库内公开 v3 回归证据，不代表正式 A–D 或隐藏 E–H 成绩。
- 对应实现和回归脚本在[模拟器仓库](https://github.com/KennyMcSimpson/gosim-agentic-observer)；团队仓库继续只保存去敏摘要和协作信息，不复制安装包、密钥或私人实验数据。

## 公开材料边界

本次只共享去敏后的摘要和迁移建议，不共享讲座视频、自动转写原始文件、私人实验日志、密码、访问令牌或模型 API key。旧 v3 smoke 和公开分数只能作为本地回归证据，不能作为正式赛或隐藏场景成绩。

## 公开来源

- [赛事 Rules/Docs](https://create.gosim.org/survey26/platform/rules)
- [赛事公告](https://create.gosim.org/survey26/platform/announcements)
- [本地模拟器仓库](https://github.com/KennyMcSimpson/gosim-agentic-observer)
