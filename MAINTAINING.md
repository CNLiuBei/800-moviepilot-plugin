# MoviePilot 插件维护说明

当前重点插件：

```text
integrations/moviepilot-plugin/plugins.v2/clouduploader/
```

这是 MoviePilot V2 内嵌版云端上传插件。它在 MoviePilot 进程内完成整理事件监听、默认 MP4 直传（可选 HLS）、R2 上传、TMDB 元数据和站点入库。

## 和旧独立上传工具的关系

| 模块 | 路径 | 使用场景 |
|---|---|---|
| MoviePilot 插件 | `integrations/moviepilot-plugin/plugins.v2/clouduploader/` | 当前唯一维护入口，放进 MoviePilot 后由整理完成事件直接触发 |
| 旧独立上传工具 | `legacy/uploader-standalone/standalone/` | 已停止维护，仅作历史参考 |

后续上传链路只维护 MoviePilot 插件版。

## 发布产物

正式发布由 `.github/workflows/release.yml` 根据 `package.v2.json` 的版本号自动生成：

- Release tag：`CloudUploader_v{version}`
- 资产：`clouduploader_v{version}.zip`

本地 `releases/*.zip` 归档不是权威发布源，不要依赖它们安装或升级。
