"""
R2 上传模块（插件内嵌版）
"""
import boto3
from botocore.config import Config

from .runtime_config import ConfigError, settings

_BOTO_CONFIG = Config(proxies={})

_MIME_MAP = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mpd": "application/dash+xml",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".ts": "video/MP2T",
    ".vtt": "text/vtt",
    ".srt": "application/x-subrip",
    ".nfo": "application/xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".json": "application/json",
}


def get_s3_client():
    """创建 R2 (S3 兼容) 客户端。"""
    missing = settings.validate_r2()
    if missing:
        raise ConfigError(
            "R2 配置缺失: " + "、".join(missing) +
            "。请填写可自动配置的 CF API Token，或手动填写完整 R2 配置。"
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=_BOTO_CONFIG,
    )
