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
from threading import Thread
from urllib.parse import unquote, urlparse

from .runtime_config import settings
from .parser import parse_filename
from .slicer import cmaf_demux_slice, get_video_duration
from .subtitles import extract_subtitles, generate_hls_subtitle_playlists
from .register import auto_register, write_episode_nfo, write_show_nfo
from .notify import notify_upload_success, notify_upload_failed
from .r2 import get_s3_client, _MIME_MAP


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


# ─── R2 上传 (跳过已存在的同尺寸文件) ───

def upload_directory_smart(local_dir: Path, r2_prefix: str, on_progress, cancel_check,
                           force_overwrite: bool = False) -> tuple[int, int]:
    """智能上传: 跳过已存在且大小相同的文件。返回 (uploaded, skipped)。"""
    s3 = get_s3_client()
    files = [f for f in local_dir.rglob("*") if f.is_file()]
    if not files:
        return 0, 0

    existing: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=settings.R2_BUCKET, Prefix=r2_prefix + "/"):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(r2_prefix) + 1:]
                existing[rel] = obj["Size"]
    except Exception:
        pass

    if force_overwrite and existing:
        keys_to_delete = [f"{r2_prefix}/{rel}" for rel in existing.keys() if rel != "uploading.json"]
        for i in range(0, len(keys_to_delete), 1000):
            batch = keys_to_delete[i:i + 1000]
            try:
                s3.delete_objects(
                    Bucket=settings.R2_BUCKET,
                    Delete={"Objects": [{"Key": k} for k in batch]},
                )
            except Exception:
                pass
        existing.clear()

    uploaded = 0
    skipped = 0
    failed = 0
    total = len(files)
    total_bytes = sum(f.stat().st_size for f in files)
    uploaded_bytes = 0
    completed_files = 0
    start_time = time.time()

    def upload_one(f: Path) -> tuple[str, int]:
        rel_key = str(f.relative_to(local_dir))
        local_size = f.stat().st_size
        if not force_overwrite and existing.get(rel_key) == local_size:
            return "skip", local_size
        key = f"{r2_prefix}/{rel_key}"
        ct = _MIME_MAP.get(f.suffix.lower(), "application/octet-stream")
        s3.upload_file(str(f), settings.R2_BUCKET, key, ExtraArgs={"ContentType": ct})
        return "ok", local_size

    def upload_phase(phase_files: list[Path]) -> None:
        nonlocal uploaded, skipped, failed, uploaded_bytes, completed_files
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
                    if r == "skip":
                        skipped += 1
                    else:
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

    # 有文件上传失败：抛异常让上层 retry 重投（已成功的同尺寸文件下次会被跳过，只补失败部分）
    if failed:
        raise RuntimeError(f"{failed} 个文件上传失败（共 {total} 个），需重试补传")

    return uploaded, skipped


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
    references_fmp4 = any(line.endswith(".m4s") for line in media_lines)
    if references_fmp4 and "#EXT-X-MAP:" not in text:
        raise RuntimeError(f"fMP4 播放清单缺少 EXT-X-MAP: {playlist.name}")


def _verify_uploaded_hls(local_dir: Path, r2_path: str, log=print) -> None:
    """Verify key playback artifacts exist in R2 after upload."""
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

    s3 = get_s3_client()
    missing: list[str] = []
    for rel in sorted(required):
        key = f"{r2_path}/{rel}"
        try:
            s3.head_object(Bucket=settings.R2_BUCKET, Key=key)
        except Exception:
            missing.append(rel)

    if missing:
        preview = ", ".join(missing[:8])
        more = f" 等 {len(missing)} 个" if len(missing) > 8 else ""
        raise RuntimeError(f"R2 上传完整性校验失败: {preview}{more}")
    log(f"   ✅ R2 完整性校验通过: {len(required)} 个关键文件")


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
    """生成 master.m3u8 和 stream.mpd（多音轨支持）"""
    from .manifest import generate_hls_master, generate_dash_mpd, validate_hls_media_playlists

    generate_hls_master(cmaf_result, output_dir, print_fn=lambda x: None, subtitles_info=subtitles_info)
    try:
        playlist_info = validate_hls_media_playlists(output_dir, print_fn=lambda x: None)
        generate_dash_mpd(cmaf_result, playlist_info, output_dir, print_fn=lambda x: None)
    except Exception as e:
        log(f"[manifest] MPD 生成失败 ({output_dir.name}): {e}")


# ─── 单任务执行 ───

def run_job(params: dict, log_fn=None, cancel_check=None) -> dict:
    """
    执行单个上传任务的完整流程。

    Args:
        params: 任务参数 dict，至少包含 filepath, tmdb_id；可选 media_type/season/episode/
                cmaf/clean_after/force_overwrite/skip_slice/skip_upload/skip_register/no_subtitles/
                retry_attempts（每阶段重试次数，默认 3）
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
    cleanup_on_fail = bool(params.get("clean_after"))

    def _fail(stage: str, error: str, r2_path=None):
        # 失败时清理本地切片产物，避免占盘（源文件始终保留）
        if cleanup_on_fail and local_output and local_output.exists():
            shutil.rmtree(local_output, ignore_errors=True)
            log(f"🧹 失败清理本地切片: {local_output}")
        return {"status": "error", "error": error, "r2_path": r2_path, "stage": stage}

    try:
        filepath = params["filepath"]
        if not os.path.isfile(filepath):
            log(f"❌ 文件不存在: {filepath}")
            return {"status": "error", "error": "文件不存在", "r2_path": None, "stage": "precheck"}

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
        if params.get("force_overwrite"):
            log("⚠️ 强制覆盖模式: 将删除 R2 旧数据并重新上传")

        if has_episode:
            r2_path = f"tmdb/{media_type}/{tmdb_id}/season/{int(season)}/episode/{int(episode)}"
        else:
            r2_path = f"tmdb/{media_type}/{tmdb_id}"
        local_output = settings.HLS_OUTPUT_DIR / r2_path
        log(f"📂 R2 路径: {r2_path}/")

        # ─── 字幕（切片前提取，避免源文件被移动）───
        subtitles = []
        subtitles_info = None  # HLS WebVTT 字幕轨信息
        video_duration = None  # 视频时长（秒），切片后填充，用于更新分集时长
        upload_verified = False
        if not params.get("no_subtitles"):
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            log("📝 提取字幕...")
            local_output.mkdir(parents=True, exist_ok=True)
            subtitles = extract_subtitles(filepath, local_output, print_fn=log)
            if subtitles:
                for sub in subtitles:
                    log(f"   字幕 [{sub['lang']}] {sub['label']}")
                subtitles_info = generate_hls_subtitle_playlists(subtitles, local_output, print_fn=log)
            else:
                log("   无内嵌字幕")

        # ─── 切片 ───
        if not params.get("skip_slice"):
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

            use_cmaf = params.get("cmaf", True)

            if use_cmaf:
                log("🎬 CMAF 切片 (音视频分离)...")
                start = time.time()

                def _do_cmaf():
                    # FFmpeg CMAF demux 切片：音视频分离 fMP4，视频流 -c:v copy 无损直切，
                    # 兼容 HEVC/MKV 等容器，无需转码。
                    log("   使用 FFmpeg CMAF (音视频分离)")
                    return cmaf_demux_slice(filepath, local_output, print_fn=log)

                # 切片失败重试（少量次数，切片多为确定性失败，重试主要应对偶发 I/O）
                ok_slice, cmaf_result = retry(
                    _do_cmaf, attempts=max(2, min(attempts, 2)), base_delay=2.0,
                    log=log, what="CMAF 切片", cancel_check=cancel_check)

                if cancel_check():
                    return {"status": "cancelled", "error": None, "r2_path": r2_path}
                if not cmaf_result:
                    log("⚠️ CMAF 切片失败，回退到 HLS 模式...")
                    use_cmaf = False
                else:
                    v_segs = len(cmaf_result["videoSegments"])
                    a_segs = len(cmaf_result["audioSegments"])
                    video_duration = cmaf_result.get("duration") or None
                    sz = sum(f.stat().st_size for f in local_output.rglob("*") if f.is_file()) / (1024**3)
                    log(f"   ✅ 视频 {v_segs} 片段 + 音频 {a_segs} 片段, {sz:.2f} GB, {time.time()-start:.1f}s")
                    log(f"   编码: {cmaf_result['videoCodec']} + {cmaf_result['audioCodec']}")
                    if subtitles_info or not (local_output / "master.m3u8").exists():
                        log("📋 生成 HLS master + DASH MPD...")
                        _generate_manifests(local_output, cmaf_result, log, subtitles_info=subtitles_info)
                    log("   ✅ master.m3u8 已就绪")

            if not use_cmaf:
                log("🎬 HLS 切片 (回退模式)...")
                start = time.time()
                ok = _hls_slice(filepath, local_output, cancel_check, log)
                if cancel_check():
                    return {"status": "cancelled", "error": None, "r2_path": r2_path}
                if not ok:
                    log("❌ 切片失败")
                    return _fail("slice", "切片失败", r2_path)
                segs = list(local_output.glob("seg-*.m4s"))
                sz = sum(f.stat().st_size for f in local_output.rglob("*") if f.is_file()) / (1024**3)
                log(f"   ✅ {len(segs)} 片段, {sz:.2f} GB, {time.time()-start:.1f}s")

        # ─── 上传 ───
        if not params.get("skip_upload"):
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            force_overwrite = params.get("force_overwrite", False)
            log("📤 强制重新上传 R2 (覆盖旧数据)..." if force_overwrite else "📤 上传 R2 (检查重复)...")
            start = time.time()
            _put_upload_marker(r2_path, "uploading", {"file": Path(filepath).name}, log)

            upload_last_log = [0]

            def upload_progress(p, speed=0, eta=0):
                tier = int(p * 10)
                if tier > upload_last_log[0]:
                    upload_last_log[0] = tier
                    speed_mb = speed / (1024 * 1024)
                    eta_str = f" | ETA {int(eta)}s" if eta > 0 else ""
                    log(f"   上传 {int(p*100)}%  ({time.time()-start:.1f}s, {speed_mb:.1f} MB/s{eta_str})")

            # 上传可重试：upload_directory_smart 会跳过已传的同尺寸文件，重试只补缺失部分
            def _do_upload():
                up, sk = upload_directory_smart(
                    local_output, r2_path, upload_progress, cancel_check, force_overwrite=force_overwrite)
                return {"uploaded": up, "skipped": sk}

            ok_up, up_res = retry(
                _do_upload, attempts=attempts, base_delay=5.0,
                log=log, what="R2 上传", cancel_check=cancel_check)
            if cancel_check():
                return {"status": "cancelled", "error": None, "r2_path": r2_path}
            if not ok_up:
                return _fail("upload", "R2 上传失败", r2_path)
            log(f"   ✅ 新上传 {up_res['uploaded']}, 跳过 {up_res['skipped']} (已存在), {time.time()-start:.1f}s")

            try:
                _verify_uploaded_hls(local_output, r2_path, log)
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

        # ─── 入库（关键步骤，失败需重试，否则前端看不到该集）───
        if not params.get("skip_register"):
            source_type = "hls"
            if params.get("cmaf", True):
                if (local_output / "master.m3u8").exists():
                    source_type = "cmaf"
                else:
                    try:
                        s3 = get_s3_client()
                        s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/master.m3u8")
                        source_type = "cmaf"
                    except Exception:
                        source_type = "hls"

            # 分集时长：优先用切片探测到的时长；若缺失且源文件还在，则补探测一次
            duration_for_register = int(video_duration) if video_duration else None
            if not duration_for_register and filepath and os.path.isfile(filepath):
                duration_for_register = get_video_duration(filepath)

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
                return {"status": "error", "error": f"站点入库失败: {err_detail}", "r2_path": r2_path, "stage": "register"}

        if upload_verified:
            _put_upload_marker(r2_path, "ready", {"file": Path(filepath).name}, log)
            _delete_upload_marker(r2_path, "uploading", log)
        else:
            log("   ⚠️ 未执行上传校验，跳过 ready.json 标记")

        log("━━━ 完成 ━━━")
        notify_upload_success(
            filename=Path(filepath).name, tmdb_id=int(tmdb_id),
            season=int(season) if season is not None else None,
            episode=int(episode) if episode is not None else None, r2_path=r2_path,
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
        notify_upload_failed(filename=Path(params.get("filepath", "?")).name, error=str(e))
        if cleanup_on_fail and local_output and local_output.exists():
            shutil.rmtree(local_output, ignore_errors=True)
            log(f"🧹 异常清理本地切片: {local_output}")
        return {"status": "error", "error": str(e), "r2_path": None, "stage": "exception"}


# ─── 传统 HLS 切片 (回退模式) ───

def _hls_slice(input_path: str, output_dir: Path, cancel_check, log) -> bool:
    """传统单文件 HLS fMP4 切片（音视频合一），CMAF 失败时回退用。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    m3u8_path = output_dir / "stream.m3u8"
    seg_pattern = str(output_dir / "seg-%05d.m4s")
    cmd = [
        settings.FFMPEG_BIN, "-i", input_path,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-hls_time", str(settings.HLS_SEGMENT_SECONDS), "-hls_list_size", "0",
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", seg_pattern,
        str(m3u8_path), "-y",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 3600  # 最长 1 小时，防止 ffmpeg 卡死导致 worker 永久阻塞
    while proc.poll() is None:
        if cancel_check():
            proc.kill()
            return False
        if time.time() > deadline:
            proc.kill()
            log("❌ HLS 切片超时（>1h），已终止")
            return False
        time.sleep(1)
    return proc.returncode == 0
