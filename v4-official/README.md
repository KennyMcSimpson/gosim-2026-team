# v4-official：官方 Python 架构修复版

2026-10-06。**本次推荐试跑版本，不使用 Pi。**

- [下载 v4.zip，完整项目上传](v4.zip)
- [源码](project/)
- [中文运行与修复说明](project/README.zh.md)
- [验证摘要、归档与源码哈希](VALIDATION.json)
- [三个版本的失败原因与修复](../FAILURE_ANALYSIS.md)

基于官方 JSONL-v4 Python 架构及朋友策略，保留 recovery 的预算与请求修复。
`python:3.12-slim`，空 `build`，`python3 -u agent.py`，仅标准库。
没有 Pi、Node、模型 worker、运行时下载或候选桥接。

模型直接 HTTP 调用，保留公告解释与反馈建议两个异步环节。沿用队伍的
`OPENAI_BASE_URL`、`OPENAI_MODEL` 及运行时 key。旧 `AGENT_MODEL_BACKEND`、
`PI_ENABLED` 开关对本版没有作用；不要把凭据写入文件。

修复 CPU 搜索节奏、决策成本单位、饱和目标请求召回和部分请求过滤，完整
虚拟请求序列最多八步。真实账本仍只由回执更新。

源码与最终 ZIP 解压验证通过。没有新正式成绩；整季平台预算、真实模型效果
和榜单提升待实测。原 v3 与 v4-pi 均保留用于追溯。

派生自 GOSIM 官方示例与朋友 PR #2；官方示例使用
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)，见
[官方许可证](../training/official-examples/gosim-observer-examples/LICENSE.md)。
