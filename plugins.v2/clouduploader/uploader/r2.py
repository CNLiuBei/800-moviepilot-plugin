"""
R2 上传模块（插件内嵌版）
"""
from __future__ import annotations

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from .runtime_config import ConfigError, settings

# Slow uplinks (≈0.5 MB/s) need long read timeouts for multipart parts.
_BOTO_CONFIG = Config(
    proxies={},
    connect_timeout=30,
    read_timeout=900,
    retries={"max_attempts": 8, "mode": "standard"},
    max_pool_connections=20,
)

# Smaller parts + modest concurrency reduce per-part timeout risk on slow links.
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=2,
    use_threads=True,
)

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


def get_transfer_config() -> TransferConfig:
    """Return the shared multipart transfer settings for large R2 uploads."""
    return _TRANSFER_CONFIG


def upload_file_resilient(
    s3,
    filename: str,
    bucket: str,
    key: str,
    *,
    extra_args: dict | None = None,
    callback=None,
) -> None:
    """Upload with long timeouts and conservative multipart settings."""
    s3.upload_file(
        filename,
        bucket,
        key,
        ExtraArgs=extra_args or {},
        Callback=callback,
        Config=get_transfer_config(),
    )
