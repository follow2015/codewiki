# codewiki

[![CI](https://github.com/follow2015/codewiki/actions/workflows/ci.yml/badge.svg)](https://github.com/follow2015/codewiki/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

配置驱动、可离线交付的**代码架构知识图谱与文档生成器**。

任意项目在根目录放一个 `codewiki.toml`（或零配置直跑），即可产出：模块页 Wiki、
分层架构图与模块上下文图、标准 JSON 数据包、静态 HTML 站点，以及可离线阅读的 Zip。

## 特性

- **零网络依赖的静态链路**：Python AST 后端 + 正则降级后端，断网机器照常出全套产物。
- **图谱是唯一事实源**：渲染、AI、环检测、diff 全部消费同一份数据包，不各算各的。
- **静态优先、不伪造**：解析不到的调用落 `External` 或低置信动态边，绝不随机绑定；
  离线资源缺失只记 `ASSET_MISSING` 诊断，不假装"双击可看图"。 <!-- guarantee: no-fake-asset -->
- **离线可看图**：`assets/mermaid.min.js` 随包分发，换机器双击 HTML 也能看架构图。 <!-- guarantee: offline-diagram -->
- **大仓不崩**：节点超限自动按层分包；增量分析（改动 <5% 秒级出结果）。
- **分层架构地图**：首页内联 SVG（层带卡片 + 跨层引用 TOP），分层泄漏一眼可见。
- **可选 AI 层**：架构标注 / 交互问答，工具闭集 + 沙箱 + 审计，AI 整段缺失等同关闭。

## 环境基线

- Python **>= 3.14**（`requires-python` 安装期拦截）
- 可选：tree-sitter extra（TS/Go 后端）、OpenAI 兼容端点（AI 层）

## 快速开始

```bash
pip install -e .

# 零配置直跑
codewiki analyze --src /path/to/your/repo --out wiki-output

# 或先起草一份配置（扫描真实目录 + 按依赖排层序，不用手写）
codewiki init --src .
codewiki analyze --src . --out wiki-output

# 环境自检
codewiki doctor

# 打包交付
codewiki export --dir wiki-output --zip wiki.zip
```

产物结构：

```
wiki-output/
  ├── index.html                 # 静态站点入口（免服务端，含分层架构地图）
  ├── index.md                   # 索引：分层 → 模块 + 诊断摘要
  ├── modules/<slug>.html|.md    # 每模块一页：符号 / 上下游 / 注解 / 内嵌图
  ├── diagrams/                  # 宏观图 + 模块上下文图（mermaid 源）
  ├── data/codewiki.json         # 标准数据包（渲染层与 AI 层的唯一输入）
  ├── data/codewiki.schema.json  # 冻结的 JSON Schema
  ├── assets/mermaid.min.js      # 离线看图运行时（随包，换机器也能看）
  └── diagnostics.md             # 未分层 / 环 / 解析告警 / 资源缺失
```

## 命令

| 命令 | 作用 | 需要 AI |
|---|---|---|
| `init` | 起草配置（默认 `codewiki.toml`，`--out` 指定外挂路径）：自动排除非产品目录 + 目录骨架 + 依赖排序 + 环提示；`--ai` / `--deferred` 时由模型归并 | 可选 |
| `analyze` | 纯静态分析 → 数据包 / 图 / Wiki / HTML | 否 |
| `doctor` | 环境自检（Python / 后端 / Node / 离线资源 / AI） | 否 |
| `export --zip` | 产物打包（原子化，跳过临时文件） | 否 |
| `serve` | 本地静态预览 | 否 |
| `cycles` | 依赖环检测（模块环 warning / 层级环 error）+ 环图 | 否 |
| `diff` | 变更影响分析（两快照或 git 范围） | 否 |
| `suggest-layers` | 社区发现 → 建议分层（纯静态） | 否 |
| `merge-layers` | 合并建议分层（不覆盖人工层） | 否 |
| `import-graph` | 导入外部图谱（graphify）→ 统一数据包 | 否 |
| `ai-annotate` | AI 架构标注 → 建议文件（**不改主配置**）；`--deferred` 时由 agent 作答 | 是 |
| `ai-diagram` | AI 时序图：从静态调用链出图（坏图零落盘）；`--l2-validate` 走真实渲染校验 | 是 |
| `merge-annotations` | 合并 AI 建议（人工注解受保护） | 否 |
| `agent` | 交互问答：模型调只读工具取证后作答 | 是 |

## AI 层（可选）

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=qwen2.5-coder        # 也可用 --model
export OPENAI_BASE_URL=http://localhost:11434/v1   # 也可用 --base-url（本地 Ollama / vLLM）

# ① 无网开发机：出静态产物
codewiki analyze --src . --out wiki-output

# ② 带模型的机器：只读数据包 + 源码，产出建议
codewiki ai-annotate --input wiki-output/data/codewiki.json --config codewiki.toml --src . --out /tmp/ai

# ③ 回开发机：diff 审阅后合并，重新 analyze 生效
codewiki merge-annotations /tmp/ai/suggestions/annotations.suggest.toml
```

时序图单独一条命令（`analyze` 保持纯静态，不联网）：

```bash
# 从静态 CALLS 边挑调用链 → 出 Mermaid 时序图；校验不过的图不落盘
codewiki ai-diagram --input wiki-output/data/codewiki.json --config codewiki.toml \
  --out wiki-output --top-n 3 --depth 5

# 想要真实渲染校验（需 Node + mermaid-cli）：配置 [ai] l2_validate = true
# 或临时加 --l2-validate；环境不具备时降级为只跑 L1 并留诊断
```

### 无凭据、不出网：让 agent 充当模型（`--deferred`）

在自带模型的 agent 环境（WorkBuddy / CodeBuddy）里，往往既没有 `OPENAI_API_KEY`
也不必出网。此时把"调模型"拆成三步，全程零凭据、零网络请求：

```bash
# ① 只导出待问内容；还有待作答时退出码 3（不是失败）
codewiki ai-annotate --input wiki-output/data/codewiki.json --config codewiki.toml --src . \
  --out wiki-output --deferred .ai-queue

# ② agent 读 .ai-queue/requests/*.json（文件自带作答协议），按其中 system 提示要求的
#    输出形状，把答案写进 .ai-queue/answers/<同名文件>

# ③ 重跑同一条命令：命中答案 → 产出建议文件（退出码 0）
```

请求指纹是 `sha256(purpose + model + messages)`：改提示词、改源码或换模型都会让旧答案
自动失效。答案按**不可信输入**处理 —— target 必须是本模块、evidence 行号必须真实存在，
写文件的一方绕不过这两道校验。<!-- guarantee: ai-deferred -->

`codewiki.toml` 来自被分析仓库，因此 `[ai]` 里的 **`model` / `base_url` / `write_back`
只认命令行与环境变量**（写在文件里会被忽略并提示原因）——否则 hostile 仓库可以把
API Key 发往任意地址，或让 AI 建议直接合进主配置。自己拥有的仓库要保留文件写法时，
加 `--trust-config`（或 `CODEWIKI_TRUST_CONFIG=1`）显式授权。 <!-- guarantee: ai-config-trust -->

### agent 全自动起草配置（`init` 三态）

agent 调 `codewiki` 时不需要人先想层怎么分：`codewiki init --src .` 按环境自动选路。

| 环境 | 行为 |
|---|---|
| 配了 `OPENAI_MODEL` / `--model` | 模型按职责归并；结果**逐条校验**，编造的目录整层丢弃 |
| 什么都没配 | 纯静态：目录骨架 + 按依赖排出的层序 + 点名互相依赖/成环的目录对 |
| `--deferred <dir>` | agent 自己充当模型，零凭据零网络（与 `ai-annotate` 同机制） |

deferred 三步 —— **退出码 3 表示"请求已导出、还没作答"，不是失败**：

```bash
# ① 只导出待问内容
codewiki init --src . --deferred .ai-queue

# ② 读 .ai-queue/requests/*.json（文件自带作答协议），答案写到 .ai-queue/answers/<同名文件>

# ③ 重跑同一条命令 → 落 codewiki.toml（退出码 0）
```

起草结果自带 `[languages]`（按实际扫到的语言）、`[analysis] exclude`（`--exclude` 追加项）
与从 `tsconfig` 推断的 `ts_aliases`，可以直接 `analyze --config` 使用。**半成品不落盘**：
deferred 还有待作答时既不写文件也不返回 0 —— 层边界只写好一半的配置，人不知道该改哪一层。
缺 tree-sitter 时 TS/JS 会整片被跳过（看起来与"仓库本来没有前端"一模一样），所以会打一条
warning 点名缺哪种语言、多少个文件。

**非产品目录不用手数。** `init` 按结构事实自动排除：子目录自带 `.git`（部署副本 / 另一份
克隆）、`tests` / `scripts` / `migrations` / `docs` / `deploy` / 备份 / 存档 / 运行时目录、
带版本号的第三方包快照。实测一份真实仓库，手写的 31 条 `exclude` 换成自动 12 条 +
`--languages python` 之后：683 个模块 / 18 层 / **未分层 0**，与手工精修版（670 个）只差
一份没有 `.git` 的克隆。判错就从数组里删掉，配置本身是唯一准绳；整个关掉用
`--no-auto-exclude`。**职责归并不猜** —— 目录名推不出业务含义，那是 `--ai` 的活。

外挂式配置（配置不落在被分析仓库里，一个仓放多份）用 `--out`：

```bash
codewiki init --src ~/Desktop/ipip --out configs/ipip.toml --languages python
codewiki analyze --src ~/Desktop/ipip --config configs/ipip.toml --out wiki-output/ipip
```

### 问答（`agent`）：开局概览 + 引用核对

```bash
codewiki agent "鉴权入口在哪？" --input wiki-output/data/codewiki.json --src .
```

工具是闭集，且都能只用**已落盘的数据包**或只读取证来回答：

| 工具 | 用途 |
|---|---|
| `list_modules` | 按层列模块（度数降序）：先摸清仓库，别猜 `node_id` |
| `find_symbol` | 按名字找符号，直接给出 `path:line` |
| `query_graph` | 看某节点的上下游；找不到时给出相近 id 候选 |
| `read_file` / `search_codebase` | 取证（路径受仓库边界限制、密钥文件拒读） |
| `git_log` / `git_show` | 历史（固定 argv，不接受选项） |

首轮消息带项目概览（分层、热点模块、**仅 human/reviewed 的注解**），省掉"先摸一遍仓库"
的往返。答案里的 `file:line` 会被**核对**：文件必须在仓库内、行号必须在文件范围内、
不能是密钥文件；不通过则让模型修一次（占用一轮，最后一轮不再纠错），修不好就带
`AI_CITATION_INVALID` 告警交付，绝不吞答案。核对只覆盖前 40 处引用，超出部分会记
`AI_CITATION_TRUNCATED` —— 不把"没查"说成"查过了"。

三条硬约束：AI **不改写静态产物**、**不直写主配置**（合并且由人发起）、
**受保护注解（human/reviewed）连 `--force` 也不动**；取证行号不可验证的建议
一律丢弃并记诊断。`agent` 问答的工具是闭集、shell 走白名单沙箱、轮次有硬上限、
工具输出一律当作不可信数据、每次调用都审计。 <!-- guarantee: ai-closed-toolset -->

## 性能基线（合成仓库）

| 场景 | 模块数 | 耗时 | 峰值内存 |
|---|---|---|---|
| 首次全量 | 10,000（50k 节点 / 60k 边） | **64s** | 427MB |
| 增量（改 2% = 200 文件） | 10,000 | **5.0s** | 636MB |
| 首次全量 | 5,000 | 11.5s | — |

增量靠两层：FileModel 缓存（键 = 后端 + 解析格式版本 + 路径 + mtime + 内容 hash）+ <!-- guarantee: cache-key-has-format-version -->
**增量渲染**（只重渲"改动 ∪ 影响闭包"内的模块页与图）。渲染输入指纹（分层、注解、
`min_confidence`、`max_nodes`、语言白名单、工具/格式版本）任一项变化即自动退回全量，
诊断里点名是哪一项变了；上一轮没跑完会留 `.incomplete` 标记，下一轮据此全量重渲。 <!-- guarantee: single-version-source -->

**增量产物与全量产物逐字节一致**：改文件 / 新增 / 删除 / 重命名 / 改分层 /
改 `min_confidence` / 换解析后端 / 升工具版本，八种变更都比对过完整产物树；
渲染中途抛异常会留 `.incomplete`，下一轮不复用残缺产物。 <!-- guarantee: incremental-equals-full -->

## 关于本仓

本仓是发布仓（源码 + CI + 授权），内部开发与测试在私有主仓进行。
CI 覆盖：语法检查（compileall）、端到端冒烟（对示例仓库出产物）、机密扫描。

## License

[Apache License 2.0](LICENSE)
