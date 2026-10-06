# v7 merge-pro

2026-10-07。完整候选基于官方 Python Pro 提交
`ab27e5ef32f0a834b054d855cf9706b44502204b`。

[下载 ZIP](v7-merge-pro.zip) · [最终方案](PLAN.md) ·
[Week4.2 调研](RESEARCH.md) · [源码](project/) ·
[工程验证回执](verification.json)

## 运行与测试

向赛事平台上传 `v7-merge-pro.zip` 作为完整项目。
ZIP 根目录包含 `observer.project.json`，入口为 `python3 -u agent.py`，
使用 JSONL-v4，只依赖 Python 标准库。

模型配置由平台运行时环境变量提供：`OPENAI_API_KEY`（或 `KIMI_API_KEY`）、
`OPENAI_BASE_URL`、`OPENAI_MODEL`。默认服务为 Kimi Coding，模型 `k3`；
可以设置为其他兼容接口。没有 key 或 `OBSERVER_MODEL_DISABLED=1` 时，
数值规划、结果适配和短窗仍运行。真实凭据不能放入 ZIP 或 GitHub。

默认模型调用次数不限；单问题截止、单次等待、重试和有界队列用于防阻塞。
官方 CPU/墙钟约束仍按实际 payload 执行。
`V7_OPERATIONS_APPLY=0`、`V7_GAIN_ENABLED=0`、`V7_HORIZON_ENABLED=0`
可分别关闭 Operations 应用、science 校准和短窗，供同源码消融。

在 `project` 目录检查合同测试：

```bash
python -m unittest discover -s tests -v
```

## 实现与证据

规划主干保留 Pro 的联合几何搜索、program band、指向偏差学习和 CPU pacing；
修正 REQUIRED 配置、完整 request reward 与状态恢复。
Agent 的 night plan、fault review 和 Operations 三类职责通过校验后进入规划条件，
仪器留言进入统一 report 证据门。science 校准只根据公开 best-max 增量更新，
有条件支持、预序损失门与限幅；正分比例仍仅作诊断。
两步短窗有即时收益底线和 CPU 截止。详见 [PLAN.md](PLAN.md)。

根代理已验证 56 项测试通过，并从实际 ZIP 解压检查
9/16/25/100 光纤配置的 initialize、decision 和 finish 审计。
网络与模型测试均为 mock。真实模型调用、评分轨迹和平台上传均为 0，
这是工程验证，不证明提分、超过官方 Pro 或解决过拟合。

本 ZIP 与本地交付包逐字节相同，59,938 bytes，只含九个运行文件。
源码与 ZIP 逐文件哈希见 [verification.json](verification.json)。

```text
ZIP SHA-256:
c995cb8fff57f7fe3fb6b7086658a98b5dc758981c05f25903518c1409e03428

Runtime source set SHA-256:
f76cc7852722c94c4f97a52f68abb1d9ed2e75f6d6e63cfb985fcfa09c4be0f1
```

[RESEARCH.md](RESEARCH.md) 是独立调研时的快照，最终实现与额度决策以
[PLAN.md](PLAN.md) 和当前源码为准；历史研究中的未实施描述不代表 v7 当前状态。
该目录的 GitHub 发布不触发赛事平台上传或评分。

## 来源

官方来源：[GOSIM 2026 Agentic Observer Hackathon](https://github.com/gosimfoundation/hackathon-survey26)。
沿用 [CC BY-NC 4.0 许可](LICENSE.md)。
