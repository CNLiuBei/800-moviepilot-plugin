"""
外部二进制自动安装（非 Docker 原生部署用）

负责确保 ffmpeg / ffprobe 可用：
- ffmpeg / ffprobe：优先系统 PATH；缺失时回退到 pip 包 static_ffmpeg / imageio_ffmpeg 提供的二进制

所有探测/安装结果回写到 runtime_config.settings 的 *_BIN 字段。

注：切片由 FFmpeg 完成；mediastreamvalidator 仅用于校验，缺失时跳过校验。
"""
from __future__ import annotations

import os
import shutil
from typing import Optional


from .runtime_config import settings

# ponytail: fixed list; add paths via plugin ffmpeg_bin / ffprobe_bin settings
_COMMON_BIN_DIRS = (
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
    "/bin",
)


# ─── ffmpeg / ffprobe ───

def _pip_install_static_ffmpeg(log=print) -> bool:
    """pip 安装 static-ffmpeg（提供 ffmpeg + ffprobe）。"""
    try:
        import subprocess
        import sys
        log("   ffmpeg/ffprobe: 尝试 pip 安装 static-ffmpeg …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "static-ffmpeg", "-q"],
            timeout=300,
        )
        return True
    except Exception as e:
        log(f"   ffmpeg/ffprobe: pip 安装 static-ffmpeg 失败 ({e})")
        return False


def _resolve_ffmpeg_from_pip() -> tuple[Optional[str], Optional[str]]:
    """尝试从 pip 包获取 ffmpeg/ffprobe 路径。返回 (ffmpeg, ffprobe)，任一缺失为 None。"""
    ffmpeg_path = None
    ffprobe_path = None

    # static_ffmpeg：同时提供 ffmpeg + ffprobe
    try:
        import static_ffmpeg  # type: ignore
        from static_ffmpeg import run as _sf_run  # type: ignore
        try:
            ff, fp = _sf_run.get_or_fetch_platform_executables_else_raise()
            ffmpeg_path = ff
            ffprobe_path = fp
        except Exception:
            pass
    except Exception:
        pass

    # imageio_ffmpeg：仅提供 ffmpeg
    if not ffmpeg_path:
        try:
            import imageio_ffmpeg  # type: ignore
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

    return ffmpeg_path, ffprobe_path


def ensure_ffmpeg(log=print, auto_install: bool = True) -> tuple[bool, bool]:
    """
    确保 ffmpeg/ffprobe 可用，回写 settings.FFMPEG_BIN / FFPROBE_BIN。
    返回 (has_ffmpeg, has_ffprobe)。
    """
    sys_ffmpeg = _resolve_tool(settings.FFMPEG_BIN)
    sys_ffprobe = _resolve_tool(settings.FFPROBE_BIN)
    if sys_ffmpeg:
        settings.FFMPEG_BIN = sys_ffmpeg
    if sys_ffprobe:
        settings.FFPROBE_BIN = sys_ffprobe

    has_ffmpeg = bool(sys_ffmpeg)
    has_ffprobe = bool(sys_ffprobe)

    if not (has_ffmpeg and has_ffprobe) and auto_install:
        _pip_install_static_ffmpeg(log=log)

    if not (has_ffmpeg and has_ffprobe):
        pip_ffmpeg, pip_ffprobe = _resolve_ffmpeg_from_pip()
        if not has_ffmpeg and pip_ffmpeg and os.path.isfile(pip_ffmpeg):
            settings.FFMPEG_BIN = pip_ffmpeg
            has_ffmpeg = True
            log(f"   ffmpeg: 使用 pip 提供的二进制 {pip_ffmpeg}")
        if not has_ffprobe and pip_ffprobe and os.path.isfile(pip_ffprobe):
            settings.FFPROBE_BIN = pip_ffprobe
            has_ffprobe = True
            log(f"   ffprobe: 使用 pip 提供的二进制 {pip_ffprobe}")

    return has_ffmpeg, has_ffprobe


def probe_binaries() -> dict:
    """只读探测当前 ffmpeg/ffprobe/mediastreamvalidator 是否可用（不触发 pip 安装）。"""
    ffmpeg = _resolve_tool(settings.FFMPEG_BIN)
    ffprobe = _resolve_tool(settings.FFPROBE_BIN)
    validator = _resolve_tool(settings.MEDIASTREAMVALIDATOR_BIN)
    return {
        "ffmpeg": bool(ffmpeg),
        "ffprobe": bool(ffprobe),
        "mediastreamvalidator": bool(validator),
        "ffmpeg_path": ffmpeg or settings.FFMPEG_BIN,
        "ffprobe_path": ffprobe or settings.FFPROBE_BIN,
        "mediastreamvalidator_path": validator,
    }


def ensure_all(log=print, auto_install: bool = True) -> dict:
    """
    一次性确保所有二进制可用。返回检测结果 dict。
    """
    has_ffmpeg, has_ffprobe = ensure_ffmpeg(log=log, auto_install=auto_install)
    has_packager = ensure_packager(log=log)
    has_validator = ensure_mediastreamvalidator(log=log)
    return {
        "ffmpeg": has_ffmpeg,
        "ffprobe": has_ffprobe,
        "packager": has_packager,
        "apple_hls": has_validator,
        "mediastreamvalidator": has_validator,
        "ffmpeg_path": settings.FFMPEG_BIN,
        "ffprobe_path": settings.FFPROBE_BIN,
        "packager_path": settings.PACKAGER_BIN if has_packager else None,
        "mediastreamvalidator_path": settings.MEDIASTREAMVALIDATOR_BIN if has_validator else None,
    }


def ensure_packager(log=print) -> bool:
    """确保 Shaka Packager 可用（用于字幕 fMP4 IMSC1/stpp 转换）。"""
    sys_packager = shutil.which(settings.PACKAGER_BIN)
    if sys_packager:
        settings.PACKAGER_BIN = sys_packager
        return True
    try:
        import subprocess, sys
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "shaka-packager", "-q"],
            timeout=120
        )
        after = shutil.which("packager") or shutil.which("shaka-packager")
        if after:
            settings.PACKAGER_BIN = after
            log("   packager: pip 安装成功")
            return True
    except Exception:
        pass
    log("   packager: 不可用（字幕将不支持 fMP4 IMSC1）")
    return False


def _resolve_tool(configured: str) -> str | None:
    found = shutil.which(configured)
    if found:
        return found
    if os.path.isabs(configured) and os.path.isfile(configured):
        return configured
    if configured and "/" not in configured:
        for d in _COMMON_BIN_DIRS:
            candidate = os.path.join(d, configured)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def ensure_mediastreamvalidator(log=print) -> bool:
    """探测 Apple mediastreamvalidator（HLS 校验，可选）。"""
    resolved = _resolve_tool(settings.MEDIASTREAMVALIDATOR_BIN)
    if resolved:
        settings.MEDIASTREAMVALIDATOR_BIN = resolved
        log(f"   mediastreamvalidator: {resolved}")
        return True
    log("   mediastreamvalidator: 未安装（将跳过 Apple 官方校验）")
    return False
