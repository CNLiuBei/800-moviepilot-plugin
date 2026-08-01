"""Queue ordering helpers for CloudUploader (season/episode ascending)."""
from __future__ import annotations

import re
from pathlib import Path


def episode_sort_key_from_params(params: dict) -> tuple[int, int]:
    """Return (season, episode) for PriorityQueue; missing → (999999, 999999)."""
    season = params.get("season")
    episode = params.get("episode")
    try:
        return (int(season), int(episode))
    except (TypeError, ValueError):
        pass
    name = Path(str(params.get("filepath") or "")).name
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (999999, 999999)


def episode_sort_key_from_progress(prog: dict, key: str = "") -> tuple[int, int]:
    """Sort key for progress list rows."""
    try:
        return (int(prog.get("season")), int(prog.get("episode")))
    except (TypeError, ValueError):
        pass
    name = str(prog.get("name") or key or "")
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (999999, 999999)
