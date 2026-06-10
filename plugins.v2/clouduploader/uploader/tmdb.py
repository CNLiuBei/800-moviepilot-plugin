"""
TMDB API 封装（插件内嵌版）

认证自动兼容 TMDB v4 Bearer Token 与 v3 API Key（见 runtime_config.tmdb_auth）。
"""
import httpx

from .runtime_config import settings


def _get(url: str, extra_params: dict, timeout: int = 15) -> httpx.Response:
    auth = settings.tmdb_auth
    params = {**auth["params"], **extra_params}
    return httpx.get(url, params=params, headers=auth["headers"], timeout=timeout)


def search_tmdb(query: str, media_type: str = "tv") -> list:
    """搜索 TMDB，返回最多 5 条结果。"""
    resp = _get(
        f"https://api.themoviedb.org/3/search/{media_type}",
        {"query": query, "language": "zh-CN", "page": 1},
    )
    resp.raise_for_status()
    return resp.json().get("results", [])[:5]


def search_tmdb_multi(query: str) -> list:
    """搜索 TMDB multi 接口（同时搜 movie + tv），返回最多 5 条。"""
    resp = _get(
        "https://api.themoviedb.org/3/search/multi",
        {"query": query, "language": "zh-CN", "page": 1},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [r for r in results if r.get("media_type") in ("movie", "tv")][:5]


def detect_media_type_by_id(tmdb_id: int) -> str:
    """通过 TMDB ID 自动检测 movie / tv，默认 movie。"""
    resp = _get(f"https://api.themoviedb.org/3/movie/{tmdb_id}", {"language": "zh-CN"}, timeout=10)
    if resp.status_code == 200:
        return "movie"
    resp = _get(f"https://api.themoviedb.org/3/tv/{tmdb_id}", {"language": "zh-CN"}, timeout=10)
    if resp.status_code == 200:
        return "tv"
    return "movie"
