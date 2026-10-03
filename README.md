# GOSIM 2026 巡天智能体 · 团队仓库

用于三位队友共同维护比赛需要的代码、实验记录和资料。先保持简单，目录随实际内容再增加。

最新共享记录：[2026-10-02 讲座与赛事消息摘要](TEAM_SUMMARY.md)。

正式开发前的 v4 边界与迁移清单：[2026-10-02 v4 迁移检查点](V4_MIGRATION_CHECKPOINT_2026-10-02.md)。

## 官方材料（本仓已镜像）

| 路径 | 说明 |
|------|------|
| [third-party/gosim-observer-examples/](third-party/gosim-observer-examples/) | 官方示例原包 zip + sha256 + 来源说明 |
| [scripts/fetch-official-examples.sh](scripts/fetch-official-examples.sh) | 校验并解压官方 examples |
| [practice-cards/alpha-incomplete/](practice-cards/alpha-incomplete/) | 官网公开练习卡 α **残缺包**（truth 为空） |
| [baselines/python-agent-baseline-unmodified.zip](baselines/python-agent-baseline-unmodified.zip) | 官方 python 示例未修改提交包（空白对照） |

上游 release：https://github.com/gosimfoundation/hackathon-survey26/releases/tag/examples-2026-10-02

一键取用：

```bash
./scripts/fetch-official-examples.sh
```

## 仓库分工

- **本仓（共享）**：官方可再分发材料、团队纪要、无密钥对照包。
- **个人 fork**（实验 / SYNTHETIC）：https://github.com/youwenzhang19/gosim-2026-team — 合成练习卡、个人跑分摘要、实验脚本。非官方分。
- **应用仓库**：https://github.com/KennyMcSimpson/gosim-agentic-observer — 本地公开场景练习应用；此处不重复存放安装包。

这是公开仓库：只提交可以公开的内容，不要提交密码、访问令牌、模型 API key 或私人数据。第三方材料先确认来源及再分发条件。赛事规则和提交方式以[官网规则](https://create.gosim.org/survey26/platform/rules)与[公告](https://create.gosim.org/survey26/platform/announcements)为准。
