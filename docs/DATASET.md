# DAXQO 轨迹数据集

本地收集器保存脱敏后的 action、agent feedback 和 task event 记录。记录遵循 `schema/trajectory-v0.1.schema.json` 中的 `daxqo.trajectory/0.1` 格式；`device.id`、`session_id` 与 `record_id` 用于稳定关联和幂等接收。

## 评分导出

`export` 生成 `train.jsonl`、`test.jsonl` 和 `manifest.json`。每条样本包含 `state`、`candidate_actions`、`action`、`next_state`、`reward`、`label`、`confidence`、设备/任务/动作标识及合并后的任务和决策上下文。

- 成功动作只有在同设备、同 action 的 `human_review` 或 `fixture_assertion` 独立验证后才标记 `positive`，reward 为 1。
- 失败动作标记 `negative`，reward 为 0；失败记录不会被验证反馈改成正例。
- 默认不导出未验证成功或 unknown 动作。启用 `include_unverified` 后，这些样本标记 `unverified`，reward 为 null，绝不会变成正例。
- fixture 来源的任务或 fixture assertion 会标注 `data_origin: fixture`，便于训练时筛选。

本格式可作为未来 Jev 训练接口的稳定输入层；模型侧可从 state 和候选动作选择 action，并使用结果、独立验证及 reward 做评分或偏好训练。

## 隐私与拆分

入库前会清理密码、验证码、卡号、聊天内容、邮箱、电话、OTP 和密钥；可编辑控件内容会清空。检测到敏感 UI 时整页禁止训练并清空节点。敏感应用、隐私排除标记和不允许训练的状态不会进入评分导出。原始 audit 记录仍保留收集时的动作参数标识，用于本地追溯；transition 去重签名会忽略 snapshot/request/action ID、时间戳等一次性参数。

已知私聊、短信及协作聊天应用按明确包名族排除，包括 WhatsApp、Telegram、Signal、Slack、Teams、钉钉、飞书及系统短信等。包名只匹配完整名称或点分子命名空间，不将相似名称当作同一应用；白名单不能覆盖敏感判定。Python 与原生观察器使用相同登记和测试。

未知应用中，明确的正文资源标识也会触发整页排除，例如 `message_text`、`message_body`、`chatMessageContent`、`sms_body`（含控件前缀和明确的 view/label 后缀）。普通“消息”文本、`dialog_message`、`notification_message`、`message_title`、`message_count` 不因这些新增规则被排除。被排除记录清空节点、动作目标/参数、候选及选中动作的内容，并移除截图。读取旧库和导出时重新应用当前规则，原始审计文件不就地改写。

包名登记参考官方分发/源码入口：[Signal Android](https://github.com/signalapp/Signal-Android)、[Telegram Android](https://www.telegram.org/android)、[Slack Android](https://slack.com/downloads/android)。登记只覆盖已知客户端；新增客户端需补充登记或明确的正文控件语义。

拆分按任务 ID 和规范化用户任务模板始终合组，并可再按设备或应用分组；相同状态转移也不能跨拆分。transition 去重只影响最终导出的样本数量，重复行关联的所有任务、设备和应用分组仍参与无泄漏合组。若可用数据只有一个无泄漏组，不会强行制造测试集；测试拆分为空，manifest 会说明限制。统计提供动作结果、独立验证、隐私排除、设备/应用分布、重复转移及 tree/observer/total 耗时摘要；缺失耗时不参与平均值与 p95。
