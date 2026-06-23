"""
HLS / DASH Manifest 生成与验证模块
"""
import math
from pathlib import Path

from .runtime_config import settings


# ─── HLS Media Playlist 验证 ───

class HLSValidationError(Exception):
    """Raised when an HLS Media Playlist fails validation."""
    pass


def repair_hls_target_duration(playlist_path: Path, print_fn=None) -> bool:
    """
    修正 #EXT-X-TARGETDURATION，使其不小于所有 #EXTINF 时长的向上取整。
    FFmpeg 偶发写出偏小的 TARGETDURATION（如 6 vs 6.465s）。
    返回 True 表示已改写文件。
    """
    if not playlist_path.exists():
        return False

    lines = playlist_path.read_text(encoding="utf-8").splitlines()
    target_idx = None
    target_duration = None
    max_seg_dur = 0.0

    for index, line in enumerate(lines):
        line_s = line.strip()
        if line_s.startswith("#EXT-X-TARGETDURATION:"):
            target_idx = index
            try:
                target_duration = int(line_s.split(":")[1])
            except (ValueError, IndexError):
                target_duration = None
        elif line_s.startswith("#EXTINF:"):
            dur_str = line_s[len("#EXTINF:"):].rstrip(",").strip()
            try:
                max_seg_dur = max(max_seg_dur, float(dur_str))
            except ValueError:
                continue

    if max_seg_dur <= 0:
        return False

    required = max(1, math.ceil(max_seg_dur))
    if target_duration is not None and target_duration >= required:
        return False

    if print_fn:
        old = target_duration if target_duration is not None else "缺失"
        print_fn(
            f"   🔧 修正 {playlist_path.name} #EXT-X-TARGETDURATION: "
            f"{old} → {required} (最大片段 {max_seg_dur:.3f}s)"
        )

    new_line = f"#EXT-X-TARGETDURATION:{required}"
    if target_idx is not None:
        lines[target_idx] = new_line
    else:
        insert_at = 1 if lines and lines[0].strip() == "#EXTM3U" else 0
        lines.insert(insert_at, new_line)

    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def validate_hls_media_playlist(playlist_path: Path, track_type: str, print_fn=None) -> list[dict]:
    """
    验证 FFmpeg 生成的 HLS Media Playlist 的完整性。

    Args:
        playlist_path: HLS Media Playlist 文件路径
        track_type: "video" 或 "audio"

    Returns:
        解析出的片段信息列表: [{"index": 0, "duration": 6.006, "filename": "seg-v-00000.m4s"}, ...]

    Raises:
        HLSValidationError: 当播放列表缺少必需标签或格式不正确时
    """
    if not playlist_path.exists():
        raise HLSValidationError(f"播放列表文件不存在: {playlist_path}")

    if repair_hls_target_duration(playlist_path, print_fn=print_fn):
        pass  # 已自动修正 TARGETDURATION，继续校验

    content = playlist_path.read_text(encoding="utf-8")
    lines = content.strip().splitlines()

    if not lines:
        raise HLSValidationError(f"播放列表为空: {playlist_path}")

    # Check #EXTM3U header
    if lines[0].strip() != "#EXTM3U":
        raise HLSValidationError(
            f"播放列表缺少 #EXTM3U 头: {playlist_path} (首行: {lines[0]!r})"
        )

    # Check #EXT-X-VERSION
    has_version_7 = any(
        line.strip().startswith("#EXT-X-VERSION:") and int(line.strip().split(":")[1]) >= 7
        for line in lines if line.strip().startswith("#EXT-X-VERSION:")
    )
    if not has_version_7:
        has_any_version = any(line.strip().startswith("#EXT-X-VERSION:") for line in lines)
        if not has_any_version:
            raise HLSValidationError(f"播放列表缺少 #EXT-X-VERSION 标签: {playlist_path}")
        version_line = next(l for l in lines if l.strip().startswith("#EXT-X-VERSION:"))
        version_num = int(version_line.strip().split(":")[1])
        if version_num < 6:
            raise HLSValidationError(
                f"播放列表版本过低 (需要 >= 6, 实际: {version_num}): {playlist_path}"
            )

    # Check #EXT-X-MAP
    if track_type == "stream":
        has_map = any('#EXT-X-MAP:URI="' in line for line in lines)
    elif track_type == "video":
        expected_init = "init-v.mp4"
        has_map = any('#EXT-X-MAP:URI="' in line and expected_init in line for line in lines)
    else:
        expected_init = "init-a"
        has_map = any('#EXT-X-MAP:URI="' in line and expected_init in line for line in lines)
    if not has_map:
        hint = "init" if track_type == "stream" else expected_init
        raise HLSValidationError(
            f'播放列表缺少 #EXT-X-MAP:URI ({hint}): {playlist_path}'
        )

    # Check #EXT-X-TARGETDURATION
    target_duration = None
    for line in lines:
        line_s = line.strip()
        if line_s.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = int(line_s.split(":")[1])
            except (ValueError, IndexError):
                raise HLSValidationError(f"#EXT-X-TARGETDURATION 值无效: {line_s}")
            break
    if target_duration is None:
        raise HLSValidationError(f"播放列表缺少 #EXT-X-TARGETDURATION: {playlist_path}")
    if target_duration <= 0:
        raise HLSValidationError(
            f"#EXT-X-TARGETDURATION 必须为正整数 (实际: {target_duration}): {playlist_path}"
        )

    # Parse #EXTINF entries
    segments = []
    i = 0
    idx = 0
    while i < len(lines):
        line_s = lines[i].strip()
        if line_s.startswith("#EXTINF:"):
            dur_str = line_s[len("#EXTINF:"):].rstrip(",").strip()
            try:
                duration = float(dur_str)
            except ValueError:
                raise HLSValidationError(f"#EXTINF 时长解析失败: {line_s}")
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j >= len(lines):
                raise HLSValidationError(f"#EXTINF 后缺少片段文件名 (行 {i+1}): {playlist_path}")
            filename = lines[j].strip()
            segments.append({"index": idx, "duration": duration, "filename": filename})
            idx += 1
            i = j + 1
        else:
            i += 1

    if not segments:
        raise HLSValidationError(f"播放列表无 #EXTINF 片段条目: {playlist_path}")

    # Check #EXT-X-ENDLIST
    last_meaningful = ""
    for line in reversed(lines):
        if line.strip():
            last_meaningful = line.strip()
            break
    if last_meaningful != "#EXT-X-ENDLIST":
        raise HLSValidationError(
            f"播放列表缺少 #EXT-X-ENDLIST (末尾: {last_meaningful!r}): {playlist_path}"
        )

    # Validate TARGETDURATION >= max segment duration
    max_seg_dur = max(seg["duration"] for seg in segments)
    if target_duration < math.ceil(max_seg_dur):
        raise HLSValidationError(
            f"#EXT-X-TARGETDURATION ({target_duration}) 小于最大片段时长 "
            f"({max_seg_dur:.3f}s, ceil={math.ceil(max_seg_dur)}): {playlist_path}"
        )

    return segments


def _extract_target_duration(playlist_path: Path) -> int:
    """Extract #EXT-X-TARGETDURATION value from a playlist file."""
    content = playlist_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        line_s = line.strip()
        if line_s.startswith("#EXT-X-TARGETDURATION:"):
            return int(line_s.split(":")[1])
    return 6


def validate_hls_media_playlists(output_dir: Path, print_fn=None) -> dict:
    """
    验证 FFmpeg 生成的 stream.m3u8 媒体播放列表。

    Returns:
        字典包含 videoSegments, audioSegments, videoTargetDuration, audioTargetDuration
    """
    if print_fn is None:
        print_fn = print

    playlist = output_dir / "stream.m3u8"
    print_fn("   🔍 验证 stream.m3u8...")
    segments = validate_hls_media_playlist(playlist, "stream", print_fn=print_fn)
    print_fn(
        f"   ✅ stream.m3u8: {len(segments)} 片段, "
        f"总时长 {sum(s['duration'] for s in segments):.2f}s"
    )
    target = _extract_target_duration(playlist)
    return {
        "videoSegments": segments,
        "audioSegments": segments,
        "videoTargetDuration": target,
        "audioTargetDuration": target,
    }


# ─── HLS Master Playlist 生成 ───

def _hls_attr(value: object) -> str:
    """Escape a value for quoted HLS attributes."""
    return str(value).replace("\\", "\\\\").replace('"', r'\"')


def generate_hls_master(slice_result: dict, output_dir: Path, print_fn=None, subtitles_info: list[dict] | None = None) -> str:
    """生成 HLS Master Playlist (master.m3u8)。"""
    if print_fn is None:
        print_fn = print

    vcodec = slice_result["videoCodec"]
    acodec = slice_result["audioCodec"]
    bandwidth = slice_result["bandwidth"]
    average_bandwidth = slice_result.get("averageBandwidth") or bandwidth
    frame_rate = float(slice_result.get("frameRate") or 0)
    width = slice_result["width"]
    height = slice_result["height"]

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        "",
    ]

    if subtitles_info:
        default_index = 0
        for i, sub in enumerate(subtitles_info):
            lang = str(sub.get("lang") or "").lower()
            name = str(sub.get("name") or "")
            if lang in ("zh-hans", "zhs", "chi", "zho", "zh", "cmn") or any(
                hint in name for hint in ("简体", "简中", "简体中文")
            ):
                if "繁" not in name and "traditional" not in name.lower():
                    default_index = i
                    break
        autoselected_langs: set[str] = set()
        for i, sub in enumerate(subtitles_info):
            is_default = "YES" if i == default_index else "NO"
            lang = str(sub["lang"])
            is_autoselect = "NO"
            if lang not in autoselected_langs:
                is_autoselect = "YES"
                autoselected_langs.add(lang)
            lines.append(
                f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="{_hls_attr(lang)}",'
                f'NAME="{_hls_attr(sub["name"])}",DEFAULT={is_default},AUTOSELECT={is_autoselect},'
                f'FORCED=NO,URI="{_hls_attr(sub["uri"])}"'
            )
        lines.append("")

    stream_attrs = [
        f"BANDWIDTH={int(bandwidth)}",
        f"AVERAGE-BANDWIDTH={int(average_bandwidth)}",
        f'CODECS="{_hls_attr(f"{vcodec},{acodec}")}"',
        f"RESOLUTION={width}x{height}",
    ]
    if frame_rate > 0:
        stream_attrs.append(f"FRAME-RATE={frame_rate:.3f}".rstrip("0").rstrip("."))
    if subtitles_info:
        stream_attrs.append('SUBTITLES="subs"')
    lines.append(f"#EXT-X-STREAM-INF:{','.join(stream_attrs)}")
    lines.append(slice_result.get("hlsVideo", "stream.m3u8"))

    content = "\n".join(lines) + "\n"
    master_path = output_dir / "master.m3u8"
    master_path.write_text(content, encoding="utf-8")
    sub_count = len(subtitles_info) if subtitles_info else 0
    print_fn(f"   ✅ master.m3u8 已生成 ({sub_count} 字幕)")
    return content


# ─── DASH MPD 生成 ───

def generate_dash_mpd(slice_result: dict, playlist_info: dict, output_dir: Path, print_fn=None) -> str:
    """生成 DASH MPD (stream.mpd)，复合 fMP4 SegmentTemplate。"""
    if print_fn is None:
        print_fn = print

    video_segments = playlist_info["videoSegments"]
    vcodec = slice_result["videoCodec"]
    acodec = slice_result["audioCodec"]
    bandwidth = slice_result["bandwidth"]
    width = slice_result["width"]
    height = slice_result["height"]
    init_file = slice_result.get("audioInit", "init.mp4")

    total_duration = sum(seg["duration"] for seg in video_segments)

    hours = int(total_duration // 3600)
    minutes = int((total_duration % 3600) // 60)
    seconds = total_duration % 60
    if hours > 0:
        duration_iso = f"PT{hours}H{minutes}M{seconds:.3f}S"
    elif minutes > 0:
        duration_iso = f"PT{minutes}M{seconds:.3f}S"
    else:
        duration_iso = f"PT{seconds:.3f}S"

    video_target_dur = playlist_info.get("videoTargetDuration", 6)
    combined_codecs = f"{vcodec},{acodec}"
    mpd = f'''<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="{duration_iso}"
     minBufferTime="PT2S"
     profiles="urn:mpeg:dash:profile:isoff-on-demand:2011,urn:mpeg:dash:profile:cmaf:2019">
  <Period>
    <AdaptationSet mimeType="video/mp4" contentType="video"
                   segmentAlignment="true" startWithSAP="1">
      <Representation id="main" bandwidth="{bandwidth}"
                      width="{width}" height="{height}" codecs="{combined_codecs}">
        <SegmentTemplate timescale="1" duration="{video_target_dur}"
                         initialization="{init_file}"
                         media="seg-$Number$.m4s"
                         startNumber="0"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
'''
    mpd_path = output_dir / "stream.mpd"
    mpd_path.write_text(mpd, encoding="utf-8")
    print_fn(f"   ✅ stream.mpd 已生成 (duration: {duration_iso})")
    return mpd
