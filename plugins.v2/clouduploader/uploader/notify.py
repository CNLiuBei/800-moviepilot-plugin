"""
通知模块（插件内嵌版）

渠道：
- MoviePilot 内置通知：由插件注入 _mp_notifier（受总开关 notify 控制）
- Telegram Bot 私聊：受事件开关控制
- Telegram 频道/群：仅「新片入库成功」
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


def send_telegram_photo(
    photo_url: str,
    caption: str,
    *,
    event: str | None = None,
    force_all_targets: bool = False,
    reply_markup: dict | None = None,
) -> dict:
    """Send photo+caption to routed targets; caption uses HTML."""
    token, chats, early = _resolve_send_targets(event=event, force_all_targets=force_all_targets)
    if early:
        return early
    safe_caption = policy_mod.escape_html(caption)
    if len(safe_caption) > 1024:
        safe_caption = safe_caption[:1020] + "…"
    payload: dict = {
        "photo": photo_url,
        "caption": safe_caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post_telegram(
        token,
        chats,
        endpoint="sendPhoto",
        payload_base=payload,
    )


def send_telegram_message(
    title: str,
    message: str,
    *,
    event: str | None = None,
    force_all_targets: bool = False,
    reply_markup: dict | None = None,
) -> dict:
    """
    Send text to TG targets.

    - force_all_targets / event is None: all enabled Bot+频道（测试用）
    - event set: Bot 按事件开关；频道仅 register_success
    """
    token, chats, early = _resolve_send_targets(event=event, force_all_targets=force_all_targets)
    if early:
        return early
    text = policy_mod.format_telegram_html(title, message)
    payload: dict = {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post_telegram(token, chats, payload_base=payload)


def _resolve_send_targets(
    *,
    event: str | None,
    force_all_targets: bool,
) -> tuple[str, list[str], dict | None]:
    token = str(_policy.get("tg_bot_token") or settings.TG_BOT_TOKEN or "").strip()
    if not token:
        return "", [], {"ok": False, "sent": 0, "targets": [], "errors": ["未配置 Bot Token"]}
    policy = {**_policy, "tg_bot_token": token}
    if force_all_targets or event is None:
        chats = policy_mod.resolve_tg_chat_ids(policy)
        if not chats and settings.TG_CHAT_ID:
            chats = [str(settings.TG_CHAT_ID).strip()]
    else:
        chats = policy_mod.resolve_tg_targets_for_event(policy, event)
    if not chats:
        return token, [], {"ok": False, "sent": 0, "targets": [], "errors": ["没有匹配的发送目标"]}
    return token, chats, None


def _post_telegram(
    token: str,
    chats: list[str],
    *,
    endpoint: str = "sendMessage",
    payload_base: dict,
) -> dict:
    errors: list[str] = []
    sent = 0
    for chat_id in chats:
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/{endpoint}",
                json={"chat_id": chat_id, **payload_base},
                timeout=20,
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
    """Send a one-off test message to all enabled TG targets."""
    result = send_telegram_message(
        "☁️ CloudUploader 测试通知",
        "配置正常。Bot 私聊按事件推送；频道/群仅推送新片入库。",
        force_all_targets=True,
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


def _send_telegram(title: str, message: str, *, event: str) -> None:
    send_telegram_message(title, message, event=event)


def _notify_mp(title: str, message: str) -> None:
    if _mp_notifier is None:
        return
    try:
        _mp_notifier(title, message)
    except Exception:
        pass


def _notify_job(title: str, message: str, *, event: str, mp: bool = True) -> None:
    """Job 结果通知：可选 MP + 按事件路由的 TG。"""
    if mp:
        _notify_mp(title, message)
    _send_telegram(title, message, event=event)


def notify_tg_event(event: str, title: str, message: str) -> None:
    """仅 Telegram（入队/扫描等）。"""
    _send_telegram(title, message, event=event)


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


def notify_register_success(
    filename: str,
    tmdb_id: int,
    season: int = None,
    episode: int = None,
    quality: str = "",
    upload_mode: str = "",
    media_type: str = "movie",
    duration_secs: int | None = None,
) -> None:
    """新片入库成功：频道/群发海报卡片 + 立即播放按钮。"""
    del filename, upload_mode
    from .notify_register_card import (
        TELEGRAM_CAPTION_LIMIT,
        build_play_url,
        fetch_register_card_meta,
        format_register_caption,
        play_button_markup,
    )

    try:
        meta = fetch_register_card_meta(
            int(tmdb_id),
            media_type,
            season=season,
            episode=episode,
        )
    except Exception as e:
        logger.warning("[CloudUploader] 入库通知拉取 TMDB 失败: %s", e)
        meta = {
            "title": f"TMDB {tmdb_id}",
            "year": "",
            "media_type": "tv" if str(media_type).lower() == "tv" else "movie",
            "rating": None,
            "genres": [],
            "runtime_minutes": None,
            "image_url": None,
            "season": season,
            "episode": episode,
        }

    caption = format_register_caption(
        meta,
        quality=quality,
        duration_secs=duration_secs,
    )
    play_url = build_play_url(
        settings.API_BASE,
        meta.get("media_type") or media_type,
        int(tmdb_id),
        season=season,
        episode=episode,
    )
    markup = play_button_markup(play_url)
    event = policy_mod.EVENT_REGISTER_SUCCESS
    photo = meta.get("image_url")
    if photo and len(caption) <= TELEGRAM_CAPTION_LIMIT:
        result = send_telegram_photo(
            photo, caption, event=event, reply_markup=markup,
        )
        if result.get("sent"):
            return
        logger.warning(
            "[CloudUploader] 入库海报发送失败，回退文本: %s",
            "; ".join(result.get("errors") or []),
        )
    lines = caption.split("\n", 1)
    body = lines[1] if len(lines) > 1 else ""
    if play_url and not markup:
        # Should not happen for https URLs; keep text fallback link
        body = (body + f"\n<a href=\"{policy_mod.escape_html(play_url)}\">立即播放</a>").strip()
    elif play_url and body:
        pass
    send_telegram_message(
        lines[0],
        body,
        event=event,
        reply_markup=markup,
    )


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
