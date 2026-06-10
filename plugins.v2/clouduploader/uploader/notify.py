"""
通知模块（插件内嵌版）

支持两种通知渠道：
- MoviePilot 内置通知：由插件注入 _mp_notifier 回调
- Telegram：沿用独立工具配置（可选）
"""
import httpx

from .runtime_config import settings

# 插件注入的 MoviePilot 通知回调：notifier(title: str, text: str)
_mp_notifier = None


def set_mp_notifier(fn) -> None:
    """插件主体注入 MoviePilot 通知回调。"""
    global _mp_notifier
    _mp_notifier = fn


def _notify(title: str, message: str) -> None:
    # MoviePilot 通知
    if _mp_notifier is not None:
        try:
            _mp_notifier(title, message)
        except Exception:
            pass
    # Telegram（可选）
    if settings.TG_BOT_TOKEN and settings.TG_CHAT_ID:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": settings.TG_CHAT_ID,
                    "text": f"*{title}*\n{message}",
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
        except Exception:
            pass


def notify_upload_success(filename: str, tmdb_id: int, season: int = None,
                          episode: int = None, r2_path: str = "") -> None:
    ep_info = f"S{season:02d}E{episode:02d}" if season is not None and episode is not None else "电影"
    _notify(
        "✅ 上传完成",
        f"📤 {filename}\nTMDB: {tmdb_id} | {ep_info}\n路径: {r2_path}",
    )


def notify_upload_failed(filename: str, error: str) -> None:
    _notify("❌ 上传失败", f"📄 {filename}\n错误: {error[:200]}")
