# 巡天智能体 v6：官方 Python 架构改进版

2026-10-06。本版从 v5 复制到独立的 `v6` 目录后修改；
`v4-official`、`v2(jmk)` 和其他旧版本均未修改。

本版完全不使用 Pi 或 Node，也不含候选选择桥接、子进程模型 worker、运行时下载
或依赖安装。`observer.project.json` 的 `build` 为空；入口为
`python3 -u agent.py`，镜像 `python:3.12-slim`，仅使用 Python 标准库。
平台旧的 `AGENT_MODEL_BACKEND`、`PI_ENABLED` 对本版没有作用。

模型直接通过 Python HTTP 调用，保留两个异步环节：公告解释和基于回执的
优先级/风险建议。Python 负责数值搜索、状态账本、几何和动作验证；过期建议
不会应用，模型失败后仍继续确定性规划。运行时沿用队伍的 `OPENAI_BASE_URL`、
`OPENAI_MODEL`、`OPENAI_API_KEY` 或 `KIMI_API_KEY`；凭据不写入文件。

## 四项优先修复

- 缓存静态三角函数、空间分组和天球向量，减少重复计算。
- REQUIRED 按公开几何可行窗口和最晚可见夜晚排序，临近最后窗口时提高粗筛优先级。
- 活动请求按 deadline/reward 排序，最多保留八个请求锚点，并保留达到 threshold 的最短合法曝光。
- Hard-mode `state_resync` 后恢复失效 REQUIRED 和未完成请求目标，恢复目标在搜索裁剪前重新加入活动目录。
- LLM 公告解释在成功时实际更新 `state.extra_avoid`；trace 明确区分 submitted、result、applied、fallback 和 skipped，并记录状态前后值和决策消费者。
- 决策机会成本使用正确秒数单位，曝光排序同时考虑额外规划成本。
- 已达到最高科学分的目标仍可被新请求召回，科学收益为零但能推进请求的布局仍可保留。
- 虚拟请求序列最多八步，只执行第一步，真实账本只由平台反馈更新。
- 保留单次最佳科学分、曝光级反馈聚合、REQUIRED 日历、均匀性及失效回滚。

## v6 REQUIRED 与空转改进

- REQUIRED 的预计完成门限、时长边界和专属召回改用中位数质量估计；请求完成仍使用保守低位估计，且真实进度只由平台回执更新。
- REQUIRED urgency 按未来公开几何/质量可行机会数整形；天气不确定，因此这些不是实际天气机会。
- 保留已评估的最佳有效科学布局作为无正效用计划时的 best-effort 后备，不增加搜索量；trace 记录 `search_summary` 与 `best_effort_fallback`。统计可区分无可见目标、无 pointing、效用过滤和搜索截止。

## 验证边界

`../results/regression_summary.json` 记录 LLM 成功/回退、请求低位门限、REQUIRED
中位数门限、urgency 曲线、best-effort、目标召回和 `state_resync`；
`../results/protocol_smoke.json` 记录 JSONL-v4 入口响应。
官方 Windows local runner 因 `WinError 10093` 未形成有效分数，本地分数也不代替正式评分。
本包不含测试输入、正式题数据、truth 或凭据。

v3 的正式回执已证实 A/B/C 提前耗尽 CPU；v4 的整季平台预算和正式增分仍未验证。
短 REQUIRED 曝光仍可能导致大量决策。本地毫秒耗时、预测收益和合成模型通过
不能证明 SOTA。线上先检查整季是否完成，再比较分项。

将 `v6.zip` 作为完整项目上传。重新打包：
`python3 pack_agent.py --out ../v6.zip`。
