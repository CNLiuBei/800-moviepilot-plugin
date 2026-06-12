"""
运行时配置中心（插件内嵌版）

与独立工具的 config.py 不同：
- 不在 import 时强制校验，避免插件加载即崩溃
- 配置由插件主体通过 configure() 注入（来源：MoviePilot 插件表单）
- 各业务模块统一从这里读取配置
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """配置项非法。"""


def normalize_base_url(value: object, label: str = "URL") -> str:
    """规范化站点根地址，避免 httpx 抛出难懂的 Invalid port。"""
    raw = str(value or "").strip().replace("：", ":")
    raw = "".join(ch for ch in raw if ord(ch) >= 32)
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as e:
        raise ConfigError(f"{label} 配置错误: {raw!r} ({e})") from e
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{label} 必须是 http(s) 站点根地址: {raw!r}")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


class _Settings:
    """全局配置单例。插件 init_plugin 时调用 configure() 填充。"""

    # R2
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = "flix-800-assets"

    # 站点 API
    API_BASE: str = ""
    API_ADMIN_KEY: str = ""
    API_USERNAME: str = ""
    API_PASSWORD: str = ""

    # TMDB
    TMDB_TOKEN: str = ""

    @property
    def tmdb_auth(self) -> dict:
        """
        返回 TMDB 请求所需的 (headers, params) 认证片段。
        自动区分 v4 Bearer Token 与 v3 API Key：
        - v4 Token：很长（JWT，含点号），用 Authorization: Bearer
        - v3 Key：32 位十六进制，用 api_key 查询参数
        """
        tok = (self.TMDB_TOKEN or "").strip()
        if not tok:
            return {"headers": {}, "params": {}}
        # v4 token 通常以 eyJ 开头或含 '.'，且远长于 32
        if len(tok) > 40 or "." in tok:
            return {"headers": {"Authorization": f"Bearer {tok}"}, "params": {}}
        return {"headers": {}, "params": {"api_key": tok}}

    # 切片
    HLS_SEGMENT_SECONDS: int = 6
    HLS_OUTPUT_DIR: Path = Path("/tmp/clouduploader-hls")
    UPLOAD_CONCURRENCY: int = 8

    # CMAF
    CMAF_AUDIO_BITRATE: str = "192k"
    CMAF_AUDIO_CHANNELS: int = 2
    CMAF_VIDEO_FALLBACK_CRF: int = 23

    # 外部二进制路径（可被插件覆盖；默认从 PATH 查找）
    FFMPEG_BIN: str = "ffmpeg"
    FFPROBE_BIN: str = "ffprobe"
    PACKAGER_BIN: str = "packager"  # Shaka Packager（字幕 fMP4 IMSC1/stpp）

    # 通知（沿用独立工具的 Telegram，可选；插件内另有 MoviePilot 通知）
    TG_BOT_TOKEN: str = ""
    TG_CHAT_ID: str = ""

    @property
    def R2_ENDPOINT(self) -> str:
        if not self.R2_ACCOUNT_ID:
            raise ConfigError("R2 账户 ID 缺失，无法生成 R2 endpoint")
        return f"https://{self.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

    def configure(self, **kwargs) -> None:
        """由插件注入配置。仅覆盖非 None 值。"""
        for key, value in kwargs.items():
            if value is None:
                continue
            key_upper = key.upper()
            if hasattr(self, key_upper):
                # 类型对齐
                cur = getattr(self, key_upper)
                if isinstance(cur, Path):
                    value = Path(value)
                elif isinstance(cur, int) and not isinstance(value, bool):
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                elif key_upper == "API_BASE":
                    value = normalize_base_url(value, "流媒体站地址")
                elif isinstance(cur, str):
                    value = str(value).strip()
                setattr(self, key_upper, value)

    def validate_r2(self) -> list[str]:
        """返回 R2 上传所需缺失项。"""
        missing = []
        if not self.R2_ACCOUNT_ID:
            missing.append("R2 账户 ID")
        if not self.R2_ACCESS_KEY_ID:
            missing.append("R2 Access Key")
        if not self.R2_SECRET_ACCESS_KEY:
            missing.append("R2 Secret Key")
        if not self.R2_BUCKET:
            missing.append("R2 Bucket")
        return missing

    def validate(self) -> list[str]:
        """返回缺失的必填项列表（空列表表示配置完整）。"""
        missing = self.validate_r2()
        if not self.TMDB_TOKEN:
            missing.append("TMDB Token")
        if not self.API_BASE:
            missing.append("流媒体站地址")
        if not self.API_ADMIN_KEY and not (self.API_USERNAME and self.API_PASSWORD):
            missing.append("站点认证（Admin Key 或 用户名+密码）")
        return missing


settings = _Settings()
