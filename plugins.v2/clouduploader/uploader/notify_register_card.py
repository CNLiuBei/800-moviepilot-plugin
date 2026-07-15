"""新片入库 Telegram 卡片：上海报/剧照，下文案（无文件大小）+ 立即播放按钮。"""
from __future__ import annotations

from typing import Any

from .tmdb_http import tmdb_get_json

TELEGRAM_CAPTION_LIMIT = 1024


def build_play_url(
    site_base: str,
    media_type: str,
    tmdb_id: int,
    *,
    season: int | None = None,
    episode: int | None = None,
) -> str | None:
    """Build HTTPS watch URL for Telegram inline button (matches site clean paths)."""
    base = str(site_base or "").strip().rstrip("/")
    if not base:
        return None
    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"https://{base}"
    kind = "tv" if str(media_type).strip().lower() == "tv" else "movie"
    url = f"{base}/{kind}/{int(tmdb_id)}"
    if kind == "tv" and season is not None and episode is not None:
        url = f"{url}?season={int(season)}&episode={int(episode)}"
    return url


def play_button_markup(play_url: str | None) -> dict | None:
    url = str(play_url or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return None
    return {
        "inline_keyboard": [[{"text": "▶️ 立即播放", "url": url}]],
    }


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时"
    if minutes:
        return f"{minutes}分钟"
    return f"{total}秒"


def format_runtime_minutes(minutes: int | None) -> str:
    if minutes is None:
        return ""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return ""
    return format_duration(m * 60)


def build_image_url(file_path: str | None, size: str = "w1280") -> str | None:
    path = (file_path or "").strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://image.tmdb.org/t/p/{size}{path}"


def fetch_register_card_meta(
    tmdb_id: int,
    media_type: str,
    *,
    season: int | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """Fetch zh-CN TMDB fields needed for the入库 caption + image."""
    kind = "tv" if str(media_type).strip().lower() == "tv" else "movie"
    data = tmdb_get_json(f"/{kind}/{int(tmdb_id)}", params={"language": "zh-CN"})
    title = (
        (data.get("title") or data.get("name") or data.get("original_title") or data.get("original_name") or "")
        .strip()
    )
    date = (data.get("release_date") or data.get("first_air_date") or "").strip()
    year = date[:4] if len(date) >= 4 else ""
    genres = [g.get("name") for g in (data.get("genres") or []) if g.get("name")]
    rating = data.get("vote_average")
    runtime = data.get("runtime")
    still = ""
    if kind == "tv" and season is not None and episode is not None:
        try:
            ep = tmdb_get_json(
                f"/tv/{int(tmdb_id)}/season/{int(season)}/episode/{int(episode)}",
                params={"language": "zh-CN"},
            )
            if ep.get("runtime"):
                runtime = ep.get("runtime")
            still = (ep.get("still_path") or "").strip()
        except Exception:
            still = ""

    backdrop = (data.get("backdrop_path") or "").strip()
    poster = (data.get("poster_path") or "").strip()
    image_path = still or backdrop or poster
    image_size = "w1280" if (still or backdrop) else "w780"

    return {
        "media_type": kind,
        "title": title or f"TMDB {tmdb_id}",
        "year": year,
        "rating": rating,
        "genres": genres,
        "runtime_minutes": runtime,
        "backdrop_path": backdrop,
        "poster_path": poster,
        "image_url": build_image_url(image_path, image_size),
        "season": season,
        "episode": episode,
    }


def format_register_caption(
    meta: dict[str, Any],
    *,
    quality: str = "",
    duration_secs: int | None = None,
) -> str:
    """
    Caption style:
      千与千寻 (2001) 已入库
      评分：8.5，类型：电影，类别：动画，质量：BluRay 1080p，时长：2小时5分
    """
    title = str(meta.get("title") or "").strip() or "未知标题"
    year = str(meta.get("year") or "").strip()
    season = meta.get("season")
    episode = meta.get("episode")
    kind = meta.get("media_type") or "movie"

    if season is not None and episode is not None:
        head = f"{title} S{int(season):02d}E{int(episode):02d}"
        if year:
            head = f"{head} ({year})"
    else:
        head = f"{title} ({year})" if year else title
    head = f"{head} 已入库"

    parts: list[str] = []
    rating = meta.get("rating")
    try:
        if rating is not None and float(rating) > 0:
            parts.append(f"评分：{float(rating):.1f}")
    except (TypeError, ValueError):
        pass

    type_label = "剧集" if kind == "tv" else "电影"
    parts.append(f"类型：{type_label}")

    genres = [str(g).strip() for g in (meta.get("genres") or []) if str(g).strip()]
    if genres:
        category = "、".join(genres[:2])
        parts.append(f"类别：{category}")

    q = str(quality or "").strip()
    if q:
        parts.append(f"质量：{q}")

    duration = format_duration(duration_secs) or format_runtime_minutes(meta.get("runtime_minutes"))
    if duration:
        parts.append(f"时长：{duration}")

    if not parts:
        return head
    return f"{head}\n{'，'.join(parts)}"
