# v9-t / v10-t 结果诊断（2026-10-07）

这批接口已有成功响应：v9 为 709 次，v10 为 547 次。部分卡受到 Kimi
五小时用量上限拒绝；成功回复仍存在大量过期、决策利用不足和 REQUIRED
漏观测。不能继续把低分全部归因于 API，也不能把动作变化当作提分证明。

- [完整诊断及责任边界](REPORT.md)
- [对话检查点与待办](CONVERSATION_CHECKPOINT.md)
- [脱敏逐卡数据](results-audit.json)
- [只读复算脚本](audit_results.py)

## 复算

需要 Kenny 提供的原始 `v9-t.zip` 和 `v10-t.zip`，Python 3.9 或以上，无第三方依赖：

```powershell
python audit_results.py --archive "<v9-t.zip 路径>" --archive "<v10-t.zip 路径>" --portable --output results-audit.json
```

脚本只读取 ZIP 内的 JSON 和日志，不执行附件内容。公开汇总采用 `--portable`：
仅保存档案文件名，不保存本机路径、最后错误详情或进行中的请求详情。
这里没有原始结果 ZIP、原始日志、模型请求/回复正文或运行凭据。

两包均只有 A-D、A1-C1，没有 D1。没有运行源码指纹，也没有配对模型消融；
本次诊断不证明 v9/v10 的因果收益或隐藏场景泛化。本目录是分析归档，不能作为
完整项目提交包上传赛事平台。
