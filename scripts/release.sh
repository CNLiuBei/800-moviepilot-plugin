#!/usr/bin/env bash
#
# CloudUploader 插件发布脚本
#
# 作用：从 package.v2.json 读取版本号，按 MoviePilot 安装规范打包并创建 GitHub Release。
#   - Release tag : CloudUploader_v{version}
#   - 资产文件名  : clouduploader_v{version}.zip （全小写）
#   - zip 根目录直接是插件文件（__init__.py / requirements.txt / uploader/）
#
# 依赖：gh（已登录）、zip、python3
#
# 用法：
#   bash scripts/release.sh           # 按当前版本发布
#   bash scripts/release.sh --force   # 同版本已存在时，删除旧 Release/tag 重新发布
#
set -euo pipefail

PID="CloudUploader"
PLUGIN_DIR_NAME="clouduploader"
REPO="CNLiuBei/800-moviepilot-plugin"

# 切到仓库根目录（脚本在 scripts/ 下）
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

# ── 1. 读取版本号 ──
PKG="package.v2.json"
if [[ ! -f "$PKG" ]]; then
  echo "❌ 未找到 $PKG"; exit 1
fi
VERSION="$(python3 -c "import json,sys; print(json.load(open('$PKG'))['$PID']['version'])")"
if [[ -z "$VERSION" ]]; then
  echo "❌ 无法从 $PKG 读取 $PID 的版本号"; exit 1
fi

TAG="${PID}_v${VERSION}"
ASSET="${PLUGIN_DIR_NAME}_v${VERSION}.zip"
PLUGIN_SRC="plugins.v2/${PLUGIN_DIR_NAME}"

echo "📦 插件: $PID"
echo "🏷  版本: $VERSION"
echo "🔖 Tag : $TAG"
echo "🗜  资产: $ASSET"

if [[ ! -d "$PLUGIN_SRC" ]]; then
  echo "❌ 插件目录不存在: $PLUGIN_SRC"; exit 1
fi

# ── 2. 检查依赖 ──
command -v gh  >/dev/null || { echo "❌ 未安装 gh CLI"; exit 1; }
command -v zip >/dev/null || { echo "❌ 未安装 zip"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "❌ gh 未登录，请先 gh auth login"; exit 1; }

# ── 3. 已存在同版本 Release 的处理 ──
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  if [[ "$FORCE" == "1" ]]; then
    echo "⚠️  已存在 Release $TAG，--force 删除重建..."
    gh release delete "$TAG" --repo "$REPO" --cleanup-tag --yes
  else
    echo "❌ Release $TAG 已存在。如需覆盖请加 --force"; exit 1
  fi
fi

# ── 4. 打包（zip 根目录 = 插件文件，排除缓存）──
OUT="$(mktemp -d)/${ASSET}"
( cd "$PLUGIN_SRC" && zip -r -X "$OUT" . -x "*.pyc" -x "*__pycache__*" -x "*.DS_Store" >/dev/null )
echo "✅ 已打包: $(du -h "$OUT" | cut -f1)"
echo "   zip 根目录内容:"
unzip -l "$OUT" | awk 'NR>3 && $4 !~ /\// {print "     "$4}' | grep -v '^     $' || true

# ── 5. 创建 Release ──
gh release create "$TAG" "$OUT" \
  --repo "$REPO" \
  --title "${PID} v${VERSION}" \
  --notes "云端自动上传 v${VERSION}。MoviePilot 添加本仓库后可直接安装/升级。"

echo "🎉 发布完成: https://github.com/${REPO}/releases/tag/${TAG}"

# ── 6. 校验资产名是否符合 MoviePilot 规范 ──
ACTUAL="$(gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[0].name')"
if [[ "$ACTUAL" == "$ASSET" ]]; then
  echo "✅ 资产名校验通过: $ACTUAL"
else
  echo "⚠️ 资产名不符: 期望 $ASSET 实得 $ACTUAL"
fi

rm -rf "$(dirname "$OUT")"
