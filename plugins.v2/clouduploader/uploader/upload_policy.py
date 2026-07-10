from __future__ import annotations


def normalize_upload_mode(value: object) -> str:
    return "hls" if str(value or "").strip().lower() == "hls" else "direct"


def direct_mode_enabled(params: dict) -> bool:
    if "upload_mode" in params:
        return normalize_upload_mode(params.get("upload_mode")) == "direct"
    return bool(params.get("direct_mp4", True))


def validate_upload_identity(media_type, season, episode):
    normalized_type = "tv" if str(media_type).strip().lower() == "tv" else "movie"
    if (season is None) != (episode is None):
        return normalized_type, None, None, "season 和 episode 必须同时提供"
    try:
        normalized_season = int(season) if season is not None else None
        normalized_episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        return normalized_type, None, None, "season/episode 必须是整数"
    if normalized_type == "tv" and (normalized_season is None or normalized_episode is None):
        return normalized_type, normalized_season, normalized_episode, "电视剧直传必须提供 season 和 episode"
    return normalized_type, normalized_season, normalized_episode, None


def recovery_policy_from_marker(marker: dict | None) -> dict:
    """Restore upload policy fields from an R2 uploaded/ready marker."""
    data = marker if isinstance(marker, dict) else {}
    source_type = str(data.get("sourceType") or "").strip().lower()
    if "uploadMode" in data:
        mode = normalize_upload_mode(data.get("uploadMode"))
    elif source_type == "mp4":
        mode = "direct"
    elif source_type in {"cmaf", "hls"}:
        mode = "hls"
    else:
        mode = "direct"
    return {
        "upload_mode": mode,
        "direct_mp4": mode == "direct",
        "h264_compat": bool(data.get("h264Compat")),
    }


def recovery_policy_from_source_type(source_type: str) -> dict:
    """Map a remote media object type to retry policy fields."""
    normalized = str(source_type or "").strip().lower()
    mode = "direct" if normalized == "mp4" else "hls"
    return {
        "upload_mode": mode,
        "direct_mp4": mode == "direct",
        "h264_compat": False,
    }
