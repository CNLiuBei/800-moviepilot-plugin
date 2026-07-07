"""
MoviePilot V2 插件: 云端自动上传（全合并版）

下载整理完成后，在插件进程内直接完成：
Apple HLS 切片（官方工具生成并校验）→ R2 上传 → TMDB 元数据 → 站点入库。

无需再单独运行上传工具服务，插件丢进 MoviePilot 即可使用。
依赖外部二进制：ffmpeg / ffprobe（必需，全平台可 auto-install）；mediastreamvalidator 仅 macOS 可选。
"""
import os
import queue
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.core.config import settings as mp_settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .uploader.runtime_config import settings
from .uploader.runtime_config import ConfigError, normalize_base_url
from .uploader.job_runner import run_job
from .uploader import notify as _notify_mod
from .uploader import env as _env
from .uploader import cf_auto as _cf_auto


def _has_episode_numbers(season, episode) -> bool:
    return season is not None and episode is not None


def _episode_label(season, episode) -> str:
    return f"S{season}E{episode}" if _has_episode_numbers(season, episode) else "电影"


class CloudUploader(_PluginBase):
    # ─── 插件元信息 ───
    plugin_name = "云端自动上传"
    plugin_desc = "整理完成后自动 FFmpeg HLS 切片→上传R2→入库到流媒体站，全流程在插件内完成。"
    plugin_icon = "upload.png"
    plugin_version = "2.8.3"
    plugin_author = "cn"
    author_url = "https://github.com/CNLiuBei/800-moviepilot-plugin"
    plugin_config_prefix = "clouduploader_"
    plugin_order = 80
    auth_level = 1

    # ─── 运行时状态 ───
    _enabled = False
    _notify = True
    _delay = 30
    _clean_after = True
    _auto_install = True
    _watch_enabled = False
    _watch_dirs: List[str] = []
    _watch_delay = 20
    _scan_on_start = True
    _config_ready = False
    _config: dict = {}

    # 二进制环境检测结果（由后台安装线程填充）
    _env_status: dict = {}
    # CF Token 自动推导出的 R2 配置（供详情页展示）
    _cf_derived: dict = {}

    # 任务队列（单后台线程顺序执行，避免并发切片占满资源）
    _task_queue: Optional["queue.Queue"] = None
    _worker: Optional[threading.Thread] = None
    _worker_stop = False
    _stats = {"queued": 0, "running": 0, "success": 0, "error": 0}
    # 实时任务进度（内存，供 get_page 展示）
    _task_progress: Dict[str, dict] = {}
    _PROGRESS_MAX_HISTORY = 20  # 详情页最多展示最近 N 条已完成任务

    # 任务去重：队列中或执行中的任务 key 集合（配合锁防并发重复入队）
    _inflight: set = set()
    _inflight_lock = threading.Lock()
    # 事件防抖：dedup_key -> 上次受理时间戳，避免 MoviePilot 对同一文件重复派发事件
    _recent_events: Dict[str, float] = {}
    _EVENT_DEBOUNCE_SECONDS = 120  # 同一集在该时间窗内的重复事件直接忽略
    _UPLOAD_MARKER_STALE_SECONDS = 6 * 3600  # R2 uploading.json 超过该时间视为半成品
    # 目录监控：path -> debounce timer
    _watch_observer = None
    _watch_timers: Dict[str, threading.Timer] = {}
    _watch_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """读取配置并初始化任务队列。"""
        config = config or {}
        self._config = config
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._delay = int(config.get("delay") or 30)
        self._clean_after = bool(config.get("clean_after", True))
        self._auto_install = bool(config.get("auto_install", True))
        self._watch_enabled = bool(config.get("watch_enabled", False))
        self._watch_delay = int(config.get("watch_delay") or 20)
        self._scan_on_start = bool(config.get("scan_on_start", True))
        self._watch_dirs = self._parse_watch_dirs(config.get("watch_dirs", ""))
        self._stop_directory_watch()

        # ── TMDB Token：用户不填则回退到 MoviePilot 自带的 TMDB API Key ──
        tmdb_token = (config.get("tmdb_token") or "").strip()
        if not tmdb_token:
            tmdb_token = getattr(mp_settings, "TMDB_API_KEY", "") or ""

        # ── R2 配置：手填完整时优先用手填；否则尝试 CF API Token 自动推导 ──
        r2_account_id = (config.get("r2_account_id") or "").strip()
        r2_access_key_id = (config.get("r2_access_key_id") or "").strip()
        r2_secret_access_key = (config.get("r2_secret_access_key") or "").strip()
        r2_bucket = config.get("r2_bucket") or "flix-800-assets"
        manual_r2_ready = bool(r2_account_id and r2_access_key_id and r2_secret_access_key)

        cf_token = (config.get("cf_api_token") or "").strip()
        if cf_token and not manual_r2_ready:
            try:
                derived = _cf_auto.auto_configure(
                    cf_token,
                    prefer_bucket=r2_bucket,
                    create_if_missing=bool(config.get("cf_create_bucket", False)),
                    log=lambda m: logger.info(f"[CloudUploader] CF自动配置 {m}"),
                )
                if derived:
                    r2_account_id = derived["account_id"]
                    r2_access_key_id = derived["access_key_id"]
                    r2_secret_access_key = derived["secret_access_key"]
                    if derived.get("bucket"):
                        r2_bucket = derived["bucket"]
                    self._cf_derived = derived
                else:
                    logger.warning(
                        "[CloudUploader] CF Token 自动配置失败，回退手填 R2 配置。"
                        "若日志含 nodename/ConnectError，说明无法访问 api.cloudflare.com，"
                        "请在下方手动填写 R2 账户 ID、Access Key、Secret Key 后保存。"
                    )
            except Exception as e:
                logger.error(f"[CloudUploader] CF 自动配置异常: {e}")
        elif cf_token and manual_r2_ready:
            logger.info("[CloudUploader] 已检测到完整的手动 R2 配置，跳过 CF Token 自动推导")

        # 注入业务配置到 runtime settings
        hls_dir = self.get_data_path() / "hls-output"
        try:
            api_base = normalize_base_url(config.get("api_base", ""), "流媒体站地址")
        except ConfigError as e:
            logger.error(f"[CloudUploader] {e}")
            api_base = ""
        settings.configure(
            r2_account_id=r2_account_id,
            r2_access_key_id=r2_access_key_id,
            r2_secret_access_key=r2_secret_access_key,
            r2_bucket=r2_bucket,
            api_base=api_base,
            api_admin_key=config.get("api_admin_key", ""),
            api_username=config.get("api_username", ""),
            api_password=config.get("api_password", ""),
            tmdb_token=tmdb_token,
            tmdb_proxy_base=(config.get("tmdb_proxy_base") or "").strip(),
            tmdb_image_proxy_base=(config.get("tmdb_image_proxy_base") or "").strip(),
            hls_output_dir=hls_dir,
            hls_segment_seconds=config.get("segment_seconds") or 6,
            upload_concurrency=config.get("concurrency") or 8,
            ffmpeg_bin=config.get("ffmpeg_bin") or "ffmpeg",
            ffprobe_bin=config.get("ffprobe_bin") or "ffprobe",
            mediastreamvalidator_bin=config.get("mediastreamvalidator_bin") or "mediastreamvalidator",
            tg_bot_token=config.get("tg_bot_token", ""),
            tg_chat_id=config.get("tg_chat_id", ""),
        )
        missing = settings.validate()
        self._config_ready = not missing
        if self._enabled and missing:
            logger.error(
                "[CloudUploader] 配置缺失: " + "、".join(missing) +
                "；已暂停上传、扫描和对账，请补齐配置后重新保存插件。"
            )

        # 注入 MoviePilot 通知回调
        if self._notify:
            _notify_mod.set_mp_notifier(self._mp_notify)
        else:
            _notify_mod.set_mp_notifier(None)

        # 启动任务队列工作线程
        if self._enabled and self._config_ready:
            self._start_worker()
            # 后台准备外部二进制（探测 ffmpeg/ffprobe），不阻塞插件加载
            threading.Thread(target=self._prepare_env, daemon=True).start()
            # 重启恢复：把上次未完成/失败的任务重新入队
            threading.Thread(target=self._recover_tasks, daemon=True).start()
            self._start_directory_watch()
            if self._scan_on_start:
                threading.Thread(target=self._scan_library_on_start, daemon=True).start()

    # ─── 任务持久化（用插件 save_data，重启不丢、失败可对账重投）───

    _TASKS_KEY = "tasks"
    _MAX_TASK_RETRY = 5  # 跨任务级别的最大重投次数（对账兜底用）

    def _load_tasks(self) -> dict:
        return self.get_data(self._TASKS_KEY) or {}

    def _save_tasks(self, tasks: dict):
        # 控制体量：成功的任务只保留最近 200 条
        done = [(k, v) for k, v in tasks.items() if v.get("status") == "success"]
        if len(done) > 200:
            done.sort(key=lambda kv: kv[1].get("updated", 0), reverse=True)
            for k, _ in done[200:]:
                tasks.pop(k, None)
        self.save_data(self._TASKS_KEY, tasks)

    def _task_summary(self, tasks: Optional[dict] = None) -> dict:
        """统计持久化任务状态，供详情页和日志展示。"""
        summary = {"pending": 0, "running": 0, "success": 0, "error": 0, "missing_file": 0}
        for task in (tasks or self._load_tasks()).values():
            status = task.get("status")
            if status in summary:
                summary[status] += 1
            if status == "error" and task.get("error") == "文件不存在":
                summary["missing_file"] += 1
        return summary

    @staticmethod
    def _task_file_exists(task: dict) -> bool:
        fp = task.get("params", {}).get("filepath")
        return bool(fp and os.path.isfile(fp))

    @staticmethod
    def _task_key(params: dict) -> str:
        """任务唯一键：tmdb + 季集（或文件路径）。"""
        tid = params.get("tmdb_id")
        s = params.get("season")
        e = params.get("episode")
        if tid and _has_episode_numbers(s, e):
            return f"{tid}_S{int(s):02d}E{int(e):02d}"
        if tid:
            return f"{tid}_movie"
        return str(params.get("filepath", "?"))

    def _record_task(self, key: str, params: dict, status: str, error: str = "", stage: str = ""):
        tasks = self._load_tasks()
        prev = tasks.get(key, {})
        tasks[key] = {
            "params": params,
            "status": status,           # pending / running / success / error
            "error": error,
            "stage": stage,
            "retries": prev.get("retries", 0) + (1 if status == "error" else 0),
            "updated": int(time.time()),
        }
        self._save_tasks(tasks)

    def _recover_tasks(self):
        """重启后把未完成（pending/running/error 且未超重投上限）的任务重新入队。"""
        time.sleep(20)  # 等环境准备
        if not self._enabled or not self._config_ready:
            logger.info("[CloudUploader] 配置未完整，跳过重启恢复任务")
            return
        tasks = self._load_tasks()
        recovered = 0
        changed = False
        for key, t in tasks.items():
            if t.get("status") in ("pending", "running"):
                if self._task_file_exists(t):
                    if self._enqueue(t["params"], record=False):
                        recovered += 1
                else:
                    t["status"] = "error"
                    t["error"] = "文件不存在"
                    t["stage"] = "precheck"
                    t["retries"] = self._MAX_TASK_RETRY
                    t["updated"] = int(time.time())
                    changed = True
            elif (t.get("status") == "error" and t.get("error") != "文件不存在"
                  and t.get("retries", 0) < self._MAX_TASK_RETRY and self._task_file_exists(t)):
                if self._enqueue(t["params"], record=False):
                    recovered += 1
        if changed:
            self._save_tasks(tasks)
        if recovered:
            logger.info(f"[CloudUploader] 重启恢复：重新入队 {recovered} 个未完成任务")

    def reconcile(self):
        """定时对账兜底：重投持久化中仍未成功、且未超重投上限的任务（由 get_service 调度）。"""
        if not self._enabled or not self._config_ready:
            return
        tasks = self._load_tasks()
        requeued = 0
        for key, t in tasks.items():
            if (t.get("status") == "error" and t.get("error") != "文件不存在"
                    and t.get("retries", 0) < self._MAX_TASK_RETRY):
                # 文件还在才重投
                fp = t.get("params", {}).get("filepath")
                if fp and os.path.isfile(fp):
                    if self._enqueue(t["params"], record=False):
                        requeued += 1
        if requeued:
            logger.info(f"[CloudUploader] 对账兜底：重投 {requeued} 个失败任务")

    def _scan_library_on_start(self):
        """插件启动后延迟扫描一次，补齐实时监控启动前已存在的媒体文件。"""
        time.sleep(max(30, self._delay + 10))
        if self._enabled and self._config_ready:
            logger.info("[CloudUploader] 启动扫描：检查媒体库漏传文件")
            self.scan_library()

    def _prepare_env(self):
        """后台探测 ffmpeg/ffprobe（缺失时回退 pip 包）。"""
        try:
            self._env_status = _env.resolve_environment(
                log=lambda m: logger.info(f"[CloudUploader] {m}"),
                auto_install=self._auto_install,
            )
            logger.info(f"[CloudUploader] 环境就绪: ffmpeg={self._env_status.get('ffmpeg')} "
                        f"ffprobe={self._env_status.get('ffprobe')}")
        except Exception as e:
            logger.error(f"[CloudUploader] 环境准备失败: {e}")

    # ─── 任务队列 ───

    def _start_worker(self):
        if self._worker and self._worker.is_alive():
            return
        self._task_queue = queue.Queue()
        self._worker_stop = False
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        logger.info("[CloudUploader] 任务队列已启动")

    def _worker_loop(self):
        while not self._worker_stop:
            try:
                params = self._task_queue.get(timeout=2)
            except queue.Empty:
                continue
            if params is None:
                break
            self._stats["queued"] = max(0, self._stats["queued"] - 1)
            self._stats["running"] += 1
            key = self._task_key(params)
            self._record_task(key, params, "running")

            # 初始化实时进度
            filename = Path(params.get("filepath", "?")).name
            self._task_progress[key] = {
                "name": filename,
                "status": "running",
                "stage": "准备中",
                "logs": [],
                "started": int(time.time()),
                "updated": int(time.time()),
            }

            def _progress_log(msg, _key=key):
                """带进度追踪的日志回调"""
                logger.info(f"[CloudUploader] {msg}")
                prog = self._task_progress.get(_key)
                if prog:
                    prog["logs"].append(msg)
                    # 只保留最近 30 条日志
                    if len(prog["logs"]) > 30:
                        prog["logs"] = prog["logs"][-30:]
                    prog["updated"] = int(time.time())
                    # 从日志内容推断当前阶段
                    if "切片" in msg and ("CMAF" in msg or "HLS" in msg):
                        prog["stage"] = "切片中"
                    elif "上传" in msg and "%" in msg:
                        prog["stage"] = msg.strip()[:40]
                    elif "📤" in msg:
                        prog["stage"] = "上传中"
                    elif "NFO" in msg:
                        prog["stage"] = "写元数据"
                    elif "入库" in msg:
                        prog["stage"] = "入库中"
                    elif "完成" in msg:
                        prog["stage"] = "完成"
                    elif "字幕" in msg:
                        prog["stage"] = "提取字幕"
                    elif "元数据" in msg or "🔍" in msg:
                        prog["stage"] = "元数据查询"

            try:
                result = run_job(params, log_fn=_progress_log)
                status = result.get("status")
                if status == "success":
                    self._stats["success"] += 1
                    self._record_task(key, params, "success")
                    if key in self._task_progress:
                        self._task_progress[key]["status"] = "success"
                        self._task_progress[key]["stage"] = "✅ 完成"
                elif status == "cancelled":
                    self._record_task(key, params, "pending")
                    if key in self._task_progress:
                        self._task_progress[key]["status"] = "cancelled"
                        self._task_progress[key]["stage"] = "已取消"
                else:
                    self._stats["error"] += 1
                    retry_params = params
                    if result.get("stage") == "register" and result.get("r2_path"):
                        retry_params = {
                            **params,
                            "remote_uploaded": True,
                            "remote_uploaded_marker": True,
                            "skip_slice": True,
                            "skip_upload": True,
                            "no_subtitles": True,
                            "clean_after": False,
                        }
                    self._record_task(key, retry_params, "error",
                                      error=result.get("error", ""), stage=result.get("stage", ""))
                    if key in self._task_progress:
                        self._task_progress[key]["status"] = "error"
                        self._task_progress[key]["stage"] = f"❌ {result.get('error', '失败')[:30]}"
            except Exception as e:
                self._stats["error"] += 1
                self._record_task(key, params, "error", error=str(e), stage="worker")
                logger.error(f"[CloudUploader] 任务异常: {e}")
                if key in self._task_progress:
                    self._task_progress[key]["status"] = "error"
                    self._task_progress[key]["stage"] = f"❌ {str(e)[:30]}"
            finally:
                self._stats["running"] = max(0, self._stats["running"] - 1)
                self._task_queue.task_done()
                # 任务执行完毕，从在途集合移除，允许后续对账/恢复时重投
                with self._inflight_lock:
                    self._inflight.discard(key)
                # 清理过旧的已完成进度记录
                self._trim_progress_history()

    def _enqueue(self, params: dict, record: bool = True) -> bool:
        """入队一个上传任务。已在队列中或正在执行的相同任务会被去重跳过。

        返回 True 表示成功入队，False 表示因重复被跳过。
        """
        key = self._task_key(params)
        if not self._config_ready:
            missing = settings.validate()
            error = "配置缺失: " + "、".join(missing)
            logger.warning(f"[CloudUploader] {error}，跳过入队: {key}")
            if record:
                self._record_task(key, params, "error", error=error, stage="precheck")
            return False
        with self._inflight_lock:
            if key in self._inflight:
                logger.info(f"[CloudUploader] 任务已在队列/执行中，跳过重复入队: {key}")
                return False
            self._inflight.add(key)

        if not self._task_queue:
            self._start_worker()
        if record:
            self._record_task(key, params, "pending")
        self._stats["queued"] += 1
        self._task_queue.put(params)
        # 在进度列表中标记排队
        if key not in self._task_progress:
            self._task_progress[key] = {
                "name": Path(params.get("filepath", "?")).name,
                "status": "pending",
                "stage": "排队中",
                "logs": [],
                "started": int(time.time()),
                "updated": int(time.time()),
            }
        return True

    def _trim_progress_history(self):
        """清理过旧的已完成进度记录，只保留最近 N 条。"""
        done_keys = [k for k, v in self._task_progress.items()
                     if v.get("status") in ("success", "error", "cancelled")]
        if len(done_keys) > self._PROGRESS_MAX_HISTORY:
            done_keys.sort(key=lambda k: self._task_progress[k].get("updated", 0))
            for k in done_keys[:-self._PROGRESS_MAX_HISTORY]:
                self._task_progress.pop(k, None)

    def _mp_notify(self, title: str, text: str):
        """供 uploader.notify 调用的 MoviePilot 通知回调。"""
        try:
            self.post_message(mtype=NotificationType.Organize, title=title, text=text)
        except Exception:
            pass

    # ─── MoviePilot 插件接口 ───

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan_library,
                "methods": ["POST"],
                "summary": "立即扫描媒体库补传",
                "description": "后台扫描媒体库目录，将有整理历史但尚未上传的文件加入云端上传队列。",
            },
            {
                "path": "/reconcile",
                "endpoint": self.api_reconcile,
                "methods": ["POST"],
                "summary": "立即执行上传对账",
                "description": "后台重投仍可恢复的失败上传任务。",
            },
        ]

    def api_scan_library(self) -> dict:
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if not self._config_ready:
            return {"success": False, "message": "配置缺失: " + "、".join(settings.validate())}
        threading.Thread(target=self.scan_library, daemon=True).start()
        return {"success": True, "message": "已开始后台扫描"}

    def api_reconcile(self) -> dict:
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if not self._config_ready:
            return {"success": False, "message": "配置缺失: " + "、".join(settings.validate())}
        threading.Thread(target=self.reconcile, daemon=True).start()
        return {"success": True, "message": "已开始后台对账"}

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时服务：对账兜底 + 媒体库目录扫描补传。"""
        if not self._enabled or not self._config_ready:
            return []
        interval = int(self._config.get("reconcile_interval") or 30)
        scan_interval = int(self._config.get("scan_interval") or 0)
        services = [{
            "id": "CloudUploaderReconcile",
            "name": "云端上传对账兜底",
            "trigger": "interval",
            "func": self.reconcile,
            "kwargs": {"minutes": max(5, interval)},
        }]
        if scan_interval > 0:
            services.append({
                "id": "CloudUploaderScanLibrary",
                "name": "云端上传目录扫描补传",
                "trigger": "interval",
                "func": self.scan_library,
                "kwargs": {"minutes": max(10, scan_interval)},
            })
        return services

    # ─── 目录监控 / 媒体库目录扫描补传 ───

    _MEDIA_EXTS = frozenset({".mkv", ".mp4", ".ts", ".avi", ".mov", ".wmv", ".flv", ".m2ts", ".iso"})

    @staticmethod
    def _parse_watch_dirs(raw) -> List[str]:
        """解析用户配置的监控目录，一行一个，兼容逗号/分号分隔。"""
        if not raw:
            return []
        if isinstance(raw, (list, tuple, set)):
            parts = raw
        else:
            parts = re.split(r"[\n,;]+", str(raw))
        dirs = []
        seen = set()
        for part in parts:
            part = str(part).strip()
            if not part:
                continue
            path = os.path.abspath(os.path.expanduser(part))
            if path and path not in seen:
                dirs.append(path)
                seen.add(path)
        return dirs

    def _get_library_dirs(self) -> List[str]:
        """获取 MoviePilot 本地媒体库目录。"""
        try:
            from app.helper.directory import DirectoryHelper
            return [d.library_path for d in DirectoryHelper().get_local_library_dirs()
                    if d.library_path and os.path.isdir(d.library_path)]
        except Exception as e:
            logger.error(f"[CloudUploader] 获取媒体库目录失败: {e}")
            return []

    def _get_watch_dirs(self) -> List[str]:
        """监控目录：用户显式配置优先，留空则使用 MoviePilot 本地媒体库目录。"""
        dirs = self._watch_dirs or self._get_library_dirs()
        valid_dirs = []
        for directory in dirs:
            if os.path.isdir(directory):
                valid_dirs.append(directory)
            else:
                logger.warning(f"[CloudUploader] 监控目录不存在，跳过: {directory}")
        return valid_dirs

    def _cleanup_roots(self) -> List[str]:
        """源文件清理边界：只允许清空目录向上删除到媒体库/监控目录根。"""
        roots = []
        seen = set()
        for directory in [*self._get_library_dirs(), *self._get_watch_dirs()]:
            root = os.path.abspath(os.path.expanduser(directory))
            if root and os.path.isdir(root) and root not in seen:
                roots.append(root)
                seen.add(root)
        return roots

    def _build_upload_params(self, filepath: str, tmdb_id, media_type: str,
                             season=None, episode=None, cleanup_roots: Optional[List[str]] = None) -> dict:
        """构造上传任务参数，集中处理清理边界等运行期选项。"""
        return {
            "filepath": filepath,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "cmaf": True,
            "clean_after": self._clean_after,
            "cleanup_roots": cleanup_roots if cleanup_roots is not None else self._cleanup_roots(),
            "cleanup_parent_depth": 2 if _has_episode_numbers(season, episode) else 1,
        }

    def _start_directory_watch(self):
        """启动 watchdog 实时目录监控。"""
        if not self._watch_enabled:
            return
        if self._watch_observer:
            return

        watch_dirs = self._get_watch_dirs()
        if not watch_dirs:
            logger.warning("[CloudUploader] 未找到可监控目录，目录监控未启动")
            return

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception as e:
            logger.error(f"[CloudUploader] 目录监控依赖 watchdog 不可用: {e}")
            return

        plugin = self

        class MediaWatchHandler(FileSystemEventHandler):
            def on_created(self, event):
                plugin._handle_watch_event(event)

            def on_moved(self, event):
                plugin._handle_watch_event(event)

            def on_modified(self, event):
                plugin._handle_watch_event(event)

        observer = Observer()
        handler = MediaWatchHandler()
        scheduled = 0
        for directory in watch_dirs:
            try:
                observer.schedule(handler, directory, recursive=True)
                scheduled += 1
                logger.info(f"[CloudUploader] 已监控目录: {directory}")
            except Exception as e:
                logger.error(f"[CloudUploader] 监控目录失败 {directory}: {e}")

        if not scheduled:
            return

        try:
            observer.daemon = True
            observer.start()
            self._watch_observer = observer
            logger.info(f"[CloudUploader] 目录监控已启动，共 {scheduled} 个目录")
        except Exception as e:
            logger.error(f"[CloudUploader] 启动目录监控失败: {e}")

    def _stop_directory_watch(self):
        """停止 watchdog 实时目录监控。"""
        with self._watch_lock:
            for timer in self._watch_timers.values():
                timer.cancel()
            self._watch_timers.clear()
        if self._watch_observer:
            try:
                self._watch_observer.stop()
                self._watch_observer.join(timeout=5)
            except Exception as e:
                logger.warning(f"[CloudUploader] 停止目录监控失败: {e}")
            finally:
                self._watch_observer = None

    def _handle_watch_event(self, event):
        """接收 watchdog 事件并做防抖，避免文件写入过程中反复触发。"""
        if (not self._enabled or not self._config_ready or not self._watch_enabled
                or getattr(event, "is_directory", False)):
            return
        path = getattr(event, "dest_path", None) or getattr(event, "src_path", None)
        if not path or Path(path).suffix.lower() not in self._MEDIA_EXTS:
            return

        filepath = os.path.abspath(path)
        with self._watch_lock:
            old = self._watch_timers.pop(filepath, None)
            if old:
                old.cancel()
            timer = threading.Timer(self._watch_delay, self._process_watched_file, args=(filepath,))
            timer.daemon = True
            self._watch_timers[filepath] = timer
            timer.start()

    def _is_file_stable(self, filepath: str, interval: int = 5) -> bool:
        """确认文件还在且大小不再变化，避免上传未写完的文件。"""
        if not os.path.isfile(filepath):
            return False
        try:
            size1 = os.path.getsize(filepath)
            time.sleep(max(1, interval))
            return os.path.isfile(filepath) and os.path.getsize(filepath) == size1 and size1 > 0
        except OSError:
            return False

    def _process_watched_file(self, filepath: str):
        """目录监控触发后，等待文件稳定并尝试按整理历史入队。"""
        with self._watch_lock:
            self._watch_timers.pop(filepath, None)
        if not self._config_ready:
            logger.warning(f"[CloudUploader] 配置缺失，目录监控跳过: {filepath}")
            return

        if not self._is_file_stable(filepath):
            logger.info(f"[CloudUploader] 监控文件尚未稳定，延后处理: {filepath}")
            self._handle_watch_event(type("WatchEvent", (), {"is_directory": False, "src_path": filepath})())
            return

        tasks = self._load_tasks()
        # 整理历史有时会晚于文件事件落库，给它几次机会。
        result = "no_history"
        for attempt in range(1, 4):
            result = self._check_and_enqueue_file(filepath, tasks)
            if result != "no_history":
                break
            time.sleep(10)

        if result == "queued":
            logger.info(f"[CloudUploader] 目录监控入队: {Path(filepath).name}")
            if self._notify:
                self._mp_notify("【云端上传】目录监控入队", Path(filepath).name)
        elif result == "no_history":
            logger.warning(f"[CloudUploader] 目录监控跳过（无整理历史/TMDB）: {filepath}")
        else:
            logger.info(f"[CloudUploader] 目录监控跳过: {result} | {Path(filepath).name}")

    def scan_library(self):
        """
        扫描 MP 媒体库目录，找出尚未上传到 R2 的文件并补传。

        流程：
        1. 从 DirectoryHelper 获取所有本地媒体库目录
        2. 递归扫描目录下的媒体文件
        3. 按文件路径查 TransferHistory，拿到 tmdb_id / season / episode
        4. 用 R2 head_object 检查 master.m3u8/stream.m3u8 是否存在
        5. 不存在且任务记录中没有 success → 入队补传
        """
        if not self._enabled or not self._config_ready:
            return

        logger.info("[CloudUploader] 开始扫描媒体库目录...")
        start = time.time()

        # 1. 获取媒体库目录
        lib_dirs = self._get_library_dirs()

        if not lib_dirs:
            logger.warning("[CloudUploader] 未配置本地媒体库目录，跳过扫描")
            return

        logger.info(f"[CloudUploader] 扫描目录: {lib_dirs}")

        # 2. 扫描所有媒体文件
        media_files = []
        for lib_dir in lib_dirs:
            for root, _, files in os.walk(lib_dir):
                for fname in files:
                    if Path(fname).suffix.lower() in self._MEDIA_EXTS:
                        media_files.append(os.path.join(root, fname))

        logger.info(f"[CloudUploader] 共发现 {len(media_files)} 个媒体文件")

        # 3. 加载已有任务记录（避免重复入队已成功的）
        tasks = self._load_tasks()
        cleanup_roots = self._cleanup_roots()

        # 4. 逐文件检查
        queued = 0
        skipped_success = 0
        skipped_no_history = 0
        skipped_r2_exists = 0
        skipped_inflight = 0
        skipped_uploading = 0

        for filepath in media_files:
            try:
                result = self._check_and_enqueue_file(filepath, tasks, cleanup_roots=cleanup_roots)
                if result == "queued":
                    queued += 1
                elif result == "success":
                    skipped_success += 1
                elif result == "no_history":
                    skipped_no_history += 1
                elif result == "r2_exists":
                    skipped_r2_exists += 1
                elif result == "inflight":
                    skipped_inflight += 1
                elif result == "uploading":
                    skipped_uploading += 1
            except Exception as e:
                logger.warning(f"[CloudUploader] 扫描文件异常 {filepath}: {e}")

        elapsed = time.time() - start
        logger.info(
            f"[CloudUploader] 扫描完成 ({elapsed:.1f}s): "
            f"补传入队 {queued}, R2已存在 {skipped_r2_exists}, "
            f"任务已成功 {skipped_success}, 已在队列 {skipped_inflight}, "
            f"上传中 {skipped_uploading}, 无整理记录 {skipped_no_history}"
        )
        if queued > 0 and self._notify:
            self._mp_notify("【云端上传】目录扫描补传", f"发现 {queued} 个未上传文件，已入队")

    def _check_and_enqueue_file(self, filepath: str, tasks: dict,
                                cleanup_roots: Optional[List[str]] = None) -> str:
        """
        检查单个文件是否需要补传。

        Returns:
            "success"    - 任务记录中已成功，跳过
            "no_history" - MP 整理历史中找不到该文件，跳过
            "r2_exists"  - R2 上已有 ready.json，跳过
            "inflight"   - 已在队列/执行中，跳过
            "uploading"   - R2 上存在较新的 uploading.json，暂不补传
            "queued"     - 已入队补传
        """
        # 3a. 查 TransferHistory，按 dest 路径精确匹配
        try:
            from app.db.transferhistory_oper import TransferHistoryOper
            record = TransferHistoryOper().get_by_dest(dest=filepath)
        except Exception as e:
            logger.debug(f"[CloudUploader] 查整理历史失败 {filepath}: {e}")
            return "no_history"

        if not record or not record.tmdbid:
            return "no_history"

        tmdb_id = record.tmdbid
        media_type = "movie" if record.type == "电影" else "tv"

        # 解析 season / episode（从整理历史的 seasons/episodes 字段，格式 "S01"/"E02"）
        season = None
        episode = None
        if record.seasons:
            m = re.match(r"S(\d+)", record.seasons, re.IGNORECASE)
            if m:
                season = int(m.group(1))
        if record.episodes:
            m = re.match(r"E(\d+)", record.episodes, re.IGNORECASE)
            if m:
                episode = int(m.group(1))

        # 3b. 检查任务记录中是否已成功
        task_key = self._task_key({
            "tmdb_id": tmdb_id, "season": season, "episode": episode,
        })
        if tasks.get(task_key, {}).get("status") == "success":
            return "success"

        # 3c. 检查 R2 标记/播放清单
        if _has_episode_numbers(season, episode):
            r2_path = f"tmdb/{media_type}/{tmdb_id}/season/{int(season)}/episode/{int(episode)}"
        else:
            r2_path = f"tmdb/{media_type}/{tmdb_id}"

        force_overwrite = False
        try:
            from .uploader.r2 import get_s3_client
            s3 = get_s3_client()
            marker_state = self._r2_upload_marker_state(s3, r2_path)
            if marker_state == "ready":
                self._record_task(task_key, {
                    "filepath": filepath, "tmdb_id": tmdb_id,
                    "media_type": media_type, "season": season, "episode": episode,
                }, "success", stage="r2_ready")
                return "r2_exists"
            if marker_state == "uploaded":
                return self._enqueue_remote_register(
                    filepath=filepath,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    season=season,
                    episode=episode,
                    has_uploaded_marker=True,
                    cleanup_roots=cleanup_roots,
                )
            if marker_state == "uploading":
                return "uploading"
            if marker_state == "stale_uploading":
                logger.warning(f"[CloudUploader] 发现超时半成品，按缺失文件补传: {r2_path}")

            if not force_overwrite:
                exists_key = None
                for playlist in ("master.m3u8", "stream.m3u8"):
                    try:
                        s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/{playlist}")
                        exists_key = playlist
                        break
                    except Exception:
                        continue
                if not exists_key:
                    raise FileNotFoundError(f"{r2_path}/master.m3u8")
                logger.info(f"[CloudUploader] 发现旧版已上传播放清单，补执行入库: {r2_path}/{exists_key}")
                return self._enqueue_remote_register(
                    filepath=filepath,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    season=season,
                    episode=episode,
                    has_uploaded_marker=False,
                    cleanup_roots=cleanup_roots,
                )
        except Exception:
            pass  # head_object 抛 404 / NoSuchKey 表示不存在，继续入队

        # 3d. 文件必须还在磁盘上才能入队
        if not os.path.isfile(filepath):
            return "no_history"

        # 3e. 入队补传
        params = self._build_upload_params(
            filepath=filepath,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            cleanup_roots=cleanup_roots,
        )
        if force_overwrite:
            params["force_overwrite"] = True
        enqueued = self._enqueue(params)
        if enqueued:
            logger.info(
                f"[CloudUploader] 扫描补传入队: TMDB-{tmdb_id} "
                f"{_episode_label(season, episode)} | {Path(filepath).name}"
            )
            return "queued"
        return "inflight"  # 已在队列中（_enqueue 返回 False 表示去重跳过）

    def _enqueue_remote_register(self, filepath: str, tmdb_id, media_type: str,
                                 season=None, episode=None,
                                 has_uploaded_marker: bool = False,
                                 cleanup_roots: Optional[List[str]] = None) -> str:
        """R2 已有完整对象时，只补站点入库，不重复切片/上传。"""
        params = self._build_upload_params(
            filepath=filepath,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            cleanup_roots=cleanup_roots,
        )
        params.update({
            "remote_uploaded": True,
            "remote_uploaded_marker": has_uploaded_marker,
            "skip_slice": True,
            "skip_upload": True,
            "no_subtitles": True,
            "clean_after": False,
        })
        enqueued = self._enqueue(params)
        if enqueued:
            logger.info(
                f"[CloudUploader] 补入库入队: TMDB-{tmdb_id} "
                f"{_episode_label(season, episode)} | {Path(filepath).name}"
            )
            return "queued"
        return "inflight"

    def _r2_upload_marker_state(self, s3, r2_path: str) -> str:
        """Return ready/uploaded/uploading/stale_uploading/none for upload markers under an R2 path."""
        try:
            s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/ready.json")
            return "ready"
        except Exception:
            pass

        try:
            s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/uploaded.json")
            return "uploaded"
        except Exception:
            pass

        try:
            marker = s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_path}/uploading.json")
        except Exception:
            return "none"

        last_modified = marker.get("LastModified")
        try:
            marker_ts = last_modified.timestamp() if last_modified else time.time()
        except Exception:
            marker_ts = time.time()
        if time.time() - marker_ts > self._UPLOAD_MARKER_STALE_SECONDS:
            return "stale_uploading"
        return "uploading"

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置表单。"""
        def _row(*cols):
            return {"component": "VRow", "content": list(cols)}

        def _switch(model, label, md=4):
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": "VSwitch", "props": {"model": model, "label": label}}]}

        def _text(model, label, placeholder="", md=6, ptype="text"):
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": "VTextField",
                                 "props": {"model": model, "label": label,
                                           "placeholder": placeholder, "type": ptype}}]}

        def _textarea(model, label, placeholder="", md=12):
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": "VTextarea",
                                 "props": {"model": model, "label": label,
                                           "placeholder": placeholder, "rows": 2}}]}

        return [
            {
                "component": "VForm",
                "content": [
                    _row(_switch("enabled", "启用插件", md=3),
                         _switch("notify", "发送通知", md=3),
                         _switch("clean_after", "上传后清理源文件", md=3),
                         _switch("auto_install", "自动安装切片器", md=3)),
                    _row(_text("delay", "提交延迟(秒)", "30", md=3, ptype="number"),
                         _text("segment_seconds", "切片时长(秒)", "6", md=3, ptype="number"),
                         _text("concurrency", "上传并发数", "8", md=3, ptype="number"),
                         _text("reconcile_interval", "对账间隔(分)", "30", md=3, ptype="number")),
                    _row(_text("scan_interval", "目录扫描间隔(分，0=禁用)", "0", md=4, ptype="number"),
                         _switch("scan_on_start", "启动后扫描一次", md=4),
                         {"component": "VCol", "props": {"cols": 12, "md": 4},
                          "content": [{"component": "VAlert", "props": {
                              "type": "info", "variant": "tonal", "density": "compact",
                              "text": "目录扫描：补传已在媒体库但未上传的文件。建议启用启动扫描，定时扫描可设为 120。"
                          }}]}),
                    _row(_switch("watch_enabled", "实时监控目录", md=3),
                         _text("watch_delay", "监控延迟(秒)", "20", md=3, ptype="number"),
                         {"component": "VCol", "props": {"cols": 12, "md": 6},
                          "content": [{"component": "VAlert", "props": {
                              "type": "info", "variant": "tonal", "density": "compact",
                              "text": "实时监控：文件创建/移动后等待稳定，再按整理历史自动入队。监控目录留空时使用本地媒体库目录。"
                          }}]}),
                    _row(_textarea("watch_dirs", "监控目录（一行一个，留空=媒体库目录）",
                                   "/media/library\n/mnt/downloads", md=12)),
                    # R2：填一个 CF API Token 即可自动推导账户ID/密钥/桶（推荐）
                    _row({"component": "VCol", "props": {"cols": 12},
                          "content": [{"component": "VTextField",
                                       "props": {"model": "cf_api_token",
                                                 "label": "Cloudflare User API Token（cfut_ 开头，填这个自动配置 R2，推荐）",
                                                 "placeholder": "dash.cloudflare.com/profile/api-tokens 创建，赋予 R2 读写权限",
                                                 "type": "password"}}]}),
                    _row(_switch("cf_create_bucket", "桶不存在时自动创建", md=6),
                         _text("r2_bucket", "R2 存储桶名", "flix-800-assets", md=6)),
                    # R2 手动配置（不用 CF Token 时填）
                    _row(_text("r2_account_id", "R2 账户 ID（手动配置时填）", md=6),
                         _text("r2_access_key_id", "R2 Access Key（手动）", md=6)),
                    _row(_text("r2_secret_access_key", "R2 Secret Key（手动）", md=12, ptype="password")),
                    # 站点
                    _row(_text("api_base", "流媒体站地址", "https://your-domain.example", md=6),
                         _text("api_admin_key", "站点 Admin Key (优先)", md=6, ptype="password")),
                    _row(_text("api_username", "站点用户名 (无 Admin Key 时)", md=6),
                         _text("api_password", "站点密码", md=6, ptype="password")),
                    # TMDB
                    _row(_text("tmdb_token", "TMDB Token/API Key（留空则用 MoviePilot 自带）", md=12, ptype="password")),
                    _row(_text("tmdb_proxy_base", "TMDB 元数据代理（留空默认 tmdb.liubei.org）", md=6),
                         _text("tmdb_image_proxy_base", "TMDB 图片代理 /api/t/p 基址（留空走站点）", md=6)),
                    # 二进制路径
                    _row(_text("ffmpeg_bin", "ffmpeg 路径", "ffmpeg", md=6),
                         _text("ffprobe_bin", "ffprobe 路径", "ffprobe", md=6)),
                    _row(_text("mediastreamvalidator_bin", "Apple mediastreamvalidator 路径（可选校验）", "mediastreamvalidator", md=12)),
                    # Telegram (可选)
                    _row(_text("tg_bot_token", "Telegram Bot Token (可选)", md=6, ptype="password"),
                         _text("tg_chat_id", "Telegram Chat ID (可选)", md=6)),
                    _row({
                        "component": "VCol", "props": {"cols": 12},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": "info", "variant": "tonal",
                                "text": ("整理完成后，插件等待指定延迟确认文件存在，"
                                         "再在后台队列内依次完成 FFmpeg HLS 切片→R2上传→站点入库。\n"
                                         "R2 配置：填一个 Cloudflare R2 API Token 即可自动获取账户ID/密钥/桶，无需手填。\n"
                                         "TMDB：留空自动用 MoviePilot 自带；直连失败时走 tmdb.liubei.org 中继（代连 TMDB 官方，不读站点库）。\n"
                                         "切片：FFmpeg fMP4 HLS（视频 copy + 默认音轨杜比转 AAC）；mediastreamvalidator 可选校验。\n"
                                         "字幕：优先提取内嵌字幕；内嵌无中文时自动读取同目录同名/同季集 .ass/.srt 外挂字幕（内嵌与外挂均有中文时仅用内嵌）。\n"
                                         "因此通常只需填：CF API Token + 流媒体站地址 + 站点认证。"),
                            },
                        }],
                    }),
                ],
            }
        ], {
            "enabled": False, "notify": True, "clean_after": True, "auto_install": True,
            "delay": 30, "segment_seconds": 6, "concurrency": 8, "reconcile_interval": 30,
            "scan_interval": 0, "scan_on_start": True,
            "watch_enabled": False, "watch_delay": 20, "watch_dirs": "",
            "cf_api_token": "", "cf_create_bucket": False,
            "r2_account_id": "", "r2_bucket": "flix-800-assets",
            "r2_access_key_id": "", "r2_secret_access_key": "",
            "api_base": "", "api_admin_key": "",
            "api_username": "", "api_password": "", "tmdb_token": "",
            "tmdb_proxy_base": "", "tmdb_image_proxy_base": "",
            "ffmpeg_bin": "ffmpeg", "ffprobe_bin": "ffprobe",
            "mediastreamvalidator_bin": "mediastreamvalidator",
            "tg_bot_token": "", "tg_chat_id": "",
        }

    def get_page(self) -> List[dict]:
        """插件详情页：显示环境检测 + 队列统计 + 任务进度。"""
        env = self._env_status or _env.probe_environment()
        if env.get("ffmpeg_path"):
            settings.FFMPEG_BIN = env["ffmpeg_path"]
        if env.get("ffprobe_path"):
            settings.FFPROBE_BIN = env["ffprobe_path"]
        if env.get("mediastreamvalidator_path"):
            settings.MEDIASTREAMVALIDATOR_BIN = env["mediastreamvalidator_path"]
        if env.get("packager_path"):
            settings.PACKAGER_BIN = env["packager_path"]
        missing = settings.validate()

        env_lines = [_env.format_env_header(env)]
        for tool in env.get("tools") or []:
            env_lines.append(_env.format_tool_line(tool))
        env_lines.append("切片器: FFmpeg fMP4 HLS（内置 manifest 校验全平台可用）")
        if missing:
            env_lines.append("⚠️ 配置缺失: " + "、".join(missing))
        else:
            env_lines.append("✅ 配置完整")
        # R2 来源说明
        if self._cf_derived:
            env_lines.append(f"R2: ✅ 由 CF Token 自动配置（账户 {self._cf_derived.get('account_id','')[:8]}…，桶 {settings.R2_BUCKET}）")
        elif not settings.validate_r2():
            env_lines.append(f"R2: 手动配置（桶 {settings.R2_BUCKET}）")
        if self._watch_enabled:
            watch_dirs = self._get_watch_dirs()
            if self._watch_observer:
                env_lines.append(f"目录监控: ✅ 已启动（{len(watch_dirs)} 个目录，延迟 {self._watch_delay}s）")
            elif watch_dirs:
                env_lines.append("目录监控: ⚠️ 已启用但未启动，请检查 watchdog 依赖或日志")
            else:
                env_lines.append("目录监控: ⚠️ 已启用但没有可用目录")
        else:
            env_lines.append("目录监控: 未启用")
        env_lines.append(f"启动扫描: {'已启用' if self._scan_on_start else '未启用'}")

        stats = self._stats
        stats_text = (f"排队: {stats['queued']} | 运行: {stats['running']} | "
                      f"成功: {stats['success']} | 失败: {stats['error']}")
        persisted = self._task_summary()
        persisted_text = (
            f"持久化任务  成功: {persisted['success']} | 失败: {persisted['error']} | "
            f"待处理: {persisted['pending'] + persisted['running']} | "
            f"文件已不存在: {persisted['missing_file']}"
        )

        env_ok = bool(env.get("ready")) and not missing

        # ─── 构建任务进度列表 ───
        progress_items = []
        # 先显示运行中的，再显示排队的，最后显示已完成的
        sorted_tasks = sorted(
            self._task_progress.items(),
            key=lambda kv: (
                0 if kv[1].get("status") == "running" else
                1 if kv[1].get("status") == "pending" else 2,
                -kv[1].get("updated", 0)
            )
        )
        for key, prog in sorted_tasks[:30]:  # 最多展示 30 条
            status = prog.get("status", "?")
            name = prog.get("name", "?")
            stage = prog.get("stage", "")
            updated = prog.get("updated", 0)

            # 状态图标
            if status == "running":
                icon = "🔄"
            elif status == "pending":
                icon = "⏳"
            elif status == "success":
                icon = "✅"
            elif status == "error":
                icon = "❌"
            else:
                icon = "⚪"

            # 运行时长
            started = prog.get("started", 0)
            if status == "running" and started:
                elapsed = int(time.time()) - started
                mins, secs = divmod(elapsed, 60)
                time_str = f" ({mins}m{secs}s)" if mins else f" ({secs}s)"
            else:
                time_str = ""

            # 最后一条日志
            last_log = ""
            logs = prog.get("logs", [])
            if logs and status == "running":
                last_log = f"\n    └ {logs[-1][:60]}"

            progress_items.append(f"{icon} {name} — {stage}{time_str}{last_log}")

        progress_text = "\n".join(progress_items) if progress_items else "暂无任务"

        page = [
            {"component": "VAlert",
             "props": {"type": "success" if env_ok else "warning", "variant": "tonal",
                       "text": "环境检测\n" + "\n".join(env_lines)}},
            {"component": "VAlert",
             "props": {"type": "info", "variant": "outlined", "class": "mt-2",
                       "text": f"任务队列  {stats_text}"}},
            {"component": "VAlert",
             "props": {"type": "warning" if persisted["error"] else "info",
                       "variant": "outlined", "class": "mt-2",
                       "text": persisted_text}},
        ]

        # 任务进度详情
        if progress_items:
            page.append({
                "component": "VAlert",
                "props": {
                    "type": "info" if any(p.get("status") == "running" for p in self._task_progress.values()) else "success",
                    "variant": "tonal",
                    "class": "mt-2",
                    "style": "white-space: pre-wrap; font-family: monospace; font-size: 12px;",
                    "text": f"任务进度（刷新页面更新）\n\n{progress_text}",
                },
            })

        return page

    # ─── 事件监听 ───

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """监听「整理完成」事件，解析媒体信息后入队上传。"""
        if not self._enabled:
            return

        event_data = event.event_data or {}
        logger.info("[CloudUploader] 收到 TransferComplete 事件")

        transfer_info = event_data.get("transferinfo") or event_data.get("transfer_info")
        media_info = event_data.get("mediainfo") or event_data.get("media_info")
        meta_info = event_data.get("meta")

        file_path = self._extract_file_path(transfer_info)
        if not file_path:
            logger.warning("[CloudUploader] 无法获取文件路径，跳过")
            return

        tmdb_id = None
        media_type = "tv"
        if media_info:
            if hasattr(media_info, "tmdb_id"):
                tmdb_id = media_info.tmdb_id
                if hasattr(media_info, "type"):
                    mtype = str(media_info.type).lower()
                    media_type = "movie" if "movie" in mtype or "电影" in mtype else "tv"
            elif isinstance(media_info, dict):
                tmdb_id = media_info.get("tmdb_id")
                media_type = media_info.get("type", "tv")

        season = None
        episode = None
        if meta_info:
            if hasattr(meta_info, "begin_season"):
                season = meta_info.begin_season
                episode = meta_info.begin_episode
            elif isinstance(meta_info, dict):
                season = meta_info.get("begin_season") or meta_info.get("season")
                episode = meta_info.get("begin_episode") or meta_info.get("episode")

        if not tmdb_id:
            logger.warning(f"[CloudUploader] 无 TMDB ID，跳过: {file_path}")
            return

        title = ""
        if media_info:
            title = getattr(media_info, "title", "") or (
                media_info.get("title", "") if isinstance(media_info, dict) else "")

        ep_str = _episode_label(season, episode)

        # 事件防抖：MoviePilot 可能对同一文件在极短时间内重复派发 TransferComplete，
        # 这里按 (tmdb_id, season, episode, 文件名) 在时间窗内去重，避免重复入队。
        dedup_key = f"{tmdb_id}_{season}_{episode}_{Path(file_path).name}"
        now = time.time()
        with self._inflight_lock:
            last = self._recent_events.get(dedup_key, 0)
            # 顺手清理过期记录，避免无限增长
            if len(self._recent_events) > 200:
                expired = [k for k, t in self._recent_events.items()
                           if now - t > self._EVENT_DEBOUNCE_SECONDS]
                for k in expired:
                    self._recent_events.pop(k, None)
            if now - last < self._EVENT_DEBOUNCE_SECONDS:
                logger.info(f"[CloudUploader] 重复事件忽略（{self._EVENT_DEBOUNCE_SECONDS}s 内）: {ep_str} | {Path(file_path).name}")
                return
            self._recent_events[dedup_key] = now

        logger.info(f"[CloudUploader] 整理完成: TMDB-{tmdb_id} {ep_str} | {Path(file_path).name}")

        threading.Thread(
            target=self._delayed_enqueue,
            args=(file_path, tmdb_id, media_type, season, episode, title),
            daemon=True,
        ).start()

    def _delayed_enqueue(self, filepath, tmdb_id, media_type, season, episode, title):
        """延迟确认文件存在后入队。"""
        if self._delay > 0:
            logger.info(f"[CloudUploader] 等待 {self._delay}s...")
            time.sleep(self._delay)
        if not self._config_ready:
            logger.warning(f"[CloudUploader] 配置缺失，跳过上传入队: {Path(filepath).name}")
            return

        if not os.path.isfile(filepath):
            logger.warning(f"[CloudUploader] 文件不存在，跳过: {filepath}")
            if self._notify:
                self._mp_notify("【云端上传】文件不存在", f"文件: {filepath}")
            return

        enqueued = self._enqueue(self._build_upload_params(
            filepath=filepath,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
        ))
        if enqueued and self._notify:
            ep_str = _episode_label(season, episode)
            self._mp_notify("【云端上传】已入队", f"{title} {ep_str} | TMDB-{tmdb_id}")

    @staticmethod
    def _item_path(item) -> Optional[str]:
        """从 FileItem（对象或 dict）中取出 path。"""
        if not item:
            return None
        if hasattr(item, "path"):
            return getattr(item, "path", None)
        if isinstance(item, dict):
            return item.get("path")
        return None

    def _extract_file_path(self, transfer_info) -> Optional[str]:
        """从 transfer_info 中提取整理后的「单个文件」目标路径。

        关键：必须用 transferinfo.target_item.path（每个文件各自的目标路径，
        整理回调中从不被改写）。绝不能优先用 file_list_new[0]——MoviePilot 在
        批量整理时，会把「最后完成的那个任务」的 file_list_new 重新赋值为整批
        所有文件的列表，导致 [0] 指向第一集，进而漏传/错传该批最后一集。
        """
        if not transfer_info:
            return None

        # 1. 首选：本文件的目标项（最可靠，按文件粒度，不会被批次逻辑篡改）
        target_item = getattr(transfer_info, "target_item", None)
        if target_item is None and isinstance(transfer_info, dict):
            target_item = transfer_info.get("target_item")
        path = self._item_path(target_item)
        if path:
            return str(path)

        # 2. 次选：目标目录项 + 文件名（仅当能定位到具体文件名时）
        target_diritem = getattr(transfer_info, "target_diritem", None)
        if target_diritem is None and isinstance(transfer_info, dict):
            target_diritem = transfer_info.get("target_diritem")
        dir_path = self._item_path(target_diritem)
        fileitem = getattr(transfer_info, "fileitem", None)
        if fileitem is None and isinstance(transfer_info, dict):
            fileitem = transfer_info.get("fileitem")
        fname = None
        if fileitem is not None:
            fname = getattr(fileitem, "name", None) or (
                fileitem.get("name") if isinstance(fileitem, dict) else None)
        if dir_path and fname:
            return str(Path(dir_path) / fname)

        # 3. 兜底：file_list_new。仅当恰好只有 1 个元素时才可信；
        #    多元素说明已被批次逻辑改写成整批列表，无法判断对应哪个文件，宁可放弃。
        file_list = getattr(transfer_info, "file_list_new", None)
        if file_list is None and isinstance(transfer_info, dict):
            file_list = transfer_info.get("file_list_new")
        if file_list and len(file_list) == 1:
            return str(file_list[0])
        return None

    def stop_service(self):
        """停止任务队列和目录监控。"""
        self._stop_directory_watch()
        self._worker_stop = True
        if self._task_queue:
            try:
                self._task_queue.put_nowait(None)
            except Exception:
                pass
