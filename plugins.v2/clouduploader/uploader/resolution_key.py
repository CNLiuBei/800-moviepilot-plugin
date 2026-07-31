import re

def normalize_quality_key(raw) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return "未知"
    if re.search(r"\b(2160p?|4k|uhd)\b", text) or "2160" in text:
        return "2160p"
    if re.search(r"\b(1440p?|qhd|2k)\b", text) or "1440" in text:
        return "1440p"
    if re.search(r"\b(1080p?)\b", text) or "1080" in text:
        return "1080p"
    if re.search(r"\b(720p?)\b", text) or "720" in text:
        return "720p"
    if re.search(r"\b(480p?|576p?)\b", text) or "480" in text or "576" in text:
        return "480p"
    if text in {"未知", "unknown", "原画"}:
        return "未知"
    return "未知"


def quality_key_from_width(width) -> str:
    try:
        w = int(width)
    except (TypeError, ValueError):
        return "未知"
    if w >= 3840:
        return "2160p"
    if w >= 2560:
        return "1440p"
    if w >= 1920:
        return "1080p"
    if w >= 1280:
        return "720p"
    if w >= 640:
        return "480p"
    return "未知"


def quality_key_from_height(height) -> str:
    """兼容旧名；实际按宽度语义使用时请调用 quality_key_from_width。"""
    return quality_key_from_width(height)


def quality_r2_segment(key: str) -> str:
    k = normalize_quality_key(key)
    return "unknown" if k == "未知" else k


def format_source_label(key: str) -> str:
    return normalize_quality_key(key)


def append_resolution_to_r2_path(base_path: str, raw_resolution: str) -> tuple[str, str]:
    key = normalize_quality_key(raw_resolution)
    segment = quality_r2_segment(key)
    base = (base_path or "").rstrip("/")
    if base.endswith(f"/{segment}"):
        return base, key
    return f"{base}/{segment}", key
