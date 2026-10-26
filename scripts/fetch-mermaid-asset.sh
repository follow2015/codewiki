#!/usr/bin/env bash
# 取/升级 mermaid 运行时到 codewiki/assets/mermaid.min.js。
#
# 该资产**已提交进仓**并随 wheel 分发（pyproject [tool.setuptools.package-data]
# codewiki = ["assets/*.js"]）—— README 承诺"随包分发"，wheel 构建以源码树为准，
# 不入库则克隆后构建的 wheel 必缺这个文件。本脚本只服务"升级版本"这一个场景：
#   bash scripts/fetch-mermaid-asset.sh [版本]     # 默认 11
# 升级后请连同新文件一起提交。
set -euo pipefail

#: 当前**入库**的版本。想确认仓里那份到底是哪个版本，看这一行 —— 资产是压缩后
#: 的产物，文件里没有 banner、grep 不到版本号，不写死在这里就只能靠猜。
#: 升级三步：改这个值 → 跑本脚本 → 连同新资产一起提交。
#: ⚠️ 11.x → 12.x 是 breaking，不是"改个数字"：12 把默认布局从 dagre 换成 ELK
#: （图会重新排布、默认配色也变）、单文件 IIFE 资产增大约 500KB(gzip)、运行时要求
#: ES2024 与 Safari 17.4+、并移除 defaultRenderer。升之前要先决定观感与浏览器下限。
PINNED_VERSION="11.17.2"
VERSION="${1:-$PINNED_VERSION}"
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
