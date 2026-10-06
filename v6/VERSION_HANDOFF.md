# GOSIM Agent Observer 版本接手说明

更新时间：2026-10-06（北京时间）

本文供后续大模型或开发者接手使用。上传代码位于
`C:/Users/HP/Documents/Harry/gosim-2026-team-main/v6/project/`；v5 和
`v4-official` 均保留在原位置，未被本轮修改。

## 版本关系

```text
v4-official
    │ 复制并实施 REQUIRED、请求、state_resync、LLM 证据链改进
    ▼
v5
    │ 复制并实施 med_k、urgency 整形、best-effort 降级
    ▼
v6（当前交付版本）
```

协议和运行入口没有改变：JSONL-v4、`python3 -u agent.py`、标准库 Python、
`python:3.12-slim`。不要把版本号 v5/v6 与官方协议字符串
`participant-agent-protocol-v4` 混淆。

## v4-official → v5

### 1. REQUIRED 目标调度

v5 在 `agent_core/calendar.py` 和 `planner_search.py` 中加入公开几何可行窗口、
最晚可行夜晚和最短达标曝光的优先级。接近最后可行窗口的 REQUIRED 目标会进入
专属召回通道，避免被普通科学目标的粗排序淘汰。

相关位置：

- `project/agent_core/calendar.py::RequiredCalendar`
- `project/agent_core/planner_search.py::_value`、REQUIRED anchor 搜索

### 2. 请求调度

活动请求按照 deadline/reward 排序，最多保留有界的请求锚点。每个请求目标会加入
达到 completion threshold 所需的最短合法曝光，避免 2400/3600 秒通用曝光吞掉
请求剩余窗口。

请求仍使用保守的 `low_k` 完成估计；真实完成状态只能由平台反馈更新。

相关位置：

- `project/agent_core/planner_search.py::_request_views`
- `::_duration_candidates`
- 请求 anchor 和 `_two_step` lookahead

### 3. Hard mode `state_resync` 恢复

收到 `state_resync` 后，v5 会重建恢复队列，把失效 REQUIRED 目标和未完成请求目标
重新加入活动目录，在候选裁剪前提高其优先级，并写入 `data_loss_recovery` trace。

相关位置：

- `project/agent_core/state.py::refresh_recovery_queue`
- `project/agent_core/state.py::recovery_target_indices`
- `project/agent_core/planner.py::decide`

### 4. LLM 证据链

公告解释和反馈适应仍在后台线程执行。v5 增加以下 trace 事件：

- `llm_submitted`
- `llm_result`
- `llm_applied`
- `llm_fallback`
- `llm_skipped`

成功的 `notice_interpretation` 会把 `state.extra_avoid` 从旧值更新为解析结果，
并记录 `state_before`、`state_after`、`changed`、`fresh`、`succeeded`、`parsed`
及决策消费者 `Planner._direction_factor`。

相关位置：

- `project/agent_core/advisor.py`
- `project/agent_core/llm_client.py`
- `project/agent_core/planner.py::_direction_factor`

## v5 → v6

### A. REQUIRED 使用中位数质量估计

v5 原先用 `low_k` 同时判断科学保守收益和 REQUIRED 是否达到门限。v6 在
`planner_search.py::_item` 中增加：

```python
"med_k": k * scales[1] * min(1.0, direction)
```

REQUIRED 的完成收益、REQUIRED duration boundary 和 REQUIRED anchor 的最短曝光
改用 `med_k`；请求 completion、请求 anchor、uniformity 等仍使用 `low_k`，避免
把请求完成判断过度乐观化。

注意：`med_k` 是规划估计，不会直接修改 `state.factor`，也不会把目标标记为已完成。
真实进度仍只由平台回执更新。

相关位置：

- `project/agent_core/planner_search.py::_item`
- `::_target_gain`
- `::_duration_candidates`
- `::_search` 中 REQUIRED anchor 的最短曝光
- `project/agent_core/planner.py::_target_gain`

### B. REQUIRED urgency 整形

`calendar.py::RequiredCalendar.urgency` 改为按未来公开几何/质量可行机会数计算：

- 已到达或错过最后可行夜晚：`penalty * 16.0`
- 未来机会数不少于 6：只保留 `penalty * 0.25` 的小提示
- 机会数越少，优先级越接近最后窗口的高值

这不是天气预测。“future”表示公开几何和质量模型推算的机会数，未来天气仍未知。
`6.0`、`0.25`、`11.75` 是可调参数，修改时应重新比较 REQUIRED 漏项和科学分。

### C. best-effort 降级与空转诊断

v6 的搜索会保留已经完成合法几何和动作评估的最佳 science 布局。如果正常 utility
筛选后没有可选计划，但已有合法布局且 science/valid 均为正，则执行该布局：

- action 的 `decision_source` 为 `best-effort`
- trace 写入 `best_effort_fallback`
- 不额外增加搜索量

每次搜索还会写入 `search_summary`，区分：

- `too_little_time`
- `no_visible_candidates`
- `no_pointings`
- `utility_filtered`
- `deadline_cutoff`
- `legal_layouts`
- `plans_kept`

best-effort 不能解决“根本没有可见目标/合法 pointing”的等待；它主要覆盖已经算出
合法布局、但 utility 选择为空的情况。

相关位置：

- `project/agent_core/planner_search.py::_joint_pointing`
- `::_search`
- `::_two_step`
- `::plan`

## 当前验证证据

使用 Python 3.12 runtime 已通过：

- `project/` 和 `results/` 全量编译
- `results/run_v6_regressions.py`
- `results/run_llm_http_smoke.py`
- `results/run_protocol_smoke.py`

重点回归结果：

- REQUIRED：`low_k * duration = 0.3336`，`med_k * duration = 0.5004` 时，
  REQUIRED 仍生成门限候选，示例候选时长为 835 秒。
- 请求：仍保留使用 `low_k` 的 126 秒门限候选，最短曝光回归仍为 60 秒。
- best-effort：生成 `decision_source="best-effort"`，并写入
  `search_summary`、`best_effort_fallback`。
- LLM：本地 OpenAI-compatible stub 走通 HTTP、`llm_result` 和 `llm_applied`，
  `state.extra_avoid` 成功从空集合变为 `{"S"}`。

证据文件：

- `results/regression_summary.json`
- `results/llm_http_smoke.json`
- `results/protocol_smoke.json`
- `results/protocol_smoke_llm_trace.jsonl`
- `VALIDATION.json`

## 打包与接手检查

重新打包：

```text
python3 project/pack_agent.py --out v6.zip
```

打包脚本只收集 `project/` 的 20 个运行文件，不包含 `results/`、凭据、`.env`、
测试缓存或 ZIP 自身。修改代码后应重新生成 `v6.zip`、`v6.zip.sha256`，并同步
`VALIDATION.json` 的 ZIP 大小、文件数和哈希。

后续大模型开始工作前，建议先检查：

1. 当前目录是否仍是 `v6/`，不要误改 `v5/` 或 `v4-official/`。
2. 是否保留 JSONL-v4 入口和 action validation 契约。
3. 是否把 request 的 `low_k` 与 REQUIRED 的 `med_k` 混用了。
4. 是否把合成回归或本地 stub 当成正式平台分数。
5. 是否比较了 REQUIRED 漏项、science、CPU、等待次数和正式终止原因。

## 未验证边界

当前没有新的正式平台分数、hidden truth、真实线上模型调用或榜单提升证据。此前
Windows local runner 曾因 `WinError 10093` 在有效决策前失败；该输出不能作为成绩。
v6 的最终收益必须通过正式平台或完整可用的官方 local runner 重新验证。
