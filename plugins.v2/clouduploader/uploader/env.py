"""
插件运行时环境：外部二进制探测、跨平台路径解析、详情页展示。

分层：
  必需  ffmpeg / ffprobe     → 全平台，可 pip 安装 static-ffmpeg
  内置  manifest 校验         → 全平台，无外部依赖
  可选  mediastreamvalidator  → 仅 macOS（Apple HLS Tools）
  可选  packager              → 字幕 fMP4，缺失则跳过
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from .runtime_config import settings

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class ToolStatus:
    name: str
    required: bool
    available: bool
    path: str | None
    # ok | missing | skipped | optional_missing
    state: str
    hint: str = ""


def _is_docker() -> bool:
    if os.path.isfile("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="ignore") as f:
            return "docker" in f.read()
    except OSError:
        return False


def platform_label() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform == "win32":
        return "Windows"
    if _is_docker():
        return "Linux (Docker)"
    return "Linux"


def search_dirs() -> tuple[str, ...]:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return tuple(
            p for p in (
                os.path.join(local, "Programs", "ffmpeg", "bin") if local else "",
                r"C:\ffmpeg\bin",
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin"),
            ) if p
        )
    if sys.platform == "darwin":
        return ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
    return ("/usr/local/bin", "/usr/bin", "/bin")


def resolve_tool(configured: str) -> str | None:
    """解析可执行文件：PATH → 绝对路径 → 平台常见目录。"""
    if not configured:
        return None
    found = shutil.which(configured)
    if found:
        return found
    if os.path.isabs(configured) and os.path.isfile(configured):
        return configured
    base = os.path.basename(configured.replace("\\", "/"))
    if base and base == configured.replace("\\", "/"):
        for d in search_dirs():
            candidate = os.path.join(d, base)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _pip_install_static_ffmpeg(log: LogFn = print) -> bool:
    try:
        import subprocess
        log("   ffmpeg/ffprobe: 尝试 pip 安装 static-ffmpeg …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "static-ffmpeg", "-q"],
            timeout=300,
        )
        return True
    except Exception as e:
        log(f"   ffmpeg/ffprobe: pip 安装 static-ffmpeg 失败 ({e})")
        return False


def _resolve_ffmpeg_from_pip() -> tuple[str | None, str | None]:
    ffmpeg_path = None
    ffprobe_path = None
    try:
        from static_ffmpeg import run as sf_run  # type: ignore
        try:
            ff, fp = sf_run.get_or_fetch_platform_executables_else_raise()
            ffmpeg_path, ffprobe_path = ff, fp
        except Exception:
            pass
    except Exception:
        pass
    if not ffmpeg_path:
        try:
            import imageio_ffmpeg  # type: ignore
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return ffmpeg_path, ffprobe_path


def _ffmpeg_install_hint() -> str:
    if sys.platform == "win32":
        return "开启自动安装，或填写 ffmpeg.exe 完整路径"
    if sys.platform == "darwin":
        return "brew install ffmpeg，或开启自动安装"
    if _is_docker():
        return "MoviePilot 镜像通常已内置；否则开启自动安装"
    return "apt install ffmpeg，或开启自动安装"


def _validator_hint() -> str:
    if sys.platform != "darwin":
        return "仅 macOS 可用，已用内置 HLS 校验"
    return "从 Apple Developer 下载 HLS Tools 并安装到 /usr/local/bin"


def ensure_ffmpeg(log: LogFn = print, auto_install: bool = True) -> tuple[bool, bool]:
    ff = resolve_tool(settings.FFMPEG_BIN)
    fp = resolve_tool(settings.FFPROBE_BIN)
    if ff:
        settings.FFMPEG_BIN = ff
    if fp:
        settings.FFPROBE_BIN = fp
    has_ffmpeg, has_ffprobe = bool(ff), bool(fp)

    if not (has_ffmpeg and has_ffprobe) and auto_install:
        _pip_install_static_ffmpeg(log=log)

    if not (has_ffmpeg and has_ffprobe):
        pip_ff, pip_fp = _resolve_ffmpeg_from_pip()
        if not has_ffmpeg and pip_ff and os.path.isfile(pip_ff):
            settings.FFMPEG_BIN = pip_ff
            has_ffmpeg = True
            log(f"   ffmpeg: 使用 pip 二进制 {pip_ff}")
        if not has_ffprobe and pip_fp and os.path.isfile(pip_fp):
            settings.FFPROBE_BIN = pip_fp
            has_ffprobe = True
            log(f"   ffprobe: 使用 pip 二进制 {pip_fp}")

    return has_ffmpeg, has_ffprobe


def ensure_packager(log: LogFn = print) -> bool:
    resolved = resolve_tool(settings.PACKAGER_BIN)
    if resolved:
        settings.PACKAGER_BIN = resolved
        return True
    log("   packager: 不可用（字幕将跳过 fMP4 IMSC1）")
    return False


def ensure_mediastreamvalidator(log: LogFn = print) -> bool:
    if sys.platform != "darwin":
        log("   mediastreamvalidator: 当前平台不适用（使用内置 HLS 校验）")
        return False
    resolved = resolve_tool(settings.MEDIASTREAMVALIDATOR_BIN)
    if resolved:
        settings.MEDIASTREAMVALIDATOR_BIN = resolved
        log(f"   mediastreamvalidator: {resolved}")
        return True
    log("   mediastreamvalidator: 未安装（使用内置 HLS 校验）")
    return False


def build_tool_statuses(
    *,
    has_ffmpeg: bool,
    has_ffprobe: bool,
    has_packager: bool,
    has_validator: bool,
) -> list[ToolStatus]:
    ff_path = settings.FFMPEG_BIN if has_ffmpeg else None
    fp_path = settings.FFPROBE_BIN if has_ffprobe else None
    val_path = settings.MEDIASTREAMVALIDATOR_BIN if has_validator else None
    pkg_path = settings.PACKAGER_BIN if has_packager else None

    tools = [
        ToolStatus("ffmpeg", True, has_ffmpeg, ff_path,
                   "ok" if has_ffmpeg else "missing", _ffmpeg_install_hint()),
        ToolStatus("ffprobe", True, has_ffprobe, fp_path,
                   "ok" if has_ffprobe else "missing", _ffmpeg_install_hint()),
        ToolStatus("mediastreamvalidator", False, has_validator, val_path,
                   "ok" if has_validator else ("skipped" if sys.platform != "darwin" else "optional_missing"),
                   _validator_hint()),
        ToolStatus("packager", False, has_packager, pkg_path,
                   "ok" if has_packager else "optional_missing", "可选，用于字幕 fMP4"),
    ]
    return tools


def resolve_environment(log: LogFn = print, auto_install: bool = True) -> dict:
    """探测/安装外部二进制，回写 settings，返回结构化环境报告。"""
    has_ffmpeg, has_ffprobe = ensure_ffmpeg(log=log, auto_install=auto_install)
    has_packager = ensure_packager(log=log)
    has_validator = ensure_mediastreamvalidator(log=log)
    tools = build_tool_statuses(
        has_ffmpeg=has_ffmpeg,
        has_ffprobe=has_ffprobe,
        has_packager=has_packager,
        has_validator=has_validator,
    )
    return {
        "platform": platform_label(),
        "ready": has_ffmpeg and has_ffprobe,
        "ffmpeg": has_ffmpeg,
        "ffprobe": has_ffprobe,
        "packager": has_packager,
        "apple_hls": has_validator,
        "mediastreamvalidator": has_validator,
        "ffmpeg_path": settings.FFMPEG_BIN,
        "ffprobe_path": settings.FFPROBE_BIN,
        "packager_path": settings.PACKAGER_BIN if has_packager else None,
        "mediastreamvalidator_path": settings.MEDIASTREAMVALIDATOR_BIN if has_validator else None,
        "tools": tools,
    }


def probe_environment() -> dict:
    """只读探测，不触发 pip 安装。"""
    ff = resolve_tool(settings.FFMPEG_BIN)
    fp = resolve_tool(settings.FFPROBE_BIN)
    pkg = resolve_tool(settings.PACKAGER_BIN)
    val = resolve_tool(settings.MEDIASTREAMVALIDATOR_BIN) if sys.platform == "darwin" else None
    if ff:
        settings.FFMPEG_BIN = ff
    if fp:
        settings.FFPROBE_BIN = fp
    if pkg:
        settings.PACKAGER_BIN = pkg
    if val:
        settings.MEDIASTREAMVALIDATOR_BIN = val
    has_ffmpeg, has_ffprobe = bool(ff), bool(fp)
    has_packager = bool(pkg)
    has_validator = bool(val)
    return {
        "platform": platform_label(),
        "ready": has_ffmpeg and has_ffprobe,
        "ffmpeg": has_ffmpeg,
        "ffprobe": has_ffprobe,
        "packager": has_packager,
        "apple_hls": has_validator,
        "mediastreamvalidator": has_validator,
        "ffmpeg_path": ff or settings.FFMPEG_BIN,
        "ffprobe_path": fp or settings.FFPROBE_BIN,
        "packager_path": pkg,
        "mediastreamvalidator_path": val,
        "tools": build_tool_statuses(
            has_ffmpeg=has_ffmpeg,
            has_ffprobe=has_ffprobe,
            has_packager=has_packager,
            has_validator=has_validator,
        ),
    }


def format_tool_line(tool: ToolStatus) -> str:
    label = tool.name
    if tool.state == "ok":
        return f"{label}: ✅ {tool.path}"
    if tool.state == "skipped":
        return f"{label}: ➖ {tool.hint}"
    if tool.required:
        return f"{label}: ❌ 未找到（{tool.hint}）"
    return f"{label}: ⚠️ 未安装（{tool.hint}）"


def format_env_header(env: dict) -> str:
    ready = "✅ 就绪" if env.get("ready") else "⚠️ 未就绪"
    return f"平台: {env.get('platform', '?')} | 切片环境 {ready}"


# 兼容旧 import
ensure_all = resolve_environment
probe_binaries = probe_environment
