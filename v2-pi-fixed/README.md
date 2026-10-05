# v2-pi-fixed：回归修复候选

基于指定原 v2 的 Pi 适配修复，保留两份失败包供追溯。本包尚无新线上成绩。

- [网页完整项目 ZIP](v2-pi-fixed.zip)
- [对应源码及测试](project/)
- [失败原因、修复范围和证据边界](../PI_FAILURE_ANALYSIS.md)
- [验证回执](VALIDATION.json)
- [ZIP 与源码校验](SHA256SUMS)

默认 `AGENT_MODEL_BACKEND=pi-fixed`：恢复异步公告和反馈两个原 v2 模型角色，
通过单轮 Pi SDK 工具调用；默认关闭同步候选替换与额外候选收集。
修复同一请求内建议过期、角色额度竞争、JSON/工具指令冲突；
模型否决一次之后，冷却后重新满足完整多晚证据的故障不会被永久压住。
实际采用的建议、模型失败原因、故障检查和可选候选取舍均写入平台日志。

`pi-review` 是显式实验模式，拒绝降低估计科学/总收益率、REQUIRED、请求奖励、
有效概率和均匀度的候选；估计不劣不能证明整季得分不劣。
`v2` 保留原 HTTP 顾问的新鲜度、额度和故障否决语义，供对照；`disabled` 不调用模型。

测试命令（在 `project` 内）：`python -m unittest discover -s tests -v`。
公共几何检查需要 `GOSIM_PUBLIC_CARDS_DIR` 下的 a/b/c/d 下载目录；
裁判模块默认使用仓库 `training/official-examples/gosim-observer-examples/runner`，
可由 `GOSIM_OFFICIAL_RUNNER_DIR` 指定。缺少公开输入时这些检查明确跳过，不冒充通过。
数值门槛与真实几何/pending 分开验证；几何提交夹具单独注入可评审取舍以覆盖失败回退。

交付验证包含 30 项回归、48 次无模型动作/pending 对照、真实 Pi 1.0.3 + Node 22.20.0
的本地模拟接口和四种光纤协议检查。没有调用真实付费模型或启动新正式/练习评分。
故障否决上限是行为变化，线上误报和提分仍待同配置对照。

上传时模型配置沿用原 v2；凭据只放平台运行设置，不放 ZIP。
