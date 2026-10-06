# v5：官方 Python 架构改进版

2026-10-06。此目录是 `v4-official` 的独立副本；原目录没有被修改。

- [上传包 v5.zip](v5.zip)
- [源码](project/)
- [中文运行与改动说明](project/README.zh.md)
- [验证清单](VALIDATION.json)
- [合成回归与 trace](results/)

## 四项优先改进

1. REQUIRED 目标按公开几何可行窗口、最晚可见夜晚和最短达标曝光排序；临近最后窗口的目标进入粗筛优先队列。
2. 活动请求按 deadline 和 reward 排序，保留有界的多目标召回；每个目标把达到 completion threshold 的最短合法曝光加入候选，避免长曝光吞掉请求窗口。
3. Hard-mode `state_resync` 后重建恢复队列，重新召回失效 REQUIRED 和未完成请求目标，并在 trace 中记录事件、失效目标和恢复目标。
4. LLM 只在后台线程调用；公告解释成功后会改变 `state.extra_avoid`，trace 同时记录 `llm_submitted`、`llm_result`、`llm_applied`、`llm_fallback` 和 `llm_skipped`，包含成功、解析、是否应用、是否改变状态以及决策消费者。

模型仍通过 Python `urllib` 调用，数值搜索、状态账本、几何和动作验证由 Python 负责。模型失败、超时或上下文过期时，确定性规划继续运行。凭据只从运行时环境读取，不进入源码或 ZIP。

## 验证边界

`results/regression_summary.json` 已通过 LLM 成功应用、LLM 超时回退、请求短曝光、请求目标召回、REQUIRED 召回和 `state_resync` 合成检查。`results/llm_http_smoke.json` 还通过本地 OpenAI-compatible stub 走通了真实 HTTP 客户端、`llm_result` 和 `llm_applied` 链路；它不是线上模型或正式评分证据。`results/protocol_smoke.json` 验证官方 JSONL-v4 stdout 协议；Windows 官方 local runner 另有 `WinError 10093`，因此没有把它的失败输出当作有效分数。

没有新的正式平台评分、真实模型调用或 hidden truth 结果。上传前运行：

```text
python3 pack_agent.py --out ../v5.zip
```

官方示例许可证：`../training/official-examples/gosim-observer-examples/LICENSE.md`。
