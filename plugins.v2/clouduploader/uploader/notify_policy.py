"""Telegram / MoviePilot 通知策略：事件开关、目标解析、成功文案字段。"""
from __future__ import annotations

EVENT_SUCCESS = "success"
EVENT_FAILED = "failed"
EVENT_REGISTER_FAILED = "register_failed"
EVENT_ENQUEUE = "enqueue"
EVENT_SCAN = "scan"

_EVENT_DEFAULTS = {
    EVENT_SUCCESS: True,
    EVENT_FAILED: True,
    EVENT_REGISTER_FAILED: True,
    EVENT_ENQUEUE: False,
    EVENT_SCAN: False,
}

_FIELD_KEYS = (
    "tg_field_filename",
    "tg_field_tmdb",
    "tg_field_episode",
    "tg_field_quality_mode",
)


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def escape_html(text: object) -> str:
    """Escape text for Telegram HTML parse_mode."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_telegram_html(title: str, message: str) -> str:
    body = str(message or "").strip()
    head = f"<b>{escape_html(title)}</b>"
    if not body:
        return head
    return f"{head}\n{escape_html(body)}"


def normalize_notify_policy(config: dict | None) -> dict:
    """Normalize plugin form / runtime config into a notify policy dict."""
    raw = dict(config or {})
    bot_chat = str(raw.get("tg_bot_chat_id") or raw.get("tg_chat_id") or "").strip()
    channel_id = str(raw.get("tg_channel_id") or "").strip()
    token = str(raw.get("tg_bot_token") or "").strip()

    policy = {
        "tg_bot_token": token,
        "tg_bot_enabled": _as_bool(raw.get("tg_bot_enabled"), True),
        "tg_bot_chat_id": bot_chat,
        "tg_channel_enabled": _as_bool(raw.get("tg_channel_enabled"), False),
        "tg_channel_id": channel_id,
        "tg_chat_id": bot_chat,  # legacy alias
    }
    for event, default in _EVENT_DEFAULTS.items():
        key = f"tg_event_{event}"
        policy[key] = _as_bool(raw.get(key), default)
    for field in _FIELD_KEYS:
        policy[field] = _as_bool(raw.get(field), True)
    return policy


def event_enabled(policy: dict, event: str) -> bool:
    return bool(policy.get(f"tg_event_{event}", _EVENT_DEFAULTS.get(event, False)))


def resolve_tg_chat_ids(policy: dict) -> list[str]:
    """Return destination chat ids when token is present and targets are enabled."""
    if not str(policy.get("tg_bot_token") or "").strip():
        return []
    targets: list[str] = []
    bot_chat = str(policy.get("tg_bot_chat_id") or "").strip()
    if policy.get("tg_bot_enabled", True) and bot_chat:
        targets.append(bot_chat)
    channel = str(policy.get("tg_channel_id") or "").strip()
    if policy.get("tg_channel_enabled") and channel and channel not in targets:
        targets.append(channel)
    return targets


def build_success_body(
    policy: dict,
    *,
    filename: str,
    tmdb_id: int | None = None,
    season: int | None = None,
    episode: int | None = None,
    quality: str = "",
    upload_mode: str = "",
    r2_path: str = "",
) -> str:
    """Build success message body. r2_path is accepted for API compat but never included."""
    del r2_path
    lines: list[str] = []
    if policy.get("tg_field_filename", True) and filename:
        lines.append(f"📤 {filename}")
    detail_parts: list[str] = []
    if policy.get("tg_field_tmdb", True) and tmdb_id is not None:
        detail_parts.append(f"TMDB: {tmdb_id}")
    if policy.get("tg_field_episode", True):
        if season is not None and episode is not None:
            detail_parts.append(f"S{int(season):02d}E{int(episode):02d}")
        else:
            detail_parts.append("电影")
    if detail_parts:
        lines.append(" | ".join(detail_parts))
    if policy.get("tg_field_quality_mode", True):
        qm = [p for p in (str(quality or "").strip(), str(upload_mode or "").strip()) if p]
        if qm:
            lines.append(" · ".join(qm))
    return "\n".join(lines) if lines else (filename or "上传完成")
