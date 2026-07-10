"""Prepare a single browser-oriented MP4 for direct upload."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .env import resolve_tool
from .runtime_config import settings
from .slicer import probe_audio_streams, probe_video_info, select_audio_streams

_BROWSER_SAFE_AUDIO_CODECS = frozenset({"aac", "mp4a"})


def _parse_frame_rate(value: object) -> float:
    raw = str(value or "").strip()
    if not raw or raw == "0/0":
        return 0.0
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else 0.0
        return float(raw)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_direct_media(input_path: str, ffprobe_bin: str | None = None) -> dict:
    """Probe container, primary video, and primary audio metadata."""
    ffprobe = ffprobe_bin or resolve_tool(settings.FFPROBE_BIN)
    if not ffprobe:
        raise RuntimeError("ffprobe 不可用，无法验证直传媒体")

    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries",
                "format=format_name,duration,bit_rate:"
                "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
                "-of", "json", input_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe 验证超时（60 秒）") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 验证失败:\n{result.stderr[-1200:]}")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("ffprobe 返回了无效 JSON") from exc

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    media_format = payload.get("format") or {}

    def _as_int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _as_float(value: object) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "formatName": str(media_format.get("format_name") or ""),
        "videoCodec": str(video.get("codec_name") or "").lower(),
        "audioCodec": str(audio.get("codec_name") or "").lower(),
        "width": _as_int(video.get("width")),
        "height": _as_int(video.get("height")),
        "bitrate": _as_int(media_format.get("bit_rate")),
        "frameRate": _parse_frame_rate(
            video.get("avg_frame_rate") or video.get("r_frame_rate")
        ),
        "duration": _as_float(media_format.get("duration")),
    }


def _remove_partial_output(output_path: Path) -> None:
    try:
        output_path.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_direct_mp4(
    input_path: str,
    output_path: Path,
    h264_compat: bool,
    original_language: str | None,
    print_fn=print,
) -> dict:
    """Remux media to a verified fast-start MP4, transcoding only when needed."""
    ffmpeg = resolve_tool(settings.FFMPEG_BIN)
    ffprobe = resolve_tool(settings.FFPROBE_BIN)
    if not ffmpeg or not ffprobe:
        missing = "ffmpeg" if not ffmpeg else "ffprobe"
        raise RuntimeError(f"{missing} 不可用，无法准备直传 MP4")

    try:
        video_info = probe_video_info(input_path, ffprobe_bin=ffprobe)
        audio_tracks = probe_audio_streams(input_path, ffprobe_bin=ffprobe)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe 源媒体探测超时（60 秒）") from exc
    video_codec = str(video_info.get("codec") or "").lower()
    selected_tracks = select_audio_streams(
        audio_tracks,
        original_language,
        print_fn=print_fn,
    )
    if not selected_tracks:
        raise RuntimeError("源文件无可用音轨，无法准备直传 MP4")
    default_track = next(
        (track for track in selected_tracks if track.get("is_default")),
        selected_tracks[0],
    )

    audio_codec = str(default_track.get("codec") or "").lower()
    channels = int(default_track.get("channels") or 2)
    cmd = [
        ffmpeg, "-hide_banner", "-y", "-i", input_path,
        "-map", "0:v:0", "-map", f"0:a:{default_track['audio_index']}",
    ]
    if h264_compat and video_codec != "h264":
        cmd += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p",
        ]
        video_copied = False
    else:
        cmd += ["-c:v", "copy"]
        video_copied = True
    if video_codec in {"hevc", "h265"} and video_copied:
        cmd += ["-tag:v", "hvc1"]

    if audio_codec in _BROWSER_SAFE_AUDIO_CODECS:
        cmd += ["-c:a", "copy"]
        audio_copied = True
    else:
        cmd += [
            "-c:a", "aac", "-profile:a", "aac_low",
            "-b:a", settings.CMAF_AUDIO_BITRATE,
            "-ac", str(min(channels, settings.CMAF_AUDIO_CHANNELS)),
        ]
        audio_copied = False

    cmd += ["-movflags", "+faststart", "-f", "mp4", str(output_path)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=14400
        )
    except subprocess.TimeoutExpired as exc:
        _remove_partial_output(output_path)
        raise RuntimeError("FFmpeg 直传 MP4 处理超时（14400 秒）") from exc
    if result.returncode != 0:
        _remove_partial_output(output_path)
        raise RuntimeError(f"FFmpeg 直传 MP4 处理失败:\n{result.stderr[-1200:]}")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        _remove_partial_output(output_path)
        raise RuntimeError("FFmpeg 未生成非空的直传 MP4")

    try:
        verified = probe_direct_media(str(output_path), ffprobe_bin=ffprobe)
    except RuntimeError:
        _remove_partial_output(output_path)
        raise
    if "mp4" not in verified["formatName"].lower():
        _remove_partial_output(output_path)
        raise RuntimeError(
            f"直传输出验证失败：容器不是 MP4 ({verified['formatName'] or 'unknown'})"
        )
    if not verified["videoCodec"] or not verified["audioCodec"]:
        _remove_partial_output(output_path)
        raise RuntimeError("直传输出验证失败：缺少视频或音频轨")

    print_fn(
        f"   ✅ 直传 MP4: 视频 {verified['videoCodec']} "
        f"{'copy' if video_copied else '→ h264'} + 音频 "
        f"{verified['audioCodec']} {'copy' if audio_copied else '→ AAC-LC'}"
    )
    return {
        "path": str(output_path),
        "videoCodec": verified["videoCodec"],
        "audioCodec": verified["audioCodec"],
        "videoCopied": video_copied,
        "audioCopied": audio_copied,
        "duration": verified["duration"],
        "size": output_path.stat().st_size,
        "width": verified["width"],
        "height": verified["height"],
        "bitrate": verified["bitrate"],
        "frameRate": verified["frameRate"],
    }
