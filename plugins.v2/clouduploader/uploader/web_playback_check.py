"""Web player compatibility checks for uploaded media."""

from __future__ import annotations

_WEB_VIDEO_CODECS = frozenset({"h264", "avc1", "hevc", "h265"})
_WEB_AUDIO_CODECS = frozenset({"aac", "mp4a"})
_HEVC_CODECS = frozenset({"hevc", "h265"})
_PRESIGNED_GET_EXPIRES = 300


def assert_web_playable(probe: dict, *, source: str = "媒体") -> None:
    """
    Raise if probe metadata is not suitable for the site web player.

    Criteria match frontend playback expectations:
    - MP4 container
    - H.264 or HEVC (HEVC must use hvc1 tag)
    - AAC audio
    - positive duration and resolution
    """
    format_name = str(probe.get("formatName") or "").lower()
    if "mp4" not in format_name:
        raise RuntimeError(
            f"{source} 未通过 Web 可播检查：容器不是 MP4 "
            f"({format_name or 'unknown'})"
        )

    video_codec = str(probe.get("videoCodec") or "").lower()
    if video_codec not in _WEB_VIDEO_CODECS:
        raise RuntimeError(
            f"{source} 未通过 Web 可播检查：视频编码不支持 "
            f"({video_codec or 'missing'}，需要 h264/hevc)"
        )

    if video_codec in _HEVC_CODECS:
        tag = str(probe.get("videoCodecTag") or "").lower()
        if tag and tag != "hvc1":
            raise RuntimeError(
                f"{source} 未通过 Web 可播检查：HEVC 需使用 hvc1 标签 "
                f"(当前 {tag})"
            )
        if not tag:
            raise RuntimeError(
                f"{source} 未通过 Web 可播检查：HEVC 缺少 hvc1 标签"
            )

    audio_codec = str(probe.get("audioCodec") or "").lower()
    if audio_codec not in _WEB_AUDIO_CODECS:
        raise RuntimeError(
            f"{source} 未通过 Web 可播检查：音频编码不支持 "
            f"({audio_codec or 'missing'}，需要 aac)"
        )

    try:
        duration = float(probe.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise RuntimeError(
            f"{source} 未通过 Web 可播检查：时长无效 ({duration})"
        )

    try:
        width = int(probe.get("width") or 0)
        height = int(probe.get("height") or 0)
    except (TypeError, ValueError):
        width, height = 0, 0
    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"{source} 未通过 Web 可播检查：分辨率无效 "
            f"({width}x{height})"
        )


def verify_remote_mp4_web_playable(
    s3,
    bucket: str,
    key: str,
    *,
    ffprobe_bin: str | None = None,
    expires_in: int = _PRESIGNED_GET_EXPIRES,
) -> dict:
    """
    Presign a remote MP4, ffprobe it over HTTPS, and assert web playability.

    R2 supports Range requests so ffprobe can inspect codecs without a full download.
    """
    from .direct_media import probe_direct_media

    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )
    probe = probe_direct_media(url, ffprobe_bin=ffprobe_bin)
    assert_web_playable(probe, source="R2 远端 video.mp4")
    return probe
