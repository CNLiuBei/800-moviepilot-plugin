"""
任务执行器（插件内嵌版）

负责单个上传任务的完整生命周期: 切片 → 字幕 → 上传 → NFO → 入库。
与独立工具版本的区别：
- 去掉 rich/JobManager 依赖，改用简单的 log_fn 回调（对接 MoviePilot 日志）
- 配置统一从 runtime_config.settings 读取
- run_job 接受 params(dict) + log_fn，不再依赖 Web 任务对象
"""
import os
import json
import re
import shutil
import subprocess
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event, Thread
from urllib.parse import unquote, urlparse

from .runtime_config import settings
from .env import resolve_tool
from .parser import parse_filename
from .slicer import apple_hls_slice, get_video_duration
from .subtitles import resolve_subtitles_for_upload, generate_hls_subtitle_playlists
from .tmdb import get_original_language, get_imdb_id, verify_tmdb_metadata
from .register import auto_register, write_episode_nfo, write_show_nfo
from .notify import (
    notify_upload_success,
    notify_upload_failed,
    notify_register_failed,
    notify_register_success,
)
from .r2 import get_s3_client, upload_file_resilient, _MIME_MAP
from .direct_media import prepare_direct_mp4
from .upload_policy import direct_mode_enabled
from .web_playback_check import verify_remote_mp4_web_playable


# ─── 重试工具 ───

def retry(fn, attempts: int = 3, base_delay: float = 3.0, log=print, what: str = "操作",
          cancel_check=None):
    """
    带指数退避的重试包装。fn 返回真值视为成功。
    任一次抛异常或返回假值都会重试，直到成功或耗尽次数。

    Returns:
        (ok: bool, result): ok 表示最终是否成功，result 为 fn 的返回值/None
    """
    last_exc = None
    for i in range(1, attempts + 1):
        if cancel_check and cancel_check():
            return False, None
        try:
            result = fn()
            if result:
                if i > 1:
                    log(f"   ✅ {what} 第 {i} 次重试成功")
                return True, result
            last_exc = None
        except Exception as e:
            last_exc = e
            log(f"   ⚠️ {what} 第 {i}/{attempts} 次失败: {e}")
        if i < attempts:
            delay = base_delay * (2 ** (i - 1))
            log(f"   ⏳ {delay:.0f}s 后重试 {what}...")
            time.sleep(delay)
    if last_exc:
        log(f"   ❌ {what} 重试 {attempts} 次仍失败: {last_exc}")
    else:
        log(f"   ❌ {what} 重试 {attempts} 次仍未成功")
    return False, None


# ─── R2 上传（覆盖目标前缀，避免历史残留）───

_UPLOAD_MARKER_FILES = {"uploading.json", "uploaded.json", "ready.json"}
_UPLOAD_MARKERS_TO_KEEP_WHILE_UPLOADING = {"uploading.json"}


def _list_r2_prefix(s3, r2_prefix: str) -> dict[str, int]:
    existing: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.R2_BUCKET, Prefix=r2_prefix + "/"):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(r2_prefix) + 1:]
            if rel:
                existing[rel] = obj["Size"]
    return existing


def _delete_r2_objects(s3, keys: list[str]) -> int:
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        response = s3.delete_objects(
            Bucket=settings.R2_BUCKET,
            Delete={"Objects": [{"Key": key} for key in batch]},
        )
        errors = response.get("Errors") or []
        if errors:
            failed_keys = ", ".join(str(item.get("Key", "")) for item in errors[:5])
            raise RuntimeError(f"R2 旧文件删除失败: {failed_keys}")
        deleted += len(batch)
    return deleted

def upload_directory_smart(local_dir: Path, r2_prefix: str, on_progress, cancel_check,
                           force_overwrite: bool = False) -> tuple[int, int]:
    """覆盖上传: 先清空目标前缀旧对象，再上传本地目录全部文件。返回 (uploaded, deleted)。"""
    s3 = get_s3_client()
    files = [f for f in local_dir.rglob("*") if f.is_file()]
    if not files:
        return 0, 0

    existing = _list_r2_prefix(s3, r2_prefix)
    hls_complete = "ready.json" in existing and (
        "master.m3u8" in existing or "stream.m3u8" in existing
    )
    if not force_overwrite and hls_complete:
        return 0, 0

    keys_to_delete = [
        f"{r2_prefix}/{rel}"
        for rel in existing.keys()
        if rel not in _UPLOAD_MARKERS_TO_KEEP_WHILE_UPLOADING
    ]
    deleted = _delete_r2_objects(s3, keys_to_delete) if keys_to_delete else 0

    uploaded = 0
    failed = 0
    total = len(files)
    total_bytes = sum(f.stat().st_size for f in files)
    uploaded_bytes = 0
    completed_files = 0
    start_time = time.time()

    def upload_one(f: Path) -> tuple[str, int]:
        rel_key = str(f.relative_to(local_dir)).replace("\\", "/")
        local_size = f.stat().st_size
        key = f"{r2_prefix}/{rel_key}"
        ct = _MIME_MAP.get(f.suffix.lower(), "application/octet-stream")
        upload_file_resilient(
            s3,
            str(f),
            settings.R2_BUCKET,
            key,
            extra_args={"ContentType": ct},
        )
        return "ok", local_size

    def upload_phase(phase_files: list[Path]) -> None:
        nonlocal uploaded, failed, uploaded_bytes, completed_files
        if not phase_files:
            return
        with ThreadPoolExecutor(max_workers=settings.UPLOAD_CONCURRENCY) as pool:
            futs = {pool.submit(upload_one, f): f for f in phase_files}
            for fut in as_completed(futs):
                if cancel_check():
                    return
                try:
                    r, size = fut.result()
                    uploaded_bytes += size
                    if r == "ok":
                        uploaded += 1
                except Exception as e:
                    failed += 1
                    failed_file = futs[fut]
                    print(f"[upload] 失败: {failed_file.name} - {e}")
                completed_files += 1
                elapsed = time.time() - start_time
                speed = uploaded_bytes / elapsed if elapsed > 0 else 0
                eta = (total_bytes - uploaded_bytes) / speed if speed > 0 else 0
                on_progress(completed_files / total, speed, eta)

    def rel_name(f: Path) -> str:
        return str(f.relative_to(local_dir)).replace("\\", "/")

    media_files = [f for f in files if f.suffix.lower() not in {".m3u8", ".mpd"}]
    child_manifests = [
        f for f in files
        if f.suffix.lower() in {".m3u8", ".mpd"} and rel_name(f) != "master.m3u8"
    ]
    master_manifests = [f for f in files if rel_name(f) == "master.m3u8"]

    # Upload manifests last so clients do not see playlists before referenced media is present.
    upload_phase(media_files)
    if not cancel_check() and failed == 0:
        upload_phase(child_manifests)
    if not cancel_check() and failed == 0:
        upload_phase(master_manifests)

    # 有文件上传失败：抛异常让上层 retry；重试会重新覆盖目标前缀，避免半成品残留。
    if failed:
        raise RuntimeError(f"{failed} 个文件上传失败（共 {total} 个），需重试补传")

    local_rels = {rel_name(f) for f in files}
    remote_rels = {
        rel for rel in _list_r2_prefix(s3, r2_prefix).keys()
        if rel not in _UPLOAD_MARKER_FILES
    }
    extra = sorted(remote_rels - local_rels)
    missing = sorted(local_rels - remote_rels)
    if extra or missing:
        details = []
        if extra:
            details.append("多余: " + ", ".join(extra[:5]))
        if missing:
            details.append("缺失: " + ", ".join(missing[:5]))
        raise RuntimeError("R2 覆盖上传校验失败（" + "；".join(details) + "）")

    return uploaded, deleted


def _upload_marker_payload(
    filepath: str,
    source_type: str,
    quality: str,
    subtitles: list[dict],
    duration_secs: int | None,
    upload_mode: str = "direct",
    h264_compat: bool = False,
    video_codec: str | None = None,
    width: int | None = None,
    height: int | None = None,
    bitrate: int | None = None,
    frame_rate: float | None = None,
) -> dict:
    from .upload_policy import normalize_upload_mode

    payload = {
        "file": Path(filepath).name,
        "sourceType": source_type,
        "quality": quality or "原画",
        "durationSecs": duration_secs,
        "subtitles": subtitles,
        "uploadMode": normalize_upload_mode(upload_mode),
        "h264Compat": bool(h264_compat),
    }
    if video_codec:
        payload["videoCodec"] = str(video_codec)
    if width:
        payload["width"] = int(width)
    if height:
        payload["height"] = int(height)
    if bitrate:
        payload["bitrate"] = int(bitrate)
    if frame_rate:
        payload["frameRate"] = float(frame_rate)
    return payload


def _put_upload_marker(r2_path: str, status: str, payload: dict | None = None, log=print) -> None:
    """Write a small R2 marker so later reconciliation can distinguish partial uploads."""
    try:
        body = {
            "status": status,
            "updatedAt": int(time.time()),
            **(payload or {}),
        }
        get_s3_client().put_object(
            Bucket=settings.R2_BUCKET,
            Key=f"{r2_path}/{status}.json",
            Body=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        log(f"   ⚠️ 上传标记写入失败 ({status}): {e}")


def _delete_upload_marker(r2_path: str, status: str, log=print) -> None:
    try:
        get_s3_client().delete_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/{status}.json")
    except Exception as e:
        log(f"   ⚠️ 上传标记清理失败 ({status}): {e}")


def _read_upload_marker(r2_path: str, status: str, log=print) -> dict:
    try:
        obj = get_s3_client().get_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/{status}.json")
        body = obj["Body"].read()
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"   ⚠️ 上传标记读取失败 ({status}): {e}")
        return {}


def _remote_source_type(r2_path: str) -> str:
    s3 = get_s3_client()
    for source_type, playlist in (
        ("cmaf", "master.m3u8"),
        ("hls", "stream.m3u8"),
        ("mp4", "video.mp4"),
    ):
        try:
            s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/{playlist}")
            return source_type
        except Exception:
            continue
    raise RuntimeError("R2 缺少 master.m3u8/stream.m3u8/video.mp4，无法确认播放源")


def upload_mp4_direct(
    filepath: str,
    r2_prefix: str,
    extra_files: list[Path] | None,
    on_progress,
    cancel_check,
    force_overwrite: bool = False,
) -> tuple[int, int]:
    """
    不分片直传：上传已准备的 MP4 为 video.mp4，并附带字幕等旁路文件。
    返回 (uploaded, deleted)。
    """
    s3 = get_s3_client()
    src = Path(filepath)
    if not src.is_file():
        raise FileNotFoundError(f"直传 MP4 不存在: {filepath}")

    existing = _list_r2_prefix(s3, r2_prefix)
    if (
        not force_overwrite
        and "ready.json" in existing
        and "video.mp4" in existing
    ):
        return 0, 0

    keys_to_delete = [
        f"{r2_prefix}/{rel}"
        for rel in existing.keys()
        if rel not in _UPLOAD_MARKERS_TO_KEEP_WHILE_UPLOADING
    ]
    deleted = _delete_r2_objects(s3, keys_to_delete) if keys_to_delete else 0

    uploads: list[tuple[Path, str]] = [(src, "video.mp4")]
    for f in extra_files or []:
        if f.is_file() and f.resolve() != src.resolve():
            uploads.append((f, f.name))

    total_bytes = sum(p.stat().st_size for p, _ in uploads)
    uploaded_bytes = 0
    uploaded = 0
    start_time = time.time()

    def _emit_progress(done: int) -> None:
        elapsed = time.time() - start_time
        speed = done / elapsed if elapsed > 0 else 0
        eta = (total_bytes - done) / speed if speed > 0 else 0
        pct = done / total_bytes if total_bytes else 1.0
        on_progress(pct, speed, eta)

    for local_path, rel_key in uploads:
        if cancel_check():
            raise RuntimeError("上传已取消")
        key = f"{r2_prefix}/{rel_key}"
        ct = "video/mp4" if rel_key == "video.mp4" else _MIME_MAP.get(
            local_path.suffix.lower(), "application/octet-stream"
        )

        file_done = [0]
        stop_ticker = Event()

        def progress_cb(bytes_amount):
            file_done[0] += bytes_amount

        def progress_ticker():
            while not stop_ticker.wait(3.0):
                _emit_progress(uploaded_bytes + file_done[0])

        ticker = Thread(target=progress_ticker, name="r2-upload-progress", daemon=True)
        ticker.start()
        try:
            # 立即打一条 0%，之后由 ticker 每 3 秒汇报，不依赖 boto Callback 频率
            _emit_progress(uploaded_bytes)
            upload_file_resilient(
                s3,
                str(local_path),
                settings.R2_BUCKET,
                key,
                extra_args={"ContentType": ct},
                callback=progress_cb,
            )
            uploaded_bytes += local_path.stat().st_size
            _emit_progress(uploaded_bytes)
            uploaded += 1
        finally:
            stop_ticker.set()
            ticker.join(timeout=1.0)

    for local_path, rel_key in uploads:
        key = f"{r2_prefix}/{rel_key}"
        expected_type = (
            "video/mp4"
            if rel_key == "video.mp4"
            else _MIME_MAP.get(local_path.suffix.lower(), "application/octet-stream")
        )
        remote = s3.head_object(Bucket=settings.R2_BUCKET, Key=key)
        if int(remote.get("ContentLength") or -1) != local_path.stat().st_size:
            if rel_key == "video.mp4":
                raise RuntimeError("R2 远端文件大小不一致")
            raise RuntimeError(f"R2 旁路文件 {rel_key} 大小不一致")
        remote_type = str(remote.get("ContentType") or "").split(";", 1)[0].strip()
        if remote_type != expected_type:
            if rel_key == "video.mp4":
                raise RuntimeError("R2 video.mp4 Content-Type 不是 video/mp4")
            raise RuntimeError(
                f"R2 旁路文件 {rel_key} Content-Type 不是 {expected_type}"
            )

    # Size/type alone is not enough: confirm the remote object is web-playable.
    video_key = f"{r2_prefix}/video.mp4"
    probe = verify_remote_mp4_web_playable(s3, settings.R2_BUCKET, video_key)
    print(
        f"[upload] Web 可播校验通过: {probe.get('videoCodec')}/"
        f"{probe.get('audioCodec')} {probe.get('width')}x{probe.get('height')}"
    )
    return uploaded, deleted


def _playlist_uri_value(value: str) -> str | None:
    value = value.strip().strip('"').strip("'")
    if not value or value.startswith("#") or value.startswith("data:"):
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path or "")
    if not path or path.startswith("/"):
        return None
    return path


def _playlist_references(playlist: Path, local_dir: Path) -> set[str]:
    refs: set[str] = set()
    attr_pattern = re.compile(r'\bURI=(?:"([^"]+)"|([^,\s]+))')
    rel_parent = playlist.relative_to(local_dir).parent

    try:
        text = playlist.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return refs

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        candidates: list[str] = []
        if line.startswith("#"):
            candidates.extend((m.group(1) or m.group(2) or "") for m in attr_pattern.finditer(line))
        else:
            candidates.append(line)

        for candidate in candidates:
            uri_path = _playlist_uri_value(candidate)
            if not uri_path:
                continue
            resolved = (rel_parent / uri_path).as_posix()
            normalized = Path(resolved)
            if ".." in normalized.parts:
                continue
            refs.add(normalized.as_posix())
    return refs


def _assert_fmp4_playlist_has_map(playlist: Path) -> None:
    try:
        text = playlist.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        raise RuntimeError(f"播放清单读取失败: {playlist.name}: {e}") from e

    media_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    references_fmp4 = any(urlparse(line).path.endswith(".m4s") for line in media_lines)
    if references_fmp4 and "#EXT-X-MAP:" not in text:
        raise RuntimeError(f"fMP4 播放清单缺少 EXT-X-MAP: {playlist.name}")


def _verify_uploaded_hls(local_dir: Path, r2_path: str, log=print) -> None:
    """Validate local playback manifests before continuing registration."""
    required: set[str] = set()

    for playlist in local_dir.rglob("*.m3u8"):
        _assert_fmp4_playlist_has_map(playlist)
        required.add(playlist.relative_to(local_dir).as_posix())
        required.update(_playlist_references(playlist, local_dir))
    for manifest in local_dir.rglob("*.mpd"):
        required.add(manifest.relative_to(local_dir).as_posix())
    for init_file in local_dir.rglob("init*.mp4"):
        required.add(init_file.relative_to(local_dir).as_posix())

    # Some generated subtitle files are referenced from JSON rather than playlists.
    for standalone_subtitle in local_dir.glob("*.vtt"):
        required.add(standalone_subtitle.relative_to(local_dir).as_posix())

    if not required:
        raise RuntimeError("本地切片目录缺少可校验的 HLS 文件")

    log(f"   ✅ 本地播放清单校验通过: {len(required)} 个关键文件")


def _is_empty_real_dir(path: str) -> bool:
    """Return True only for a real directory with no entries at all."""
    if not path or os.path.islink(path) or not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def _cleanup_empty_parent_dirs(
    start_dir: str,
    roots: list[str],
    log,
    max_depth: int = 2,
) -> None:
    """Delete empty parent directories without crossing configured roots."""
    if not start_dir or not os.path.isdir(start_dir):
        return

    try:
        max_depth = max(1, int(max_depth or 1))
    except (TypeError, ValueError):
        max_depth = 1
    normalized_roots = {
        os.path.abspath(os.path.expanduser(root))
        for root in (roots or [])
        if root
    }
    current = os.path.abspath(start_dir)
    removed = 0

    while current and os.path.isdir(current) and removed < max_depth:
        if current in normalized_roots:
            break
        if normalized_roots and not any(
            current == root or current.startswith(root + os.sep)
            for root in normalized_roots
        ):
            break
        try:
            if not _is_empty_real_dir(current):
                break
            os.rmdir(current)
            removed += 1
            log(f"🧹 已清理空目录: {current}")
        except Exception as e:
            log(f"⚠️ 清理空目录失败: {current} - {e}")
            break
        if not normalized_roots:
            break
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent

    if removed and not normalized_roots:
        log("⚠️ 未配置清理边界，仅清理了可确认的空父目录")


# ─── CMAF Manifest 生成 ───

def _generate_manifests(output_dir: Path, cmaf_result: dict, log, subtitles_info: list[dict] | None = None):
    """生成 master.m3u8 和 stream.mpd，并用 mediastreamvalidator 校验。"""
    from .manifest import generate_hls_master, generate_dash_mpd, validate_hls_media_playlists

    generate_hls_master(cmaf_result, output_dir, print_fn=log, subtitles_info=subtitles_info)
    try:
        playlist_info = validate_hls_media_playlists(output_dir, print_fn=log)
        generate_dash_mpd(cmaf_result, playlist_info, output_dir, print_fn=log)
    except Exception as e:
        log(f"   ⚠️ DASH MPD 生成跳过: {e}")
    _validate_with_apple_tool(output_dir / "master.m3u8", log)


def _validate_with_apple_tool(master_playlist: Path, log) -> None:
    import sys
    if sys.platform != "darwin":
        return
    validator = resolve_tool(settings.MEDIASTREAMVALIDATOR_BIN)
    if not validator:
        log("   ℹ️ mediastreamvalidator 未安装，已用内置 HLS 校验")
        return
    result = subprocess.run(
        [validator, str(master_playlist)],
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=str(master_playlist.parent),
    )
    output = "\n".join(part for part in [result.stdout, result.stderr] if part)
    if result.returncode != 0:
        raise RuntimeError(f"Apple HLS 校验失败:\n{output[-1000:]}")
    log("   ✅ Apple mediastreamvalidator 校验通过")


# ─── 单任务执行 ───

def run_job(params: dict, log_fn=None, cancel_check=None) -> dict:
    """
    执行单个上传任务的完整流程。

    Args:
        params: 任务参数 dict，至少包含 filepath, tmdb_id；可选 media_type/season/episode/
                cmaf/clean_after/force_overwrite/skip_slice/skip_upload/skip_register/skip_metadata_check/
                no_subtitles/retry_attempts（每阶段重试次数，默认 3）/
                no_opensubtitles（禁用 OpenSubtitles v3 中文字幕兜底）/
                direct_mp4（准备浏览器兼容 MP4 后直传，sourceType=mp4）
        log_fn: 日志回调 log_fn(str)，默认 print
        cancel_check: 取消检查回调 () -> bool，默认永不取消

    Returns:
        {"status": "success"|"error"|"cancelled", "error": str|None, "r2_path": str|None,
         "stage": 失败所处阶段}
    """
    log = log_fn or print
    if cancel_check is None:
        cancel_check = lambda: False

    attempts = int(params.get("retry_attempts") or 3)
    local_output = None
    direct_path = None
    media_meta: dict = {}
    cleanup_on_fail = bool(params.get("clean_after"))

    def _cleanup_direct_output():
        """Best-effort cleanup for prepared direct media; never remove the source."""
        nonlocal direct_path
        if not direct_path:
            return
        prepared_output = Path(direct_path)
        try:
            source_value = params.get("filepath")
            if source_value and prepared_output.resolve() == Path(source_value).resolve():
                return
            existed = prepared_output.exists()
            prepared_output.unlink(missing_ok=True)
            if existed:
                log(f"🧹 已清理直传临时文件: {prepared_output}")
        except Exception as e:
            log(f"⚠️ 清理直传临时文件失败 ({prepared_output}): {e}")
        finally:
            direct_path = None

    def _fail(stage: str, error: str, r2_path=None, *, notify: bool = True):
        # 失败时清理本地切片产物，避免占盘（源文件始终保留）
        if notify:
            notify_upload_failed(
                filename=Path(str(params.get("filepath") or "?")).name,
                error=error,
                stage=stage,
            )
        if cleanup_on_fail and local_output and local_output.exists():
            shutil.rmtree(local_output, ignore_errors=True)
            log(f"🧹 失败清理本地切片: {local_output}")
        return {"status": "error", "error": error, "r2_path": r2_path, "stage": stage}

    try:
        missing_config = settings.validate()
        if missing_config:
            msg = "配置缺失: " + "、".join(missing_config)
            log(f"❌ {msg}，已停止任务，避免切片后上传失败")
            return _fail("precheck", msg, notify=True)

        direct_mp4 = direct_mode_enabled(params)
        if direct_mp4:
            params["skip_slice"] = True
        ffmpeg = resolve_tool(settings.FFMPEG_BIN)
        ffprobe = resolve_tool(settings.FFPROBE_BIN)
        if not ffmpeg or not ffprobe:
            msg = (
                "直传环境未就绪: 重封装需要 ffmpeg/ffprobe"
                if direct_mp4
                else "媒体处理环境未就绪: 未找到 ffmpeg/ffprobe"
            )
            log(f"❌ {msg}")
            return _fail("precheck", msg)

        filepath = params["filepath"]
        if not os.path.isfile(filepath):
            log(f"❌ 文件不存在: {filepath}")
            return _fail("precheck", "文件不存在")

        info = parse_filename(filepath)
        season = params.get("season") if params.get("season") is not None else info.get("season")
        episode = params.get("episode") if params.get("episode") is not None else info.get("episode")
        media_type = params.get("media_type") or "tv"
        tmdb_id = params.get("tmdb_id")
        resolution = params.get("resolution") or info.get("resolution") or ""
        has_episode = season is not None and episode is not None
        episode_label = f"S{season}E{episode}" if has_episode else "电影"

        log(f"📄 文件: {Path(filepath).name}")
        log(f"   TMDB ID: {tmdb_id} | {media_type} | {episode_label} | {resolution or '?'}")

        # 优先用 ffprobe 真实宽度定档（超宽按宽边）
        try:
            from .resolution_key import quality_key_from_width
            from .slicer import probe_video_info
            probed = probe_video_info(filepath)
            probed_w = int(probed.get("width") or 0)
            if probed_w > 0:
                width_key = quality_key_from_width(probed_w)
                if width_key != "未知":
                    log(f"   探测宽度: {probed_w} → {width_key}"
                        + (f"（覆盖文件名 {resolution}）" if resolution and resolution != width_key else ""))
                    resolution = width_key
        except Exception as probe_err:
            log(f"   ⚠️ 宽度探测失败，回退文件名/参数: {probe_err}")

        if params.get("force_overwrite"):
            log("⚠️ 强制覆盖模式: 将删除当前分辨率目录旧数据并重新上传")

        if not params.get("skip_metadata_check"):
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": None}
            log("🔍 查询 TMDB 元数据...")
            ok_meta, resolved_type, meta_err, meta_warn = verify_tmdb_metadata(
                int(tmdb_id), media_type, season, episode,
            )
            if not ok_meta:
                log(f"❌ {meta_err}")
                return _fail("metadata", meta_err)
            if resolved_type != media_type:
                log(f"   ℹ️ 媒体类型修正: {media_type} → {resolved_type}")
                media_type = resolved_type
            if meta_warn:
                log(f"⚠️ {meta_warn}")
            else:
                log(f"   ✅ TMDB 元数据可用 ({media_type}/{tmdb_id})")

        if has_episode:
            r2_path = f"tmdb/{media_type}/{tmdb_id}/season/{int(season)}/episode/{int(episode)}"
        else:
            r2_path = f"tmdb/{media_type}/{tmdb_id}"
        from .resolution_key import append_resolution_to_r2_path

        r2_path, quality_key = append_resolution_to_r2_path(r2_path, resolution)
        resolution = quality_key
        local_output = settings.HLS_OUTPUT_DIR / r2_path
        log(f"📂 R2 路径: {r2_path}/")

        remote_uploaded = bool(params.get("remote_uploaded"))
        has_remote_marker = bool(params.get("remote_uploaded_marker"))
        remote_marker = _read_upload_marker(r2_path, "uploaded", log) if has_remote_marker else {}
        if not remote_uploaded and not params.get("skip_slice") and local_output.exists():
            shutil.rmtree(local_output, ignore_errors=True)
            log(f"🧹 已清理旧本地输出: {local_output}")

        # ─── 字幕（切片前提取，避免源文件被移动）───
        subtitles = []
        subtitles_info = None  # HLS WebVTT 字幕轨信息
        video_duration = None  # 视频时长（秒），切片后填充，用于更新分集时长
        upload_verified = False
        original_language = params.get("original_language")
        if not original_language and tmdb_id and not remote_uploaded:
            original_language, tmdb_lang_error = get_original_language(int(tmdb_id), media_type)
            if original_language:
                log(f"   TMDB 原声语言: {original_language}")
            elif tmdb_lang_error:
                log(f"   ⚠️ 未能读取 TMDB 原声语言 ({tmdb_lang_error})")

        imdb_id = params.get("imdb_id")
        if not imdb_id and tmdb_id and not remote_uploaded and not params.get("no_subtitles"):
            imdb_id, imdb_error = get_imdb_id(int(tmdb_id), media_type)
            if imdb_id:
                log(f"   IMDb: {imdb_id}")
            elif imdb_error:
                log(f"   ⚠️ 未能读取 IMDb id ({imdb_error})")

        if remote_uploaded:
            marker_subtitles = remote_marker.get("subtitles")
            if isinstance(marker_subtitles, list):
                subtitles = [sub for sub in marker_subtitles if isinstance(sub, dict)]
            if remote_marker.get("durationSecs"):
                video_duration = int(remote_marker["durationSecs"])
            if not resolution and remote_marker.get("quality"):
                resolution = str(remote_marker["quality"])
            for key in ("videoCodec", "width", "height", "bitrate", "frameRate"):
                if remote_marker.get(key) is not None:
                    media_meta[key] = remote_marker[key]
            log("📦 R2 已有完整上传标记，本次跳过切片/上传，直接补入库")
        elif not params.get("no_subtitles"):
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            log("📝 提取字幕...")
            local_output.mkdir(parents=True, exist_ok=True)
            subtitles = resolve_subtitles_for_upload(
                filepath,
                local_output,
                print_fn=log,
                original_language=original_language,
                imdb_id=imdb_id,
                media_type=media_type,
                season=int(season) if season is not None else None,
                episode=int(episode) if episode is not None else None,
                opensubtitles=not bool(params.get("no_opensubtitles")),
            )
            if subtitles:
                for sub in subtitles:
                    source = {
                        "external": "外挂",
                        "opensubtitles": "OpenSubtitles",
                    }.get(str(sub.get("source") or ""), "内嵌")
                    log(f"   字幕 [{sub['lang']}] {sub['label']} ({source})")
            else:
                log("   无内嵌/外挂/OpenSubtitles 字幕")

        # ─── 切片 / 直传准备 ───
        if direct_mp4 and not remote_uploaded:
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            local_output.mkdir(parents=True, exist_ok=True)
            log("🎬 准备直传 MP4...")
            prepared = prepare_direct_mp4(
                filepath,
                local_output / "video.mp4",
                h264_compat=bool(params.get("h264_compat")),
                original_language=original_language,
                print_fn=log,
            )
            video_duration = prepared.get("duration") or None
            direct_path = prepared["path"]
            media_meta = {
                "videoCodec": prepared.get("videoCodec"),
                "width": prepared.get("width"),
                "height": prepared.get("height"),
                "bitrate": prepared.get("bitrate"),
                "frameRate": prepared.get("frameRate"),
            }
            if not prepared.get("videoCopied"):
                log("   ✅ H.264 兼容转码完成")
            elif not prepared.get("audioCopied"):
                log("   ✅ 音频转 AAC 完成")
            else:
                log("   ✅ 快速重封装完成")
        elif not params.get("skip_slice") and not remote_uploaded:
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}

            # 磁盘空间检查：对实际写入目录（local_output 已在字幕阶段创建；
            # 若跳过字幕则此处确保存在）做检查，避免对不存在路径调用 disk_usage 抛错。
            local_output.mkdir(parents=True, exist_ok=True)
            file_size = os.path.getsize(filepath)
            try:
                disk_free = shutil.disk_usage(str(local_output)).free
                if disk_free < file_size * 1.5:
                    log(f"❌ 磁盘空间不足: 剩余 {disk_free // (1024**3)} GB, "
                        f"需要约 {file_size * 1.5 // (1024**3)} GB")
                    return _fail("slice", "磁盘空间不足", r2_path)
            except OSError as e:
                # 检查本身失败不应阻断流程，仅记录
                log(f"   ⚠️ 磁盘空间检查跳过: {e}")

            log("🎬 CMAF fMP4 切片（音画分离，stream copy）...")
            start = time.time()

            def _do_apple_hls():
                return apple_hls_slice(
                    filepath, local_output, print_fn=log,
                    original_language=original_language,
                )

            # 切片失败重试（少量次数，切片多为确定性失败，重试主要应对偶发 I/O）
            ok_slice, cmaf_result = retry(
                _do_apple_hls, attempts=max(2, min(attempts, 2)), base_delay=2.0,
                log=log, what="Apple HLS 切片", cancel_check=cancel_check)

            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            if not cmaf_result:
                return _fail("slice", "Apple HLS 切片失败", r2_path)

            v_segs = len(cmaf_result["videoSegments"])
            video_duration = cmaf_result.get("duration") or None
            media_meta = {
                "videoCodec": cmaf_result.get("videoCodec"),
                "width": cmaf_result.get("width"),
                "height": cmaf_result.get("height"),
                "bitrate": cmaf_result.get("bandwidth") or cmaf_result.get("averageBandwidth"),
                "frameRate": cmaf_result.get("frameRate"),
            }
            sz = sum(f.stat().st_size for f in local_output.rglob("*") if f.is_file()) / (1024**3)
            log(f"   ✅ CMAF {v_segs} 视频片段, {sz:.2f} GB, {time.time()-start:.1f}s")
            log(f"   编码: {cmaf_result['videoCodec']} + {cmaf_result['audioCodec']}")
            if subtitles and not subtitles_info:
                subtitles_info = generate_hls_subtitle_playlists(subtitles, local_output, print_fn=log)
            if subtitles_info or not (local_output / "master.m3u8").exists():
                log("📋 生成 HLS master + DASH MPD...")
                _generate_manifests(local_output, cmaf_result, log, subtitles_info=subtitles_info)
            log("   ✅ master.m3u8 已就绪")
        # ─── 上传 ───
        if not params.get("skip_upload") and not remote_uploaded:
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            force_overwrite = params.get("force_overwrite", False)
            start = time.time()
            _put_upload_marker(r2_path, "uploading", {"file": Path(filepath).name}, log)

            upload_last_log_at = [0.0]

            def upload_progress(p, speed=0, eta=0):
                now = time.time()
                if p < 1 and now - upload_last_log_at[0] < 3.0:
                    return
                upload_last_log_at[0] = now
                speed_mb = speed / (1024 * 1024)
                eta_str = f" | ETA {int(eta)}s" if eta > 0 else ""
                log(f"   上传 {int(p*100)}%  ({now - start:.1f}s, {speed_mb:.1f} MB/s{eta_str})")

            if direct_mp4:
                log("📤 不分片直传 R2 (video.mp4)...")
                from .r2 import get_transfer_config
                _xfer = get_transfer_config()
                log(
                    f"   分片上传: 并发 {_xfer.max_concurrency}"
                    f"（表单上传并发数）/ 分片 {_xfer.multipart_chunksize // (1024 * 1024)}MB"
                )
                extra_files = []
                for sub in subtitles:
                    sub_file = sub.get("file")
                    if not sub_file:
                        continue
                    candidate = local_output / sub_file
                    if candidate.is_file():
                        extra_files.append(candidate)

                def _do_direct_upload():
                    up, deleted = upload_mp4_direct(
                        direct_path, r2_path, extra_files, upload_progress, cancel_check,
                        force_overwrite=force_overwrite,
                    )
                    return {"uploaded": up, "deleted": deleted}

                ok_up, up_res = retry(
                    _do_direct_upload, attempts=attempts, base_delay=5.0,
                    log=log, what="R2 直传", cancel_check=cancel_check)
            else:
                log("📤 覆盖当前分辨率目录 (清理同档旧文件)...")

                # 覆盖当前分辨率前缀：先清理该档旧对象，再完整上传，不影响其它分辨率目录。
                def _do_upload():
                    up, deleted = upload_directory_smart(
                        local_output, r2_path, upload_progress, cancel_check, force_overwrite=force_overwrite)
                    return {"uploaded": up, "deleted": deleted}

                ok_up, up_res = retry(
                    _do_upload, attempts=attempts, base_delay=5.0,
                    log=log, what="R2 上传", cancel_check=cancel_check)
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            if not ok_up:
                return _fail("upload", "R2 上传失败", r2_path)
            log(f"   ✅ 覆盖当前分辨率目录 {up_res['uploaded']} 个文件，清理旧文件 {up_res['deleted']} 个，{time.time()-start:.1f}s")
            log("   ✅ R2 Web 可播校验通过，继续入库")
            upload_verified = True
        elif remote_uploaded:
            try:
                _remote_source_type(r2_path)
            except Exception as e:
                return _fail("verify", str(e), r2_path)
            upload_verified = True
        elif local_output and local_output.exists():
            try:
                _verify_uploaded_hls(local_output, r2_path, log)
            except Exception as e:
                return _fail("verify", str(e), r2_path)
            upload_verified = True

        # ─── NFO ───
        log("📋 写 NFO...")
        if has_episode:
            write_episode_nfo(int(tmdb_id), int(season), int(episode), r2_path, resolution, filepath, print_fn=log)
        write_show_nfo(int(tmdb_id), media_type, print_fn=log)

        # ─── 播放源类型 + uploaded 标记（入库成功前保留，便于对账补登）───
        marker_source_type = str(remote_marker.get("sourceType") or "")
        allowed_types = {"cmaf", "hls", "mp4"}
        source_type = marker_source_type if marker_source_type in allowed_types else "hls"
        if direct_mp4 and not marker_source_type:
            source_type = "mp4"
        elif not marker_source_type:
            if remote_uploaded:
                source_type = _remote_source_type(r2_path)
            elif local_output and (local_output / "master.m3u8").exists():
                source_type = "cmaf"
            else:
                try:
                    s3 = get_s3_client()
                    s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/master.m3u8")
                    source_type = "cmaf"
                except Exception:
                    try:
                        s3 = get_s3_client()
                        s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/video.mp4")
                        source_type = "mp4"
                    except Exception:
                        source_type = "hls"

        duration_for_register = int(video_duration) if video_duration else None
        if not duration_for_register and filepath and os.path.isfile(filepath):
            duration_for_register = get_video_duration(filepath)

        # 入库前若已有探测宽度，再次对齐档位
        try:
            from .resolution_key import quality_key_from_width
            meta_w = int((media_meta or {}).get("width") or 0)
            if meta_w > 0:
                width_key = quality_key_from_width(meta_w)
                if width_key != "未知":
                    resolution = width_key
        except Exception:
            pass

        if upload_verified:
            _put_upload_marker(
                r2_path,
                "uploaded",
                _upload_marker_payload(
                    filepath, source_type, resolution or "原画",
                    subtitles, duration_for_register,
                    upload_mode=params.get("upload_mode") or ("direct" if source_type == "mp4" else "hls"),
                    h264_compat=bool(params.get("h264_compat")),
                    video_codec=media_meta.get("videoCodec"),
                    width=media_meta.get("width"),
                    height=media_meta.get("height"),
                    bitrate=media_meta.get("bitrate"),
                    frame_rate=media_meta.get("frameRate"),
                ),
                log,
            )
            _delete_upload_marker(r2_path, "uploading", log)

        # ─── 入库（关键步骤，失败需重试，否则前端看不到该集）───
        if not params.get("skip_register"):
            register_error = [""]

            def _do_register():
                ok, msg = auto_register(
                    tmdb_id=int(tmdb_id), media_type=media_type,
                    season=int(season) if season is not None else None,
                    episode=int(episode) if episode is not None else None,
                    r2_path=r2_path, quality=resolution or "原画",
                    subtitles=subtitles, duration_secs=duration_for_register,
                    source_type=source_type, print_fn=log,
                )
                if not ok:
                    register_error[0] = msg
                return ok

            ok_reg, _ = retry(_do_register, attempts=attempts, base_delay=4.0,
                              log=log, what="站点入库", cancel_check=cancel_check)
            if not ok_reg:
                # 切片和上传已成功，仅入库失败：保留 R2 数据，标记失败便于对账补入库
                err_detail = register_error[0] or "未知原因"
                log(f"❌ 入库失败（R2 数据已上传，将由对账兜底重试）— {err_detail}")
                notify_register_failed(
                    filename=Path(filepath).name,
                    error=err_detail,
                    r2_path=r2_path,
                )
                return {"status": "error", "error": f"站点入库失败: {err_detail}", "r2_path": r2_path, "stage": "register"}

            notify_register_success(
                filename=Path(filepath).name,
                tmdb_id=int(tmdb_id),
                media_type=media_type,
                season=int(season) if season is not None else None,
                episode=int(episode) if episode is not None else None,
                quality=resolution or "",
                upload_mode=params.get("upload_mode") or ("direct" if direct_mp4 else "hls"),
                duration_secs=duration_for_register,
            )

            if upload_verified:
                ready_payload = {
                    "file": Path(filepath).name,
                    "sourceType": source_type,
                    "uploadMode": params.get("upload_mode") or ("direct" if direct_mp4 else "hls"),
                    "h264Compat": bool(params.get("h264_compat")),
                }
                for key in ("videoCodec", "width", "height", "bitrate", "frameRate"):
                    if media_meta.get(key) is not None:
                        ready_payload[key] = media_meta[key]
                _put_upload_marker(r2_path, "ready", ready_payload, log)
                _delete_upload_marker(r2_path, "uploaded", log)
                _delete_upload_marker(r2_path, "uploading", log)
            else:
                log("   ⚠️ 未执行上传校验，跳过 ready.json 标记")
        elif upload_verified:
            log("   ⏭ skip_register：保留 uploaded 标记，不写 ready.json")
        else:
            log("   ⚠️ 未执行上传校验，跳过 ready.json 标记")

        log("━━━ 完成 ━━━")
        notify_upload_success(
            filename=Path(filepath).name,
            tmdb_id=int(tmdb_id),
            season=int(season) if season is not None else None,
            episode=int(episode) if episode is not None else None,
            r2_path=r2_path,
            quality=resolution or "",
            upload_mode=params.get("upload_mode") or ("direct" if direct_mp4 else "hls"),
        )

        # 清理本地切片
        if params.get("clean_after") and local_output.exists():
            shutil.rmtree(local_output, ignore_errors=True)
            log(f"🧹 已清理本地切片: {local_output}")

        # 清理源文件
        if params.get("clean_after") and filepath and os.path.isfile(filepath):
            time.sleep(10)
            try:
                os.remove(filepath)
                log(f"🧹 已清理源文件: {filepath}")
                _cleanup_empty_parent_dirs(
                    os.path.dirname(filepath),
                    params.get("cleanup_roots") or [],
                    log,
                    params.get("cleanup_parent_depth") or 2,
                )
            except Exception as e:
                log(f"⚠️ 清理源文件失败: {e}")

        return {"status": "success", "error": None, "r2_path": r2_path, "stage": "done"}

    except Exception as e:
        log(f"❌ 异常: {e}")
        log(traceback.format_exc())
        notify_upload_failed(
            filename=Path(params.get("filepath", "?")).name,
            error=str(e),
            stage="exception",
        )
        if cleanup_on_fail and local_output and local_output.exists():
            shutil.rmtree(local_output, ignore_errors=True)
            log(f"🧹 异常清理本地切片: {local_output}")
        return {"status": "error", "error": str(e), "r2_path": None, "stage": "exception"}
    finally:
        _cleanup_direct_output()
