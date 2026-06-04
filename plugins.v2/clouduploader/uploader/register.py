"""
自动入库 + NFO 生成模块（插件内嵌版）
"""
import json
import time as _t

import httpx

from .runtime_config import settings
from .r2 import get_s3_client


def auto_register(
    tmdb_id: int, media_type: str, season: int | None,
    episode: int | None, r2_path: str, quality: str,
    subtitles: list[dict], duration_secs: int | None = None,
    source_type: str = "cmaf", print_fn=None,
) -> bool:
    """
    自动入库: 导入 TMDB 元数据 → 找到 episodeId → 绑播放源 + 字幕
    """
    if print_fn is None:
        print_fn = print

    print_fn("🔗 自动入库...")

    client = httpx.Client(base_url=settings.API_BASE, timeout=60, follow_redirects=True)

    # 认证：优先 admin key，否则用用户名登录
    if settings.API_ADMIN_KEY:
        client.headers["x-admin-key"] = settings.API_ADMIN_KEY
        print_fn("   ✅ API Key 认证")
    elif settings.API_USERNAME and settings.API_PASSWORD:
        resp = client.post("/api/auth/sign-in/username", json={"username": settings.API_USERNAME, "password": settings.API_PASSWORD})
        if resp.status_code != 200:
            print_fn(f"   登录失败: {resp.text[:100]}")
            return False
        print_fn("   ✅ 登录成功")
    else:
        print_fn("   ❌ 未配置 API_ADMIN_KEY 或 API_USERNAME/API_PASSWORD")
        return False
    try:
        return _do_register(client, tmdb_id, media_type, season, episode,
                            r2_path, quality, subtitles, duration_secs, source_type, print_fn)
    finally:
        client.close()


def _do_register(
    client: httpx.Client, tmdb_id: int, media_type: str, season: int | None,
    episode: int | None, r2_path: str, quality: str,
    subtitles: list[dict], duration_secs: int | None,
    source_type: str, print_fn,
) -> bool:
    """内部实现，确保 client 在 finally 中关闭。"""

    # 使用 x-admin-key 或已登录 session 认证

    # 2. 导入/刷新 TMDB 元数据
    resp = client.post("/api/admin/import-single", json={
        "tmdbId": tmdb_id, "type": media_type, "fetchEpisodes": True,
    })
    if resp.status_code != 200:
        print_fn(f"   导入失败: {resp.text[:100]}")
        return False
    try:
        import_data = resp.json()
    except (ValueError, json.JSONDecodeError):
        print_fn(f"   ❌ 导入响应非 JSON: {resp.text[:100]}")
        return False
    movie_id = import_data.get("movieId")
    if not movie_id:
        print_fn(f"   ❌ 导入未返回 movieId: {str(import_data)[:120]}")
        return False
    print_fn(f"   ✅ 影片 ID: {movie_id} (episodes: {import_data.get('episodes')})")

    # 3. 找 episodeId（带重试：导入后分集落库可能有延迟）
    #    复用最后一次成功的 detail，供第 4 步删旧源使用，避免重复请求。
    episode_id = None
    detail = None
    if season and episode:
        for attempt, delay in enumerate((1, 2, 3), start=1):
            _t.sleep(delay)
            detail_resp = client.get(f"/api/admin/movies/{movie_id}")
            if detail_resp.status_code != 200:
                continue
            try:
                detail = detail_resp.json()
            except (ValueError, json.JSONDecodeError):
                continue
            for ep in detail.get("episodes", []):
                if ep.get("season") == season and ep.get("episode") == episode:
                    episode_id = ep["id"]
                    break
            if episode_id:
                break

        if episode_id:
            print_fn(f"   ✅ 分集 ID: {episode_id} (S{season:02d}E{episode:02d})")
        else:
            print_fn(f"   ⚠️ 未找到 S{season:02d}E{episode:02d} 的分集记录")

    # 4. 删旧播放源 (电影: 删除所有无 episodeId 的源; 剧集: 删除同 episodeId 的源)
    #    复用第 3 步已取到的 detail；电影或未取到时再请求一次。
    if detail is None:
        detail_resp = client.get(f"/api/admin/movies/{movie_id}")
        try:
            detail = detail_resp.json() if detail_resp.status_code == 200 else {}
        except (ValueError, json.JSONDecodeError):
            detail = {}
    for src in detail.get("sources", []):
        if episode_id:
            if src.get("episodeId") == episode_id:
                client.delete(f"/api/admin/sources/{src['id']}")
        else:
            # 电影: 删除所有无 episodeId 的旧源（避免重复）
            if not src.get("episodeId"):
                client.delete(f"/api/admin/sources/{src['id']}")

    # 5. 添加播放源
    play_url = f"/api/r2/{r2_path}/master.m3u8" if source_type == "cmaf" else f"/api/r2/{r2_path}/stream.m3u8"
    resp = client.post("/api/admin/sources", json={
        "movieId": movie_id,
        "episodeId": episode_id,
        "label": f"原画 {quality}" if quality else "原画",
        "type": source_type,
        "url": play_url,
        "quality": quality or "原画",
        "sortOrder": 0,
    })
    if resp.status_code == 200:
        print_fn(f"   ✅ 播放源已绑定: {play_url}")
    else:
        print_fn(f"   播放源绑定失败: {resp.text[:100]}")
        return False

    # 6. 更新分集时长（秒）
    if episode_id and duration_secs:
        r = client.patch(f"/api/admin/episodes/{episode_id}", json={"duration": duration_secs})
        if r.status_code == 200:
            print_fn(f"   ✅ 分集时长已更新: {duration_secs // 60} 分 {duration_secs % 60} 秒 ({duration_secs}s)")
        else:
            print_fn(f"   ⚠️ 时长更新失败: {r.text[:80]}")

    # 7. 字幕清单
    if subtitles:
        sub_manifest = [
            {"lang": sub["lang"], "label": sub["label"], "url": f"/api/r2/{r2_path}/{sub['file']}"}
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
    return True


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
        ep = httpx.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/episode/{episode}",
            params={"language": "zh-CN", **_auth["params"]},
            headers=_auth["headers"],
            timeout=15,
        ).json()

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

        # 缩略图
        if ep.get('still_path'):
            img = httpx.get(
                f"https://image.tmdb.org/t/p/w500{ep['still_path']}",
                timeout=15, follow_redirects=True,
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
        d = httpx.get(
            f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
            params={"language": "zh-CN", "append_to_response": "credits,external_ids", **_auth["params"]},
            headers=_auth["headers"],
            timeout=15,
        ).json()

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
            Bucket=settings.R2_BUCKET, Key=f"videos/800-{tmdb_id}/{nfo_name}",
            Body="\n".join(lines).encode("utf-8"), ContentType="application/xml",
        )
        print_fn(f"   ✅ {nfo_name}")
    except Exception as e:
        print_fn(f"   ⚠️ show nfo: {e}")
