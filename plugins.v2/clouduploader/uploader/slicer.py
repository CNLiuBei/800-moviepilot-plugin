"""
FFmpeg CMAF Demux 切片模块（插件内嵌版）
将源视频分离为独立的视频轨和音频轨 fMP4 片段。
"""
import json
import math
import re
import subprocess
from pathlib import Path

from .runtime_config import settings

# 不兼容 fMP4 容器的编码 (需要转码)
_INCOMPATIBLE_CODECS = frozenset({
    'vp9', 'vp8', 'av1', 'theora', 'mpeg2video', 'mpeg1video', 'rawvideo',
})


def _fix_target_duration(playlist_path: Path) -> None:
    """
    修正 FFmpeg 写出的 #EXT-X-TARGETDURATION。

    FFmpeg 用 round() 计算 TARGETDURATION，当存在 X.5 以上的超长片段时
    （如 8.2s → round=8），会写出小于实际最大片段时长的值，违反 HLS 规范
    （RFC 8216 要求 TARGETDURATION >= ceil(max segment duration)）。

    -c:v copy 模式下片段只能在关键帧切分，时长不可控，故必须在切片后按
    实际 #EXTINF 的 ceil 最大值回写 TARGETDURATION。
    """
    if not playlist_path.exists():
        return
    content = playlist_path.read_text(encoding="utf-8")
    durations = [float(m) for m in re.findall(r"#EXTINF:([\d.]+)", content)]
    if not durations:
        return
    correct = max(1, math.ceil(max(durations)))
    new_content, n = re.subn(
        r"#EXT-X-TARGETDURATION:\d+",
        f"#EXT-X-TARGETDURATION:{correct}",
        content,
        count=1,
    )
    if n and new_content != content:
        playlist_path.write_text(new_content, encoding="utf-8")


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
         "-show_entries", "stream=codec_name,width,height,bit_rate",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=60,
    )
    probe_format = subprocess.run(
        [settings.FFPROBE_BIN, "-v", "quiet",
         "-show_entries", "format=duration,bit_rate",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=60,
    )

    info = {"codec": "h264", "width": 1920, "height": 1080, "bitrate": 2000000, "duration": 0.0}

    try:
        stream_data = json.loads(probe_stream.stdout)
        streams = stream_data.get("streams", [])
        if streams:
            s = streams[0]
            info["codec"] = s.get("codec_name", "h264").lower()
            info["width"] = int(s.get("width", 1920))
            info["height"] = int(s.get("height", 1080))
            stream_br = s.get("bit_rate")
            if stream_br and stream_br != "N/A":
                info["bitrate"] = int(stream_br)
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
    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    return info


def cmaf_demux_slice(input_path: str, output_dir: Path, print_fn=None) -> dict | None:
    """CMAF demuxed 切片: 将源视频分离为独立的视频轨和音频轨 fMP4 片段。"""
    if print_fn is None:
        print_fn = print

    output_dir.mkdir(parents=True, exist_ok=True)

    print_fn("   🔍 探测源视频信息...")
    probe_info = probe_video_info(input_path)
    src_codec = probe_info["codec"]
    width = probe_info["width"]
    height = probe_info["height"]
    bitrate = probe_info["bitrate"]
    duration = probe_info["duration"]

    print_fn(f"   编码: {src_codec} | 分辨率: {width}x{height} | 码率: {bitrate // 1000}kbps | 时长: {duration:.1f}s")

    if src_codec in _INCOMPATIBLE_CODECS:
        video_codec_args = ["-c:v", "libx264", "-crf", str(settings.CMAF_VIDEO_FALLBACK_CRF), "-preset", "medium"]
        print_fn(f"   ⚠️ 源编码 {src_codec} 不兼容 fMP4, 转码为 H.264 (CRF {settings.CMAF_VIDEO_FALLBACK_CRF})")
    else:
        video_codec_args = ["-c:v", "copy"]

    # 视频轨切片
    print_fn("   🎬 切片视频轨...")
    video_m3u8 = output_dir / "stream-v.m3u8"
    video_seg_pattern = str(output_dir / "seg-v-%05d.m4s")

    video_cmd = [
        settings.FFMPEG_BIN, "-i", input_path,
        "-map", "0:v:0", *video_codec_args, "-an",
        "-f", "hls",
        "-hls_time", str(settings.HLS_SEGMENT_SECONDS),
        "-hls_list_size", "0",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init-v.mp4",
        "-hls_segment_filename", video_seg_pattern,
        "-hls_flags", "independent_segments",
        str(video_m3u8), "-y",
    ]
    result = subprocess.run(video_cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        print_fn(f"FFmpeg 视频轨错误:\n{result.stderr[-500:]}")
        return None

    # 修正 TARGETDURATION（copy 模式片段时长不可控，FFmpeg 可能写出违反 HLS 规范的值）
    _fix_target_duration(video_m3u8)

    # 音频轨切片 (支持多音轨)
    audio_probe = subprocess.run(
        [settings.FFPROBE_BIN, "-v", "quiet", "-select_streams", "a",
         "-show_entries", "stream=index,codec_name,channels,tags",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=60,
    )
    audio_streams: list[dict] = []
    try:
        probe_data = json.loads(audio_probe.stdout)
        for i, s in enumerate(probe_data.get("streams", [])):
            lang = (s.get("tags") or {}).get("language", "und")
            title = (s.get("tags") or {}).get("title", "")
            audio_streams.append({"index": i, "lang": lang, "title": title, "channels": s.get("channels", 2)})
    except (json.JSONDecodeError, ValueError):
        audio_streams = [{"index": 0, "lang": "und", "title": "", "channels": 2}]

    if not audio_streams:
        audio_streams = [{"index": 0, "lang": "und", "title": "", "channels": 2}]

    print_fn(f"   🎵 切片音频轨 ({len(audio_streams)} 条)...")
    audio_tracks_info: list[dict] = []

    for ai, astream in enumerate(audio_streams):
        suffix = f"a{ai}" if len(audio_streams) > 1 else "a"
        audio_m3u8 = output_dir / f"stream-{suffix}.m3u8"
        audio_seg_pattern = str(output_dir / f"seg-{suffix}-%05d.m4s")
        init_filename = f"init-{suffix}.mp4"

        audio_cmd = [
            settings.FFMPEG_BIN, "-i", input_path,
            "-map", f"0:a:{astream['index']}",
            "-c:a", "aac", "-b:a", settings.CMAF_AUDIO_BITRATE, "-ac", str(settings.CMAF_AUDIO_CHANNELS),
            "-vn",
            "-f", "hls",
            "-hls_time", str(settings.HLS_SEGMENT_SECONDS),
            "-hls_list_size", "0",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", init_filename,
            "-hls_segment_filename", audio_seg_pattern,
            "-hls_flags", "independent_segments",
            str(audio_m3u8), "-y",
        ]
        result = subprocess.run(audio_cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            if ai == 0:
                print_fn(f"FFmpeg 音频轨 {ai} 错误:\n{result.stderr[-300:]}")
                return None
            else:
                print_fn(f"   ⚠️ 音频轨 {ai} ({astream['lang']}) 切片失败，跳过")
                continue

        # 修正 TARGETDURATION，与视频轨保持一致的规范处理
        _fix_target_duration(audio_m3u8)

        segs = sorted([f.name for f in output_dir.glob(f"seg-{suffix}-*.m4s")])
        audio_tracks_info.append({
            "index": ai, "suffix": suffix, "lang": astream["lang"],
            "title": astream["title"], "channels": astream["channels"],
            "init": init_filename, "m3u8": f"stream-{suffix}.m3u8", "segments": segs,
        })
        if len(audio_streams) > 1:
            print_fn(f"      音轨 {ai}: {astream['lang']} ({astream['title'] or 'default'}) → {len(segs)} 片段")

    if not audio_tracks_info:
        print_fn("❌ 无音频轨生成")
        return None

    video_segments = sorted([f.name for f in output_dir.glob("seg-v-*.m4s")])
    if not (output_dir / "init-v.mp4").exists():
        print_fn("❌ init-v.mp4 未生成")
        return None
    if not video_segments:
        print_fn("❌ 无视频片段生成")
        return None

    primary_audio = audio_tracks_info[0]
    audio_segments = primary_audio["segments"]

    # 确定实际编码字符串
    video_codec_str = "avc1.64001f"
    init_v_path = output_dir / "init-v.mp4"
    if init_v_path.exists():
        try:
            codec_probe = subprocess.run(
                [settings.FFPROBE_BIN, "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_tag_string,profile,level",
                 "-of", "json", str(init_v_path)],
                capture_output=True, text=True, timeout=30,
            )
            codec_data = json.loads(codec_probe.stdout)
            streams = codec_data.get("streams", [])
            if streams:
                tag = streams[0].get("codec_tag_string", "")
                if tag and tag != "N/A":
                    if tag.startswith("avc"):
                        video_codec_str = "avc1.64001f"
                    elif tag.startswith("hev") or tag.startswith("hvc"):
                        video_codec_str = "hev1.1.6.L120"
                    else:
                        video_codec_str = tag
        except (json.JSONDecodeError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass

    audio_codec_str = "mp4a.40.2"

    print_fn(f"   ✅ 视频: {len(video_segments)} 片段 | 音频: {len(audio_segments)} 片段")
    print_fn(f"   编码: video={video_codec_str}, audio={audio_codec_str}")

    return {
        "videoSegments": video_segments,
        "audioSegments": audio_segments,
        "audioTracks": audio_tracks_info,
        "videoInit": "init-v.mp4",
        "audioInit": primary_audio["init"],
        "hlsVideo": "stream-v.m3u8",
        "hlsAudio": primary_audio["m3u8"],
        "duration": duration,
        "videoCodec": video_codec_str,
        "audioCodec": audio_codec_str,
        "width": width,
        "height": height,
        "bandwidth": bitrate,
    }
