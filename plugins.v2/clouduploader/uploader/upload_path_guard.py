"""Guard manual upload paths so they stay under configured media roots."""
from __future__ import annotations

import os


def assert_filepath_allowed(
    filepath: str,
    allowed_roots: list[str] | None,
) -> tuple[str | None, str | None]:
    """
    Resolve filepath and ensure it is a real file under one of allowed_roots.

    Returns (resolved_path, None) on success, or (None, error_message) on failure.
    Symlinks that escape allowed roots are rejected via realpath.
    """
    raw = (filepath or "").strip()
    if not raw:
        return None, "文件路径为空"
    roots = [r for r in (allowed_roots or []) if str(r).strip()]
    if not roots:
        return None, "未配置媒体库/监控目录，拒绝手动上传路径"
    try:
        resolved = os.path.realpath(os.path.expanduser(raw))
    except OSError:
        return None, f"无法解析路径: {raw}"
    if not os.path.isfile(resolved):
        return None, f"文件不存在: {raw}"
    for root in roots:
        try:
            root_resolved = os.path.realpath(os.path.expanduser(str(root).strip()))
        except OSError:
            continue
        if not root_resolved or not os.path.isdir(root_resolved):
            continue
        try:
            if os.path.commonpath([resolved, root_resolved]) == root_resolved:
                return resolved, None
        except ValueError:
            continue
    return None, f"路径不在允许的媒体库/监控目录内: {raw}"
