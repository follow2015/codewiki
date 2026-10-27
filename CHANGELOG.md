# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按语义化版本。
**数据包 schema 每次变更必须单独成行并写明升级方式**（规则见 `CONTRIBUTING.md`）。

## [Unreleased]

### 数据包 schema

- 无变更：`SCHEMA_VERSION` 仍为 `1`，1.x 内只增不删。读取入口统一到
  `codewiki.packetio.load_packet()`：版本更新时明确报错（不降级解析）、版本更旧时
  走 `MIGRATIONS` 逐级迁移。旧数据包可直接读，无需手工处理。

### 安全

- `codewiki/ai/guard/` 收敛为最底层安全边界（路径、密钥判定与脱敏），并修掉三个
  实测越权面：`doctor --probe` 用仓库给的 `base_url` 直连、`search_codebase` 经符号
  链接读到仓库外、搜索结果回传绝对路径。
- 工具调用收敛成**一条必经的管道**（`codewiki/ai/registry.py`）：参数校验（未知参数
  一律拒，模型无法覆盖 `repo_root` 等上下文）→ 执行 → 输出过滤（剔密钥、脱敏、
  分隔符中和、截断）→ 审计。新增工具不再需要各自记得做这几件事。
- 子进程入口统一到 `codewiki/subproc.py`，并用 ruff banned-api（TID251）锁死
  「`subprocess` 只在其中导入」。
- `[ai]` 的 `model` / `base_url` / `write_back` 只认命令行与环境变量；仓库配置里的
  值被忽略并说明替代途径（`--trust-config` 可显式放行）。

### 新增

- 可注入时钟：`SOURCE_DATE_EPOCH` 与 `--timestamp`（显式参数优先）。非法值报错，
  不静默退回真实时间 —— 这是「两次运行逐字节相同」的前提。
- 「增量产物 == 全量产物」逐字节验收：改文件 / 新增 / 删除 / 重命名 / 改分层 /
  改 `min_confidence` / 换解析后端 / 升工具版本八种变更，外加渲染中途抛异常。
  写这套验收反查出三个真实缺陷：`--min-confidence` 半生效、换解析后端不进指纹、
  新增模块的邻居页面不重渲。
- `scripts/check_guarantees.py`：README 的 `<!-- guarantee: id -->` 与测试的
  `@pytest.mark.guarantee("id")` 双向比对，防止文档承诺失效而无人发现。
- `codewiki/assets/mermaid.min.js.sha256`：离线资产的 SHA-256 入库，测试与
  `scripts/fetch-mermaid-asset.sh --verify` 每次比对。
- `CONTRIBUTING.md`、`.pre-commit-config.yaml`。

### 变更

- 运行统计（缓存命中数、增量说明）改走 stdout，不再写进 `data/codewiki.json`。
  归档物里混入"这一轮怎么跑的"会让增量产物永远比全量多一条诊断，逐字节比对作废。
- mermaid 运行时 11.17.2 → 12.2.0，并在 `_MERMAID_INIT` 显式写
  `layout:"dagre",look:"classic"` 钉住旧观感（12 的默认布局换成 ELK、观感换 neo）。
  代价：资产 3.4MB → 5.2MB，运行时要求 ES2024 / Safari 17.4+。
- CI 矩阵收成两端（`requires-python` 下界 + 开发基线），action 升到声明 node24 的
  版本；`networkx` 下限由 `>=3.7` 降到 `>=3.6`（3.7 要 Python >=3.12，与
  `requires-python = ">=3.11"` 冲突，3.11 那条腿此前根本装不上）。

### 修复

- `packet_from_parts` 下沉到 `codewiki.packetio`，解开 `graph → render` 依赖环。
- `scripts/sync-to-deploy.sh` 去掉 bash 4 依赖（本机只有 3.2），并修掉白名单通配符
  被 shell 提前展开导致**子包整体漏同步**的问题（脚本仍 rc=0、仍打印"已覆盖"，
  只有公开仓 CI 才照出原形）。
- 打包：`packages.find` 的 `namespaces` 默认开启会把 `codewiki-deploy/` 副本整棵树
  打进 wheel，已关闭。
- `Completed.truncated` 此前用 `len(stdout) > max_output` 判断，恒为假。

## [1.0.0]

首个发布版本：`analyze` / `doctor` / `export` / `serve` / `cycles` / `diff` /
`suggest-layers` / `merge-layers` / `import-graph`，可选 AI 层
（`ai-annotate` / `merge-annotations` / `agent`），静态 HTML 站点与离线 Zip 交付。
