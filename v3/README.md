# v3 科学收益修正版

2026-10-05。基于朋友已合并的 [PR #2](https://github.com/KennyMcSimpson/gosim-2026-team/pull/2)
及 [v2 集中版](../v2(jmk)/concentrated-20261005/README.md)，根据 Kenny 提供的 v1
平台回执修正观测策略。目标是改善正式题科学收益与请求调度；是否增分由下一轮
平台结果确认。

- [下载 v3.zip](v3.zip)：直接作为完整项目上传。
- [查看对应源码](project/)及[包内中文说明](project/README.zh.md)。
- [验证摘要](VALIDATION.json)及[SHA-256 校验文件](v3.zip.sha256)。

`v3.zip` 是此前交付的 `gosim-science-repair-20261005.zip` 改名后的同一份文件，
没有重新打包。51,039 字节、20 个文件；`project/` 与 ZIP 解压内容逐字节一致。
本页、验证摘要和校验文件不进入上传包。包内日期标题是打包时记录，本目录的
发布版本名为 **v3**。

## 相对 v2 更新

| 更新 | 具体行为 | 主要源码 |
| --- | --- | --- |
| 单次曝光科学收益 | 官方取每个目标的有效单次曝光最高分；收益率相差不超过 3% 时，优先收益更完整的一次曝光，减少重复短曝光碎片。 | [planner_search.py](project/agent_core/planner_search.py) |
| 视场召回 | 同时考虑可实现科学收益与空间密度，增加多目标共同受益的指向候选。 | [planner_search.py](project/agent_core/planner_search.py) |
| 曝光级反馈 | 一次曝光的多根光纤先聚合，再更新天气、方向、program-band 和故障证据，避免 100 根光纤被当成 100 次独立观测。 | [state.py](project/agent_core/state.py) |
| 仪器效率与 program | 分开估计含仪器效率的曝光质量和不含效率的 program-band 质量；从公开反馈更新效率，替代固定 0.95 除数。 | [planner_search.py](project/agent_core/planner_search.py)、[state.py](project/agent_core/state.py) |
| 修复与失效恢复 | report/resync 后清除过期质量与故障推断；保留有效曝光账本并重新规划。 | [state.py](project/agent_core/state.py)、[planner.py](project/agent_core/planner.py) |
| 方向屏蔽衰减 | 零分方向的经验屏蔽限时保存，公告变化时清除，避免永久放弃可恢复的天区。 | [planner.py](project/agent_core/planner.py) |
| 请求目标召回 | 每个活跃请求优先召回一个可见且预计能达门槛的目标，在光纤 top-k 和指向候选截断时保留，解决低科学权重请求目标被挤掉的问题。 | [planner_search.py](project/agent_core/planner_search.py) |
| 两步请求规划 | 第二步临时扣除第一步预计完成的目标，搜索剩余目标；返回时恢复视图，真实进度只由平台反馈推进。快速档保留有限两步搜索。 | [planner_search.py](project/agent_core/planner_search.py) |
| 请求时长与快速档 | 将各请求门槛都纳入时长候选；缺省剩余数量从公开完成要求推导；快速档增加中心、角点和四象限代表光纤。 | [planner_search.py](project/agent_core/planner_search.py) |

保留 v2 的独立 science/completion 账本、作废回滚、REQUIRED 未来机会、均匀性
和 CPU 档位，以及异步公告解释与反馈调整。策略不按卡名、月份、固定 RA 或
目标编号硬编码。附件只用于定位风险，附件分数没有作为新正式成绩。

## 上传与继续开发

上传 **本页的 v3.zip**。包根目录含 `observer.project.json`，协议 `jsonl-v4`；
平台使用 `python:3.12-slim` 运行 `python3 -u agent.py`。只用标准库，支持合法
平方光纤配置，包括 9/16/25/100。

平台模型设置沿用原版：`OPENAI_API_KEY` 或 `KIMI_API_KEY`、`OPENAI_BASE_URL`、
`OPENAI_MODEL` 及对应联网域名。默认仍为 Kimi Coding 和 `k3`。启动需要有效
运行时 key；key 在平台设置，不能写入文件。没有免 key 模式。仓库根目录是
版本归档导航，直接使用此 ZIP 可以确保平台拿到 v3 的 manifest 与源码。

修改源码后，在 `project/` 下重新打包并重新验证：

```bash
python3 pack_agent.py --out ../v3.zip
```

重新打包可能因文件时间戳改变 ZIP 哈希。提交新版时同步更新本页、校验文件和
验证摘要；原 `v3.zip` 的验证结果不能自动沿用到修改后的包。

## 验证范围

- 13 项既有合成契约检查及 5 项科学收益修复行为检查通过。
- 请求竞争回归覆盖正常/快速档、过期/不可达控制、重叠请求和不同天区两步规划；同一召回测试在旧 v2 因 `request plan missing at fast level 0` 失败，新版通过。
- 四种公开光纤配置的协议、官方动作合法性、零偏移几何命中、合成 resync 和 localhost 两个模型职责通过。
- 用户旧回执每卡 5 个样本的反馈回放为 `REPLAY_OK`；只在对应动作之后使用公开 score 反馈，没有读取 truth 或向策略提供 quality/factor。
- 最终 ZIP 逐文件匹配已验证源码，解压后四配置协议复验通过；不含凭据、题目输入、truth、日志或缓存。

这版改变了观测策略。上述检查不证明线上增分、隐藏泛化、真实模型可用性或
平台整季 CPU 预算，**没有产生新正式成绩，也没有宣称 SOTA**。效率估计、
天气质量和有限两步收益仍为启发式近似。

下一轮重点对比 `sum_best_scores`、总分、请求完成、REQUIRED 遗漏、作废数量、
结束夜次及终止原因。回放估计分不能代替平台分数。

## 来源与校验

朋友基线提交：`8792dd2fc15fdaacf131a47c17ddb32594f3e63a`。v2 发布提交：
`9b75f7d0a942f5c7306e83781711a19dd3548148`；目录整理已合并于 `0658278`。
保留 [v1 源码](../v1(lhl)/python-agent-baseline-unmodified/)和 [v2 源码](../v2(jmk)/concentrated-20261005/project/)。

ZIP SHA-256：`e97e22b58bac079c7fa476fab8256e27ab9a765a2cedd6a8ec318e26bbf922f0`。

派生自 GOSIM 2026 官方 Python 示例及 team PR #2。官方示例按
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 提供，见
[官方 LICENSE.md](../training/official-examples/gosim-observer-examples/LICENSE.md)。
