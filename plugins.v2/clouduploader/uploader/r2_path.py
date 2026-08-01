"""R2 object-key prefix builders shared by scan, enqueue, and run_job."""
from __future__ import annotations

from .resolution_key import append_resolution_to_r2_path


def build_media_r2_prefix(
    media_type: str,
    tmdb_id,
    season=None,
    episode=None,
) -> str:
    """TMDB media prefix without quality segment."""
    media = str(media_type or "").strip() or "movie"
    tid = tmdb_id
    if season is not None and episode is not None:
        return f"tmdb/{media}/{tid}/season/{int(season)}/episode/{int(episode)}"
    return f"tmdb/{media}/{tid}"


def build_quality_r2_path(
    media_type: str,
    tmdb_id,
    resolution: str = "",
    season=None,
    episode=None,
) -> tuple[str, str]:
    """Quality-aware R2 prefix used for upload markers and media objects."""
    base = build_media_r2_prefix(media_type, tmdb_id, season=season, episode=episode)
    return append_resolution_to_r2_path(base, resolution)


def marker_lookup_paths(base_prefix: str, resolution: str = "") -> list[str]:
    """
    Paths to check for upload markers.

    Prefer the quality directory used by current uploads; also include the
    legacy base prefix (no quality segment) for older objects.
    """
    base = (base_prefix or "").rstrip("/")
    quality_path, _key = append_resolution_to_r2_path(base, resolution)
    paths = [quality_path]
    if quality_path != base:
        paths.append(base)
    return paths
