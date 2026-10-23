#!/usr/bin/env bash
# 取 mermaid 运行时到 codewiki/assets/（离线交付前跑一次）。
#
# 为什么不把它提交进仓：3.5MB 的第三方构建产物，每次升级都会在 git 里再留一份
# 全量副本；而它只影响"离线看图"这一项可选能力。仓库里保留**取它的脚本**，
# 产物里保留**它本身**（analyze 时复制进 wiki-output/assets/）。
#
# 用法：
#   bash scripts/fetch-mermaid-asset.sh [版本]     # 默认 11
set -euo pipefail

VERSION="${1:-11}"
TARGET="codewiki/assets/mermaid.min.js"
URL="https://cdn.jsdelivr.net/npm/mermaid@${VERSION}/dist/mermaid.min.js"

mkdir -p "$(dirname "$TARGET")"
echo "下载 mermaid@${VERSION} → ${TARGET}"
curl -fsSL "$URL" -o "${TARGET}.tmp"

# 校验：小于 1MB 或不含 mermaid 字样 ⇒ 拿到的多半是错误页/占位，不能用
size=$(wc -c < "${TARGET}.tmp")
if [ "$size" -lt 1048576 ] || ! grep -q "mermaid" "${TARGET}.tmp"; then
  echo "校验失败：${TARGET}.tmp 只有 ${size} 字节或不含 mermaid 内容" >&2
  rm -f "${TARGET}.tmp"
  exit 1
fi
mv "${TARGET}.tmp" "$TARGET"
echo "完成：${TARGET}（${size} 字节）"
