# v2 的 Pi 适配候选 - 2026-10-05

基线是团队仓库提交 `1589f104cf2bda6dc5bd2df7d9c008caed32ce03` 下的
`v2(jmk)/concentrated-20261005/project`。v2 效果最好来自 Kenny 的线上反馈，
本次没有重新测分。

保留 v2 的数值搜索、状态、几何、评分、请求与 REQUIRED 规划、CPU 档位和
动作校验。Python 仍负责比赛 JSONL；常驻 Node 工作进程使用真正的
Pi agent-core/ai 1.0.3，通过工具读取公开公告、请求/反馈与故障证据，
提交受限建议。第一版迁移现有模型职责，尚未包含动态生成或重载策略代码。

平台继续使用官方示例的 `python:3.12-slim` 镜像，构建时在工作目录下载
SHA-256 固定的 Node 22.20.0。Pi 已预先打包并附校验，不需平台执行 npm
安装；无需写系统目录。
当前机器无法执行 Linux 容器验证，平台构建和真实模型工具调用仍待检查。

平台变量：

| 变量 | 用途 |
| --- | --- |
| `AGENT_MODEL_BACKEND=pi` | 默认，启用 Pi 工具循环 |
| `AGENT_MODEL_BACKEND=v2` | 原 v2 HTTP 客户端，用于对照；仍要求 key |
| `AGENT_MODEL_BACKEND=disabled` | 关闭模型建议，沿用 v2 数值规划 |
| `OPENAI_API_KEY` 或 `KIMI_API_KEY` | 仅在运行时提供，不写入文件 |
| `OPENAI_BASE_URL` | OpenAI-compatible 接口，默认 Kimi Coding |
| `OPENAI_MODEL` | 默认 `k3`，沿用 v2 配置 |
| `PI_NODE_EXECUTABLE` | 本地开发可指定 Node 路径 |
| `AGENT_TRACE_PATH` | 可选运行 trace，不放进提交包 |

Pi 超时、无 key、Node 缺失、工具或模型失败时回到 v2 规则路径。
原有 Python 过期答案检查和动作验证继续生效。Pi 多步调用共用整局调用
预算，并保留末尾墙钟储备；比赛 stdout 仅输出协议响应。结束、EOF 和
重复初始化都会关闭工作进程。

Windows 本地运行需要 Node >=22.19，然后启动 Python 入口。修改 Node 工具
源码后，在 `pi_adapter` 执行 `npm ci --include=dev --ignore-scripts` 和
`node build_bundle.mjs` 重建 bundle；Linux 构建脚本仅用于比赛镜像。

重新打包：`python3 pack_agent.py --out ../gosim-v2-pi-20261005.zip`。
包内包含源码、Pi bundle、第三方许可证、锁文件、构建脚本、说明与 manifest，
不含依赖目录、Node
二进制、题目数据、测试、日志或凭据。

工程检查通过不等于正式得分提升；真实模型、平台构建、整季预算与线上
成绩仍需实测。本轮没有上传、确认参赛版本、推送仓库或运行评分。
