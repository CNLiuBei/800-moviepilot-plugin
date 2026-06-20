"""
外部二进制自动安装（非 Docker 原生部署用）

负责确保 ffmpeg / ffprobe 可用：
- ffmpeg / ffprobe：优先系统 PATH；缺失时回退到 pip 包 static_ffmpeg / imageio_ffmpeg 提供的二进制

所有探测/安装结果回写到 runtime_config.settings 的 *_BIN 字段。

注：Apple HTTP Live Streaming Tools 只能探测，不自动安装；缺失时切片任务会失败。
"""
from __future__ import annotations

import os
import shutil
from typing import Optional


from .runtime_config import settings


# ─── ffmpeg / ffprobe ───

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


def ensure_ffmpeg(log=print) -> tuple[bool, bool]:
    """
    确保 ffmpeg/ffprobe 可用，回写 settings.FFMPEG_BIN / FFPROBE_BIN。
    返回 (has_ffmpeg, has_ffprobe)。
    """
    # 1. 系统 PATH（含用户自定义路径）
    sys_ffmpeg = shutil.which(settings.FFMPEG_BIN)
    sys_ffprobe = shutil.which(settings.FFPROBE_BIN)
    if sys_ffmpeg:
        settings.FFMPEG_BIN = sys_ffmpeg
    if sys_ffprobe:
        settings.FFPROBE_BIN = sys_ffprobe

    has_ffmpeg = bool(sys_ffmpeg)
    has_ffprobe = bool(sys_ffprobe)

    # 2. 缺失则尝试 pip 包
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


def ensure_all(log=print) -> dict:
    """
    一次性确保所有二进制可用。返回检测结果 dict。
    """
    has_ffmpeg, has_ffprobe = ensure_ffmpeg(log=log)
    has_packager = ensure_packager(log=log)
    apple_hls = ensure_apple_hls_tools(log=log)
    return {
        "ffmpeg": has_ffmpeg,
        "ffprobe": has_ffprobe,
        "packager": has_packager,
        "apple_hls": apple_hls["available"],
        "mediafilesegmenter": apple_hls["mediafilesegmenter"],
        "mediasubtitlesegmenter": apple_hls["mediasubtitlesegmenter"],
        "variantplaylistcreator": apple_hls["variantplaylistcreator"],
        "mediastreamvalidator": apple_hls["mediastreamvalidator"],
        "ffmpeg_path": settings.FFMPEG_BIN,
        "ffprobe_path": settings.FFPROBE_BIN,
        "packager_path": settings.PACKAGER_BIN if has_packager else None,
        "mediafilesegmenter_path": settings.MEDIAFILESEGMENTER_BIN if apple_hls["mediafilesegmenter"] else None,
        "mediasubtitlesegmenter_path": settings.MEDIASUBTITLESEGMENTER_BIN if apple_hls["mediasubtitlesegmenter"] else None,
        "variantplaylistcreator_path": settings.VARIANTPLAYLISTCREATOR_BIN if apple_hls["variantplaylistcreator"] else None,
        "mediastreamvalidator_path": settings.MEDIASTREAMVALIDATOR_BIN if apple_hls["mediastreamvalidator"] else None,
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
    return None


def ensure_apple_hls_tools(log=print) -> dict:
    """探测 Apple HTTP Live Streaming Tools。官方工具需用户预先安装。"""
    tools = {
        "mediafilesegmenter": "MEDIAFILESEGMENTER_BIN",
        "mediasubtitlesegmenter": "MEDIASUBTITLESEGMENTER_BIN",
        "variantplaylistcreator": "VARIANTPLAYLISTCREATOR_BIN",
        "mediastreamvalidator": "MEDIASTREAMVALIDATOR_BIN",
    }
    result: dict[str, bool] = {}
    for name, setting_name in tools.items():
        configured = getattr(settings, setting_name)
        resolved = _resolve_tool(configured)
        if resolved:
            setattr(settings, setting_name, resolved)
        result[name] = bool(resolved)

    result["available"] = result["mediafilesegmenter"] and result["mediastreamvalidator"]
    if result["available"]:
        log(f"   Apple HLS Tools: mediafilesegmenter={settings.MEDIAFILESEGMENTER_BIN}")
    else:
        log("   Apple HLS Tools: 未完整安装（切片任务会失败）")
    return result
