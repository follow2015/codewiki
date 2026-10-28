# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按语义化版本。
**数据包 schema 每次变更必须单独成行并写明升级方式**（规则见 `CONTRIBUTING.md`）。

## [Unreleased]

### 数据包 schema

- 无变更：`SCHEMA_VERSION` 仍为 `1`。

### 新增

- 标注 Prompt 增加**上下游邻居**段：新模块 `codewiki/ai/neighbors.py` 收集模块级
  一跳 import 关系（上游谁依赖它、下游它依赖谁，带边类型；按路径排序、每方向上限 12，
  被截掉的数量写在文本里），`PROMPT_VERSION` 升为 `annotate-v3`，缓存键新增
  `neighbors_hash`。此前只喂本模块源码，模型看不出它在链路中的位置，注解只能写成
  "这是一个工具模块"；而缓存键只含文件内容时，本模块一字未改、上下游却变了会继续
  复用旧答案 —— 这是"注解与图谱对不上"最隐蔽的来源。
- 客户端（`ai/client.py`）支持原生 `tools` / `response_format`，`ChatResult` 增加
  `tool_calls` / `finish_reason` / `degraded`。网关不认这两个字段（400/422）时自动
  **去掉扩展重发一次**，把降级如实记在结果上，并记住本次被拒的字段（后续不再重发，
  省掉每个模块一次 400）。`ai-annotate` 由此要求 `response_format={"type":"json_object"}`；
  `agent` 支持原生 `tool_calls`（单轮最多执行 4 条，观察同样按不可信数据包裹），
  客户端声明不支持时自动退回文本 JSON 协议。
- 标注解析失败**带错误原因重试一次**：`_parse_suggestions` 改为返回 `ParseOutcome`
  （区分"答了但格式坏"与"答无法确定"），失败时把错误 + 上一轮原文回喂；重试成功记
  `AI_PARSE_RETRY`，仍失败记 `AI_PARSE_FAILED` 并**不写缓存** —— 缓存一段解析不了的
  原文等于把该模块永久锁死在失败状态。
- `query_graph` 的返回补上**边类型**：`edge_types` 聚合计数 + 逐边
  `{from,to,type}` 明细（上限 40 条，`edges_truncated` 写明被截掉多少）。只给"边数"
  时模型分不清 `IMPORTS`（静态导入）与 `CALLS` / `USES_DYNAMIC`（运行期耦合），
  判断不了改动影响面，只能回去 grep 白烧一轮。
- `ai-annotate --deferred DIR`：把"调模型"换成**文件回放**，由上层 agent（WorkBuddy /
  CodeBuddy 等自带模型的环境）自己作答。缺答案时把请求写成
  `DIR/requests/<purpose>-<key>.json`（内含完整 messages、作答协议与输出形状），命令以
  退出码 3 结束；agent 写好 `DIR/answers/<同名文件>` 后重跑同一条命令即命中并跑完。
  全程**不读 `OPENAI_API_KEY`、不发网络请求**。请求指纹为
  `sha256(purpose + model + messages)`：改提示词、改源码或换模型都会让旧答案失效，
  不会拿昨天的结论回答今天的问题。答案仍按**不可信输入**处理（target 必须是本模块、
  evidence 行号必须真实存在），写文件的一方绕不过这两道校验。
- `agent` 首轮消息带**项目概览**（分层、按依赖度数排序的热点模块、**仅 human/reviewed
  的注解**）。数据包里早就算好了这些事实，此前首条消息只有"问题：…"，模型头几轮都在用
  工具重新摸仓库。概览内容来自被分析仓库，同样按不可信数据处理（截断 + 分隔符中性化 +
  model 档脱敏）。
- 两个**图谱发现**工具：`list_modules`（按层列模块、度数降序）与 `find_symbol`（按名字
  找符号，直接给 `path:line`）；`query_graph` 找不到节点时改喂"相近 id 候选"，而不是
  只回一句"节点不存在"。`query_graph` 要求精确 `mod::pkg/a.py`，而模型开局并不知道有
  哪些 id，此前只能盲搜再拼。
- **答案引用核对**：抽出答案里的 `file:line` / `file:a-b`，校验文件在仓库内（复用
  `guard.resolve_in_root`）、行号落在文件范围内、且不是密钥文件。不通过时让模型用
  `read_file` 核实后改一次（占用一轮，最后一轮不再纠错），仍不通过则带
  `AI_CITATION_INVALID` 告警**交付原答案**，绝不吞答案。此前提示词要求"结论必须引用
  file:line"，但没有任何代码去核对 —— 编造 `x.py:9999` 也会原样交付。

### 变更

- JSON 抽取从贪婪正则 `\{.*\}` 改为**括号配平**（新模块 `codewiki/ai/jsonx.py`）：
  扫描时跳过字符串字面量与 `\"` 转义，取第一个**能解析成对象**的片段，失败时返回
  可回喂的原因（"花括号未配平" / "JSON 解析失败：第 N 行第 M 列" / "顶层不是对象"）。
  贪婪匹配在"先解释再作答"或"一轮给了两个对象"时会把整段吞下 —— 模型明明答对了
  却被判成解析失败，`agent` 与 `ai-annotate` 都受影响。
- 标注取证片段改为**带行号**（`   12| code`）。`read_file` 的原始输出不带行号，模型
  此前只能自己数行 —— `evidence` 的"行号存在于文件范围"校验因此形同虚设（数错了只是
  换一个位置，仍然通过）。`PROMPT_VERSION` 随之升为 `annotate-v2`，v1 缓存不再复用。
- `configs/ipip.toml` 合入 30 条 AI 架构注解（`source = "ai"`；人工那 1 条
  `mod::app/services` 属受保护条目，原样保留）。合入前逐条核对：target 必须是产物里的
  真实模块节点，70 处"标识符 + 行号"与 33 处行号列表必须命中真实源码行 —— 核对后
  标记 `reviewed = true`，此后 `merge-annotations` 视其为已审阅、不再被 AI 覆盖。
- `.gitignore` 补 `.ai-queue/` 与 `*.suggest.toml`。前者是 `--deferred` 的 `requests/`
  与 `answers/`，带待问内容与源码片段；后者在被审阅、合进 `codewiki.toml` 之前只是候选。
  两者都不该随仓库分发。

### 修复

- 标注证据核对从"行号存在"升级为"行号存在**且**能支持结论"。此前判据是"任一条证据
  的行号落在文件范围内即通过"，于是一条指向 `README.md:1` 的证据可以为任何结论背书。
  现在从 note 抽**代码名锚点**（反引号内容、`snake_case`、`CamelCase`、全大写常量），
  要求所引行区间内至少命中一个：`note` 说 `mark_stale` 却引 `class Base:` 所在行，
  行号虽真却证明不了结论 —— 判 `AI_EVIDENCE_UNSUPPORTED` 并丢弃。纯中文结论抽不出
  锚点时如实退化为存在性核对（不假装核过支持关系），边界写在 `check_evidence` 文档里。
- 模块页的审阅标记改为小写（`AI（reviewed=false）`）。此前把 Python 的 `bool` 直接插进
  f-string，页面上是 `reviewed=False` —— 与配置里的 `reviewed = false` 字面不一致，
  读页面的人会把它当成另一个字段名。
- 缺答案不再中断整轮标注：`--deferred` 的"待作答"与 `AI_UNAVAILABLE` 分属两种后果，
  前者继续问下一个模块（否则 30 个模块要来回跑 30 遍才能凑齐请求清单），后者才停。
- FileModel 缓存目录（`.codewiki/cache/`）首次创建即写入 `.gitignore`（内容 `*`），与 AI
  缓存同构。此前该目录不带忽略文件，一次 `git add -A` 就会把缓存连同源码提交；缓存跟着
  clone 走之后，解析器改了行为而 `parser_version` 忘了 bump 时仍会命中旧 `FileModel` ——
  测试全绿，产物是旧的。测试也不再往仓库内夹具写缓存（CLI 侧 `--no-cache`、pipeline 侧
  `no_cache=True`），跑完 `find tests/fixtures -name .codewiki` 为空。
- `from pkg import sub`（`sub` 是子模块）的 `IMPORTS` 边指向子模块本身，而不是
  `pkg/__init__.py`。Python 的语义是先找包属性、找不到再当子模块导入；此前一律落到
  `__init__.py`，包没有 `__init__`（命名空间包）时更是整条退化成 `ext::pkg` —— 图上少
  一条真实依赖，度数、热点排序、AI 语境都跟着失真。`__init__` 里确实有同名符号时仍按
  "符号优先"解析。

## [1.0.1] - 2026-10-28

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
- **CLI 拆分为包**：`cli.py` 1052 行 → `cli/__init__.py` 29 行 + 19 个模块
  （13 个子命令各一模块），分析主体 230 行搬进 `codewiki/pipeline/analyze.py`
  （`AnalyzeRequest` + `run()`，去 argparse 化）。产物经 `diff -rq` 逐字节全等。
- 解析注册表改为**按轮注入**（`AnalyzeRequest.registry`，缺省每轮
  `build_registry()`），不再共享进程级单例 —— "哪个后端解析了文件"由此可测可观测。
- 解析后端黄金从 Python 两个后端扩到三种语言四个后端（python ast/regex、
  typescript/go treesitter），并声明 regex 的降级边界：不产出调用/继承事实。

### 修复

- `packet_from_parts` 下沉到 `codewiki.packetio`，解开 `graph → render` 依赖环。
- `scripts/sync-to-deploy.sh` 去掉 bash 4 依赖（本机只有 3.2），并修掉白名单通配符
  被 shell 提前展开导致**子包整体漏同步**的问题（脚本仍 rc=0、仍打印"已覆盖"，
  只有公开仓 CI 才照出原形）。
- 打包：`packages.find` 的 `namespaces` 默认开启会把 `codewiki-deploy/` 副本整棵树
  打进 wheel，已关闭。
- `Completed.truncated` 此前用 `len(stdout) > max_output` 判断，恒为假。
- **发布链路接线**：`release.yml` 补进同步白名单（此前部署仓里根本没有它，
  "打 tag 就发布"在真实发布仓不存在）；tag 版本校验与 wheel 校验从内联
  `python -c` 抽成 `scripts/check_release_tag.py` / `scripts/verify-wheel.py`
  共享脚本，本地可预先验。
- 新增 `git_log` / `git_show` 有类型工具，`--allow-shell` 可退役；mermaid 渲染
  显式写死 `securityLevel:"strict"`。
- Gitea CI 修复：自建实例把 `uses: owner/repo@ref` 短写法解析到实例自身导致
  job 0 秒即红，改用完整 URL；手写依赖清单加漂移守卫（pyproject 声明的依赖
  必须出现在 workflow 安装清单里）。

## [1.0.0]

首个发布版本：`analyze` / `doctor` / `export` / `serve` / `cycles` / `diff` /
`suggest-layers` / `merge-layers` / `import-graph`，可选 AI 层
（`ai-annotate` / `merge-annotations` / `agent`），静态 HTML 站点与离线 Zip 交付。
