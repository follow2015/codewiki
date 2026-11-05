#!/usr/bin/env bash
# 取/升级交互图运行时到 codewiki/assets/graph/cytoscape.min.js（WP8）。
#
# 该资产**已提交进仓**并随 wheel 分发（pyproject 的 package-data 里有
# assets/graph/*.js）—— 交互页要在断网的机器上打开，运行时不能靠 CDN。
# 本脚本只服务两个场景：
#   bash scripts/fetch-graph-asset.sh [版本]     # 下载/升级，并重算哈希
#   bash scripts/fetch-graph-asset.sh --verify   # 只校验现有资产（CI 用，零网络）
# 升级后请连同新资产与 .sha256 一起提交，并同步 NOTICE 里的版本号
# （tests/test_graph_asset.py 会把两处钉死，忘了就红）。
set -euo pipefail

#: 当前**入库**的版本。资产是压缩后的产物，文件里没有 banner、grep 不到版本
#: 号，不写死在这里就只能靠猜。升级三步：改这个值 → 跑本脚本 → 连同新资产
#: 与 NOTICE 里的版本一起提交。
#:
#: 为什么是 3.34.4：下载时 npm 上 ``latest`` 指向的稳定版（脚本不追 latest，
#: 必须写死 —— 追 latest 意味着同一份源码今天和明天构建出不同产物）。
#:
#: 为什么不要扩展：cytoscape-dagre 会把 dagre 一起带进来（另加约 100 KB），而
#: 层与节点的坐标本来就在 Python 侧算好了（见 render/layer_map.py），交互页用
#: ``preset`` 直接吃那份坐标即可 —— 用不着图库再排一次。预算只有 500 KB，
#: 100 KB 花在重复劳动上不划算。
PINNED_VERSION="3.34.4"
TARGET="codewiki/assets/graph/cytoscape.min.js"
#: 与资产一同入库的哈希。**不要手改** —— 由本脚本在下载成功后重算，
#: tests/test_graph_asset.py 每次比对实际字节。
DIGEST="${TARGET}.sha256"
#: 截断下载的下限（与测试里的 MIN_BYTES 同口径）。
MIN_BYTES=204800

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
URL="https://cdn.jsdelivr.net/npm/cytoscape@${VERSION}/dist/cytoscape.min.js"

mkdir -p "$(dirname "$TARGET")"
echo "下载 cytoscape@${VERSION} → ${TARGET}"
curl -fsSL "$URL" -o "${TARGET}.tmp"

# 校验：小于下限或不含 cytoscape 字样 ⇒ 拿到的多半是错误页/占位，不能用。
size=$(wc -c < "${TARGET}.tmp")
if [ "$size" -lt "$MIN_BYTES" ] || ! grep -q "cytoscape" "${TARGET}.tmp"; then
  echo "校验失败：${TARGET}.tmp 只有 ${size} 字节或不含 cytoscape 内容" >&2
  rm -f "${TARGET}.tmp"
  exit 1
fi
mv "${TARGET}.tmp" "$TARGET"

# 哈希必须**跟着资产一起**重算：它是"仓里这份就是刚下载的那份"的唯一证据。
digest=$(_sha256_of "$TARGET")
printf '%s  %s\n' "$digest" "$(basename "$TARGET")" > "$DIGEST"
echo "完成：${TARGET}（${size} 字节）"
echo "哈希：${digest} → ${DIGEST}"
echo "别忘了：NOTICE 里的版本号要与 PINNED_VERSION（${VERSION}）一致。"
