"""
HLS fMP4 切片模块（插件内嵌版）。

FFmpeg 一次性完成：视频 copy + TMDB 默认音轨（杜比等转 AAC）→ stream.m3u8 + init.mp4 + seg-*.m4s。
生成后由 mediastreamvalidator 校验（见 job_runner）。
"""
import json
import math
import subprocess
from pathlib import Path

from .runtime_config import settings
from .subtitles import _LOW_PRIORITY_LABEL_HINTS, _subtitle_category

_COPYABLE_VIDEO_CODECS = frozenset({"h264", "hevc", "h265"})
# Apple HLS fMP4 可直接封装、无需转码的音轨编码（见 HLS Authoring Specification）
_COPYABLE_AUDIO_CODECS = frozenset({
    "aac", "mp4a",
    "ac3", "ac-3",
    "eac3", "eac-3", "ec-3",
    "flac", "alac",
})
# Chrome/Firefox MSE 仅稳定解码 AAC；杜比等须转码（Safari 原生 HLS 可播杜比）
_TRANSCODE_TO_AAC_AUDIO_CODECS = frozenset({
    "ac3", "ac-3",
    "eac3", "eac-3", "ec-3",
    "flac", "alac",
})


def get_video_duration(filepath: str, ffprobe_bin: str | None = None) -> int | None:
    """用 ffprobe 读取视频真实时长, 返回秒数 (整数), 失败返回 None。"""
    try:
        result = subprocess.run(
            [ffprobe_bin or settings.FFPROBE_BIN, "-v", "quiet",
             "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=30,
        )
        raw = result.stdout.strip()
        if raw:
            return int(float(raw))
    except Exception:
        pass
    return None


def probe_video_info(input_path: str, ffprobe_bin: str | None = None) -> dict:
    """用 ffprobe 探测源视频编码、分辨率、码率和时长。"""
    ffprobe = ffprobe_bin or settings.FFPROBE_BIN
    probe_stream = subprocess.run(
        [ffprobe, "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,profile,level,width,height,bit_rate,avg_frame_rate,r_frame_rate",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=60,
    )
    probe_format = subprocess.run(
        [ffprobe, "-v", "quiet",
         "-show_entries", "format=duration,bit_rate",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=60,
    )

    info = {
        "codec": "h264",
        "width": 1920,
        "height": 1080,
        "bitrate": 2000000,
        "average_bitrate": 2000000,
        "duration": 0.0,
        "frame_rate": 0.0,
        "profile": "",
        "level": 0,
    }

    def parse_rate(value: str) -> float:
        try:
            if not value or value == "0/0":
                return 0.0
            if "/" in value:
                num, den = value.split("/", 1)
                den_f = float(den)
                return float(num) / den_f if den_f else 0.0
            return float(value)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    try:
        stream_data = json.loads(probe_stream.stdout)
        streams = stream_data.get("streams", [])
        if streams:
            s = streams[0]
            info["codec"] = s.get("codec_name", "h264").lower()
            info["profile"] = s.get("profile", "")
            info["level"] = int(s.get("level") or 0)
            info["width"] = int(s.get("width", 1920))
            info["height"] = int(s.get("height", 1080))
            stream_br = s.get("bit_rate")
            if stream_br and stream_br != "N/A":
                info["bitrate"] = int(stream_br)
                info["average_bitrate"] = int(stream_br)
            info["frame_rate"] = parse_rate(s.get("avg_frame_rate") or s.get("r_frame_rate") or "")
    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    try:
        format_data = json.loads(probe_format.stdout)
        fmt = format_data.get("format", {})
        dur = fmt.get("duration")
        if dur and dur != "N/A":
            info["duration"] = float(dur)
        fmt_br = fmt.get("bit_rate")
        if fmt_br and fmt_br != "N/A":
            fmt_bitrate = int(fmt_br)
            if info["bitrate"] == 2000000:
                info["bitrate"] = fmt_bitrate
            info["average_bitrate"] = fmt_bitrate
    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    return info


def probe_audio_streams(input_path: str, ffprobe_bin: str | None = None) -> list[dict]:
    """探测所有音轨，返回 [{audio_index, lang, title, codec, channels}, ...]。"""
    result = subprocess.run(
        [ffprobe_bin or settings.FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "a", input_path],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return []
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (json.JSONDecodeError, ValueError):
        return []

    tracks: list[dict] = []
    for audio_index, stream in enumerate(streams):
        tags = stream.get("tags") or {}
        tracks.append({
            "audio_index": audio_index,
            "lang": (tags.get("language") or "und").strip().lower(),
            "title": (tags.get("title") or "").strip(),
            "codec": (stream.get("codec_name") or "").strip().lower(),
            "channels": int(stream.get("channels") or 2),
            "tags": tags,
            "disposition": stream.get("disposition") or {},
        })
    return tracks


def _lang_matches(code: str, lang: str, title: str) -> bool:
    code = (code or "").strip().lower()
    lang = (lang or "").strip().lower()
    title = (title or "").strip().lower()
    if not code:
        return False
    if lang == code or lang.startswith(f"{code}-"):
        return True
    if code == "ja" and lang in {"jpn", "jp"}:
        return True
    if code == "ko" and lang in {"kor", "kr"}:
        return True
    if code == "en" and lang in {"eng", "en-us", "en-gb"}:
        return True
    if code == "zh" and lang in {"zho", "chi", "cmn", "zh-cn", "zh-hans"}:
        return True
    hints = {
        "ja": ("日文", "日语", "日語", "japanese", "jpn"),
        "ko": ("韩文", "韩语", "韓語", "korean", "kor"),
        "en": ("英文", "英语", "english", "eng"),
        "zh": ("国语", "中文", "华语", "mandarin", "chinese"),
    }
    return any(h in title for h in hints.get(code, ()))


def _audio_track_score(track: dict, label: str, original_language: str | None) -> tuple[int, int]:
    disposition = track.get("disposition") or {}
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
    if original_language and _lang_matches(original_language, track["lang"], label):
        quality_penalty -= 10
    return quality_penalty, int(track.get("audio_index") or 0)


def select_audio_streams(
    tracks: list[dict],
    original_language: str | None = None,
    *,
    print_fn=None,
) -> list[dict]:
    """保留所有非评论音轨；TMDB original_language 匹配轨设为 DEFAULT。"""
    if print_fn is None:
        print_fn = print
    if not tracks:
        return []

    def _is_commentary(title: str) -> bool:
        normalized = (title or "").strip().lower()
        return any(h in normalized for h in ("commentary", "评论", "导评", "descr", "director"))

    def _is_original(track: dict) -> bool:
        label = track.get("title") or ""
        if original_language and _lang_matches(original_language, track["lang"], label):
            return True
        return _subtitle_category(track["lang"], label, original_language) == "original"

    kept = [t for t in tracks if not _is_commentary(t.get("title", ""))]
    if not kept:
        return []

    default_idx = 0
    if original_language:
        for i, track in enumerate(kept):
            if _is_original(track):
                default_idx = i
                break
    else:
        for i, track in enumerate(kept):
            if (track.get("disposition") or {}).get("default"):
                default_idx = i
                break

    selected: list[dict] = []
    for i, track in enumerate(kept):
        label = track.get("title") or _audio_display_name(track)
        selected.append({
            **track,
            "title": label,
            "is_default": i == default_idx,
        })

    default_track = selected[default_idx]
    if len(selected) == 1:
        src = f"TMDB 原声 [{original_language}]" if original_language else "容器默认"
        print_fn(f"   音轨 1 条 ({src}): [{default_track['lang']}] {default_track['title']}")
    else:
        if original_language:
            print_fn(
                f"   音轨 {len(selected)} 条，TMDB 原声 [{original_language}] 默认: "
                f"[{default_track['lang']}] {default_track['title']}"
            )
        else:
            print_fn(
                f"   音轨 {len(selected)} 条，默认: "
                f"[{default_track['lang']}] {default_track['title']}"
            )
    return selected


def _audio_display_name(track: dict) -> str:
    _LANG_NAMES = {
        "zho": "国语", "chi": "国语", "cmn": "国语", "zh": "国语",
        "yue": "粤语", "can": "粤语",
        "eng": "英语", "en": "英语",
        "jpn": "日语", "ja": "日语",
        "kor": "韩语", "ko": "韩语",
        "und": "默认",
    }
    lang = track.get("lang") or "und"
    return _LANG_NAMES.get(lang, lang)


def select_audio_stream(tracks: list[dict], original_language: str | None = None) -> dict | None:
    """兼容旧调用：返回首选单音轨。"""
    picked = select_audio_streams(tracks, original_language)
    return picked[0] if picked else None


def _audio_codec_string(codec: str) -> str:
    normalized = (codec or "").lower()
    if normalized in {"aac", "mp4a"}:
        return "mp4a.40.2"
    if normalized in {"ac3", "ac-3"}:
        return "ac-3"
    if normalized in {"eac3", "eac-3", "ec-3"}:
        return "ec-3"
    if normalized == "flac":
        return "flac"
    if normalized == "alac":
        return "alac"
    return "mp4a.40.2"


def _should_transcode_audio_to_aac(codec: str) -> bool:
    normalized = (codec or "").strip().lower()
    if normalized in {"aac", "mp4a"}:
        return False
    if normalized in _TRANSCODE_TO_AAC_AUDIO_CODECS:
        return True
    return not _is_copyable_audio_codec(normalized)


def _is_copyable_audio_codec(codec: str) -> bool:
    return (codec or "").strip().lower() in _COPYABLE_AUDIO_CODECS


def _normalize_media_playlist(playlist_path: Path) -> None:
    """master 已声明 INDEPENDENT-SEGMENTS 时，media playlist 去重。"""
    try:
        lines = playlist_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    filtered = [line for line in lines if line.strip() != "#EXT-X-INDEPENDENT-SEGMENTS"]
    if filtered != lines:
        playlist_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")


def apple_hls_slice(
    input_path: str,
    output_dir: Path,
    print_fn=None,
    original_language: str | None = None,
) -> dict | None:
    """FFmpeg fMP4 HLS：视频 copy + TMDB 默认音轨，一次性切片为 stream.m3u8。"""
    if print_fn is None:
        print_fn = print

    output_dir.mkdir(parents=True, exist_ok=True)
    probe_info = probe_video_info(input_path)
    src_codec = probe_info["codec"]
    if src_codec not in _COPYABLE_VIDEO_CODECS:
        print_fn(
            f"   ❌ 视频编码 {src_codec} 不在免转码白名单内（仅 h264/hevc），已跳过切片"
        )
        return None

    audio_tracks = probe_audio_streams(input_path)
    selected_tracks = select_audio_streams(audio_tracks, original_language, print_fn=print_fn)
    if not selected_tracks:
        print_fn("   ❌ 源文件无可用音轨，无法切片")
        return None

    default_track = next(t for t in selected_tracks if t.get("is_default"))
    extra = len(selected_tracks) - 1
    if extra:
        print_fn(f"   ℹ️  另有 {extra} 条音轨未上架（仅 TMDB 默认音轨进主播放列表）")

    audio_map = f"0:a:{default_track['audio_index']}"
    codec = (default_track.get("codec") or "").lower()
    label = default_track.get("title") or default_track.get("lang") or "音轨"
    channels = int(default_track.get("channels") or 2)
    transcode_channels = min(channels, settings.CMAF_AUDIO_CHANNELS)
    segment_seconds = str(settings.HLS_SEGMENT_SECONDS)

    print_fn(
        f"   FFmpeg fMP4 切片: 视频 {src_codec} copy + 默认音轨 "
        f"[{default_track['lang']}] {label}"
    )

    cmd = [
        settings.FFMPEG_BIN, "-i", input_path,
        "-map", "0:v:0", "-map", audio_map,
        "-c:v", "copy",
        "-copyts", "-avoid_negative_ts", "make_zero",
    ]
    if _should_transcode_audio_to_aac(codec):
        print_fn(
            f"      音轨: {codec or '?'} → AAC "
            f"{settings.CMAF_AUDIO_BITRATE} / {transcode_channels}ch"
        )
        cmd += [
            "-c:a", "aac", "-b:a", settings.CMAF_AUDIO_BITRATE,
            "-ac", str(transcode_channels),
        ]
    else:
        codec_label = _audio_codec_string(codec)
        ch_info = f" / {channels}ch" if channels > 2 else ""
        print_fn(f"      音轨: {codec} copy ({codec_label}{ch_info})")
        cmd += ["-c:a", "copy"]

    playlist_path = output_dir / "stream.m3u8"
    cmd += [
        "-f", "hls",
        "-hls_time", segment_seconds,
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(output_dir / "seg-%d.m4s"),
        str(playlist_path),
        "-y",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    if result.returncode != 0 or not playlist_path.is_file():
        print_fn(f"   ❌ FFmpeg HLS 切片失败:\n{result.stderr[-800:]}")
        return None

    _normalize_media_playlist(playlist_path)

    segments = sorted(f.name for f in output_dir.glob("seg-*.m4s") if f.is_file())
    if not segments or not (output_dir / "init.mp4").is_file():
        print_fn("   ❌ FFmpeg 未生成 init.mp4 或分片")
        return None

    source_codec = default_track.get("codec") or ""
    transcoded = _should_transcode_audio_to_aac(source_codec)
    audio_codec_str = "mp4a.40.2" if transcoded else _audio_codec_string(source_codec)
    video_codec_str = _codec_string_for_master(src_codec, probe_info)
    combined_bw = _measure_hls_bitrates(playlist_path)
    bandwidth = combined_bw["peak"] or probe_info["bitrate"]
    average_bandwidth = combined_bw["average"] or probe_info.get("average_bitrate") or bandwidth

    width = probe_info["width"]
    height = probe_info["height"]
    duration = probe_info["duration"]
    print_fn(
        f"   ✅ fMP4 HLS: {len(segments)} 片段 | {width}x{height} | "
        f"音轨 [{default_track['lang']}] {default_track['title']}"
    )
    return {
        "videoSegments": segments,
        "audioSegments": segments,
        "hlsVideo": "stream.m3u8",
        "hlsAudio": "stream.m3u8",
        "audioInit": "init.mp4",
        "defaultAudioLang": default_track["lang"],
        "defaultAudioTitle": default_track["title"],
        "duration": duration,
        "videoCodec": video_codec_str,
        "audioCodec": audio_codec_str,
        "width": width,
        "height": height,
        "bandwidth": bandwidth,
        "averageBandwidth": average_bandwidth,
        "frameRate": probe_info.get("frame_rate") or 0.0,
    }


def _h264_profile_hex(profile: str) -> str:
    normalized = (profile or "").lower()
    if "baseline" in normalized:
        return "42"
    if "main" in normalized:
        return "4d"
    if "high" in normalized:
        return "64"
    return "64"


def _codec_string_for_master(codec: str, probe_info: dict | None = None) -> str:
    normalized = (codec or "").lower()
    probe_info = probe_info or {}
    if normalized in {"hevc", "h265"}:
        return "hvc1.1.6.L120"
    profile_hex = _h264_profile_hex(str(probe_info.get("profile") or ""))
    try:
        level = int(probe_info.get("level") or 31)
    except (TypeError, ValueError):
        level = 31
    level = max(10, min(level, 255))
    return f"avc1.{profile_hex}00{level:02x}"


def _measure_hls_bitrates(playlist_path: Path) -> dict[str, int]:
    """按 HLS playlist 的分片时长和文件大小估算峰值/平均码率。"""
    if not playlist_path.exists():
        return {"peak": 0, "average": 0}
    try:
        lines = playlist_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {"peak": 0, "average": 0}

    peak = 0.0
    total_bits = 0
    total_duration = 0.0
    pending_duration = 0.0
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except (IndexError, ValueError):
                pending_duration = 0.0
            continue
        if not pending_duration or not line or line.startswith("#"):
            continue

        segment_path = playlist_path.parent / line
        if not segment_path.exists():
            pending_duration = 0.0
            continue
        bits = segment_path.stat().st_size * 8
        bitrate = bits / pending_duration
        peak = max(peak, bitrate)
        total_bits += bits
        total_duration += pending_duration
        pending_duration = 0.0

    average = total_bits / total_duration if total_duration > 0 else 0
    return {"peak": math.ceil(peak), "average": math.ceil(average)}


# ─── 字幕 fMP4 IMSC1 打包（Shaka Packager / stpp） ───

def pack_subtitles_fmp4(
    vtt_files: list[dict], output_dir: Path, print_fn=None
) -> list[dict]:
    """
    使用 Shaka Packager 将 VTT 字幕转换为 fMP4 IMSC1 (stpp) 格式。
    Apple AVPlayer 对 CMAF fMP4 仅支持 IMSC1/TTML (stpp)。

    Args:
        vtt_files: [{"file": "sub-0-zho.vtt", "lang": "zho", "label": "简体中文"}, ...]
        output_dir: 输出目录（字幕文件放在 subs/ 子目录下）

    Returns:
        [{"lang": "zh-Hans", "name": "简体中文", "dir": "subs/zhs",
          "uri": "subs/zhs/stream.m3u8", "init": "subs/zhs/init.mp4"}, ...]
    """
    from .env import resolve_tool
    from .runtime_config import settings
    import subprocess

    if print_fn is None:
        print_fn = print

    packager_bin = resolve_tool(settings.PACKAGER_BIN)
    if not packager_bin:
        print_fn("   ⚠️ Shaka Packager 不可用，跳过字幕 fMP4 打包")
        return []
    settings.PACKAGER_BIN = packager_bin

    LANG_MAP = {
        'zho': ('zh-Hans', '简体中文'), 'chi': ('zh-Hans', '简体中文'),
        'zhs': ('zh-Hans', '简体中文'), 'zh': ('zh-Hans', '简体中文'),
        'zht': ('zh-Hant', '繁体中文'), 'cht': ('zh-Hant', '繁体中文'),
        'yue': ('yue', '繁體中文(粵語)'), 'can': ('yue', '繁體中文(粵語)'),
        'eng': ('en', 'English'), 'en': ('en', 'English'),
        'jpn': ('ja', '日本語'), 'ja': ('ja', '日本語'),
        'kor': ('ko', '한국어'), 'ko': ('ko', '한국어'),
    }

    subs_dir = output_dir / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)
    results = []
    used_names: set = set()

    for i, sub in enumerate(vtt_files):
        lang_raw = sub.get('lang', 'und')
        label = sub.get('label', '')
        vtt_file = sub.get('file', '')
        vtt_path = output_dir / vtt_file

        if not vtt_path.exists():
            continue

        mapped_lang, mapped_name = LANG_MAP.get(lang_raw, (lang_raw, label or lang_raw))
        unique_name = mapped_name
        if unique_name in used_names:
            unique_name = f"{mapped_name} ({i+1})"
        used_names.add(unique_name)

        dir_name = lang_raw
        existing = [r['dir'].split('/')[-1] for r in results]
        if dir_name in existing:
            dir_name = f"{lang_raw}_{len(results)}"

        track_dir = subs_dir / dir_name
        track_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            packager_bin,
            f"in={vtt_path},stream=text,format=ttml+mp4,lang={mapped_lang},"
            f"init_segment={track_dir}/init.mp4,"
            f"segment_template={track_dir}/seg-$Number%05d$.m4s",
            "--segment_duration", "10",
            "--fragment_duration", "10",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                print_fn(f"   ⚠️ 字幕打包失败 [{unique_name}]: {result.stderr[-200:]}")
                continue
        except Exception as e:
            print_fn(f"   ⚠️ 字幕打包异常 [{unique_name}]: {e}")
            continue

        segments = sorted(track_dir.glob("seg-*.m4s"))
        if not segments:
            continue

        duration = _get_vtt_duration(vtt_path)
        if duration <= 0:
            duration = 3600.0

        stream_m3u8 = track_dir / "stream.m3u8"
        target_dur = max(1, int(duration) + 1)
        lines = [
            "#EXTM3U", "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{target_dur}",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            '#EXT-X-MAP:URI="init.mp4"',
        ]
        for seg in segments:
            lines.append("#EXTINF:10.000,")
            lines.append(seg.name)
        lines.append("#EXT-X-ENDLIST")
        stream_m3u8.write_text("\n".join(lines) + "\n", encoding="utf-8")

        rel_dir = f"subs/{dir_name}"
        results.append({
            "lang": mapped_lang,
            "name": unique_name,
            "dir": rel_dir,
            "uri": f"{rel_dir}/stream.m3u8",
            "init": f"{rel_dir}/init.mp4",
        })
        print_fn(f"   ✅ 字幕 fMP4 [{unique_name}] (stpp): {len(segments)} 段")

    return results


def _get_vtt_duration(vtt_path: Path) -> float:
    try:
        text = vtt_path.read_text(encoding='utf-8', errors='ignore')
        last_sec = 0.0
        for line in text.split('\n'):
            if '-->' not in line:
                continue
            parts = line.strip().split('-->')
            if len(parts) < 2:
                continue
            end_str = parts[1].strip().split()[0].replace(',', '.')
            ts_parts = end_str.split(':')
            if len(ts_parts) == 3:
                secs = float(ts_parts[0]) * 3600 + float(ts_parts[1]) * 60 + float(ts_parts[2])
            elif len(ts_parts) == 2:
                secs = float(ts_parts[0]) * 60 + float(ts_parts[1])
            else:
                secs = float(ts_parts[0])
            if secs > last_sec:
                last_sec = secs
        return last_sec
    except Exception:
        return 0.0
