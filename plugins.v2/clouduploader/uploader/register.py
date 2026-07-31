"""
自动入库 + NFO 生成模块（插件内嵌版）
"""
import json
import time

import httpx

from .runtime_config import ConfigError, normalize_base_url, settings
from .r2 import get_s3_client
from .tmdb_http import tmdb_download_image, tmdb_get_json

_HTTPX_REQUEST_KWARGS = {"trust_env": False}
_TMDB_NFO_TIMEOUT = 25
_TMDB_NFO_DIRECT_TIMEOUT = 4
_DIRECTOR_JOBS = frozenset({"Director", "Co-Director"})
_WRITER_JOBS = frozenset({"Writer", "Screenplay", "Teleplay", "Story"})


def _esc(value) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _crew_names(crew, jobs: frozenset[str], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for member in crew or []:
        name = (member.get("name") or "").strip()
        if not name or member.get("job") not in jobs or name in seen:
            continue
        seen.add(name)
        names.append(name)
        if limit and len(names) >= limit:
            break
    return names


def _studio_names(d: dict, media_type: str) -> list[str]:
    if media_type == "tv":
        return [name for name in (n.get("name") for n in d.get("networks", [])) if name]
    return [
        name
        for name in (c.get("name") for c in d.get("production_companies", [])[:5])
        if name
    ]


def _episode_crew(ep: dict) -> list[dict]:
    credits = ep.get("credits") if isinstance(ep.get("credits"), dict) else {}
    return list(ep.get("crew") or credits.get("crew") or [])


def _episode_guest_stars(ep: dict) -> list[dict]:
    credits = ep.get("credits") if isinstance(ep.get("credits"), dict) else {}
    return list(ep.get("guest_stars") or credits.get("guest_stars") or [])


def _append_show_metadata_lines(lines: list[str], d: dict, media_type: str) -> None:
    tagline = (d.get("tagline") or "").strip()
    if tagline:
        lines.append(f"  <tagline>{_esc(tagline)}</tagline>")
        lines.append(f"  <outline>{_esc(tagline)}</outline>")

    crew = (d.get("credits") or {}).get("crew") or []
    for name in _crew_names(crew, _DIRECTOR_JOBS, limit=5):
        lines.append(f"  <director>{_esc(name)}</director>")
    for name in _crew_names(crew, _WRITER_JOBS, limit=8):
        lines.append(f"  <credits>{_esc(name)}</credits>")

    for studio in _studio_names(d, media_type):
        lines.append(f"  <studio>{_esc(studio)}</studio>")


def _format_tmdb_rating(vote_average, vote_count=None) -> str:
    try:
        text = f"{float(vote_average):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        text = "0"
    attrs = 'name="themoviedb" max="10" default="true"'
    if vote_count is not None:
        attrs += f' votes="{int(vote_count)}"'
    return f"  <rating {attrs}>{text}</rating>"


def _premiered_date(d: dict, media_type: str) -> str:
    if media_type == "tv":
        return (d.get("first_air_date") or "").strip()
    return (d.get("release_date") or "").strip()


def _append_show_identity_lines(lines: list[str], d: dict, media_type: str, tmdb_id: int) -> None:
    premiered = _premiered_date(d, media_type)
    if premiered:
        lines.append(f"  <premiered>{premiered}</premiered>")

    if media_type == "tv":
        status = (d.get("status") or "").strip()
        if status:
            lines.append(f"  <status>{_esc(status)}</status>")
        last_air = (d.get("last_air_date") or "").strip()
        if last_air and last_air != premiered:
            lines.append(f"  <enddate>{last_air}</enddate>")

    for code in d.get("origin_country") or []:
        if code:
            lines.append(f"  <country>{_esc(code)}</country>")

    lines.append(_format_tmdb_rating(d.get("vote_average", 0), d.get("vote_count")))
    lines.append(f"  <uniqueid type=\"tmdb\" default=\"true\">{tmdb_id}</uniqueid>")
    imdb = (d.get("external_ids") or {}).get("imdb_id", "")
    if imdb:
        lines.append(f'  <uniqueid type="imdb" default="false">{imdb}</uniqueid>')


_TMDB_MIRROR_SIZES = frozenset({
    "w45", "w92", "w154", "w185", "w300", "w342", "w500", "w780", "w1280", "h632", "original",
})


def _tmdb_file_name(path: str) -> str | None:
    raw = (path or "").strip()
    if not raw:
        return None
    name = raw.rsplit("/", 1)[-1]
    if not name or name.startswith("."):
        return None
    return name


def _tmdb_mirror_key(path: str, size: str) -> str | None:
    """R2 key aligned with TMDB CDN layout: tmdb/t/p/{size}/{file}."""
    file_name = _tmdb_file_name(path)
    if not file_name:
        return None
    mirror_size = size if size in _TMDB_MIRROR_SIZES else "original"
    return f"tmdb/t/p/{mirror_size}/{file_name}"


def _content_type_for_tmdb_path(path: str) -> str:
    lower = (path or "").lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".svg"):
        return "image/svg+xml"
    return "image/jpeg"


def _pick_logo_path(images: dict | None) -> str | None:
    """Pick TMDB logo path (zh → en → first), aligned with 800 workers pickLogo."""
    logos = (images or {}).get("logos") or []
    if not logos:
        return None
    zh = next((item for item in logos if item.get("iso_639_1") == "zh"), None)
    en = next((item for item in logos if item.get("iso_639_1") == "en"), None)
    chosen = zh or en or logos[0]
    path = (chosen or {}).get("file_path") or ""
    if not path:
        return None
    if path.lower().endswith(".svg"):
        return None
    return path


def _upload_tmdb_image(path: str, size: str, r2_key: str, print_fn, label: str) -> None:
    if not path:
        return
    try:
        downloaded = tmdb_download_image(path, size, timeout=_TMDB_NFO_TIMEOUT)
        if not downloaded:
            print_fn(f"   ⚠️ {label} 下载失败（直连与代理均失败）")
            return
        body, content_type = downloaded
        get_s3_client().put_object(
            Bucket=settings.R2_BUCKET,
            Key=r2_key,
            Body=body,
            ContentType=content_type or _content_type_for_tmdb_path(path),
        )
        print_fn(f"   ✅ {label} ({len(body) // 1024} KB)")
    except Exception as e:
        print_fn(f"   ⚠️ {label}: {e}")


def _api_error(resp: httpx.Response, prefix: str) -> str:
    """从 API 响应中提取可读错误信息。"""
    payload = {}
    try:
        data = resp.json()
        if isinstance(data, dict):
            payload = data
    except (ValueError, json.JSONDecodeError, TypeError):
        payload = {}
    detail = (
        payload.get("message")
        or payload.get("detail")
        or payload.get("error")
        or payload.get("status_message")
        or (resp.text or "").strip()[:500]
    )
    if detail:
        return f"{prefix} [{resp.status_code}] — {detail}"
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
    if source_type == "cmaf":
        play_url = f"/api/r2/{r2_path}/master.m3u8"
    elif source_type == "mp4":
        play_url = f"/api/r2/{r2_path}/video.mp4"
    else:
        play_url = f"/api/r2/{r2_path}/stream.m3u8"

    # TV/动漫：导入时同步全部分集，保证详情页剧集列表可用
    ok, err = _import_tmdb(client, tmdb_id, media_type, fetch_episodes=(media_type == "tv"), print_fn=print_fn)
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
    print_fn("   📣 站点已配置 Telegram 上新通知时，首次绑定播放源后将自动推送到频道")
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
    """按 TMDB 身份绑定播放源（同分辨率槽位替换）。返回 (ok, error_msg)。"""
    from .resolution_key import format_source_label, normalize_quality_key

    quality_key = normalize_quality_key(quality)
    payload = {
        "tmdbId": tmdb_id,
        "mediaType": media_type,
        "label": format_source_label(quality_key),
        "sourceType": source_type,
        "url": play_url,
        "quality": quality_key,
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

    def put_minimal_episode_nfo(reason: str):
        try:
            lines = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                "<episodedetails>",
                f"  <title>S{season:02d}E{episode:02d}</title>",
                f"  <season>{season}</season>",
                f"  <episode>{episode}</episode>",
                f"  <uniqueid type=\"tmdb\">{tmdb_id}</uniqueid>",
                f"  <fileinfo><streamdetails><video><codec>copy</codec><aspect>{_esc(resolution or 'original')}</aspect></video></streamdetails></fileinfo>",
                "</episodedetails>",
            ]
            get_s3_client().put_object(
                Bucket=settings.R2_BUCKET, Key=f"{r2_path}/episode.nfo",
                Body="\n".join(lines).encode("utf-8"), ContentType="application/xml",
            )
            print_fn(f"   ⚠️ episode.nfo 使用最小元数据: {reason}")
        except Exception as e:
            print_fn(f"   ⚠️ episode.nfo 最小元数据写入失败: {e}")

    try:
        try:
            ep = tmdb_get_json(
                f"/tv/{tmdb_id}/season/{season}/episode/{episode}",
                {"append_to_response": "credits"},
                timeout=_TMDB_NFO_TIMEOUT,
                direct_timeout=_TMDB_NFO_DIRECT_TIMEOUT,
                proxy_timeout=_TMDB_NFO_TIMEOUT,
            )
        except httpx.HTTPError as e:
            put_minimal_episode_nfo(_request_error("TMDB episode 请求失败", e))
            return

        crew = _episode_crew(ep)
        guest_stars = _episode_guest_stars(ep)

        lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<episodedetails>"]
        lines.append(f"  <title>{_esc(ep.get('name', ''))}</title>")
        lines.append(f"  <season>{season}</season>")
        lines.append(f"  <episode>{episode}</episode>")
        lines.append(f"  <plot>{_esc(ep.get('overview', ''))}</plot>")
        air_date = (ep.get("air_date") or "").strip()
        if air_date:
            lines.append(f"  <aired>{air_date}</aired>")
            lines.append(f"  <premiered>{air_date}</premiered>")
        lines.append(_format_tmdb_rating(ep.get("vote_average", 0), ep.get("vote_count")))
        runtime = ep.get("runtime")
        if runtime:
            lines.append(f"  <runtime>{int(runtime)}</runtime>")
        episode_tmdb_id = ep.get("id", "")
        if episode_tmdb_id:
            lines.append(f"  <uniqueid type=\"tmdb\" default=\"true\">{episode_tmdb_id}</uniqueid>")
        for name in _crew_names(crew, _DIRECTOR_JOBS, limit=3):
            lines.append(f"  <director>{_esc(name)}</director>")
        for name in _crew_names(crew, _WRITER_JOBS, limit=5):
            lines.append(f"  <credits>{_esc(name)}</credits>")
        for g in guest_stars[:10]:
            lines.append(
                f"  <actor><name>{_esc(g['name'])}</name>"
                f"<role>{_esc(g.get('character', ''))}</role></actor>"
            )
        still_path = ep.get("still_path") or ""
        if still_path:
            lines.append(f"  <thumb>https://image.tmdb.org/t/p/w500{still_path}</thumb>")
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

        if still_path:
            mirror_key = _tmdb_mirror_key(still_path, "w500")
            if mirror_key:
                _upload_tmdb_image(
                    still_path, "w500", mirror_key, print_fn,
                    label=mirror_key,
                )
        else:
            print_fn("   无 TMDB 剧照")

    except Exception as e:
        print_fn(f"   ⚠️ episode.nfo: {e}")


def write_show_nfo(tmdb_id: int, media_type: str, print_fn=None):
    """写 tvshow.nfo / movie.nfo。"""
    if print_fn is None:
        print_fn = print

    def put_minimal_show_nfo(reason: str):
        try:
            root = "tvshow" if media_type == "tv" else "movie"
            nfo_name = "tvshow.nfo" if media_type == "tv" else "movie.nfo"
            lines = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                f"<{root}>",
                f"  <title>TMDB {tmdb_id}</title>",
                f"  <uniqueid type=\"tmdb\">{tmdb_id}</uniqueid>",
                f"</{root}>",
            ]
            get_s3_client().put_object(
                Bucket=settings.R2_BUCKET, Key=f"tmdb/{media_type}/{tmdb_id}/{nfo_name}",
                Body="\n".join(lines).encode("utf-8"), ContentType="application/xml",
            )
            print_fn(f"   ⚠️ {nfo_name} 使用最小元数据: {reason}")
        except Exception as e:
            print_fn(f"   ⚠️ show nfo 最小元数据写入失败: {e}")

    try:
        try:
            d = tmdb_get_json(
                f"/{media_type}/{tmdb_id}",
                {
                    "append_to_response": "credits,external_ids,images",
                    "include_image_language": "zh,en,null",
                },
                timeout=_TMDB_NFO_TIMEOUT,
                direct_timeout=_TMDB_NFO_DIRECT_TIMEOUT,
                proxy_timeout=_TMDB_NFO_TIMEOUT,
            )
        except httpx.HTTPError as e:
            put_minimal_show_nfo(_request_error("TMDB show 请求失败", e))
            return

        root = "tvshow" if media_type == "tv" else "movie"
        title = d.get("name") or d.get("title") or ""

        lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', f"<{root}>"]
        lines.append(f"  <title>{_esc(title)}</title>")
        lines.append(f"  <originaltitle>{_esc(d.get('original_name') or d.get('original_title', ''))}</originaltitle>")
        lines.append(f"  <plot>{_esc(d.get('overview', ''))}</plot>")
        _append_show_metadata_lines(lines, d, media_type)
        year = (d.get("first_air_date") or d.get("release_date") or "")[:4]
        if year:
            lines.append(f"  <year>{year}</year>")
        if media_type == "movie" and d.get("runtime"):
            lines.append(f"  <runtime>{int(d['runtime'])}</runtime>")
        _append_show_identity_lines(lines, d, media_type, tmdb_id)
        for g in d.get("genres", []):
            lines.append(f"  <genre>{_esc(g['name'])}</genre>")
        cast = (d.get("credits", {}).get("cast", []))[:15]
        for a in cast:
            lines.append(
                f"  <actor><name>{_esc(a['name'])}</name>"
                f"<role>{_esc(a.get('character', ''))}</role></actor>"
            )
        poster_path = d.get("poster_path") or ""
        backdrop_path = d.get("backdrop_path") or ""
        if poster_path:
            lines.append(f"  <thumb>https://image.tmdb.org/t/p/w500{poster_path}</thumb>")
        if backdrop_path:
            lines.append(f"  <fanart>https://image.tmdb.org/t/p/w1280{backdrop_path}</fanart>")
        lines.append(f"</{root}>")

        nfo_name = "tvshow.nfo" if media_type == "tv" else "movie.nfo"
        r2_base = f"tmdb/{media_type}/{tmdb_id}"
        s3 = get_s3_client()
        s3.put_object(
            Bucket=settings.R2_BUCKET, Key=f"{r2_base}/{nfo_name}",
            Body="\n".join(lines).encode("utf-8"), ContentType="application/xml",
        )
        print_fn(f"   ✅ {nfo_name}")
        if poster_path:
            mirror_key = _tmdb_mirror_key(poster_path, "w500")
            if mirror_key:
                _upload_tmdb_image(
                    poster_path, "w500", mirror_key, print_fn,
                    label=mirror_key,
                )
        if backdrop_path:
            mirror_key = _tmdb_mirror_key(backdrop_path, "w1280")
            if mirror_key:
                _upload_tmdb_image(
                    backdrop_path, "w1280", mirror_key, print_fn,
                    label=mirror_key,
                )
        logo_path = _pick_logo_path(d.get("images"))
        if logo_path:
            mirror_key = _tmdb_mirror_key(logo_path, "w500")
            if mirror_key:
                _upload_tmdb_image(
                    logo_path, "w500", mirror_key, print_fn,
                    label=mirror_key,
                )
    except Exception as e:
        print_fn(f"   ⚠️ show nfo: {e}")
