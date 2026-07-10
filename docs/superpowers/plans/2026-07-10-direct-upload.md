# CloudUploader Default Direct Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship CloudUploader 2.8.6 with direct MP4 upload as the default, safe MKV/TS remuxing, optional H.264 compatibility transcoding, reliable recovery, and explicit HEVC browser handling.

**Architecture:** Add a pure upload-policy module and a focused FFmpeg direct-media module, then make the existing queue runner select direct or HLS processing from persisted task parameters. Keep R2 paths and Admin API contracts stable. Add a small web-player capability gate for HEVC and preserve uploaded media for retry when registration fails.

**Tech Stack:** Python 3.12, `unittest`, FFmpeg/ffprobe, boto3/R2, MoviePilot V2 plugin APIs, JavaScript, Vitest, Media Capabilities API.

## Global Constraints

- Target plugin version is exactly `2.8.6`.
- Default upload mode is `direct`; HLS remains selectable with `upload_mode=hls`.
- Direct output is MP4 with faststart; H.265 uses `hvc1`; audio is AAC-LC when the source audio is not browser-safe.
- `h264_compat` defaults to `false`; H.265 video is copied unless compatibility transcoding is explicitly enabled.
- Never rename MKV/TS bytes to `.mp4`; direct upload always consumes a verified MP4 output.
- Direct upload needs ffmpeg and ffprobe because remuxing, audio conversion, and subtitle extraction may be required.
- Do not use direct D1 writes as an application fallback.
- Preserve existing uncommitted direct-upload work and refine it rather than discarding unrelated changes.
- Do not create Git commits unless the user explicitly requests them.

## File Responsibility Map

- Create `plugins.v2/clouduploader/uploader/upload_policy.py`: pure mode normalization, request validation, and task policy helpers.
- Create `plugins.v2/clouduploader/uploader/direct_media.py`: ffprobe inspection and FFmpeg MP4 preparation only.
- Create `plugins.v2/clouduploader/uploader/test_upload_policy.py`: policy/config tests without MoviePilot imports.
- Create `plugins.v2/clouduploader/uploader/test_direct_media.py`: FFmpeg command and result tests.
- Create `plugins.v2/clouduploader/uploader/test_direct_upload.py`: mocked R2 upload and remote-source tests.
- Create `plugins.v2/clouduploader/uploader/test_register_mp4.py`: MP4 registration URL tests.
- Modify `plugins.v2/clouduploader/__init__.py`: config UI/defaults, automatic task parameters, manual API validation, scan/recovery, and progress copy.
- Modify `plugins.v2/clouduploader/uploader/job_runner.py`: prepare direct MP4 before upload, verify output/remote size, markers, and retry behavior.
- Modify `plugins.v2/clouduploader/uploader/register.py`: retain MP4 binding and expose specific Admin API errors.
- Modify `frontend/web/src/services/playback-session.js`: HEVC capability detection before loading MP4.
- Modify `frontend/web/src/services/playback-session.test.js`: HEVC supported/unsupported tests.
- Modify `README.md`, `MAINTAINING.md`, `package.v2.json`, and `.github/workflows/ci.yml`: contract, version, packaging, and CI.

---

### Task 1: Upload policy and default mode

**Files:**
- Create: `plugins.v2/clouduploader/uploader/upload_policy.py`
- Create: `plugins.v2/clouduploader/uploader/test_upload_policy.py`
- Modify: `plugins.v2/clouduploader/__init__.py:54-64, 94-180, 594-664, 1150-1254`

**Interfaces:**
- Produces: `normalize_upload_mode(value: object) -> str`
- Produces: `direct_mode_enabled(params: dict) -> bool`
- Produces: `validate_upload_identity(media_type: str, season: object, episode: object) -> tuple[str, int | None, int | None, str | None]`
- Produces task fields: `upload_mode: "direct" | "hls"`, `direct_mp4: bool`, `h264_compat: bool`

- [ ] **Step 1: Write failing policy tests**

```python
import unittest

from uploader.upload_policy import (
    direct_mode_enabled,
    normalize_upload_mode,
    validate_upload_identity,
)


class UploadPolicyTests(unittest.TestCase):
    def test_direct_is_default(self):
        self.assertEqual("direct", normalize_upload_mode(None))
        self.assertTrue(direct_mode_enabled({}))

    def test_hls_is_explicit(self):
        self.assertEqual("hls", normalize_upload_mode("hls"))
        self.assertFalse(direct_mode_enabled({"upload_mode": "hls"}))

    def test_legacy_direct_mp4_is_supported(self):
        self.assertTrue(direct_mode_enabled({"direct_mp4": True}))

    def test_tv_requires_season_and_episode(self):
        result = validate_upload_identity("tv", None, None)
        self.assertEqual("电视剧直传必须提供 season 和 episode", result[3])

    def test_movie_rejects_partial_episode_identity(self):
        result = validate_upload_identity("movie", 1, None)
        self.assertEqual("season 和 episode 必须同时提供", result[3])
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_upload_policy -v
```

Expected: import failure for missing `uploader.upload_policy`.

- [ ] **Step 3: Implement the pure policy module**

```python
def normalize_upload_mode(value: object) -> str:
    return "hls" if str(value or "").strip().lower() == "hls" else "direct"


def direct_mode_enabled(params: dict) -> bool:
    if "upload_mode" in params:
        return normalize_upload_mode(params.get("upload_mode")) == "direct"
    return bool(params.get("direct_mp4", True))


def validate_upload_identity(media_type, season, episode):
    normalized_type = "tv" if str(media_type).strip().lower() == "tv" else "movie"
    if (season is None) != (episode is None):
        return normalized_type, None, None, "season 和 episode 必须同时提供"
    try:
        normalized_season = int(season) if season is not None else None
        normalized_episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        return normalized_type, None, None, "season/episode 必须是整数"
    if normalized_type == "tv" and (normalized_season is None or normalized_episode is None):
        return normalized_type, normalized_season, normalized_episode, "电视剧直传必须提供 season 和 episode"
    return normalized_type, normalized_season, normalized_episode, None
```

- [ ] **Step 4: Wire policy into plugin configuration and task creation**

In `init_plugin`, persist:

```python
self._upload_mode = normalize_upload_mode(config.get("upload_mode"))
self._h264_compat = bool(config.get("h264_compat", False))
```

In `_build_upload_params`, add:

```python
"upload_mode": self._upload_mode,
"direct_mp4": self._upload_mode == "direct",
"h264_compat": self._h264_compat,
```

In `api_upload`, replace the legacy default with nullable overrides:

```python
upload_mode: str = "",
h264_compat: Optional[bool] = None,
clean_after: Optional[bool] = None,
force_overwrite: Optional[bool] = None,
```

Resolve them with:

```python
mode = normalize_upload_mode(upload_mode or self._upload_mode)
params["upload_mode"] = mode
params["direct_mp4"] = mode == "direct"
params["h264_compat"] = self._h264_compat if h264_compat is None else bool(h264_compat)
params["clean_after"] = self._clean_after if clean_after is None else bool(clean_after)
params["force_overwrite"] = False if force_overwrite is None else bool(force_overwrite)
```

Add a `VSelect` for `upload_mode` with `direct` and `hls`, plus an advanced `VSwitch` for `h264_compat`. Set form defaults to `upload_mode: "direct"` and `h264_compat: False`.

- [ ] **Step 5: Run policy tests**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_upload_policy -v
```

Expected: all policy tests pass.

- [ ] **Step 6: Review checkpoint**

Inspect `git diff` for only policy/config changes. Do not commit without explicit user authorization.

---

### Task 2: Prepare browser-oriented MP4 output

**Files:**
- Create: `plugins.v2/clouduploader/uploader/direct_media.py`
- Create: `plugins.v2/clouduploader/uploader/test_direct_media.py`
- Reuse: `plugins.v2/clouduploader/uploader/slicer.py:180-305`

**Interfaces:**
- Produces: `probe_direct_media(input_path: str) -> dict` with `formatName`, `videoCodec`, `width`, `height`, `bitrate`, `frameRate`, and `duration`
- Produces: `prepare_direct_mp4(input_path: str, output_path: Path, h264_compat: bool, original_language: str | None, print_fn=print) -> dict`
- Return keys: `path`, `videoCodec`, `audioCodec`, `videoCopied`, `audioCopied`, `duration`, `size`

- [ ] **Step 1: Write failing command-selection tests**

Mock `subprocess.run`, `resolve_tool`, `probe_video_info`, `probe_audio_streams`, and `select_audio_streams`.

```python
class DirectMediaTests(unittest.TestCase):
    def test_hevc_mkv_copies_video_tags_hvc1_and_transcodes_dts(self):
        result = prepare_direct_mp4("movie.mkv", self.output, False, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn(["-c:v", "copy"], pairwise(cmd))
        self.assertIn(["-tag:v", "hvc1"], pairwise(cmd))
        self.assertIn(["-c:a", "aac"], pairwise(cmd))
        self.assertTrue(result["videoCopied"])
        self.assertFalse(result["audioCopied"])

    def test_h264_compat_transcodes_hevc_video(self):
        prepare_direct_mp4("movie.mkv", self.output, True, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn("libx264", cmd)
        self.assertIn("yuv420p", cmd)

    def test_faststart_is_always_enabled(self):
        prepare_direct_mp4("movie.mp4", self.output, False, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn("+faststart", cmd)
```

Use a local helper in the test:

```python
def pairwise(items):
    return [items[i:i + 2] for i in range(len(items) - 1)]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_direct_media -v
```

Expected: import failure for missing `uploader.direct_media`.

- [ ] **Step 3: Implement direct-media preparation**

Build the command around existing audio selection:

```python
cmd = [
    ffmpeg, "-hide_banner", "-y", "-i", input_path,
    "-map", "0:v:0", "-map", f"0:a:{default_track['audio_index']}",
]
if h264_compat and video_codec != "h264":
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    video_copied = False
else:
    cmd += ["-c:v", "copy"]
    video_copied = True
if video_codec in {"hevc", "h265"} and video_copied:
    cmd += ["-tag:v", "hvc1"]
if audio_codec in {"aac", "mp4a"}:
    cmd += ["-c:a", "copy"]
    audio_copied = True
else:
    cmd += ["-c:a", "aac", "-b:a", settings.CMAF_AUDIO_BITRATE,
            "-ac", str(min(channels, settings.CMAF_AUDIO_CHANNELS))]
    audio_copied = False
cmd += ["-movflags", "+faststart", "-f", "mp4", str(output_path)]
```

Require ffmpeg and ffprobe, ensure the output exists and is non-empty, then re-probe the output. Raise `RuntimeError` containing the last 1200 characters of FFmpeg stderr on failure.

- [ ] **Step 4: Add real FFmpeg smoke tests with generated tiny fixtures**

Skip when ffmpeg/ffprobe are unavailable:

```python
@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
def test_generated_mkv_becomes_valid_mp4(self):
    # Generate one-second color + sine MKV, prepare it, then ffprobe output.
    prepare_direct_mp4(str(source), output, False, "en")
    probe = probe_direct_media(str(output))
    self.assertIn("mp4", probe["formatName"])
```

Use `lavfi` color and sine inputs so no binary fixture is committed.

- [ ] **Step 5: Run direct-media tests**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_direct_media -v
```

Expected: mocked tests pass; smoke test passes when FFmpeg is installed or reports a clear skip.

- [ ] **Step 6: Review checkpoint**

Confirm video is never transcoded unless `h264_compat=True`, and non-MP4 bytes cannot reach upload under a `.mp4` key.

---

### Task 3: Integrate preparation and verified direct R2 upload

**Files:**
- Create: `plugins.v2/clouduploader/uploader/test_direct_upload.py`
- Modify: `plugins.v2/clouduploader/uploader/job_runner.py:248-333, 520-850`

**Interfaces:**
- Consumes: `direct_mode_enabled(params) -> bool`
- Consumes: `prepare_direct_mp4(...) -> dict`
- Updates: `upload_mp4_direct(filepath, r2_prefix, extra_files, on_progress, cancel_check, force_overwrite) -> tuple[int, int]`

- [ ] **Step 1: Write failing mocked-R2 tests**

```python
class DirectUploadTests(unittest.TestCase):
    def test_force_overwrite_false_keeps_ready_object(self):
        self.list_mock.return_value = {"ready.json": 20, "video.mp4": 100}
        result = upload_mp4_direct(
            str(self.video), "tmdb/movie/1", [], self.progress, lambda: False,
            force_overwrite=False,
        )
        self.assertEqual((0, 0), result)
        self.delete_mock.assert_not_called()

    def test_remote_size_must_match_local_size(self):
        self.s3.head_object.return_value = {"ContentLength": 1, "ContentType": "video/mp4"}
        with self.assertRaisesRegex(RuntimeError, "远端文件大小不一致"):
            upload_mp4_direct(str(self.video), "tmdb/movie/1", [], self.progress, lambda: False, True)

    def test_remote_source_priority(self):
        self.assertEqual("cmaf", _remote_source_type("tmdb/movie/1"))
```

- [ ] **Step 2: Run tests and verify current failures**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_direct_upload -v
```

Expected: overwrite and remote-size assertions fail against the current implementation.

- [ ] **Step 3: Make overwrite and verification truthful**

Remove `del force_overwrite`. When `force_overwrite=False`, skip only if both `ready.json` and `video.mp4` exist. Otherwise clear stale objects.

After upload:

```python
remote = s3.head_object(Bucket=settings.R2_BUCKET, Key=f"{r2_prefix}/video.mp4")
if int(remote.get("ContentLength") or -1) != src.stat().st_size:
    raise RuntimeError("R2 远端文件大小不一致")
if str(remote.get("ContentType") or "").split(";", 1)[0] != "video/mp4":
    raise RuntimeError("R2 video.mp4 Content-Type 不是 video/mp4")
```

Verify every extra file by key and size before returning.

- [ ] **Step 4: Replace source-file upload with prepared-output upload**

In `run_job`, resolve:

```python
direct_mp4 = direct_mode_enabled(params)
```

For direct mode:

```python
prepared = prepare_direct_mp4(
    filepath,
    local_output / "video.mp4",
    h264_compat=bool(params.get("h264_compat")),
    original_language=original_language,
    print_fn=log,
)
video_duration = prepared.get("duration")
direct_path = prepared["path"]
```

Pass `direct_path`, not `filepath`, to `upload_mp4_direct`. Preserve the original path for NFO metadata and final source cleanup. Always clean temporary direct output after successful upload, independently of source cleanup.

- [ ] **Step 5: Require both ffmpeg and ffprobe in direct mode**

Replace the permissive direct precheck with:

```python
if not resolve_tool(settings.FFMPEG_BIN) or not resolve_tool(settings.FFPROBE_BIN):
    msg = "直传环境未就绪: 重封装需要 ffmpeg/ffprobe"
    return {"status": "error", "error": msg, "r2_path": None, "stage": "precheck"}
```

Make `_enqueue` use the same requirement for both modes, but change user-facing copy from “切片环境” to “媒体处理环境”.

- [ ] **Step 6: Run direct and subtitle tests**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_direct_upload uploader.test_direct_media uploader.test_subtitles_external -v
```

Expected: all tests pass.

- [ ] **Step 7: Review checkpoint**

Confirm logs distinguish “快速重封装”, “音频转 AAC”, “H.264 兼容转码”, and “R2 远端校验通过”.

---

### Task 4: Scan, reconcile, and marker recovery

**Files:**
- Modify: `plugins.v2/clouduploader/__init__.py:420-447, 883-1148`
- Modify: `plugins.v2/clouduploader/uploader/job_runner.py:196-230, 792-850`
- Extend: `plugins.v2/clouduploader/uploader/test_upload_policy.py`
- Extend: `plugins.v2/clouduploader/uploader/test_direct_upload.py`

**Interfaces:**
- Marker field: `uploadMode: "direct" | "hls"`
- Marker fields: `h264Compat: bool`, `videoCodec: str`, `width: int`, `height: int`, `bitrate: int`, `frameRate: float`
- Remote media candidates: `master.m3u8`, `stream.m3u8`, `video.mp4`

- [ ] **Step 1: Add failing marker/recovery tests**

```python
def test_marker_preserves_direct_policy(self):
    marker = _upload_marker_payload(
        "movie.mkv", "mp4", "1080p", [], 120,
        upload_mode="direct", h264_compat=False,
    )
    self.assertEqual("direct", marker["uploadMode"])
    self.assertFalse(marker["h264Compat"])
```

Add a pure helper test that turns a marker into retry fields:

```python
self.assertEqual(
    {"upload_mode": "direct", "direct_mp4": True, "h264_compat": False},
    recovery_policy_from_marker(marker),
)
```

- [ ] **Step 2: Run tests and verify signature failure**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_direct_upload uploader.test_upload_policy -v
```

Expected: `_upload_marker_payload` rejects new arguments or lacks fields.

- [ ] **Step 3: Persist and restore policy**

Extend `_upload_marker_payload` and its call sites with `upload_mode`, `h264_compat`, and the prepared media fields `videoCodec`, `width`, `height`, `bitrate`, and `frameRate`. Write the same media fields to `ready.json`. Add `recovery_policy_from_marker` to `upload_policy.py`.

When `_enqueue_remote_register` has an uploaded marker, read it and merge the recovery policy before enqueue. When the marker is absent, use `_remote_source_type`; map `mp4` to direct and HLS/CMAF to HLS.

- [ ] **Step 4: Make scan recognize direct objects**

Change:

```python
for media_object in ("master.m3u8", "stream.m3u8", "video.mp4"):
```

Use the found object to set recovery mode. Do not enqueue a new HLS upload when `video.mp4` already exists.

- [ ] **Step 5: Preserve direct mode after register failure**

When the worker records a register-only retry, include:

```python
"upload_mode": params.get("upload_mode", "direct" if source_type == "mp4" else "hls"),
"direct_mp4": source_type == "mp4",
"h264_compat": bool(params.get("h264_compat")),
```

- [ ] **Step 6: Run recovery tests**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_upload_policy uploader.test_direct_upload -v
```

Expected: all marker and recovery tests pass.

- [ ] **Step 7: Review checkpoint**

Manually inspect a persisted direct task and ensure restart/reconcile cannot transform it into an HLS task.

---

### Task 5: API validation, registration errors, and MP4 contract

**Files:**
- Create: `plugins.v2/clouduploader/uploader/test_register_mp4.py`
- Modify: `plugins.v2/clouduploader/__init__.py:537-664`
- Modify: `plugins.v2/clouduploader/uploader/register.py:200-370`

**Interfaces:**
- Manual API query fields: `filepath`, `tmdb_id`, `media_type`, `season`, `episode`, `upload_mode`, `h264_compat`, `clean_after`, `force_overwrite`, `resolution`
- MP4 URL: `/api/r2/{r2_path}/video.mp4`

- [ ] **Step 1: Write failing register tests**

```python
class Mp4RegisterTests(unittest.TestCase):
    def test_mp4_source_uses_video_mp4(self):
        ok, error = _do_register(
            self.client, 1726601, "movie", None, None,
            "tmdb/movie/1726601", "1080p", [], 120, "mp4", print,
        )
        source_call = self.client.post.call_args_list[1]
        self.assertEqual("/api/r2/tmdb/movie/1726601/video.mp4", source_call.kwargs["json"]["url"])
        self.assertEqual("mp4", source_call.kwargs["json"]["sourceType"])

    def test_import_error_keeps_server_message(self):
        response = Mock(status_code=500, text='{"message":"TMDB_API_KEY 未配置"}')
        self.assertIn("TMDB_API_KEY 未配置", _api_error(response, "TMDB导入失败"))
```

- [ ] **Step 2: Run tests and verify current behavior**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_register_mp4 -v
```

Expected: MP4 URL test passes; structured error test identifies whether `_api_error` needs adjustment.

- [ ] **Step 3: Complete API validation**

Call `validate_upload_identity` before `_build_upload_params`. Reject TV without season/episode. Return the resolved `upload_mode` and `h264_compat` in the API response.

Keep `clean_after` and `force_overwrite` defaults aligned with plugin config when the caller omits them; do not silently default to values opposite the configured behavior.

- [ ] **Step 4: Preserve structured Admin errors**

Make `_api_error` prefer JSON `message`, then `detail`, then response text:

```python
try:
    payload = resp.json()
except ValueError:
    payload = {}
detail = payload.get("message") or payload.get("detail") or resp.text[:500]
return f"{prefix} [{resp.status_code}] — {detail}"
```

- [ ] **Step 5: Run tests**

Run:

```bash
cd plugins.v2/clouduploader
python -m unittest uploader.test_register_mp4 uploader.test_upload_policy -v
```

Expected: all tests pass and a missing production TMDB key would be visible in plugin logs.

- [ ] **Step 6: Review checkpoint**

Verify no code path performs direct D1 writes and register failure retains `uploaded.json`.

---

### Task 6: HEVC capability gate in the web player

**Files:**
- Modify: `/Users/cn/Desktop/800/frontend/web/src/services/playback-session.js:25-121, 258-304`
- Modify: `/Users/cn/Desktop/800/frontend/web/src/services/playback-session.test.js`
- Modify: `/Users/cn/Desktop/800/workers/api/src/routes/me.ts:925-944`
- Modify: `/Users/cn/Desktop/800/workers/api/src/routes/me-cms-stream.test.ts`

**Interfaces:**
- Produces: `isHevcCodec(value: object) -> bool`
- Produces: `probeMp4CodecSupport(stream: object, navigatorLike=navigator) -> Promise<{ok: bool, codec?: string}>`
- Stream metadata consumes `type`, `codec`/`videoCodec`, `width`, `height`, `bitrate`, `frameRate`

- [ ] **Step 1: Write failing HEVC capability tests**

```javascript
it('blocks HEVC MP4 when Media Capabilities reports unsupported', async () => {
  const nav = {
    mediaCapabilities: {
      decodingInfo: vi.fn().mockResolvedValue({ supported: false, smooth: false, powerEfficient: false }),
    },
  };
  await expect(probeMp4CodecSupport({
    type: 'mp4',
    codec: 'hvc1.1.6.L120.B0',
    url: '/api/r2/movie/video.mp4',
  }, nav)).resolves.toMatchObject({ ok: false, codec: 'hvc1.1.6.L120.B0' });
});

it('allows H264 without an HEVC probe', async () => {
  await expect(probeMp4CodecSupport({
    type: 'mp4',
    codec: 'avc1.640028',
  }, {})).resolves.toEqual({ ok: true });
});
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd /Users/cn/Desktop/800/frontend/web
npm test -- playback-session.test.js
```

Expected: missing export `probeMp4CodecSupport`.

- [ ] **Step 3: Implement the capability probe**

```javascript
export function isHevcCodec(value) {
    return /(?:^|[,;\s])(hvc1|hev1|hevc|h265)(?:[.,;\s]|$)/i.test(String(value || ''));
}

export async function probeMp4CodecSupport(stream, navigatorLike = globalThis.navigator) {
    const codec = stream?.videoCodec || stream?.codec || '';
    if (!isHevcCodec(codec)) return { ok: true };
    const decodingInfo = navigatorLike?.mediaCapabilities?.decodingInfo;
    if (typeof decodingInfo !== 'function') return { ok: false, codec };
    try {
        const result = await decodingInfo.call(navigatorLike.mediaCapabilities, {
            type: 'file',
            video: {
                contentType: `video/mp4; codecs="${codec}"`,
                width: Number(stream.width) || 1920,
                height: Number(stream.height) || 1080,
                bitrate: Number(stream.bitrate) || 8_000_000,
                framerate: Number(stream.frameRate) || 30,
            },
        });
        return result.supported ? { ok: true } : { ok: false, codec };
    } catch {
        return { ok: false, codec };
    }
}
```

- [ ] **Step 4: Gate MP4 loading**

Before `probeStreamCodecSupport(playbackUrl)` in `loadInto`, call `probeMp4CodecSupport(stream)`. On failure show:

```javascript
`当前设备无法解码本片的 H.265/HEVC 视频（${codec}）。请使用支持 HEVC 的 Safari/设备，或切换 H.264 兼容源。`
```

Use existing `playerErrorActions()` so alternate-source recovery remains available.

- [ ] **Step 5: Return codec metadata from the authenticated stream API**

In `workers/api/src/routes/me.ts`, read `${dirPrefix}/ready.json` for MP4 sources and merge only validated scalar metadata:

```typescript
let mediaMeta: Record<string, unknown> = {}
if (String(src.type || '') === 'mp4') {
  try {
    const marker = await c.env.BLOB.get(`${dirPrefix}/ready.json`)
    const parsed = marker ? await marker.json<Record<string, unknown>>() : null
    if (parsed && typeof parsed === 'object') mediaMeta = parsed
  } catch {}
}
return {
  title: (quality && label && !label.includes(quality)) ? `${label} ${quality}`.trim() : (label || quality || '').trim(),
  label,
  quality,
  type: String(src.type || ''),
  url: streamUrl,
  videoCodec: typeof mediaMeta.videoCodec === 'string' ? mediaMeta.videoCodec : undefined,
  width: Number(mediaMeta.width) || undefined,
  height: Number(mediaMeta.height) || undefined,
  bitrate: Number(mediaMeta.bitrate) || undefined,
  frameRate: Number(mediaMeta.frameRate) || undefined,
}
```

Extend `workers/api/src/routes/me-cms-stream.test.ts` with an MP4 source and mocked `ready.json`, then assert the stream response contains `type: "mp4"` and the five media fields. Do not download MP4 bytes in the browser to discover codecs.

- [ ] **Step 6: Run web tests and build**

Run:

```bash
cd /Users/cn/Desktop/800/frontend/web
npm test -- playback-session.test.js
npm run build
```

Expected: HEVC tests pass and the web build completes without errors.

- [ ] **Step 7: Review checkpoint**

Confirm H.264 playback behavior is unchanged and HEVC unsupported devices fail before entering an infinite buffering state.

---

### Task 7: Documentation, CI, and release metadata

**Files:**
- Modify: `README.md`
- Modify: `MAINTAINING.md`
- Modify: `package.v2.json`
- Modify: `plugins.v2/clouduploader/__init__.py:42-49`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Release tag: `CloudUploader_v2.8.6`
- Release asset: `clouduploader_v2.8.6.zip`

- [ ] **Step 1: Align version and descriptions**

Set both version fields to `2.8.6`. Update package description to say default MP4 direct upload with optional HLS, H.265 preservation, and AAC audio compatibility.

- [ ] **Step 2: Document configuration and API**

README must include:

```text
upload_mode=direct     # default
upload_mode=hls        # optional segmentation
h264_compat=false      # preserve H.265
```

Document:

```bash
curl -X POST \
  -H "X-API-KEY: $MOVIEPILOT_API_TOKEN" \
  "http://127.0.0.1:3001/api/v1/plugin/CloudUploader/upload?filepath=/media/movie.mkv&tmdb_id=1726601&media_type=movie&upload_mode=direct"
```

Explain that HEVC remains device-dependent and that `h264_compat=true` trades processing time and size for broad browser compatibility.

- [ ] **Step 3: Fix maintenance documentation**

Remove the stale v1.1.0 zip statement. State that releases are generated by `.github/workflows/release.yml` from the package version and that local `releases/*.zip` archives are not authoritative.

- [ ] **Step 4: Run unit tests in CI**

Replace shell `find` usage and add dependency/test steps:

```yaml
- name: Install plugin dependencies
  run: python -m pip install -r plugins.v2/clouduploader/requirements.txt

- name: Compile CloudUploader plugin
  run: python -m compileall -q plugins.v2/clouduploader

- name: Test CloudUploader plugin
  working-directory: plugins.v2/clouduploader
  run: python -m unittest discover -s uploader -p "test_*.py" -v
```

- [ ] **Step 5: Run local validation**

Run:

```bash
python -m json.tool package.v2.json >/dev/null
python -m compileall -q plugins.v2/clouduploader
cd plugins.v2/clouduploader
python -m unittest discover -s uploader -p "test_*.py" -v
```

Expected: metadata parses, compilation succeeds, and all tests pass.

- [ ] **Step 6: Review checkpoint**

Check `git diff --check`, ensure no credentials or generated media entered the diff, and do not commit or publish without explicit user authorization.

---

### Task 8: End-to-end verification and production registration

**Files:**
- No source files unless verification exposes a reproducible defect.
- Use local MoviePilot logs and R2 object metadata.

**Interfaces:**
- Movie test identity: TMDB movie ID supplied by the operator.
- Required Admin endpoints: `/api/admin/session`, `/api/admin/import-single`, `/api/admin/sources`

- [ ] **Step 1: Reload the local plugin**

Run:

```bash
TOKEN="$(./moviepilot config get API_TOKEN)"
curl -fsS -H "X-API-KEY: $TOKEN" \
  "http://127.0.0.1:3001/api/v1/plugin/reload/CloudUploader"
```

Expected: JSON with `success: true`.

- [ ] **Step 2: Exercise H.264 MP4 direct mode**

Submit one MP4/H.264/AAC file with `upload_mode=direct`. Verify logs show fast remux, no video/audio transcode, R2 size match, Admin import, source bind, and `ready.json`.

- [ ] **Step 3: Exercise H.265 MKV direct mode**

Submit one MKV/H.265/non-AAC file. Verify logs show video copy, `hvc1`, audio AAC conversion, successful MP4 upload, and `sourceType=mp4`.

- [ ] **Step 4: Verify R2 and CDN Range**

Run:

```bash
curl -fsSI -H "Range: bytes=0-1023" \
  "https://cdn.guangying.org/tmdb/movie/1726601/video.mp4"
```

Expected: `206`, `Content-Type: video/mp4`, `Accept-Ranges: bytes`, and a correct `Content-Range`.

- [ ] **Step 5: Verify production Admin integration**

Call `/api/admin/session` with the configured Admin key. Then run import and source binding through the plugin.

Expected: 2xx responses. If `/api/admin/session` itself returns 500, stop plugin release verification and report a Worker admin-router blocker. If only `/api/admin/import-single` fails with `TMDB_API_KEY 未配置`, configure the Worker’s supported TMDB setting and rerun; do not patch D1 manually.

- [ ] **Step 6: Verify HEVC browser behavior**

On a supported Safari/Apple device, verify H.265 starts and seeks. On an unsupported profile/device, verify the explicit compatibility message appears and offers source switching.

- [ ] **Step 7: Final release-readiness check**

Run:

```bash
git status --short
git diff --check
python -m json.tool package.v2.json >/dev/null
```

Expected: only intended source, test, docs, CI, and version files are changed; no `.wrangler`, credentials, generated MP4, or cache files are included.

- [ ] **Step 8: Await explicit publish authorization**

Do not commit, push, tag, or create a GitHub Release until the user explicitly requests those actions.

