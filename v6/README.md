# v6：官方 Python 架构改进版

2026-10-06。此目录从 v5 复制后独立修改；v5 与 `v4-official` 均不作为本次修改目标。

- [上传包 v6.zip](v6.zip)
- [源码](project/)
- [中文运行与改动说明](project/README.zh.md)
- [验证清单](VALIDATION.json)
- [合成回归与 trace](results/)

## v5 基础改进

1. REQUIRED 目标按公开几何可行窗口、最晚可见夜晚和最短达标曝光排序；临近最后窗口的目标进入粗筛优先队列。
2. 活动请求按 deadline 和 reward 排序，保留有界的多目标召回；每个目标把达到 completion threshold 的最短合法曝光加入候选，避免长曝光吞掉请求窗口。
3. Hard-mode `state_resync` 后重建恢复队列，重新召回失效 REQUIRED 和未完成请求目标，并在 trace 中记录事件、失效目标和恢复目标。
4. LLM 只在后台线程调用；公告解释成功后会改变 `state.extra_avoid`，trace 同时记录 `llm_submitted`、`llm_result`、`llm_applied`、`llm_fallback` 和 `llm_skipped`，包含成功、解析、是否应用、是否改变状态以及决策消费者。

## v6 改进

- REQUIRED 完成估计、时长边界和专属召回使用中位数质量估计；请求完成门限仍使用保守低位估计，实际进度仍只由平台回执更新。
- REQUIRED urgency 根据未来公开可行机会数整形；这是公开几何/质量机会估计，不代表未知天气。
- 搜索保留已评估的最佳科学布局作为 best-effort 后备，不增加搜索量；`search_summary` 诊断空候选、无 pointing、效用过滤和搜索截止。

模型仍通过 Python `urllib` 调用，数值搜索、状态账本、几何和动作验证由 Python 负责。模型失败、超时或上下文过期时，确定性规划继续运行。凭据只从运行时环境读取，不进入源码或 ZIP。

## 验证边界

`results/regression_summary.json` 通过 LLM 成功应用/超时回退、请求短曝光与召回、REQUIRED 召回、`state_resync`、median REQUIRED 门限、urgency 曲线和 best-effort 回归。`results/llm_http_smoke.json` 通过本地 OpenAI-compatible stub 走通 HTTP 客户端；它不是线上模型证据。`results/protocol_smoke.json` 验证官方 JSONL-v4 stdout 协议。Windows 官方 local runner 的既有 `WinError 10093` 失败记录不是有效分数。

没有新的正式平台评分、真实模型调用或 hidden truth 结果。上传前运行：

```text
python3 pack_agent.py --out ../v6.zip
```

官方示例许可证：`../training/official-examples/gosim-observer-examples/LICENSE.md`。

