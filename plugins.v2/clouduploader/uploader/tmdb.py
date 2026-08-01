"""
TMDB API 封装（插件内嵌版）

认证自动兼容 TMDB v4 Bearer Token 与 v3 API Key（见 runtime_config.tmdb_auth）。
直连失败时自动回退到 TMDB 代理（见 tmdb_http）。
"""
from .tmdb_http import tmdb_get_json, tmdb_get_status


def search_tmdb(query: str, media_type: str = "tv") -> list:
    """搜索 TMDB，返回最多 5 条结果。"""
    data = tmdb_get_json(
        f"/search/{media_type}",
        {"query": query, "page": 1},
    )
    return data.get("results", [])[:5]


def search_tmdb_multi(query: str) -> list:
    """搜索 TMDB multi 接口（同时搜 movie + tv），返回最多 5 条。"""
    data = tmdb_get_json(
        "/search/multi",
        {"query": query, "page": 1},
    )
    results = data.get("results", [])
    return [r for r in results if r.get("media_type") in ("movie", "tv")][:5]


def detect_media_type_by_id(tmdb_id: int) -> str:
    """通过 TMDB ID 自动检测 movie / tv，默认 movie。"""
    if tmdb_get_status(f"/movie/{tmdb_id}", timeout=10) == 200:
        return "movie"
    if tmdb_get_status(f"/tv/{tmdb_id}", timeout=10) == 200:
        return "tv"
    return "movie"


def verify_tmdb_metadata(
    tmdb_id: int,
    media_type: str = "tv",
    season: int | None = None,
    episode: int | None = None,
) -> tuple[bool, str, str | None, str | None]:
    """上传前校验 TMDB 元数据是否可查。

    Returns:
        (ok, resolved_media_type, error_message, warning_message)
        - 作品缺失 / 分集查询异常：ok=False，error 有值
        - 分集在 TMDB 不存在：ok=True，warning 提示按文件名季集继续
    """
    requested = (media_type or "tv").strip().lower()
    if requested not in ("movie", "tv"):
        requested = "tv"

    def _fetch_show(kind: str) -> dict | None:
        try:
            data = tmdb_get_json(f"/{kind}/{tmdb_id}", timeout=10) or {}
        except Exception:
            return None
        if data.get("id"):
            return data
        return None

    data = _fetch_show(requested)
    resolved = requested
    if data is None:
        alternate = "movie" if requested == "tv" else "tv"
        data = _fetch_show(alternate)
        resolved = alternate

    if data is None:
        return False, requested, f"TMDB 未找到元数据: ID {tmdb_id}", None

    if resolved == "tv" and season is not None and episode is not None:
        try:
            ep = tmdb_get_json(
                f"/tv/{tmdb_id}/season/{int(season)}/episode/{int(episode)}",
                timeout=10,
            ) or {}
        except Exception as exc:
            return False, resolved, f"TMDB 分集查询失败 S{int(season)}E{int(episode)}: {exc}", None
        if not ep.get("id"):
            return (
                True,
                resolved,
                None,
                f"TMDB 未找到分集: S{int(season)}E{int(episode)}，按文件名季集继续上传",
            )

    return True, resolved, None, None


def get_original_language(tmdb_id: int, media_type: str = "tv") -> tuple[str | None, str | None]:
    """读取 TMDB 作品的 original_language（ISO 639-1，如 ko/ja/en）。

    Returns:
        (language, error_message)
    """
    try:
        data = tmdb_get_json(f"/{media_type}/{tmdb_id}", timeout=10) or {}
    except Exception as exc:
        return None, str(exc)
    lang = data.get("original_language")
    if not lang:
        return None, "TMDB 响应缺少 original_language"
    normalized = str(lang).strip().lower()
    return (normalized or None), None


def get_imdb_id(tmdb_id: int, media_type: str = "movie") -> tuple[str | None, str | None]:
    """读取 TMDB external_ids.imdb_id（tt…）。

    Returns:
        (imdb_id, error_message)
    """
    kind = (media_type or "movie").strip().lower()
    if kind not in ("movie", "tv"):
        kind = "movie"
    try:
        data = tmdb_get_json(f"/{kind}/{int(tmdb_id)}/external_ids", timeout=10) or {}
    except Exception as exc:
        return None, str(exc)
    imdb = str(data.get("imdb_id") or "").strip()
    if not imdb.startswith("tt"):
        # 电影详情偶发直接带 imdb_id
        try:
            detail = tmdb_get_json(f"/{kind}/{int(tmdb_id)}", timeout=10) or {}
        except Exception as exc:
            return None, str(exc)
        imdb = str(detail.get("imdb_id") or "").strip()
    if not imdb.startswith("tt"):
        return None, "TMDB 未返回 imdb_id"
    return imdb, None
