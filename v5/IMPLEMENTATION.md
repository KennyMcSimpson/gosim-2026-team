# v5 官方 Python 架构版交付记录

2026-10-06，北京时间。本版从 `v4-official` 复制到独立的 `v5/` 目录后修改；
`v4-official`、`v2(jmk)` 和其他旧版本没有作为修改目标。最终上传包为
`v5.zip`，文件数、字节数和 SHA-256 见 `VALIDATION.json` 与 `v5.zip.sha256`。

## 版本身份

v5 保留官方 JSONL-v4 Python agent 入口、`python:3.12-slim` 镜像、空 build 和
标准库运行时。没有 Pi、Node、候选选择桥接、运行时下载或上传凭据。模型角色仍
通过 Python HTTP 异步调用，数值搜索、状态账本、几何和动作验证由 Python 负责。

## 四项优先改进

1. REQUIRED 目标按公开几何可行窗口、最晚可见夜晚和最短达标曝光排序；接近最后
   可行窗口时进入粗筛优先队列，降低漏掉 REQUIRED 的风险。
2. 活动请求按 deadline/reward 排序，保留有界的多目标锚点；每个目标加入达到
   completion threshold 所需的最短合法曝光，避免通用长曝光吞掉请求窗口。
3. Hard-mode `state_resync` 后重建恢复队列，把失效 REQUIRED 和未完成请求目标在
   搜索裁剪前重新加入活动目录，并写入 `data_loss_recovery` trace。
4. LLM 证据链显式区分 `llm_submitted`、`llm_result`、`llm_applied`、
   `llm_fallback` 和 `llm_skipped`。成功的 `notice_interpretation` 会实际改变
   `state.extra_avoid`，并在 `llm_applied` 中记录状态前后值、`changed`、`fresh`、
   `succeeded`、`parsed` 和决策消费者 `Planner._direction_factor`。

## 已完成验证

- `project/` 全部 Python 文件通过 Python 3.12 `compileall`。
- `results/run_v5_regressions.py` 通过：LLM 公告解析并将 `extra_avoid` 从空集合改为
  `S`，超时回退有明确记录；请求最短曝光为 `[60, 100]`；请求、REQUIRED 和
  `state_resync` 恢复目标均被召回。
- `results/run_protocol_smoke.py` 通过：入口返回一条合法 JSONL-v4
   `decision_response`，进程返回码为 0。
- `results/run_llm_http_smoke.py` 通过本地 OpenAI-compatible stub 走通标准库 HTTP
  客户端；`llm_result` 为 success，随后 `llm_applied` 将 `extra_avoid` 从空集合改为
  `S`。这是调用链证据，不是线上模型质量或正式分数。
- 已检查 `v4-official` 工作树没有本轮改动。

## 证据边界

合成回归和协议 smoke 是本地工程证据，不等于平台分数。Windows 官方 local
runner 曾因 `WinError 10093` 在 agent 初始化前失败，`results/local_L1/` 中的
`agent_error` 不能作为有效成绩。当前没有新的正式平台评分、真实模型调用、hidden
truth 或榜单提升证据；正式平台 CPU/墙钟预算和整季完成情况仍需上传后验证。

模型失败、超时、过期上下文和调用上限均回退到确定性规划；短 REQUIRED 曝光仍可能
增加决策次数，这是线上评测需要重点观察的残余风险。

重新打包命令：

```text
python3 project/pack_agent.py --out v5.zip
```

官方示例许可证位于仓库训练目录；测试输入、truth、凭据和诊断结果不进入上传 ZIP。
