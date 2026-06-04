"""
字幕提取模块（插件内嵌版）
从 MKV/MP4 提取所有字幕轨 → .vtt 文件
"""
import json
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
