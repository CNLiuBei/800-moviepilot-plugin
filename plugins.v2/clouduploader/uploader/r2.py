"""
R2 上传模块（插件内嵌版）

大文件按 Cloudflare 官方 boto3 建议：低层 multipart API + ThreadPoolExecutor
并行 upload_part（避免 upload_file / TransferConfig 受 GIL 限制）。
文档: https://developers.cloudflare.com/r2/examples/aws/boto3/
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

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

# Cloudflare boto3 example uses 16 MiB parts; threshold matches part size.
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


def upload_concurrency() -> int:
    """Parallel part workers from plugin「上传并发数」, clamped 1–16."""
    try:
        concurrency = int(settings.UPLOAD_CONCURRENCY)
    except (TypeError, ValueError):
        concurrency = 8
    return max(1, min(concurrency, 16))


def get_transfer_config() -> TransferConfig:
    """Legacy TransferConfig for log/compat; uploads no longer use upload_file."""
    concurrency = upload_concurrency()
    return TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=_MULTIPART_CHUNKSIZE,
        max_concurrency=concurrency,
        use_threads=True,
    )


def _put_small_object(
    s3,
    filename: str,
    bucket: str,
    key: str,
    extra_args: dict,
    callback: Callable[[int], None] | None,
) -> None:
    with open(filename, "rb") as fh:
        body = fh.read()
    put_kwargs = {"Bucket": bucket, "Key": key, "Body": body, **extra_args}
    s3.put_object(**put_kwargs)
    if callback:
        callback(len(body))


def _upload_part_range(
    s3,
    *,
    bucket: str,
    key: str,
    upload_id: str,
    part_number: int,
    filename: str,
    offset: int,
    length: int,
    callback: Callable[[int], None] | None,
) -> dict:
    with open(filename, "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
    response = s3.upload_part(
        Bucket=bucket,
        Key=key,
        UploadId=upload_id,
        PartNumber=part_number,
        Body=data,
    )
    if callback:
        callback(len(data))
    return {"PartNumber": part_number, "ETag": response["ETag"]}


def _clamp_workers(value: int | None) -> int:
    if value is None:
        return upload_concurrency()
    try:
        workers = int(value)
    except (TypeError, ValueError):
        workers = 1
    return max(1, min(workers, 16))


def _multipart_upload_parallel(
    s3,
    filename: str,
    bucket: str,
    key: str,
    file_size: int,
    extra_args: dict,
    callback: Callable[[int], None] | None,
    *,
    part_concurrency: int | None = None,
) -> None:
    """Cloudflare-recommended: create_multipart + ThreadPoolExecutor upload_part."""
    workers = _clamp_workers(part_concurrency)
    part_size = _MULTIPART_CHUNKSIZE
    part_count = max(1, math.ceil(file_size / part_size))

    create_kwargs = {"Bucket": bucket, "Key": key, **extra_args}
    mpu = s3.create_multipart_upload(**create_kwargs)
    upload_id = mpu["UploadId"]

    ranges: list[tuple[int, int, int]] = []
    for index in range(part_count):
        offset = index * part_size
        length = min(part_size, file_size - offset)
        ranges.append((index + 1, offset, length))

    try:
        parts: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _upload_part_range,
                    s3,
                    bucket=bucket,
                    key=key,
                    upload_id=upload_id,
                    part_number=part_number,
                    filename=filename,
                    offset=offset,
                    length=length,
                    callback=callback,
                )
                for part_number, offset, length in ranges
            ]
            for future in as_completed(futures):
                parts.append(future.result())

        parts.sort(key=lambda item: item["PartNumber"])
        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except Exception:
            pass
        raise


def upload_file_resilient(
    s3,
    filename: str,
    bucket: str,
    key: str,
    *,
    extra_args: dict | None = None,
    callback=None,
    part_concurrency: int | None = None,
) -> None:
    """Upload via put_object (small) or parallel multipart (large).

    part_concurrency limits multipart upload_part workers. Callers that already
    parallelize across files should pass 1 to avoid nested thread-pool blowups.
    """
    extra = dict(extra_args or {})
    file_size = os.path.getsize(filename)
    if file_size < _MULTIPART_THRESHOLD:
        _put_small_object(s3, filename, bucket, key, extra, callback)
        return
    _multipart_upload_parallel(
        s3,
        filename,
        bucket,
        key,
        file_size,
        extra,
        callback,
        part_concurrency=part_concurrency,
    )
