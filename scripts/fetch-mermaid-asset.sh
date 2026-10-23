#!/usr/bin/env bash
# 取/升级 mermaid 运行时到 codewiki/assets/mermaid.min.js。
#
# 该资产**已提交进仓**并随 wheel 分发（pyproject [tool.setuptools.package-data]
# codewiki = ["assets/*.js"]）—— README 承诺"随包分发"，wheel 构建以源码树为准，
# 不入库则克隆后构建的 wheel 必缺这个文件。本脚本只服务"升级版本"这一个场景：
#   bash scripts/fetch-mermaid-asset.sh [版本]     # 默认 11
# 升级后请连同新文件一起提交。
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
