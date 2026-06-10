"""
字幕提取模块（插件内嵌版）
从 MKV/MP4 提取所有字幕轨 → .vtt 文件
"""
import json
import math
import re
import subprocess
from pathlib import Path

from .runtime_config import settings

_LANG_LABELS = {
    "chi": "简体中文", "zho": "中文", "zhs": "简体中文", "zht": "繁体中文",
    "zh": "中文", "zh-Hans": "简体中文", "zh-Hant": "繁体中文",
    "eng": "English", "en": "English",
    "jpn": "日本語", "ja": "日本語",
    "kor": "한국어", "ko": "한국어",
    "und": "未知",
}

_LANG_BCP47 = {
    "chi": "zh-Hans",
    "zho": "zh-Hans",
    "cmn": "zh-Hans",
    "zhs": "zh-Hans",
    "zh-hans": "zh-Hans",
    "chs": "zh-Hans",
    "zht": "zh-Hant",
    "zh-hant": "zh-Hant",
    "cht": "zh-Hant",
    "yue": "zh-Hant",
    "can": "zh-Hant",
    "eng": "en",
    "en": "en",
    "jpn": "ja",
    "ja": "ja",
    "kor": "ko",
    "ko": "ko",
}


def _bcp47_language(lang: str) -> str:
    normalized = (lang or "und").strip().replace("_", "-")
    return _LANG_BCP47.get(normalized.lower(), normalized or "und")


def _safe_track_dir(lang: str, used: dict[str, int]) -> str:
    base = re.sub(r"[^A-Za-z0-9-]+", "-", _bcp47_language(lang)).strip("-") or "und"
    used[base] = used.get(base, 0) + 1
    return base if used[base] == 1 else f"{base}-{used[base]}"


def _parse_vtt_timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (TypeError, ValueError):
        return 0.0


def _vtt_duration(text: str) -> float:
    duration = 1.0
    for match in re.finditer(r"-->\s*([0-9:.,]+)", text):
        duration = max(duration, _parse_vtt_timestamp(match.group(1)))
    return duration


def normalize_webvtt(text: str) -> str:
    """Normalize WebVTT text for Apple HLS subtitle playlists."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("WEBVTT"):
        text = f"WEBVTT\n\n{text}"
    if "X-TIMESTAMP-MAP=" not in text:
        text = re.sub(
            r"^WEBVTT[^\n]*\n?",
            lambda m: f"{m.group(0).strip()}\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n",
            text,
            count=1,
        )
    return text.rstrip() + "\n"


def generate_hls_subtitle_playlists(
    subtitles: list[dict],
    output_dir: Path,
    print_fn=None,
) -> list[dict]:
    """
    Generate Apple-compatible WebVTT media playlists under subs/<lang>/.

    The original extracted VTT files are kept in place for subtitles.json and
    older clients. The generated playlists are referenced from master.m3u8.
    """
    if print_fn is None:
        print_fn = print

    subtitle_tracks: list[dict] = []
    used_dirs: dict[str, int] = {}

    for index, sub in enumerate(subtitles):
        source_file = output_dir / sub["file"]
        if not source_file.exists():
            continue

        text = normalize_webvtt(source_file.read_text(encoding="utf-8", errors="ignore"))
        source_file.write_text(text, encoding="utf-8")

        track_dir_name = _safe_track_dir(sub.get("lang", "und"), used_dirs)
        track_dir = output_dir / "subs" / track_dir_name
        track_dir.mkdir(parents=True, exist_ok=True)

        vtt_path = track_dir / "full.vtt"
        vtt_path.write_text(text, encoding="utf-8")

        duration = max(1.0, _vtt_duration(text))
        target_duration = max(1, math.ceil(duration))
        playlist = "\n".join([
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f"#EXTINF:{duration:.3f},",
            "full.vtt",
            "#EXT-X-ENDLIST",
            "",
        ])
        (track_dir / "stream.m3u8").write_text(playlist, encoding="utf-8")

        lang = _bcp47_language(sub.get("lang", "und"))
        hls_uri = f"subs/{track_dir_name}/stream.m3u8"
        sub["hls_uri"] = hls_uri
        sub["hls_lang"] = lang
        subtitle_tracks.append({
            "lang": lang,
            "name": sub.get("label") or _LANG_LABELS.get(sub.get("lang"), lang),
            "uri": hls_uri,
            "file": sub["file"],
        })

    if subtitle_tracks:
        print_fn(f"   ✅ HLS 字幕轨已生成: {len(subtitle_tracks)} 条")
    return subtitle_tracks


def extract_subtitles(input_path: str, output_dir: Path, print_fn=None) -> list[dict]:
    """从 MKV/MP4 提取所有字幕轨 → .vtt 文件。"""
    if print_fn is None:
        print_fn = print

    probe_cmd = [
        settings.FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "s", input_path,
    ]
    result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return []

    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (json.JSONDecodeError, ValueError):
        print_fn("   ⚠️ 字幕探测输出解析失败，跳过字幕")
        return []
    if not streams:
        return []

    subtitles = []
    for i, stream in enumerate(streams):
        lang = stream.get("tags", {}).get("language", "und")
        title = stream.get("tags", {}).get("title", "")
        label = title or _LANG_LABELS.get(lang, lang)

        out_file = f"sub-{i}-{lang}.vtt"
        out_path = output_dir / out_file

        extract_cmd = [
            settings.FFMPEG_BIN, "-i", input_path,
            "-map", f"0:s:{i}", "-c:s", "webvtt",
            str(out_path), "-y",
        ]
        try:
            r = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            print_fn(f"   ⚠️ 字幕轨 {i} ({lang}) 提取超时，跳过")
            if out_path.exists():
                out_path.unlink()
            continue
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 10:
            subtitles.append({"lang": lang, "label": label, "file": out_file})
            print_fn(f"   字幕 [{lang}] {label}: {out_file}")
        else:
            if out_path.exists():
                out_path.unlink()

    return subtitles
