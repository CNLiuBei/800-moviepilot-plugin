"""
R2 上传模块（插件内嵌版）
"""
from __future__ import annotations

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from .runtime_config import ConfigError, settings

# Slow uplinks need long read timeouts for multipart parts.
_BOTO_CONFIG = Config(
    proxies={},
    connect_timeout=30,
    read_timeout=900,
    retries={"max_attempts": 8, "mode": "standard"},
    max_pool_connections=32,
)

# Baseline part size; concurrency comes from settings.UPLOAD_CONCURRENCY
# (plugin form「上传并发数」), shared with multi-file directory uploads.
_MULTIPART_CHUNKSIZE = 16 * 1024 * 1024
_MULTIPART_THRESHOLD = 16 * 1024 * 1024

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
    """Multipart settings for large R2 uploads.

    Uses plugin「上传并发数」(settings.UPLOAD_CONCURRENCY) so single-file
    direct MP4 uploads honor the same knob as multi-file HLS uploads.
    """
    try:
        concurrency = int(settings.UPLOAD_CONCURRENCY)
    except (TypeError, ValueError):
        concurrency = 8
    concurrency = max(1, min(concurrency, 16))
    return TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=_MULTIPART_CHUNKSIZE,
        max_concurrency=concurrency,
        use_threads=True,
    )


def upload_file_resilient(
    s3,
    filename: str,
    bucket: str,
    key: str,
    *,
    extra_args: dict | None = None,
    callback=None,
) -> None:
    """Upload with long timeouts; concurrency follows UPLOAD_CONCURRENCY."""
    s3.upload_file(
        filename,
        bucket,
        key,
        ExtraArgs=extra_args or {},
        Callback=callback,
        Config=get_transfer_config(),
    )
