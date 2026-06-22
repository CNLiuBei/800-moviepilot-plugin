"""
TMDB HTTP：优先直连 api.themoviedb.org / image.tmdb.org，失败时走代理。

元数据代理：tmdb.liubei.org/3（独立 Worker，代连 TMDB 官方 API，不读站点 D1）。
图片代理：站点 /api/t/p（R2 未命中时回源 image.tmdb.org，不是读站点元数据库）。
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .runtime_config import settings

_HTTPX_KW = {"trust_env": False}
_TMDB_DIRECT_API = "https://api.themoviedb.org/3"
_TMDB_DIRECT_IMAGE = "https://image.tmdb.org/t/p"
_DEFAULT_TMDB_PROXY = "https://tmdb.liubei.org/3"
_DEFAULT_SITE_ORIGIN = "https://guangying.org"

_TMDB_MIRROR_SIZES = frozenset({
    "w45", "w92", "w154", "w185", "w300", "w342", "w500", "w780", "w1280", "h632", "original",
})


def _normalized_path(path: str) -> str:
    path = (path or "").strip()
    return path if path.startswith("/") else f"/{path}"


def tmdb_api_proxy_bases() -> list[str]:
    """元数据 API 代理基址（独立 TMDB 中继或站点 /api/v1/3，不读站点业务库逻辑）。"""
    seen: set[str] = set()
    out: list[str] = []

    def add(base: str) -> None:
        cleaned = (base or "").strip().rstrip("/")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)

    custom = (settings.TMDB_PROXY_BASE or os.environ.get("TMDB_PROXY_BASE") or "").strip()
    if custom:
        add(custom)

    api_base = (settings.API_BASE or "").strip().rstrip("/")
    if api_base:
        add(f"{api_base}/api/v1/3")

    add(f"{_DEFAULT_SITE_ORIGIN}/api/v1/3")
    add(_DEFAULT_TMDB_PROXY)

    return out


def tmdb_image_proxy_bases() -> list[str]:
    """图片代理：tmdb.liubei.org/t/p 或站点 /api/t/p（回源 TMDB 并写入 R2）。"""
    seen: set[str] = set()
    out: list[str] = []

    def add(base: str) -> None:
        cleaned = (base or "").strip().rstrip("/")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)

    custom = (settings.TMDB_IMAGE_PROXY_BASE or os.environ.get("TMDB_IMAGE_PROXY_BASE") or "").strip()
    if custom:
        add(custom)

    add("https://tmdb.liubei.org/t/p")

    api_base = (settings.API_BASE or "").strip().rstrip("/")
    if api_base:
        add(f"{api_base}/api/t/p")
    add(f"{_DEFAULT_SITE_ORIGIN}/api/t/p")

    return out


def _should_fallback(status_code: int | None, error: Exception | None) -> bool:
    if error is not None:
        return True
    if status_code in (None, 200):
        return False
    if status_code in (404, 429):
        return False
    return True


def _is_tmdb_error_payload(data: object) -> bool:
    """TMDB 代理偶发 HTTP 200 但 body 为 status_code/status_message 错误体。"""
    if not isinstance(data, dict):
        return True
    if data.get("success") is False:
        return True
    status_code = data.get("status_code")
    if isinstance(status_code, int) and status_code > 1:
        return True
    if "status_message" in data and "id" not in data:
        return True
    return False


def _direct_api_params(params: dict[str, Any]) -> dict[str, Any]:
    merged = {"language": "zh-CN", **params}
    merged.update(settings.tmdb_auth.get("params") or {})
    return merged


def _proxy_api_params(params: dict[str, Any]) -> dict[str, Any]:
    return {"language": params.get("language", "zh-CN"), **{k: v for k, v in params.items() if k != "api_key"}}


def tmdb_get_json(
    path: str,
    params: dict | None = None,
    timeout: int = 20,
    *,
    direct_timeout: int | None = None,
    proxy_timeout: int | None = None,
) -> dict[str, Any]:
    """GET TMDB JSON；直连失败时依次尝试代理。

    默认直连 4s 超时（国内常不可达），代理 20s（站点 / tmdb.liubei.org 通常更慢但可达）。
    """
    path = _normalized_path(path)
    params = params or {}
    direct_wait = direct_timeout if direct_timeout is not None else min(4, timeout)
    proxy_wait = proxy_timeout if proxy_timeout is not None else timeout
    direct_url = f"{_TMDB_DIRECT_API}{path}"
    last_error: Exception | None = None
    last_status: int | None = None

    try:
        resp = httpx.get(
            direct_url,
            params=_direct_api_params(params),
            headers=dict(settings.tmdb_auth.get("headers") or {}),
            timeout=direct_wait,
            follow_redirects=True,
            **_HTTPX_KW,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not _is_tmdb_error_payload(data):
                return data
            last_status = 502
        else:
            last_status = resp.status_code
        if not _should_fallback(last_status, None):
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        last_error = exc

    proxy_params = _proxy_api_params(params)
    for base in tmdb_api_proxy_bases():
        try:
            resp = httpx.get(
                f"{base}{path}",
                params=proxy_params,
                timeout=proxy_wait,
                follow_redirects=True,
                **_HTTPX_KW,
            )
            if resp.status_code == 200:
                data = resp.json()
                if not _is_tmdb_error_payload(data):
                    return data
                last_status = 502
                continue
            last_status = resp.status_code
        except httpx.HTTPError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise httpx.HTTPStatusError(
        f"TMDB {path} failed (last HTTP {last_status})",
        request=httpx.Request("GET", direct_url),
        response=httpx.Response(last_status or 502),
    )


def tmdb_get_status(path: str, params: dict | None = None, timeout: int = 10) -> int:
    """GET 并返回 HTTP 状态码（含代理回退）。"""
    path = _normalized_path(path)
    params = params or {}
    last_status = 502

    try:
        resp = httpx.get(
            f"{_TMDB_DIRECT_API}{path}",
            params=_direct_api_params(params),
            headers=dict(settings.tmdb_auth.get("headers") or {}),
            timeout=timeout,
            follow_redirects=True,
            **_HTTPX_KW,
        )
        last_status = resp.status_code
        if resp.status_code == 200 or not _should_fallback(resp.status_code, None):
            return resp.status_code
    except httpx.HTTPError:
        pass

    proxy_params = _proxy_api_params(params)
    for base in tmdb_api_proxy_bases():
        try:
            resp = httpx.get(
                f"{base}{path}",
                params=proxy_params,
                timeout=timeout,
                follow_redirects=True,
                **_HTTPX_KW,
            )
            last_status = resp.status_code
            if resp.status_code in (200, 404):
                return resp.status_code
        except httpx.HTTPError:
            continue
    return last_status


def _image_file_part(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("empty image path")
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.lstrip("/")


def tmdb_download_image(path: str, size: str, timeout: int = 15) -> tuple[bytes, str] | None:
    """下载 TMDB 图片；直连失败时走 /api/t/p 代理。"""
    file_part = _image_file_part(path)
    mirror_size = size if size in _TMDB_MIRROR_SIZES else "original"
    direct_url = f"{_TMDB_DIRECT_IMAGE}/{mirror_size}/{file_part}"

    try:
        resp = httpx.get(direct_url, timeout=timeout, follow_redirects=True, **_HTTPX_KW)
        if resp.status_code == 200:
            ct = resp.headers.get("content-type") or "image/jpeg"
            return resp.content, ct
        if not _should_fallback(resp.status_code, None):
            return None
    except httpx.HTTPError:
        pass

    for base in tmdb_image_proxy_bases():
        try:
            resp = httpx.get(
                f"{base}/{mirror_size}/{file_part}",
                timeout=timeout,
                follow_redirects=True,
                **_HTTPX_KW,
            )
            if resp.status_code == 200:
                ct = resp.headers.get("content-type") or "image/jpeg"
                return resp.content, ct
        except httpx.HTTPError:
            continue
    return None
