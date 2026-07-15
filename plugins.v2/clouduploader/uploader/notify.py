"""
通知模块（插件内嵌版）

渠道：
- MoviePilot 内置通知：由插件注入 _mp_notifier（受总开关 notify 控制）
- Telegram：同一 Bot Token → Bot 私聊/群 + 频道；事件/字段策略见 notify_policy
"""
from __future__ import annotations

import logging

import httpx

from . import notify_policy as policy_mod
from .runtime_config import settings

logger = logging.getLogger("clouduploader.notify")

_mp_notifier = None
_policy: dict = policy_mod.normalize_notify_policy({})


def set_mp_notifier(fn) -> None:
    """插件主体注入 MoviePilot 通知回调。"""
    global _mp_notifier
    _mp_notifier = fn


def configure_notify_policy(config: dict | None) -> dict:
    """注入/刷新通知策略（表单保存时调用）。"""
    global _policy
    _policy = policy_mod.normalize_notify_policy(config)
    return _policy


def get_notify_policy() -> dict:
    return dict(_policy)


def send_telegram_message(title: str, message: str) -> dict:
    """
    Send to all enabled TG targets.

    Returns:
        {"ok": bool, "sent": int, "targets": list[str], "errors": list[str]}
    """
    token = str(_policy.get("tg_bot_token") or settings.TG_BOT_TOKEN or "").strip()
    if not token:
        return {"ok": False, "sent": 0, "targets": [], "errors": ["未配置 Bot Token"]}
    chats = policy_mod.resolve_tg_chat_ids({**_policy, "tg_bot_token": token})
    if not chats and settings.TG_CHAT_ID:
        chats = [str(settings.TG_CHAT_ID).strip()]
    if not chats:
        return {"ok": False, "sent": 0, "targets": [], "errors": ["未启用任何 Bot/频道目标"]}

    text = policy_mod.format_telegram_html(title, message)
    errors: list[str] = []
    sent = 0
    for chat_id in chats:
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
                trust_env=False,
            )
            data = {}
            try:
                data = response.json()
            except Exception:
                data = {}
            if response.status_code >= 400 or not data.get("ok", True):
                desc = data.get("description") or f"HTTP {response.status_code}"
                errors.append(f"{chat_id}: {desc}")
                logger.warning("[CloudUploader] Telegram 发送失败 %s: %s", chat_id, desc)
            else:
                sent += 1
        except Exception as e:
            errors.append(f"{chat_id}: {e}")
            logger.warning("[CloudUploader] Telegram 发送异常 %s: %s", chat_id, e)
    return {
        "ok": sent > 0 and not errors,
        "sent": sent,
        "targets": chats,
        "errors": errors,
    }


def send_test_notification() -> dict:
    """Send a one-off test message to enabled TG targets (ignores event switches)."""
    result = send_telegram_message(
        "☁️ CloudUploader 测试通知",
        "配置正常。Bot / 频道目标可用。",
    )
    if result["sent"] > 0 and result["errors"]:
        result["ok"] = True
        result["message"] = (
            f"部分成功：已发送 {result['sent']} 个目标；失败: " + "；".join(result["errors"])
        )
    elif result["ok"]:
        result["message"] = f"已发送到 {result['sent']} 个目标"
    else:
        result["message"] = "；".join(result["errors"]) or "发送失败"
    return result


def _send_telegram(title: str, message: str) -> None:
    send_telegram_message(title, message)


def _notify_mp(title: str, message: str) -> None:
    if _mp_notifier is None:
        return
    try:
        _mp_notifier(title, message)
    except Exception:
        pass


def _notify_job(title: str, message: str, *, event: str) -> None:
    """Job 结果通知：MP（若已注入）+ TG（若事件开启）。"""
    _notify_mp(title, message)
    if policy_mod.event_enabled(_policy, event):
        _send_telegram(title, message)


def notify_tg_event(event: str, title: str, message: str) -> None:
    """仅 Telegram（用于入队/扫描等，MP 由插件自行决定）。"""
    if policy_mod.event_enabled(_policy, event):
        _send_telegram(title, message)


def notify_upload_success(
    filename: str,
    tmdb_id: int,
    season: int = None,
    episode: int = None,
    r2_path: str = "",
    quality: str = "",
    upload_mode: str = "",
) -> None:
    body = policy_mod.build_success_body(
        _policy,
        filename=filename,
        tmdb_id=tmdb_id,
        season=season,
        episode=episode,
        quality=quality,
        upload_mode=upload_mode,
        r2_path=r2_path,
    )
    _notify_job("✅ 上传完成", body, event=policy_mod.EVENT_SUCCESS)


def notify_upload_failed(filename: str, error: str, *, stage: str = "") -> None:
    stage_prefix = f"[{stage}] " if stage else ""
    _notify_job(
        "❌ 上传失败",
        f"📄 {filename}\n错误: {stage_prefix}{error[:200]}",
        event=policy_mod.EVENT_FAILED,
    )


def notify_register_failed(filename: str, error: str, r2_path: str = "") -> None:
    del r2_path
    _notify_job(
        "⚠️ 入库失败",
        f"📄 {filename}\n错误: {error[:200]}\n已保留云端文件，将对账补登",
        event=policy_mod.EVENT_REGISTER_FAILED,
    )


def notify_enqueue(title: str, detail: str) -> None:
    notify_tg_event(policy_mod.EVENT_ENQUEUE, title, detail)


def notify_scan(title: str, detail: str) -> None:
    notify_tg_event(policy_mod.EVENT_SCAN, title, detail)
