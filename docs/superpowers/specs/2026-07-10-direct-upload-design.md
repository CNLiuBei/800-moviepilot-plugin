# CloudUploader 默认直传设计

日期：2026-07-10  
目标版本：2.8.6

## 目标

将 CloudUploader 的默认处理方式改为整文件直传，同时保留 HLS 分片开关。在尽量不转码视频、保留 H.265 体积优势的前提下，提高网页与移动端播放兼容性。

## 非目标

- 不为每个 H.265 文件同时生成 H.264 副本。
- 不实现边播边实时转码服务。
- 不重构 MoviePilot 的任务框架。
- 不用人工 D1 写入作为正常入库流程。

## 用户配置

- `upload_mode=direct`：默认，生成单个浏览器可用的 MP4 后上传。
- `upload_mode=hls`：沿用现有 CMAF fMP4 HLS 分片流程。
- `h264_compat=false`：默认保留 H.265；开启后将非 H.264 视频转为 H.264。
- `clean_after`：沿用现有配置，决定成功后是否清理源文件和临时文件。

手动 `/upload` API 可以覆盖全局上传模式，未指定时使用全局配置。

## 媒体处理策略

所有输入先由 ffprobe 检测容器、视频、音频和字幕。

### MP4 输入

- H.264/H.265 + AAC：快速重封装并启用 faststart，不转码。
- 音频不是 AAC：复制视频流，音频转 AAC-LC。
- H.265 输出使用 `hvc1` 标签，改善 Apple 平台兼容性。
- 开启 `h264_compat` 且视频不是 H.264：视频转 H.264，音频转 AAC。

即使输入已经是 MP4，也执行快速重封装，以统一 faststart、轨道选择和 codec tag。

### MKV、TS 等输入

不得通过改扩展名伪装成 MP4，统一通过 FFmpeg 重封装：

- 视频默认 `copy`。
- 音频统一为 AAC-LC；已兼容时允许复制。
- 选择默认视频轨和默认音轨，忽略附件。
- 外挂和内嵌字幕按现有模块转换为独立 WebVTT。

无法写入 MP4 时任务明确失败，展示编码、轨道和 FFmpeg 错误，不上传不可播放对象。

## H.265 兼容原则

H.265 不保证所有浏览器都可播放。插件默认保留 H.265，避免大规模转码。产物使用 MP4 + `hvc1` + AAC。

播放器使用 Media Capabilities API 检测 HEVC 解码能力；不支持时显示明确提示。需要全浏览器兼容的场景可开启 `h264_compat`。

## 任务流程

1. MoviePilot 整理完成、目录扫描或手动 API 创建任务。
2. `_build_upload_params` 写入最终上传模式并持久化。
3. `_enqueue` 按模式检查环境；直传与 HLS 都需要 ffmpeg/ffprobe，前者用于重封装、音频转码和字幕提取。
4. `run_job` 查询 TMDB、处理字幕并探测媒体。
5. 直传生成临时 `video.mp4`；HLS 生成现有清单和分片。
6. 上传到既有 TMDB R2 前缀。
7. 校验远端对象大小、Content-Type 和必需文件。
8. 写 NFO、字幕清单和上传标记。
9. 调用站点 Admin API 导入元数据并绑定 `sourceType=mp4` 或 HLS 类型。
10. 入库成功后写 `ready.json`，按配置清理本地文件。

## R2 契约

电影路径：

`tmdb/movie/{tmdb_id}/video.mp4`

电视剧路径：

`tmdb/tv/{tmdb_id}/season/{season}/episode/{episode}/video.mp4`

同一前缀包含 `video.mp4`、WebVTT 字幕、`subtitles.json`、NFO 和 `ready.json`。

直传必须尊重 `force_overwrite`。关闭覆盖时，已有完整 `ready.json` 的任务跳过；仅半成品或显式覆盖时清理并重传。

## 自动扫描与恢复

- R2 检测同时识别 `master.m3u8`、`stream.m3u8` 和 `video.mp4`。
- `uploaded.json` 持久化 `sourceType`、上传模式、质量、字幕和时长。
- 对账和注册重试保留原上传模式，不能把 MP4 任务重新切成 HLS。
- 电视剧直传任务必须提供季、集，缺失时拒绝入队。

## 状态与可观测性

任务详情区分媒体探测、快速重封装、音频转码、H.264 兼容转码、R2 直传、远端校验和站点入库。

日志显示视频是否复制、音频是否转码、输出大小、处理耗时和上传速度。只有远端 HEAD 校验及大小匹配后才输出“校验通过”。

## 入库错误处理

`/api/admin/import-single` 失败时保留对象和 `uploaded.json`，进入可恢复状态。插件原样显示站点响应中的具体错误，而不只显示 HTTP 500。

生产环境必须配置有效的站点 TMDB API Key。修复后由对账任务重试；直接写 D1 不作为正常兜底。

## 测试

单元测试覆盖：

- MP4/H.264/AAC 快速重封装。
- MKV/H.265/DTS → MP4/H.265/AAC，视频不转码且使用 `hvc1`。
- `h264_compat` 转码分支。
- 不可封装流的失败信息。
- 直传 R2 的覆盖、跳过、取消、进度和远端大小校验。
- `video.mp4` 的远端类型识别与扫描去重。
- 电影和电视剧 `/upload` 参数校验。
- MP4 URL、`sourceType=mp4` 和对账恢复。

集成验证覆盖：

- MP4 和 MKV 各完成一次 MoviePilot → R2 → Admin API → 播放器流程。
- CDN Range 返回 206、正确 Content-Type、Content-Range 和总大小。
- Safari、Chrome、Edge 验证 H.264；支持 HEVC 的设备验证 H.265。
- 不支持 HEVC 的设备显示兼容提示而不是无限缓冲。

## 发布要求

- `plugin_version` 与 `package.v2.json` 同步为 2.8.6。
- README 说明默认直传、HLS 开关、HEVC 限制和 `/upload` API。
- CI 执行新增单元测试，不只做语法检查。
- 发布 tag 为 `CloudUploader_v2.8.6`，资产为 `clouduploader_v2.8.6.zip`。

