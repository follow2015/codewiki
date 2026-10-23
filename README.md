# codewiki

[![CI](https://github.com/follow2015/codewiki/actions/workflows/ci.yml/badge.svg)](https://github.com/follow2015/codewiki/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

配置驱动、可离线交付的**代码架构知识图谱与文档生成器**。

任意项目在根目录放一个 `codewiki.toml`（或零配置直跑），即可产出：模块页 Wiki、
分层架构图与模块上下文图、标准 JSON 数据包、静态 HTML 站点，以及可离线阅读的 Zip。

## 特性

- **零网络依赖的静态链路**：Python AST 后端 + 正则降级后端，断网机器照常出全套产物。
- **图谱是唯一事实源**：渲染、AI、环检测、diff 全部消费同一份数据包，不各算各的。
- **静态优先、不伪造**：解析不到的调用落 `External` 或低置信动态边，绝不随机绑定。
- **离线可看图**：`assets/mermaid.min.js` 随包分发，换机器双击 HTML 也能看架构图。
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

# 或先拷示例配置再定制分层
cp codewiki.toml.example codewiki.toml
codewiki analyze --src . --config codewiki.toml --out wiki-output

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
| `analyze` | 纯静态分析 → 数据包 / 图 / Wiki / HTML | 否 |
| `doctor` | 环境自检（Python / 后端 / Node / 离线资源 / AI） | 否 |
| `export --zip` | 产物打包（原子化，跳过临时文件） | 否 |
| `serve` | 本地静态预览 | 否 |
| `cycles` | 依赖环检测（模块环 warning / 层级环 error）+ 环图 | 否 |
| `diff` | 变更影响分析（两快照或 git 范围） | 否 |
| `suggest-layers` | 社区发现 → 建议分层（纯静态） | 否 |
| `merge-layers` | 合并建议分层（不覆盖人工层） | 否 |
| `import-graph` | 导入外部图谱（graphify）→ 统一数据包 | 否 |
| `ai-annotate` | AI 架构标注 → 建议文件（**不改主配置**） | 是 |
| `merge-annotations` | 合并 AI 建议（人工注解受保护） | 否 |
| `agent` | 交互问答：模型调只读工具取证后作答 | 是 |

## AI 层（可选）

```bash
export OPENAI_API_KEY=...   # 支持 OpenAI 兼容端点（本地 Ollama / vLLM 亦可）

# ① 无网开发机：出静态产物
codewiki analyze --src . --out wiki-output

# ② 带模型的机器：只读数据包 + 源码，产出建议
codewiki ai-annotate --input wiki-output/data/codewiki.json --config codewiki.toml --src . --out /tmp/ai

# ③ 回开发机：diff 审阅后合并，重新 analyze 生效
codewiki merge-annotations /tmp/ai/suggestions/annotations.suggest.toml
```

三条硬约束：AI **不改写静态产物**、**不直写主配置**（合并且由人发起）、
**受保护注解（human/reviewed）连 `--force` 也不动**；取证行号不可验证的建议
一律丢弃并记诊断。`agent` 问答的工具是闭集、shell 走白名单沙箱、轮次有硬上限、
工具输出一律当作不可信数据、每次调用都审计。

## 性能基线（合成仓库）

| 场景 | 模块数 | 耗时 | 峰值内存 |
|---|---|---|---|
| 首次全量 | 10,000（50k 节点 / 60k 边） | **64s** | 427MB |
| 增量（改 2% = 200 文件） | 10,000 | **5.0s** | 636MB |
| 首次全量 | 5,000 | 11.5s | — |

增量靠两层：FileModel 缓存（键 = 路径 + mtime + 内容 hash）+
**增量渲染**（只重渲"改动 ∪ 影响闭包"内的模块页与图）。分层或注解有变化时
自动退回全量，并把原因写进诊断。

## 关于本仓

本仓是发布仓（源码 + CI + 授权），内部开发与测试在私有主仓进行。
CI 覆盖：语法检查（compileall）、端到端冒烟（对示例仓库出产物）、机密扫描。

## License

[Apache License 2.0](LICENSE)
