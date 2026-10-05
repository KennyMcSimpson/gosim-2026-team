# v4-pi：历史 Pi 候选归档

2026-10-06。此包是先前交付的 v4.zip，现在明确命名为 **v4-pi**。
Kenny 已更正本次需要官方 Python agent，推荐试跑 [v4-official](../v4-official/)。

- [下载冻结 v4-pi.zip](v4-pi.zip)
- [对应源码](project/)
- [当时的交付说明](ORIGINAL_IMPLEMENTATION.md)
- [验证摘要和哈希](VALIDATION.json)
- [失败归因与证据边界](../FAILURE_ANALYSIS.md)

ZIP 仅改外部文件名，未重新打包，SHA-256：
`428920dca602d351e8fd71ce167c66c3a71bf1d22abf3f2f9405ff6c18c9062c`。

该版以 v3 recovery 为数值底座，接入实际 Pi 1.0.3 + Node 22.20.0；Python
仍拥有几何、账本和动作验证。原包默认 `AGENT_MODEL_BACKEND=pi-pro`，含 Linux
构建下载。原包内 v4 名称与上传建议属于历史记录。

26 项工程测试及本地模拟 Pi 工具/四卡协议验证通过，未证明正式增分。
用户报告 Pi 结合效果不好，但本次未取得该具体 ZIP 的正式分项回执，无法把
损失全部归因于 Pi。v3 的失败分数不属于此包。

Python 框架派生自 GOSIM 官方示例，见
[官方许可证](../training/official-examples/gosim-observer-examples/LICENSE.md)。
Pi 及其依赖许可证保留在 `project/pi_adapter/` 和 ZIP 中。
