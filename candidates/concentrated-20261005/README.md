# 集中升级版 — 2026-10-05

在朋友已合并的 [PR #2](https://github.com/KennyMcSimpson/gosim-2026-team/pull/2) 上做的一版集中升级，用于下一次正式题线上试验。目标是在平台时间预算内完成更多观测，同时改善请求完成、REQUIRED 漏项和作废后的重新规划。是否提高分数仍待线上结果。

- [下载可上传 ZIP](gosim-concentrated-20261005.zip)
- [查看对应源码](project/)
- [包内中文使用说明](project/README.zh.md)
- [SHA-256 校验文件](gosim-concentrated-20261005.zip.sha256)

ZIP 和源码逐文件一致，沿用已经交付给 Kenny 的同一份包：41,723 bytes、20 个文件。此目录外的说明和校验文件不进入上传包。包内 CHANGELOG 记录的是最初打包时的状态；本页记录后续 team 仓库分享。

## 相对朋友版更新了什么

| 更新 | 具体行为和目的 | 主要源码 |
| --- | --- | --- |
| 联合观测搜索 | 共同比较指向、光纤分配、DARK/BRIGHT/BACKUP 和曝光时长，增加达到目标完成阈值的候选时长。 | [planner_search.py](project/agent_core/planner_search.py)、[planner.py](project/agent_core/planner.py) |
| 请求完成奖励 | 完整曝光必须落在发布与截止窗口内；同一请求的完成奖励只计一次，并重新启用此前饱和或被剪掉的请求目标。 | [planner_search.py](project/agent_core/planner_search.py)、[state.py](project/agent_core/state.py) |
| 有限两步估计 | 估计下一步机会时保守扣除重复目标收益，只执行当前第一步，收到真实回执后重新规划。 | [planner_search.py](project/agent_core/planner_search.py) |
| REQUIRED 截止风险 | 用公开未来几何、月光机会和同一时窗的时长/质量估计延期风险；机会表构建次数有上限。 | [calendar.py](project/agent_core/calendar.py)、[planner.py](project/agent_core/planner.py) |
| 作废后账本恢复 | 独立保存 science 最大分和 completion 保守下界；先接结果，再按官方作废窗口回滚。对六位小数回执保守处理，避免把已作废完成度继续用于排序。 | [state.py](project/agent_core/state.py) |
| 质量与风险反馈 | 分开积累方向质量、显式作废风险和 miss，正命中清除 miss；用观测回执更新后续估计。 | [state.py](project/agent_core/state.py)、[planner.py](project/agent_core/planner.py) |
| CPU 时间预算 | 缓存 RA 分区均匀性和空间索引，限制搜索/机会表构建；按剩余 CPU 调整档位并允许恢复。目的是在上传容器的每卡 900 标准化 CPU 秒预算内跑完整季。 | [clock.py](project/agent_core/clock.py)、[geometry.py](project/agent_core/geometry.py)、[planner_search.py](project/agent_core/planner_search.py) |
| 异步模型职责 | 公告解释和反馈调整改为两个异步职责；应用结果前核对公告来源或反馈 context，拒绝过期结果，满队列跳过提交。 | [advisor.py](project/agent_core/advisor.py)、[llm_client.py](project/agent_core/llm_client.py)、[planner.py](project/agent_core/planner.py) |
| 打包边界 | 使用明确的源码/文档 allowlist，排除凭据、题目输入、truth、测试、日志和缓存。 | [pack_agent.py](project/pack_agent.py) |

这版改变了收益排序、规划和剪枝，不能视为无损速度补丁。未来质量/天气和两步收益均为启发式近似，没有用正式分数调参。模型整季最多 28 个规划问题、64 次 HTTP 尝试；单次超时 8 秒、单问题预算 18 秒，末尾保留 300 秒墙钟。

## 使用方式

将本页 ZIP 作为完整项目上传。ZIP 根目录含 `observer.project.json`，协议为 `jsonl-v4`，平台使用 `python:3.12-slim` 执行 `python3 -u agent.py`。仅用 Python 标准库，无第三方依赖或 GPU 要求；支持 9/16/25/100 等合法平方光纤网格。

平台模型配置沿用朋友版：`OPENAI_API_KEY` 或 `KIMI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 和对应联网域名。默认接口为 Kimi Coding、模型 `k3`。本版启动需要 key；无 key 会退出，也未实现 `OBSERVER_MODEL_DISABLED=1` 的免 key 模式。key 只在平台运行时配置，不写入仓库或 ZIP。

继续开发或重新打包时，在 `project/` 目录执行：

```bash
python3 pack_agent.py --out ../gosim-concentrated-20261005.zip
```

重新生成的 ZIP 可能因时间戳而有不同的整体哈希；修改源码后应重新验证并更新校验文件。

## 已验证与待验证

- 编译通过，13 项合成逻辑检查全部通过，涵盖回滚、六位小数账本、请求窗口/奖励、重新激活、缓存、光纤 seam、模型 freshness/队列/预算。
- 从实际 ZIP 解压后，9/16/25/100 四种光纤配置各产生 5 个 `decision_response`，通过官方动作归一化及零偏移光纤几何检查；localhost 模拟的两个模型职责均解析并应用。
- 逐文件哈希匹配交付源码，包内没有凭据、题目数据、truth、测试输入、日志或缓存。
- 没有运行练习或正式评分，没有调用真实模型服务；平台整季 CPU 用量、真实模型可用性、线上/隐藏分数和 SOTA 均未验证。较早本地 CPU 小量诊断不能代表最终 ZIP 的平台速度。

线上试验后，重点对照总分/分项、运行结束夜次、CPU 与墙钟终止原因、请求完成数量和 REQUIRED 遗漏，再决定下一次改动。

## 来源与版本

基线 main：`8792dd2fc15fdaacf131a47c17ddb32594f3e63a`；基线策略子树：`ad3144f51760a2ae2d6b19a98918bbb50048fdd9`。原策略保留在 [baselines/python-agent-baseline-unmodified](../../baselines/python-agent-baseline-unmodified/)，目录名沿用历史命名，PR #2 已修改其中策略。

ZIP SHA-256：`648aa05c46d44a7f10de5dfcc6ae735015e5a078342823b2990e0ee1176ff349`。

本策略派生自 GOSIM 2026 Agentic Observer 官方 Python 示例及 team PR #2。官方示例按 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 提供；见仓库保留的[官方 LICENSE.md](../../training/official-examples/gosim-observer-examples/LICENSE.md)。
