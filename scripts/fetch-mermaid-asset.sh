#!/usr/bin/env bash
# 取/升级 mermaid 运行时到 codewiki/assets/mermaid.min.js。
#
# 该资产**已提交进仓**并随 wheel 分发（pyproject [tool.setuptools.package-data]
# codewiki = ["assets/*.js"]）—— README 承诺"随包分发"，wheel 构建以源码树为准，
# 不入库则克隆后构建的 wheel 必缺这个文件。本脚本只服务两个场景：
#   bash scripts/fetch-mermaid-asset.sh [版本]     # 下载/升级，并重算哈希
#   bash scripts/fetch-mermaid-asset.sh --verify   # 只校验现有资产（CI 用，零网络）
# 升级后请连同新资产与 .sha256 一起提交。
set -euo pipefail

#: 当前**入库**的版本。想确认仓里那份到底是哪个版本，看这一行 —— 资产是压缩后
#: 的产物，文件里没有 banner、grep 不到版本号，不写死在这里就只能靠猜。
#: 升级三步：改这个值 → 跑本脚本 → 连同新资产一起提交。
#: ⚠️ 12.x 是 breaking：默认布局 dagre→ELK、默认观感换 neo、运行时要求 ES2024 与
#: Safari 17.4+、且单文件 IIFE 内置了 ELK（3.4MB → 5.2MB）。`render/site.py` 的
#: `_MERMAID_INIT` 已显式写 `layout:"dagre",look:"classic"` 把观感钉回 11.x 的样子。
PINNED_VERSION="12.2.0"
TARGET="codewiki/assets/mermaid.min.js"
#: 与资产一同入库的哈希。**不要手改** —— 由本脚本在下载成功后重算，
#: tests/test_offline_asset_hash.py 每次比对实际字节。
DIGEST="${TARGET}.sha256"

# macOS 只有 shasum，多数 Linux 只有 sha256sum；两种都认。
_sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

if [ "${1:-}" = "--verify" ]; then
  if [ ! -f "$TARGET" ] || [ ! -f "$DIGEST" ]; then
    echo "校验失败：缺少 $TARGET 或 $DIGEST" >&2
    exit 1
  fi
  want=$(awk 'NR==1 {print $1}' "$DIGEST")
  got=$(_sha256_of "$TARGET")
  if [ "$want" != "$got" ]; then
    echo "校验失败：资产与 $DIGEST 不一致" >&2
    echo "  记录: $want" >&2
    echo "  实际: $got" >&2
    exit 1
  fi
  echo "校验通过：$TARGET 与入库哈希一致（${got}）"
  exit 0
fi

VERSION="${1:-$PINNED_VERSION}"
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

# 哈希必须**跟着资产一起**重算：它是"仓里这份就是刚下载的那份"的唯一证据。
# sha256sum 格式（`<hex>  <文件名>`）能直接被 `sha256sum -c` / `shasum -a 256 -c` 使用。
digest=$(_sha256_of "$TARGET")
printf '%s  %s\n' "$digest" "$(basename "$TARGET")" > "$DIGEST"
echo "完成：${TARGET}（${size} 字节）"
echo "哈希：${digest} → ${DIGEST}"
