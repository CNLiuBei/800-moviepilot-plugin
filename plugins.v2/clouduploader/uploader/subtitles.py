"""
字幕提取模块（插件内嵌版）
从 MKV/MP4 提取所有字幕轨 → .vtt 文件
"""
import json
import math
import re
import subprocess
from pathlib import Path

from .env import resolved_bin
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
    "cn": "zh-Hans",
    "sc": "zh-Hans",
    "zhs": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-cn": "zh-Hans",
    "chs": "zh-Hans",
    "gb": "zh-Hans",
    "gbk": "zh-Hans",
    "gb2312": "zh-Hans",
    "simplified": "zh-Hans",
    "zht": "zh-Hant",
    "zh-hant": "zh-Hant",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "cht": "zh-Hant",
    "tc": "zh-Hant",
    "big5": "zh-Hant",
    "traditional": "zh-Hant",
    "yue": "zh-Hant",
    "can": "zh-Hant",
    "eng": "en",
    "english": "en",
    "en": "en",
    "jpn": "ja",
    "jp": "ja",
    "japanese": "ja",
    "ja": "ja",
    "kor": "ko",
    "kr": "ko",
    "korean": "ko",
    "ko": "ko",
}

_SUBTITLE_CATEGORY_LABELS = {
    "original": "原声",
    "en": "英文",
    "zh-Hans": "简体中文",
    "zh-Hant": "繁体中文",
}

_SUBTITLE_CATEGORY_ORDER = ("original", "en", "zh-Hans", "zh-Hant")
_EN_LANGS = {"eng", "english", "en", "en-us", "en-gb"}
_ZH_HANS_LANGS = {
    "chi", "zho", "cmn", "cn", "sc", "zhs", "zh", "zh-cn", "zh-hans",
    "chs", "gb", "gbk", "gb2312", "simplified",
}
_ZH_HANT_LANGS = {
    "zht", "zh-tw", "zh-hk", "zh-mo", "zh-hant", "cht", "tc", "big5",
    "traditional", "yue", "can",
}
_NON_ORIGINAL_LANGS = _EN_LANGS | _ZH_HANS_LANGS | _ZH_HANT_LANGS | {"und", ""}

_EN_LABEL_HINTS = ("english", "eng", "英文", "英语", "英文字幕")
_ZH_HANS_LABEL_HINTS = (
    "简体", "简中", "简体中文", "简体字幕", "简日", "简英", "简繁",
    "chs", "sc", "zh-hans", "zh_cn", "zh-cn", "simplified",
)
_ZH_HANT_LABEL_HINTS = (
    "繁體", "繁体", "繁中", "繁體中文", "繁体中文", "繁體字幕", "繁体字幕",
    "繁日", "繁英", "cht", "tc", "zh-hant", "zh_tw", "zh-tw", "traditional",
)
_GENERIC_ORIGINAL_LABEL_HINTS = (
    "原声", "原聲", "原文", "原版", "original",
)
_ORIGINAL_LABEL_HINTS_BY_LANG: dict[str, tuple[str, ...]] = {
    "ja": ("日文", "日语", "日語", "日本語", "japanese", "jpn", "jp"),
    "ko": ("韩文", "韓文", "韩语", "韓語", "한국어", "korean", "kor", "kr"),
    "en": ("英文", "英语", "english", "eng"),
    "fr": ("法文", "法语", "french", "fra", "fre"),
    "de": ("德文", "德语", "german", "deu", "ger"),
    "es": ("西班牙", "spanish", "spa"),
    "it": ("意大利", "italian", "ita"),
    "pt": ("葡萄牙", "portuguese", "por"),
    "ru": ("俄文", "俄语", "russian", "rus"),
    "th": ("泰文", "泰语", "thai", "tha"),
    "vi": ("越南", "vietnamese", "vie"),
    "ar": ("阿拉伯", "arabic", "ara"),
}
_TRACK_LANG_ALIASES: dict[str, set[str]] = {
    "ja": {"ja", "jpn", "jp", "japanese"},
    "ko": {"ko", "kor", "kr", "korean"},
    "en": {"en", "eng", "english"},
    "fr": {"fr", "fre", "fra", "french"},
    "de": {"de", "deu", "ger", "german"},
    "es": {"es", "spa", "spanish"},
    "it": {"it", "ita", "italian"},
    "pt": {"pt", "por", "portuguese"},
    "ru": {"ru", "rus", "russian"},
    "th": {"th", "tha", "thai"},
    "vi": {"vi", "vie", "vietnamese"},
    "ar": {"ar", "ara", "arabic"},
}
_ORIGINAL_LABEL_HINTS = _GENERIC_ORIGINAL_LABEL_HINTS + tuple(
    hint for hints in _ORIGINAL_LABEL_HINTS_BY_LANG.values() for hint in hints
)
_LOW_PRIORITY_LABEL_HINTS = (
    "sdh", "cc", "closed caption", "hearing", "听障", "聽障",
    "commentary", "评论", "評論", "forced", "强制", "強制",
)

_EXTERNAL_SUB_EXTS = frozenset({".ass", ".srt", ".ssa", ".sub", ".vtt"})
_CHINESE_SUB_CATEGORIES = frozenset({"zh-Hans", "zh-Hant"})


def _bcp47_language(lang: str) -> str:
    normalized = (lang or "und").strip().replace("_", "-")
    return _LANG_BCP47.get(normalized.lower(), normalized or "und")


def _normalized_lang_token(lang: str) -> str:
    return (lang or "und").strip().replace("_", "-").lower()


def _language_code_aliases(code: str) -> set[str]:
    normalized = (code or "").strip().lower()
    if not normalized:
        return set()
    aliases = {normalized, *_TRACK_LANG_ALIASES.get(normalized, set())}
    for track_code, bcp47 in _LANG_BCP47.items():
        if bcp47.lower() == normalized:
            aliases.add(track_code)
    return aliases


def _track_matches_language_code(lang: str, language_code: str) -> bool:
    normalized_lang = _normalized_lang_token(lang)
    aliases = _language_code_aliases(language_code)
    return normalized_lang in aliases or _bcp47_language(lang).lower() in aliases


def _label_matches_language_code(label: str, language_code: str) -> bool:
    normalized_label = (label or "").strip().lower()
    hints = _ORIGINAL_LABEL_HINTS_BY_LANG.get(language_code.strip().lower(), ())
    return any(hint in normalized_label for hint in hints)


def _infer_track_language_code(lang: str, label: str) -> str | None:
    normalized_lang = _normalized_lang_token(lang)
    for code, aliases in _TRACK_LANG_ALIASES.items():
        if normalized_lang in aliases or _bcp47_language(lang).lower() == code:
            return code
    normalized_label = (label or "").strip().lower()
    for code, hints in _ORIGINAL_LABEL_HINTS_BY_LANG.items():
        if any(hint in normalized_label for hint in hints):
            return code
    return None


def _is_original_language_track(lang: str, label: str, original_language: str) -> bool:
    code = original_language.strip().lower()
    if _track_matches_language_code(lang, code):
        return True
    if _label_matches_language_code(label, code):
        return True
    normalized_label = (label or "").strip().lower()
    if any(hint in normalized_label for hint in _GENERIC_ORIGINAL_LABEL_HINTS):
        track_code = _infer_track_language_code(lang, label)
        if track_code is None:
            return True
        return track_code == code
    return False


def _subtitle_category(lang: str, label: str, original_language: str | None = None) -> str | None:
    normalized_lang = _normalized_lang_token(lang)
    normalized_label = (label or "").strip().lower()

    if any(hint in normalized_label for hint in _ZH_HANT_LABEL_HINTS):
        return "zh-Hant"
    if any(hint in normalized_label for hint in _ZH_HANS_LABEL_HINTS):
        return "zh-Hans"
    if any(hint in normalized_label for hint in _EN_LABEL_HINTS):
        return "en"

    if normalized_lang in _ZH_HANT_LANGS:
        return "zh-Hant"
    if normalized_lang in _ZH_HANS_LANGS:
        return "zh-Hans"
    if normalized_lang in _EN_LANGS:
        return "en"

    if original_language:
        if _is_original_language_track(lang, label, original_language):
            return "original"
        if normalized_lang not in _NON_ORIGINAL_LANGS:
            return None
        return None

    if any(hint in normalized_label for hint in _ORIGINAL_LABEL_HINTS):
        return "original"
    if normalized_lang not in _NON_ORIGINAL_LANGS:
        return "original"
    return None


def _subtitle_score(
    stream: dict,
    label: str,
    original_language: str | None = None,
) -> tuple[int, int, int]:
    disposition = stream.get("disposition") or {}
    normalized_label = (label or "").strip().lower()
    quality_penalty = 0
    if any(normalized_label.find(hint) >= 0 for hint in _LOW_PRIORITY_LABEL_HINTS):
        quality_penalty += 20
    if disposition.get("forced"):
        quality_penalty += 15
    if disposition.get("hearing_impaired"):
        quality_penalty += 15
    if disposition.get("default"):
        quality_penalty -= 5
    if disposition.get("comment"):
        quality_penalty += 20
    if original_language:
        tags = stream.get("tags") or {}
        track_lang = tags.get("language", "und")
        if _is_original_language_track(track_lang, label, original_language):
            quality_penalty -= 10
    return (quality_penalty, int(stream.get("subtitle_index") or 0), int(stream.get("index") or 0))


def _preferred_subtitle_streams(
    streams: list[dict],
    print_fn=print,
    original_language: str | None = None,
) -> list[dict]:
    candidates_by_category: dict[str, list[dict]] = {category: [] for category in _SUBTITLE_CATEGORY_ORDER}
    for subtitle_index, stream in enumerate(streams):
        tags = stream.get("tags") or {}
        lang = tags.get("language", "und")
        title = tags.get("title", "")
        label = title or _LANG_LABELS.get(lang, lang)
        category = _subtitle_category(lang, label, original_language)
        if not category:
            continue
        item = {
            "stream": stream,
            "subtitle_index": subtitle_index,
            "lang": lang,
            "label": label,
            "category": category,
            "score": _subtitle_score(
                {**stream, "subtitle_index": subtitle_index},
                label,
                original_language,
            ),
        }
        candidates_by_category.setdefault(category, []).append(item)

    selected: list[dict] = []
    for category in _SUBTITLE_CATEGORY_ORDER:
        candidates = candidates_by_category.get(category) or []
        if candidates:
            selected.append(sorted(candidates, key=lambda item: item["score"])[0])

    if selected and len(selected) < len(streams):
        kept = "、".join(
            f"{_SUBTITLE_CATEGORY_LABELS[item['category']]}[{item['lang']}] {item['label']}"
            for item in selected
        )
        print_fn(f"   智能保留字幕: {kept}；丢弃 {len(streams) - len(selected)} 条多余字幕")
    return selected


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


def _format_vtt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def _vtt_duration(text: str) -> float:
    duration = 1.0
    for match in re.finditer(r"-->\s*([0-9:.,]+)", text):
        duration = max(duration, _parse_vtt_timestamp(match.group(1)))
    return duration


def normalize_webvtt(text: str, timestamp_map: str | None = None) -> str:
    """Normalize WebVTT text for Apple HLS subtitle playlists."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("WEBVTT"):
        text = f"WEBVTT\n\n{text}"
    text = re.sub(r"^X-TIMESTAMP-MAP=.*\n?", "", text, flags=re.MULTILINE)
    if timestamp_map:
        text = re.sub(
            r"^WEBVTT[^\n]*\n?",
            lambda m: f"{m.group(0).strip()}\n{timestamp_map}\n\n",
            text,
            count=1,
        )
    return text.rstrip() + "\n"


def _parse_hls_media_segments(playlist_path: Path) -> list[float]:
    if not playlist_path.exists():
        return []
    durations: list[float] = []
    for line in playlist_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line_s = line.strip()
        if not line_s.startswith("#EXTINF:"):
            continue
        try:
            durations.append(float(line_s.split(":", 1)[1].split(",", 1)[0]))
        except (IndexError, ValueError):
            continue
    return durations


def _hls_media_playlist(output_dir: Path) -> Path | None:
    for name in ("stream-v.m3u8", "stream.m3u8"):
        candidate = output_dir / name
        if candidate.exists():
            return candidate
    return None


def _probe_hls_media_start(
    output_dir: Path,
    print_fn=print,
    ffprobe_bin: str | None = None,
) -> float:
    playlist = _hls_media_playlist(output_dir)
    if not playlist:
        return 0.0
    ffprobe = resolved_bin(ffprobe_bin, settings.FFPROBE_BIN)
    if not ffprobe:
        print_fn("   ⚠️ ffprobe 不可用，字幕 MPEGTS 回退 0")
        return 0.0
    cmd = [
        ffprobe,
        "-v", "quiet",
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,crypto,data",
        "-show_entries", "format=start_time",
        "-of", "json",
        str(playlist),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(output_dir))
        if result.returncode != 0:
            return 0.0
        data = json.loads(result.stdout)
        start = data.get("format", {}).get("start_time")
        return max(0.0, float(start or 0.0))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        print_fn(f"   ⚠️ HLS 媒体起始时间探测失败，字幕 MPEGTS 回退 0: {e}")
        return 0.0


def _parse_webvtt_cues(text: str) -> list[dict]:
    cues: list[dict] = []
    cue_lines: list[str] = []
    for raw_line in normalize_webvtt(text).splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("WEBVTT") or line.startswith("X-TIMESTAMP-MAP="):
            continue
        if not line.strip():
            if cue_lines:
                cue = _cue_from_lines(cue_lines)
                if cue:
                    cues.append(cue)
                cue_lines = []
            continue
        cue_lines.append(line)
    if cue_lines:
        cue = _cue_from_lines(cue_lines)
        if cue:
            cues.append(cue)
    return cues


def _cue_from_lines(lines: list[str]) -> dict | None:
    time_index = -1
    for i, line in enumerate(lines):
        if "-->" in line:
            time_index = i
            break
    if time_index < 0:
        return None
    time_line = lines[time_index]
    try:
        start_raw, rest = time_line.split("-->", 1)
        end_raw = rest.strip().split(None, 1)[0]
        start = _parse_vtt_timestamp(start_raw)
        end = _parse_vtt_timestamp(end_raw)
    except (IndexError, ValueError):
        return None
    if end <= start:
        return None
    return {"start": start, "end": end, "lines": _with_default_cue_settings(lines, time_index)}


def _with_default_cue_settings(lines: list[str], time_index: int) -> list[str]:
    result = list(lines)
    time_line = result[time_index]
    if re.search(r"\s(line|position|align|size|vertical):", time_line):
        return result
    result[time_index] = f"{time_line} line:85% position:50% align:center"
    return result


def _timestamp_map(local_time: float, media_start_time: float) -> str:
    mpegts = round((media_start_time + local_time) * 90000)
    return f"X-TIMESTAMP-MAP=LOCAL:{_format_vtt_timestamp(local_time)},MPEGTS:{mpegts}"


def _write_segmented_vtt_playlist(
    text: str,
    track_dir: Path,
    segment_durations: list[float],
    media_start_time: float,
) -> float:
    cues = _parse_webvtt_cues(text)
    if not segment_durations:
        duration = max(1.0, _vtt_duration(text))
        segment_durations = [duration]

    current = 0.0
    segment_files: list[tuple[str, float]] = []
    for index, duration in enumerate(segment_durations):
        segment_start = current
        segment_end = current + duration
        segment_name = f"seg-{index:05d}.vtt"
        header = [
            "WEBVTT",
            _timestamp_map(segment_start, media_start_time),
            "",
        ]
        body: list[str] = []
        for cue in cues:
            if cue["start"] < segment_end and cue["end"] > segment_start:
                body.extend(cue["lines"])
                body.append("")
        (track_dir / segment_name).write_text("\n".join(header + body).rstrip() + "\n", encoding="utf-8")
        segment_files.append((segment_name, duration))
        current = segment_end

    target_duration = max(1, math.ceil(max(duration for _, duration in segment_files)))
    playlist_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for segment_name, duration in segment_files:
        playlist_lines.append(f"#EXTINF:{duration:.3f},")
        playlist_lines.append(segment_name)
    playlist_lines.append("#EXT-X-ENDLIST")
    playlist_lines.append("")
    (track_dir / "stream.m3u8").write_text("\n".join(playlist_lines), encoding="utf-8")
    return current


def generate_hls_subtitle_playlists(
    subtitles: list[dict],
    output_dir: Path,
    print_fn=None,
    ffprobe_bin: str | None = None,
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
    used_names: dict[str, int] = {}
    media_start_time = _probe_hls_media_start(
        output_dir, print_fn=print_fn, ffprobe_bin=ffprobe_bin
    )
    segment_durations = _parse_hls_media_segments(_hls_media_playlist(output_dir) or output_dir / "stream-v.m3u8")
    if segment_durations:
        print_fn(
            f"   HLS 字幕时间线: media_start={media_start_time:.3f}s, "
            f"{len(segment_durations)} 段"
        )

    for index, sub in enumerate(subtitles):
        source_file = output_dir / sub["file"]
        if not source_file.exists():
            continue

        text = normalize_webvtt(source_file.read_text(encoding="utf-8", errors="ignore"))
        source_file.write_text(text, encoding="utf-8")

        track_dir_name = _safe_track_dir(sub.get("lang", "und"), used_dirs)
        track_dir = output_dir / "subs" / track_dir_name
        track_dir.mkdir(parents=True, exist_ok=True)

        _write_segmented_vtt_playlist(text, track_dir, segment_durations, media_start_time)

        lang = _bcp47_language(sub.get("lang", "und"))
        display_name = sub.get("label") or _LANG_LABELS.get(sub.get("lang"), lang)
        # Apple 要求同一个 SUBTITLES GROUP-ID 内 NAME 全局唯一，不按语言区分。
        name_key = display_name
        used_names[name_key] = used_names.get(name_key, 0) + 1
        if used_names[name_key] > 1:
            display_name = f"{display_name} {used_names[name_key]}"
        hls_uri = f"subs/{track_dir_name}/stream.m3u8"
        sub["hls_uri"] = hls_uri
        sub["hls_lang"] = lang
        subtitle_tracks.append({
            "lang": lang,
            "name": display_name,
            "uri": hls_uri,
            "file": sub["file"],
        })

    if subtitle_tracks:
        print_fn(f"   ✅ HLS 字幕轨已生成: {len(subtitle_tracks)} 条")
    return subtitle_tracks


def _has_chinese_subtitle(subtitles: list[dict]) -> bool:
    return any(sub.get("category") in _CHINESE_SUB_CATEGORIES for sub in subtitles)


def _external_file_score(path: Path) -> tuple[int, int, int]:
    """外挂字幕候选优先级（值越小越优先）。"""
    name = path.name.lower()
    ext_rank = {
        ".ass": 0, ".srt": 1, ".ssa": 2, ".vtt": 3, ".sub": 4,
    }.get(path.suffix.lower(), 9)
    hint_bonus = 0
    if any(hint in name for hint in _ZH_HANS_LABEL_HINTS):
        hint_bonus -= 3
    if any(hint in name for hint in _ZH_HANT_LABEL_HINTS):
        hint_bonus -= 2
    if any(hint in name for hint in _EN_LABEL_HINTS):
        hint_bonus += 1
    return (hint_bonus, ext_rank, len(name))


def _infer_external_lang_label(path: Path) -> tuple[str, str]:
    """从外挂字幕文件名推断语言与展示名。"""
    stem = path.stem
    tokens = re.split(r"[.\-_+\s]+", stem.lower())
    lang = "und"
    for token in tokens:
        if token in _ZH_HANT_LANGS or token in {"cht", "tc", "big5", "traditional"}:
            lang = "zht"
            break
        if token in _ZH_HANS_LANGS or token in {"chs", "sc", "gb", "simplified", "cn"}:
            lang = "chi"
            break
        if token in _EN_LANGS:
            lang = "eng"
            break
        if token in {"jpn", "jp", "ja"}:
            lang = "jpn"
            break
        if token in {"kor", "ko", "kr"}:
            lang = "kor"
            break
    label = stem
    if lang == "chi":
        label = "简体中文 (外挂)"
    elif lang == "zht":
        label = "繁体中文 (外挂)"
    elif lang == "und":
        label = "外挂字幕"
    else:
        label = f"{_LANG_LABELS.get(lang, lang)} (外挂)"
    return lang, label


def _external_subtitle_metadata(
    path: Path,
    video_path: Path,
    original_language: str | None = None,
) -> dict | None:
    lang, label = _infer_external_lang_label(path)
    category = _subtitle_category(lang, label, original_language)
    if not category and path.stem == video_path.stem:
        # 与视频同名的 .ass/.srt 在中文剧集场景下默认按简体中文字幕处理
        category = "zh-Hans"
        lang = "chi"
        if label == "外挂字幕":
            label = "简体中文 (外挂)"
    if not category:
        return None
    return {
        "lang": lang,
        "label": label,
        "category": category,
        "source_path": str(path),
    }


def find_external_subtitle_files(video_path: str) -> list[Path]:
    """查找与视频同目录下的外挂字幕（同名或同季集标识）。"""
    video = Path(video_path)
    parent = video.parent
    if not parent.is_dir():
        return []

    stem = video.stem
    seen: set[str] = set()
    found: list[Path] = []

    def _add(candidate: Path):
        resolved = str(candidate.resolve())
        if resolved in seen or not candidate.is_file():
            return
        seen.add(resolved)
        found.append(candidate)

    for ext in _EXTERNAL_SUB_EXTS:
        _add(parent / f"{stem}{ext}")

    episode_match = re.search(r"[sS](\d+)[eE](\d+)", stem)
    if episode_match:
        season, episode = episode_match.groups()
        episode_pattern = re.compile(rf"[sS]0*{season}[eE]0*{episode}", re.IGNORECASE)
        for child in parent.iterdir():
            if child.suffix.lower() not in _EXTERNAL_SUB_EXTS:
                continue
            if child.stem == stem:
                continue
            if episode_pattern.search(child.stem):
                _add(child)

    found.sort(key=_external_file_score)
    return found


def _convert_subtitle_file_to_vtt(
    src: Path,
    dest: Path,
    ffmpeg_bin: str | None = None,
) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolved_bin(ffmpeg_bin, settings.FFMPEG_BIN)
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-c:s", "webvtt",
        str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and dest.is_file() and dest.stat().st_size > 10


def _load_external_subtitles(
    video_path: str,
    output_dir: Path,
    print_fn=print,
    original_language: str | None = None,
    ffmpeg_bin: str | None = None,
) -> list[dict]:
    video = Path(video_path)
    files = find_external_subtitle_files(video_path)
    if not files:
        return []

    best_by_category: dict[str, tuple[tuple[int, int, int], Path, dict]] = {}
    for src in files:
        meta = _external_subtitle_metadata(src, video, original_language)
        if not meta:
            continue
        category = meta["category"]
        score = _external_file_score(src)
        prev = best_by_category.get(category)
        if prev and prev[0] <= score:
            continue
        best_by_category[category] = (score, src, meta)

    converted: list[dict] = []
    for category in _SUBTITLE_CATEGORY_ORDER:
        entry = best_by_category.get(category)
        if not entry:
            continue
        _, src, meta = entry
        index = len(converted)
        safe_lang = re.sub(r"[^A-Za-z0-9_-]+", "-", meta["lang"]).strip("-") or "und"
        out_file = f"sub-ext-{index}-{safe_lang}.vtt"
        out_path = output_dir / out_file
        if not _convert_subtitle_file_to_vtt(src, out_path, ffmpeg_bin=ffmpeg_bin):
            print_fn(f"   ⚠️ 外挂字幕转换失败: {src.name}")
            continue
        converted.append({
            "lang": meta["lang"],
            "label": meta["label"],
            "file": out_file,
            "category": meta["category"],
            "source": "external",
        })
        print_fn(f"   外挂字幕 [{meta['lang']}] {meta['label']}: {src.name} → {out_file}")

    if converted:
        kept = "、".join(
            f"{_SUBTITLE_CATEGORY_LABELS[item['category']]}[{item['lang']}]"
            for item in converted
        )
        print_fn(f"   选用外挂字幕: {kept}")
    return converted


def resolve_subtitles_for_upload(
    input_path: str,
    output_dir: Path,
    print_fn=None,
    original_language: str | None = None,
    imdb_id: str | None = None,
    media_type: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    opensubtitles: bool = True,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
) -> list[dict]:
    """
    解析上传任务字幕：优先内嵌；内嵌无中文时回退同目录外挂字幕。
    若内嵌与外挂均含中文，仅保留内嵌（取其一，避免重复轨）。
    仍无中文时，按 IMDb 走 OpenSubtitles v3 拉取并写成 VTT 一并上传。
    """
    if print_fn is None:
        print_fn = print

    embedded = extract_subtitles(
        input_path,
        output_dir,
        print_fn=print_fn,
        original_language=original_language,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
    )
    if _has_chinese_subtitle(embedded):
        print_fn("   内嵌字幕已含中文，使用内嵌字幕")
        return embedded

    external = _load_external_subtitles(
        input_path,
        output_dir,
        print_fn=print_fn,
        original_language=original_language,
        ffmpeg_bin=ffmpeg_bin,
    )
    if not external:
        result = list(embedded)
    elif not embedded:
        result = list(external)
    else:
        merged = list(embedded)
        existing_categories = {sub.get("category") for sub in merged}
        for sub in external:
            category = sub.get("category")
            if category in _CHINESE_SUB_CATEGORIES and category not in existing_categories:
                merged.append(sub)
                existing_categories.add(category)
        result = merged

    if _has_chinese_subtitle(result):
        return result

    if not opensubtitles:
        return result

    from .opensubtitles import resolve_opensubtitles_for_upload

    print_fn("   无中文字幕，尝试 OpenSubtitles v3…")
    remote = resolve_opensubtitles_for_upload(
        output_dir,
        imdb_id=imdb_id,
        media_type=media_type or "movie",
        season=season,
        episode=episode,
        print_fn=print_fn,
    )
    if not remote:
        return result

    if not result:
        return remote

    merged = list(result)
    existing_categories = {sub.get("category") for sub in merged}
    for sub in remote:
        category = sub.get("category")
        if category and category not in existing_categories:
            merged.append(sub)
            existing_categories.add(category)
    return merged


def extract_subtitles(
    input_path: str,
    output_dir: Path,
    print_fn=None,
    original_language: str | None = None,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
) -> list[dict]:
    """从 MKV/MP4 提取所有字幕轨 → .vtt 文件。

    original_language: TMDB original_language（ISO 639-1），用于判定「原声」字幕轨。
    """
    if print_fn is None:
        print_fn = print

    ffprobe = resolved_bin(ffprobe_bin, settings.FFPROBE_BIN)
    ffmpeg = resolved_bin(ffmpeg_bin, settings.FFMPEG_BIN)
    if not ffprobe or not ffmpeg:
        print_fn("   ⚠️ ffmpeg/ffprobe 不可用，跳过字幕提取")
        return []

    probe_cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
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

    selected_streams = _preferred_subtitle_streams(
        streams,
        print_fn=print_fn,
        original_language=original_language,
    )
    if not selected_streams:
        return []

    subtitles = []
    for selected_index, item in enumerate(selected_streams):
        i = item["subtitle_index"]
        lang = item["lang"]
        label = item["label"]

        safe_lang = re.sub(r"[^A-Za-z0-9_-]+", "-", lang).strip("-") or "und"
        out_file = f"sub-{selected_index}-{safe_lang}.vtt"
        out_path = output_dir / out_file

        extract_cmd = [
            ffmpeg, "-i", input_path,
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
            subtitles.append({"lang": lang, "label": label, "file": out_file, "category": item["category"]})
            print_fn(f"   字幕 [{lang}] {label}: {out_file}")
        else:
            if out_path.exists():
                out_path.unlink()

    return subtitles
