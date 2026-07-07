# 800 MoviePilot Plugin

MoviePilot 插件仓库。

## 插件

### CloudUploader（云端自动上传）

整理完成后自动 **FFmpeg fMP4 HLS 切片 → R2 上传 → TMDB 元数据 → 站点入库**，全流程在插件进程内完成，无需独立服务。

**环境（跨平台）**

| 层级 | 组件 | macOS | Linux/Docker | Windows |
|------|------|-------|--------------|---------|
| 必需 | ffmpeg / ffprobe | PATH / Homebrew / auto-install | 镜像内置或 auto-install | auto-install (`static-ffmpeg`) |
| 内置 | manifest 校验 | ✅ | ✅ | ✅ |
| 可选 | mediastreamvalidator | Apple HLS Tools 手动安装 | 不适用 | 不适用 |
| 可选 | packager | 字幕 fMP4 | 同左 | 同左 |

**无脑上手（3 项）**

1. Cloudflare API Token（自动配置 R2）
2. 流媒体站地址 + Admin API Key
3. 启用插件（TMDB 留空即用 MoviePilot 自带 Key）

详情页有「上手检查」清单；环境 API：`POST /plugin/CloudUploader/refresh_env` 重新检测切片环境。
- 站点 Admin 配置 Telegram 上新通知后，插件上传入库（首次绑定播放源）会自动推送到频道 [@TVBot800](https://t.me/TVBot800)

## 在 MoviePilot 中安装

1. 设置 → 插件 → 添加仓库：`https://github.com/CNLiuBei/800-moviepilot-plugin`
2. 插件市场安装「云端自动上传」
3. 启用后在配置界面填写 R2、TMDB、站点信息

## 发布新版本（维护者）

本仓库插件清单 `package.v2.json` 标记了 `"release": true`，因此 MoviePilot **通过 GitHub Releases 安装**，而不是逐文件下载。规则（来自 MoviePilot 源码）：

- Release tag：`CloudUploader_v{version}`
- 资产文件名：`clouduploader_v{version}.zip`（全小写）
- zip 根目录直接是插件文件（`__init__.py` / `requirements.txt` / `uploader/`）

**每次改版本号都必须配套发一个新 Release，否则无法安装/升级。**

### 方式一：自动发布（推荐）

仓库已配置 GitHub Actions（`.github/workflows/release.yml`）。
只要修改插件代码、把 `package.v2.json` 的 `version` 改成新版本号并推送到 `main`，
Actions 会自动按规范打包并创建对应 Release，无需本地操作：

```bash
# 改完代码 + 改 package.v2.json 版本号
git add . && git commit -m "..." && git push
# 推送后到 Actions 页面查看自动发布结果
```

> 若该版本的 Release 已存在，Actions 会自动跳过（不会重复发）。

### 方式二：本地手动发布

也可用脚本在本地一键发布（需 `gh` 已登录、`zip`、`python3`）：

```bash
bash scripts/release.sh           # 按当前版本发布
bash scripts/release.sh --force   # 同版本已存在时覆盖重建
```
