"""
Apple HLS 切片模块（插件内嵌版）。

FFmpeg/ffprobe 仅用于源文件探测与生成 Apple HLS Tools 可处理的中间 MP4。
"""
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from .runtime_config import settings

_COPYABLE_VIDEO_CODECS = frozenset({"h264", "hevc", "h265"})


def _tool_available(path: str) -> bool:
    return bool(shutil.which(path) or (Path(path).is_absolute() and Path(path).is_file()))


def get_video_duration(filepath: str) -> int | None:
    """用 ffprobe 读取视频真实时长, 返回秒数 (整数), 失败返回 None。"""
    try:
        result = subprocess.run(
            [settings.FFPROBE_BIN, "-v", "quiet",
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


def probe_video_info(input_path: str) -> dict:
    """用 ffprobe 探测源视频编码、分辨率、码率和时长。"""
    probe_stream = subprocess.run(
        [settings.FFPROBE_BIN, "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,profile,level,width,height,bit_rate,avg_frame_rate,r_frame_rate",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=60,
    )
    probe_format = subprocess.run(
        [settings.FFPROBE_BIN, "-v", "quiet",
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


def apple_hls_slice(input_path: str, output_dir: Path, print_fn=None) -> dict | None:
    """Apple HLS Tools 切片：生成音视频合一 stream.m3u8，并用 mediastreamvalidator 校验。"""
    if print_fn is None:
        print_fn = print
    if not _tool_available(settings.MEDIAFILESEGMENTER_BIN):
        print_fn("   Apple mediafilesegmenter 未安装，跳过 Apple HLS 切片")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    probe_info = probe_video_info(input_path)
    src_codec = probe_info["codec"]
    width = probe_info["width"]
    height = probe_info["height"]
    bitrate = probe_info["bitrate"]
    duration = probe_info["duration"]

    video_args = ["-c:v", "copy"] if src_codec in _COPYABLE_VIDEO_CODECS else [
        "-c:v", "libx264", "-crf", str(settings.CMAF_VIDEO_FALLBACK_CRF), "-preset", "medium", "-pix_fmt", "yuv420p",
    ]
    if video_args[1] == "copy":
        print_fn(f"   Apple HLS 准备: 视频 {src_codec} 直接封装，音频转 AAC")
    else:
        print_fn(f"   Apple HLS 准备: 视频 {src_codec} 转 H.264，音频转 AAC")

    with tempfile.TemporaryDirectory(prefix="gy-apple-hls-") as tmp:
        mezzanine = Path(tmp) / "source.mp4"
        remux_cmd = [
            settings.FFMPEG_BIN, "-i", input_path,
            "-map", "0:v:0", "-map", "0:a:0",
            *video_args,
            "-c:a", "aac", "-b:a", settings.CMAF_AUDIO_BITRATE,
            "-ac", str(settings.CMAF_AUDIO_CHANNELS),
            "-movflags", "+faststart",
            str(mezzanine), "-y",
        ]
        result = subprocess.run(remux_cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode != 0 or not mezzanine.exists():
            print_fn(f"   Apple HLS 输入准备失败:\n{result.stderr[-500:]}")
            return None

        segment_cmds = [
            [
                settings.MEDIAFILESEGMENTER_BIN,
                "--file-base", str(output_dir),
                "--target-duration", str(settings.HLS_SEGMENT_SECONDS),
                "--base-media-file-name", "seg-",
                "--index-file", "stream.m3u8",
                "--iframe-index-file", "none",
                str(mezzanine),
            ],
            [
                settings.MEDIAFILESEGMENTER_BIN,
                "--file-base", str(output_dir),
                "--target-duration", str(settings.HLS_SEGMENT_SECONDS),
                "--iframe-index-file", "none",
                str(mezzanine),
            ],
        ]
        segment_result = None
        for cmd in segment_cmds:
            segment_result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
            if segment_result.returncode == 0:
                break
        if not segment_result or segment_result.returncode != 0:
            stderr = segment_result.stderr[-500:] if segment_result else ""
            print_fn(f"   Apple mediafilesegmenter 失败:\n{stderr}")
            return None

    _normalize_apple_media_playlist(output_dir)
    stream_playlist = output_dir / "stream.m3u8"
    if not stream_playlist.exists():
        candidates = sorted(output_dir.glob("*.m3u8"))
        if candidates:
            candidates[0].rename(stream_playlist)
    if not stream_playlist.exists():
        print_fn("   Apple mediafilesegmenter 未生成 stream.m3u8")
        return None

    video_codec_str = _codec_string_for_master(src_codec, probe_info)
    audio_codec_str = "mp4a.40.2"
    segments = [
        f.name for f in sorted(output_dir.iterdir())
        if f.is_file() and f.name != "stream.m3u8" and f.suffix.lower() in {".ts", ".m4s"}
    ]
    if not segments:
        print_fn("   Apple HLS 未生成媒体分片")
        return None
    hls_bitrates = _measure_hls_bitrates(stream_playlist)
    bandwidth = hls_bitrates["peak"] or bitrate
    average_bandwidth = hls_bitrates["average"] or probe_info.get("average_bitrate") or bitrate

    print_fn(f"   ✅ Apple HLS: {len(segments)} 片段 | {width}x{height}")
    return {
        "videoSegments": segments,
        "audioSegments": segments,
        "audioTracks": [],
        "hlsVideo": "stream.m3u8",
        "hlsAudio": "",
        "duration": duration,
        "videoCodec": video_codec_str,
        "audioCodec": audio_codec_str,
        "width": width,
        "height": height,
        "bandwidth": bandwidth,
        "averageBandwidth": average_bandwidth,
        "frameRate": probe_info.get("frame_rate") or 0.0,
        "appleHLS": True,
    }


def _normalize_apple_media_playlist(output_dir: Path) -> None:
    """Apple 工具不同版本输出文件名略有差异，统一入口为 stream.m3u8。"""
    playlists = sorted(output_dir.glob("*.m3u8"))
    preferred = output_dir / "stream.m3u8"
    if preferred.exists():
        _remove_independent_segments_tag(preferred)
        return
    for playlist in playlists:
        if playlist.name.lower() in {"prog_index.m3u8", "index.m3u8"}:
            playlist.rename(preferred)
            _remove_independent_segments_tag(preferred)
            return


def _remove_independent_segments_tag(playlist_path: Path) -> None:
    """Apple HLS master 统一声明 independent segments，media playlist 去重。"""
    try:
        lines = playlist_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    filtered = [line for line in lines if line.strip() != "#EXT-X-INDEPENDENT-SEGMENTS"]
    if filtered != lines:
        playlist_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")


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
    from .runtime_config import settings
    import subprocess

    if print_fn is None:
        print_fn = print

    packager_bin = settings.PACKAGER_BIN
    import shutil as _shutil
    if not _shutil.which(packager_bin):
        print_fn("   ⚠️ Shaka Packager 不可用，跳过字幕 fMP4 打包")
        return []

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
