"""
自动入库 + NFO 生成模块（插件内嵌版）
"""
import json
import time

import httpx

from .runtime_config import ConfigError, normalize_base_url, settings
from .r2 import get_s3_client

_HTTPX_REQUEST_KWARGS = {"trust_env": False}


def _api_error(resp: httpx.Response, prefix: str) -> str:
    """从 API 响应中提取可读错误信息。"""
    try:
        data = resp.json()
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error") or data.get("status_message")
            if msg:
                return f"{prefix} [{resp.status_code}] — {msg}"
    except (ValueError, json.JSONDecodeError):
        pass
    text = (resp.text or "").strip()
    if text:
        return f"{prefix} [{resp.status_code}] — {text[:500]}"
    return f"{prefix} [{resp.status_code}]"


def _normalize_media_type(media_type: str) -> str:
    value = (media_type or "tv").strip().lower()
    return "movie" if value == "movie" else "tv"


def _has_episode_numbers(season, episode) -> bool:
    return season is not None and episode is not None


def _request_error(prefix: str, error: Exception) -> str:
    return f"{prefix}: {error}"


def auto_register(
    tmdb_id: int, media_type: str, season: int | None,
    episode: int | None, r2_path: str, quality: str,
    subtitles: list[dict], duration_secs: int | None = None,
    source_type: str = "cmaf", print_fn=None,
) -> tuple[bool, str]:
    """
    自动入库: 导入 TMDB 元数据 → 按 TMDB 身份绑定播放源 + 字幕

    Returns:
        (True, "")              成功
        (False, "具体错误原因")  失败
    """
    if print_fn is None:
        print_fn = print

    try:
        api_base = normalize_base_url(settings.API_BASE, "流媒体站地址")
    except ConfigError as e:
        msg = str(e)
        print_fn(f"   ❌ {msg}")
        return False, msg
    if not api_base:
        msg = "未配置流媒体站地址 (api_base)"
        print_fn(f"   ❌ {msg}")
        return False, msg

    print_fn("🔗 自动入库...")
    media_type = _normalize_media_type(media_type)

    try:
        client = httpx.Client(base_url=api_base, timeout=120, follow_redirects=True, **_HTTPX_REQUEST_KWARGS)
    except httpx.InvalidURL as e:
        msg = _request_error("流媒体站地址配置错误", e)
        print_fn(f"   ❌ {msg}")
        return False, msg

    if settings.API_ADMIN_KEY:
        client.headers["x-admin-key"] = settings.API_ADMIN_KEY
        print_fn("   ✅ API Key 认证")
    elif settings.API_USERNAME and settings.API_PASSWORD:
        try:
            resp = client.post(
                "/api/auth/sign-in/username",
                json={"username": settings.API_USERNAME, "password": settings.API_PASSWORD},
            )
        except httpx.HTTPError as e:
            msg = _request_error("认证失败: 请求站点失败", e)
            print_fn(f"   ❌ {msg}")
            return False, msg
        if resp.status_code != 200:
            msg = _api_error(resp, "认证失败: 登录失败")
            print_fn(f"   ❌ {msg}")
            return False, msg
        print_fn("   ✅ 登录成功")
    else:
        msg = "认证失败: 未配置 API_ADMIN_KEY 或 API_USERNAME/API_PASSWORD"
        print_fn(f"   ❌ {msg}")
        return False, msg

    try:
        return _do_register(
            client, tmdb_id, media_type, season, episode,
            r2_path, quality, subtitles, duration_secs, source_type, print_fn,
        )
    finally:
        client.close()


def _import_tmdb(
    client: httpx.Client,
    tmdb_id: int,
    media_type: str,
    fetch_episodes: bool,
    print_fn,
) -> tuple[bool, str]:
    try:
        resp = client.post("/api/admin/import-single", json={
            "tmdbId": tmdb_id,
            "type": media_type,
            "fetchEpisodes": bool(fetch_episodes and media_type == "tv"),
        })
    except httpx.HTTPError as e:
        msg = _request_error("TMDB导入失败: 请求站点失败", e)
        print_fn(f"   ❌ {msg}")
        return False, msg
    if resp.status_code != 200:
        msg = _api_error(resp, "TMDB导入失败")
        print_fn(f"   ❌ {msg}")
        return False, msg
    try:
        import_data = resp.json()
    except (ValueError, json.JSONDecodeError):
        msg = f"TMDB导入响应异常 — {resp.text[:500]}"
        print_fn(f"   ❌ {msg}")
        return False, msg
    print_fn(
        f"   ✅ TMDB 元数据已导入: {media_type}/{tmdb_id} "
        f"(episodes: {import_data.get('episodes')}, movieId: {import_data.get('movieId')})"
    )
    return True, ""


def _do_register(
    client: httpx.Client, tmdb_id: int, media_type: str, season: int | None,
    episode: int | None, r2_path: str, quality: str,
    subtitles: list[dict], duration_secs: int | None,
    source_type: str, print_fn,
) -> tuple[bool, str]:
    play_url = (
        f"/api/r2/{r2_path}/master.m3u8"
        if source_type == "cmaf"
        else f"/api/r2/{r2_path}/stream.m3u8"
    )

    # 首次轻量导入（不拉全季分集，避免每集重复打 TMDB）；失败再拉全量分集重试。
    ok, err = _import_tmdb(client, tmdb_id, media_type, fetch_episodes=False, print_fn=print_fn)
    if not ok:
        return False, err

    ok_src, err_src = _sync_play_source(
        client=client,
        tmdb_id=tmdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        play_url=play_url,
        source_type=source_type,
        quality=quality,
        duration_secs=duration_secs,
        print_fn=print_fn,
    )
    if ok_src:
        pass
    elif media_type == "tv" and _has_episode_numbers(season, episode):
        print_fn("   ⏳ 播放源绑定失败，拉取 TMDB 分集后重试...")
        ok, err = _import_tmdb(client, tmdb_id, media_type, fetch_episodes=True, print_fn=print_fn)
        if not ok:
            return False, err
        time.sleep(2)
        ok_src, err_src = _sync_play_source(
            client=client,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            play_url=play_url,
            source_type=source_type,
            quality=quality,
            duration_secs=duration_secs,
            print_fn=print_fn,
        )
        if not ok_src:
            return False, err_src
    else:
        return False, err_src

    if subtitles:
        sub_manifest = [
            {
                "lang": sub["lang"],
                "label": sub["label"],
                "url": f"/api/r2/{r2_path}/{sub['file']}",
                **({"hlsUrl": f"/api/r2/{r2_path}/{sub['hls_uri']}"} if sub.get("hls_uri") else {}),
            }
            for sub in subtitles
        ]
        s3 = get_s3_client()
        s3.put_object(
            Bucket=settings.R2_BUCKET,
            Key=f"{r2_path}/subtitles.json",
            Body=json.dumps(sub_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        print_fn(f"   ✅ 字幕清单: {len(sub_manifest)} 条")

    print_fn("   ✅ 入库完成!")
    return True, ""


def _sync_play_source(
    client: httpx.Client,
    tmdb_id: int,
    media_type: str,
    season: int | None,
    episode: int | None,
    play_url: str,
    source_type: str,
    quality: str,
    duration_secs: int | None,
    print_fn,
) -> tuple[bool, str]:
    """按 TMDB 身份绑定播放源。返回 (ok, error_msg)。"""
    payload = {
        "tmdbId": tmdb_id,
        "mediaType": media_type,
        "label": f"原画 {quality}" if quality else "原画",
        "sourceType": source_type,
        "url": play_url,
        "quality": quality or "原画",
        "sortOrder": 0,
        "replace": True,
    }
    if _has_episode_numbers(season, episode):
        payload["seasonNumber"] = int(season)
        payload["episodeNumber"] = int(episode)
    if duration_secs:
        payload["durationSecs"] = int(duration_secs)

    try:
        resp = client.post("/api/admin/sources", json=payload)
    except httpx.HTTPError as e:
        msg = _request_error("播放源绑定失败: 请求站点失败", e)
        print_fn(f"   ❌ {msg}")
        return False, msg
    if resp.status_code == 200:
        print_fn(f"   ✅ 播放源已绑定: {play_url}")
        return True, ""

    msg = _api_error(resp, "播放源绑定失败")
    print_fn(f"   ❌ {msg}")
    return False, msg


def write_episode_nfo(
    tmdb_id: int, season: int, episode: int,
    r2_path: str, resolution: str, video_path: str = "",
    print_fn=None,
):
    """写 episode.nfo + 从 TMDB 获取剧照作为缩略图。"""
    if print_fn is None:
        print_fn = print

    try:
        _auth = settings.tmdb_auth
        try:
            ep = httpx.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/episode/{episode}",
                params={"language": "zh-CN", **_auth["params"]},
                headers=_auth["headers"],
                timeout=15,
                **_HTTPX_REQUEST_KWARGS,
            ).json()
        except httpx.HTTPError as e:
            raise RuntimeError(_request_error("TMDB episode 请求失败", e)) from e

        def esc(s):
            return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<episodedetails>"]
        lines.append(f"  <title>{esc(ep.get('name', ''))}</title>")
        lines.append(f"  <season>{season}</season>")
        lines.append(f"  <episode>{episode}</episode>")
        lines.append(f"  <plot>{esc(ep.get('overview', ''))}</plot>")
        lines.append(f"  <aired>{ep.get('air_date', '')}</aired>")
        lines.append(f"  <rating>{ep.get('vote_average', 0)}</rating>")
        lines.append(f"  <runtime>{ep.get('runtime', 0)}</runtime>")
        lines.append(f"  <uniqueid type=\"tmdb\">{ep.get('id', '')}</uniqueid>")
        for d in [c for c in ep.get('crew', []) if c.get('job') == 'Director'][:3]:
            lines.append(f"  <director>{esc(d['name'])}</director>")
        for g in ep.get('guest_stars', [])[:10]:
            lines.append(f"  <actor><name>{esc(g['name'])}</name><role>{esc(g.get('character', ''))}</role></actor>")
        lines.append("  <fileinfo><streamdetails>")
        lines.append(f"    <video><codec>copy</codec><aspect>{resolution or 'original'}</aspect></video>")
        lines.append("    <audio><codec>aac</codec><channels>2</channels></audio>")
        lines.append("  </streamdetails></fileinfo>")
        lines.append("</episodedetails>")

        s3 = get_s3_client()
        s3.put_object(
            Bucket=settings.R2_BUCKET, Key=f"{r2_path}/episode.nfo",
            Body="\n".join(lines).encode("utf-8"), ContentType="application/xml",
        )
        print_fn(f"   ✅ episode.nfo: {ep.get('name', '')}")

        if ep.get('still_path'):
            img = httpx.get(
                f"https://image.tmdb.org/t/p/w500{ep['still_path']}",
                timeout=15, follow_redirects=True,
                **_HTTPX_REQUEST_KWARGS,
            )
            if img.status_code == 200:
                s3.put_object(
                    Bucket=settings.R2_BUCKET, Key=f"{r2_path}/thumb.jpg",
                    Body=img.content, ContentType="image/jpeg",
                )
                print_fn(f"   ✅ thumb.jpg (TMDB 剧照, {len(img.content) // 1024} KB)")
        else:
            print_fn("   无 TMDB 剧照, 前端将显示海报")

    except Exception as e:
        print_fn(f"   ⚠️ episode.nfo: {e}")


def write_show_nfo(tmdb_id: int, media_type: str, print_fn=None):
    """写 tvshow.nfo / movie.nfo。"""
    if print_fn is None:
        print_fn = print

    try:
        _auth = settings.tmdb_auth
        try:
            d = httpx.get(
                f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                params={"language": "zh-CN", "append_to_response": "credits,external_ids", **_auth["params"]},
                headers=_auth["headers"],
                timeout=15,
                **_HTTPX_REQUEST_KWARGS,
            ).json()
        except httpx.HTTPError as e:
            raise RuntimeError(_request_error("TMDB show 请求失败", e)) from e

        def esc(s):
            return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        root = "tvshow" if media_type == "tv" else "movie"
        title = d.get("name") or d.get("title") or ""

        lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', f"<{root}>"]
        lines.append(f"  <title>{esc(title)}</title>")
        lines.append(f"  <originaltitle>{esc(d.get('original_name') or d.get('original_title', ''))}</originaltitle>")
        lines.append(f"  <plot>{esc(d.get('overview', ''))}</plot>")
        lines.append(f"  <year>{(d.get('first_air_date') or d.get('release_date', ''))[:4]}</year>")
        lines.append(f"  <rating>{d.get('vote_average', 0)}</rating>")
        lines.append(f"  <uniqueid type=\"tmdb\">{tmdb_id}</uniqueid>")
        imdb = (d.get('external_ids') or {}).get('imdb_id', '')
        if imdb:
            lines.append(f'  <uniqueid type="imdb">{imdb}</uniqueid>')
        for g in d.get("genres", []):
            lines.append(f"  <genre>{esc(g['name'])}</genre>")
        cast = (d.get("credits", {}).get("cast", []))[:15]
        for a in cast:
            lines.append(f"  <actor><name>{esc(a['name'])}</name><role>{esc(a.get('character', ''))}</role></actor>")
        lines.append(f"</{root}>")

        nfo_name = "tvshow.nfo" if media_type == "tv" else "movie.nfo"
        s3 = get_s3_client()
        s3.put_object(
            Bucket=settings.R2_BUCKET, Key=f"tmdb/{media_type}/{tmdb_id}/{nfo_name}",
            Body="\n".join(lines).encode("utf-8"), ContentType="application/xml",
        )
        print_fn(f"   ✅ {nfo_name}")
    except Exception as e:
        print_fn(f"   ⚠️ show nfo: {e}")
