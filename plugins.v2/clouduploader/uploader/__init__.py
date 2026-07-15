"""
云端上传工具（插件内嵌版）

将原独立 FastAPI 上传工具的核心流水线打包进 MoviePilot 插件：
默认 MP4 直传（可选 HLS 分片）→ R2 上传 → TMDB 元数据 → 站点入库。

所有模块使用包内相对导入，配置统一从 runtime_config.settings 读取（由插件注入）。
"""
from .runtime_config import settings

__all__ = ["settings"]
