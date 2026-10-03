# 官方 gosim-observer-examples 来源说明

本目录存放 GOSIM Survey26 官方示例包的可校验副本，供团队离线取用。

## 上游来源

| 字段 | 值 |
|------|-----|
| Release | [examples-2026-10-02](https://github.com/gosimfoundation/hackathon-survey26/releases/tag/examples-2026-10-02) |
| Asset URL | https://github.com/gosimfoundation/hackathon-survey26/releases/download/examples-2026-10-02/gosim-observer-examples.zip |
| sha256 | `44611bf8b3f06020ed2dd6b802b47521c07a153d3c8e80b428df88da336f98da` |
| 校验时间 | 2026-10-03（本机对照官方 release 重新下载，hash 一致） |

## 包内内容（官方原包）

- `python/` `typescript/` `rust/`：示例智能体
- `docs/`：参赛指南
- `local-cards/L1`–`L4`：公开本地练习卡
- `runner/`：本地裁判（与平台同源评测代码）
- `LICENSE.md`、`README.md`

## 使用方式

```bash
# 校验并解压到工作区（推荐）
./scripts/fetch-official-examples.sh

# 或手动
cd third-party/gosim-observer-examples
shasum -a 256 -c gosim-observer-examples.zip.sha256
unzip gosim-observer-examples.zip
```

## 再分发说明

材料来自 GOSIM Foundation 公开 release。本仓库仅做镜像与校验，版权与许可以包内 `LICENSE.md` 及上游仓库为准。
**不要**把本机跑分产物、`.env`、API key 或私人修改混进此目录。

应用模拟器仍见独立仓库：https://github.com/KennyMcSimpson/gosim-agentic-observer
