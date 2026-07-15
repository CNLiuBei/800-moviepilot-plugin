# Telegram 插件通知设计（2026-07-15）

## Goal

CloudUploader 支持同一 Bot Token 下的 **Bot 私聊/群** 与 **频道** 双目标通知，并提供 **事件开关** 与 **成功消息字段** 选项。

## Decisions

- 模型 A：一个 `tg_bot_token`，两个目标（bot chat / channel），各自开关
- 事件默认开：上传成功、上传失败、入库失败；默认关：入队、扫描补传
- 成功字段默认开：文件名、TMDB、季集、画质/模式；**不含 R2 路径**
- 兼容：旧 `tg_chat_id` 映射为 bot chat id
- 架构：`notify_policy.py`（策略）+ `notify.py`（发送）+ 插件表单接线

## Config keys

| Key | Default |
|-----|---------|
| tg_bot_token | "" |
| tg_bot_enabled | true（有 chat id 时才真正发送） |
| tg_bot_chat_id | ""（兼容 tg_chat_id） |
| tg_channel_enabled | false |
| tg_channel_id | "" |
| tg_event_success / failed / register_failed | true |
| tg_event_enqueue / scan | false |
| tg_field_filename / tmdb / episode / quality_mode | true |

## Follow-ups (2.9.2)

- 全失败路径通知（`_fail` / precheck / metadata / verify / exception）
- Telegram 改用 HTML + 转义，避免文件名 `_` 破坏 Markdown
- `POST /test_notify` 测通 Bot/频道；发送失败写 warning 日志并返回 errors
