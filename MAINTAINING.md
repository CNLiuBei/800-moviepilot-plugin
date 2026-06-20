# MoviePilot 插件维护说明

当前重点插件：

```text
integrations/moviepilot-plugin/plugins.v2/clouduploader/
```

这是 MoviePilot V2 内嵌版云端上传插件。它在 MoviePilot 进程内完成整理事件监听、Apple HLS 切片、R2 上传、TMDB 元数据和站点入库。

## 和旧独立上传工具的关系

| 模块 | 路径 | 使用场景 |
|---|---|---|
| MoviePilot 插件 | `integrations/moviepilot-plugin/plugins.v2/clouduploader/` | 当前唯一维护入口，放进 MoviePilot 后由整理完成事件直接触发 |
| 旧独立上传工具 | `legacy/uploader-standalone/standalone/` | 已停止维护，仅作历史参考 |

后续上传链路只维护 MoviePilot 插件版。

## 发布产物

`CloudUploader-v1.1.0.zip` 是插件发布包/归档文件。修改插件源码后应重新打包生成新版 zip，并在发布说明里写清楚版本号。
