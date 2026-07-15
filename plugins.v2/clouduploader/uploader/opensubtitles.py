"""OpenSubtitles v3（Stremio Addon）字幕兜底。

当内嵌/外挂都没有中文字幕时，按 IMDb id 拉取字幕并写成 .vtt，
供 CloudUploader 一并上传到 R2。
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

DEFAULT_OPENSUBTITLES_V3_BASE = "https://opensubtitles-v3.strem.io"

_IMDB_RE = re.compile(r"^tt\d+$")

# OpenSubtitles / Stremio 语言码 → 站内字幕 category / 展示名
_LANG_META: dict[str, tuple[str, str]] = {
    "chi": ("zh-Hans", "简体中文"),
    "zho": ("zh-Hans", "简体中文"),
    "zh": ("zh-Hans", "简体中文"),
    "zh-cn": ("zh-Hans", "简体中文"),
    "zh-hans": ("zh-Hans", "简体中文"),
    "zhs": ("zh-Hans", "简体中文"),
    "chs": ("zh-Hans", "简体中文"),
    "cmn": ("zh-Hans", "简体中文"),
    "cht": ("zh-Hant", "繁体中文"),
    "zht": ("zh-Hant", "繁体中文"),
    "zh-tw": ("zh-Hant", "繁体中文"),
    "zh-hk": ("zh-Hant", "繁体中文"),
    "zh-hant": ("zh-Hant", "繁体中文"),
    "eng": ("en", "English"),
    "en": ("en", "English"),
}

_CATEGORY_PRIORITY = ("zh-Hans", "zh-Hant", "en")
_HTTPX_KW = {"trust_env": False}


def build_stremio_media_id(
    media_type: str | None,
    imdb_id: str | None,
    season: int | None = None,
    episode: int | None = None,
) -> tuple[str | None, str | None]:
    """返回 (stremio_type, media_id)；非法时 media_id 为 None。"""
    imdb = str(imdb_id or "").strip()
    if not _IMDB_RE.match(imdb):
        return None, None
    kind = str(media_type or "movie").strip().lower()
    if kind in {"movie", "movies", "film"}:
        return "movie", imdb
    if kind in {"tv", "series", "show", "anime"}:
        if season is None or episode is None:
            return "series", None
        return "series", f"{imdb}:{int(season)}:{int(episode)}"
    return "movie", imdb


def convert_srt_to_webvtt(text: str) -> str:
    """把 SRT 文本转成 WebVTT；已是 VTT 则规范化后返回。"""
    normalized = (
        str(text or "")
        .replace("\ufeff", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    if not normalized:
        return "WEBVTT\n"
    if re.match(r"^WEBVTT\b", normalized, re.I):
        return normalized if normalized.endswith("\n") else f"{normalized}\n"

    lines = normalized.split("\n")
    out: list[str] = ["WEBVTT", ""]
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if re.fullmatch(r"\d+", line):
            i += 1
            continue
        stamp = re.match(
            r"^(\d{1,2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}),(\d{3})(.*)$",
            line,
        )
        if stamp:
            out.append(
                f"{stamp.group(1)}.{stamp.group(2)} --> "
                f"{stamp.group(3)}.{stamp.group(4)}{stamp.group(5) or ''}"
            )
            i += 1
            while i < len(lines) and lines[i].strip():
                out.append(lines[i])
                i += 1
            out.append("")
            continue
        out.append(line)
        i += 1
    return f"{chr(10).join(out).strip()}\n"


def _lang_meta(lang: str) -> tuple[str, str] | None:
    key = str(lang or "").strip().lower().replace("_", "-")
    if key in _LANG_META:
        return _LANG_META[key]
    if key.startswith("zh-hans") or key.startswith("zh-cn"):
        return "zh-Hans", "简体中文"
    if key.startswith("zh-hant") or key.startswith("zh-tw") or key.startswith("zh-hk"):
        return "zh-Hant", "繁体中文"
    if key.startswith("zh"):
        return "zh-Hans", "简体中文"
    return None


def normalize_opensubtitle_entries(payload: object) -> list[dict]:
    """解析 Stremio /subtitles 响应，仅保留 http(s) URL + 认识的中/英语言。"""
    if not isinstance(payload, dict):
        return []
    items = payload.get("subtitles")
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url or not re.match(r"^https?://", url, re.I):
            continue
        lang = str(row.get("lang") or row.get("langCode") or row.get("language") or "").strip()
        meta = _lang_meta(lang)
        if not meta:
            continue
        category, label = meta
        sub_id = str(row.get("id") or f"{lang}-{len(out)}").strip()
        out.append(
            {
                "id": sub_id,
                "url": url,
                "lang": lang or category,
                "category": category,
                "label": label,
            }
        )
    return out


def pick_opensubtitle_candidates(
    entries: list[dict],
    max_per_category: int = 1,
) -> list[dict]:
    """每个 category 最多保留 max_per_category 条，优先简体 → 繁体 → 英文。"""
    picked: list[dict] = []
    counts: dict[str, int] = {key: 0 for key in _CATEGORY_PRIORITY}
    for category in _CATEGORY_PRIORITY:
        for entry in entries:
            if entry.get("category") != category:
                continue
            if counts[category] >= max_per_category:
                break
            picked.append(entry)
            counts[category] += 1
    return picked


def fetch_opensubtitles_subtitles(
    media_type: str,
    media_id: str,
    base_url: str | None = None,
    timeout: float = 20,
) -> list[dict]:
    """请求 OpenSubtitles v3 Stremio 字幕列表。"""
    base = (base_url or DEFAULT_OPENSUBTITLES_V3_BASE).rstrip("/")
    url = f"{base}/subtitles/{media_type}/{media_id}.json"
    with httpx.Client(**_HTTPX_KW) as client:
        response = client.get(
            url,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "Guangying-CloudUploader/1.0",
            },
        )
    if response.status_code >= 400:
        return []
    try:
        payload = response.json()
    except Exception:
        return []
    return normalize_opensubtitle_entries(payload)


def _download_subtitle_text(url: str, timeout: float = 30) -> str | None:
    with httpx.Client(**_HTTPX_KW, follow_redirects=True) as client:
        response = client.get(
            url,
            timeout=timeout,
            headers={
                "Accept": "text/vtt, application/x-subrip, text/plain, */*",
                "User-Agent": "Guangying-CloudUploader/1.0",
            },
        )
    if response.status_code >= 400:
        return None
    text = response.text
    if isinstance(text, str) and text.strip():
        return text
    content = response.content or b""
    if not content:
        return None
    return content.decode("utf-8", errors="ignore")


def resolve_opensubtitles_for_upload(
    output_dir: Path,
    *,
    imdb_id: str | None,
    media_type: str | None = "movie",
    season: int | None = None,
    episode: int | None = None,
    base_url: str | None = None,
    print_fn=print,
    max_per_category: int = 1,
) -> list[dict]:
    """拉取并写出 OpenSubtitles VTT；返回与 extract_subtitles 同形的字幕列表。"""
    stremio_type, media_id = build_stremio_media_id(media_type, imdb_id, season, episode)
    if not stremio_type or not media_id:
        print_fn("   OpenSubtitles: 缺少有效 IMDb id，跳过")
        return []

    try:
        entries = fetch_opensubtitles_subtitles(
            stremio_type,
            media_id,
            base_url=base_url,
        )
    except Exception as exc:
        print_fn(f"   ⚠️ OpenSubtitles 查询失败: {exc}")
        return []

    candidates = pick_opensubtitle_candidates(entries, max_per_category=max_per_category)
    if not candidates:
        print_fn(f"   OpenSubtitles: 无可用中/英字幕 ({stremio_type}/{media_id})")
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for index, item in enumerate(candidates):
        category = item["category"]
        lang = item["lang"]
        label = f"{item['label']} (OpenSubtitles)"
        safe_lang = re.sub(r"[^A-Za-z0-9_-]+", "-", str(category)).strip("-") or "und"
        out_file = f"os-{index}-{safe_lang}.vtt"
        out_path = output_dir / out_file
        try:
            raw = _download_subtitle_text(item["url"])
            if not raw:
                print_fn(f"   ⚠️ OpenSubtitles 下载失败 [{lang}]")
                continue
            vtt = convert_srt_to_webvtt(raw)
            if vtt.count(" --> ") < 1:
                print_fn(f"   ⚠️ OpenSubtitles 字幕无效 [{lang}]")
                continue
            out_path.write_text(vtt, encoding="utf-8")
        except Exception as exc:
            print_fn(f"   ⚠️ OpenSubtitles 写入失败 [{lang}]: {exc}")
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            continue

        results.append(
            {
                "lang": category,
                "label": label,
                "file": out_file,
                "category": category,
                "source": "opensubtitles",
            }
        )
        print_fn(f"   OpenSubtitles [{lang}] {label}: {out_file}")

    return results
