# 前沿资源清单：多 LLM Lean 4 sorry 消解引擎（v40）可直接集成的开源资源

- 调研日期：**2026-07-19**（所有"最新活动"以 GitHub/PyPI API 返回值为准）
- 调研方式：GitHub REST API（鉴权 token `ghp_KoWpt…`，已脱敏）、PyPI JSON API、目标站点 HTTP 探测、公开 Web 资料
- 验证状态图例：**已核实**（API/站点直接验证）｜**部分核实**（多来源第三方一致，但未核对官方页）｜**未证实**（单一来源或无法访问）
- 集成成本：S < 1 天｜M = 数天｜L = 1 周以上

---

## 1. SorryDB —— 真实世界 sorry 任务源 + 验证器 + 排行榜

| 项 | 值 |
|---|---|
| 主仓库 | https://github.com/SorryDB/SorryDB （53★，**Apache-2.0**，默认分支 master） |
| 数据仓库 | https://github.com/SorryDB/sorrydb-data （7★，无许可证声明） |
| 主页/排行榜 | https://sorrydb.org ｜ 论文 arXiv:2603.02668（**ICML 2026 接收**） |
| 最新活动 | 主仓库 push **2026-06-22**；数据仓库 push 2026-01-11；PyPI `sorrydb` 0.1.1（2025-06-21） |
| 验证状态 | **已核实**（GitHub/PyPI/站点/后端 API 均直接验证） |

**数据集格式（已核实，doc/DATABASE.md）**：单文件 JSON（非 JSONL），`{"repos": [...], "sorries": [...]}`。每条 sorry：`repo{remote, branch, commit, lean_version}`、`location{path, start_line/column, end_line/column}`、`debug_info{goal（pretty-printed 证明状态）, url}`、`metadata{blame_email_hash, blame_date, inclusion_date}`、`id`。信息足以 `git clone + checkout + lake exe cache get + lake build` 本地复现。

**可用数据产物**：
- `sorry_database.json`（64.8 MB，全量）
- `deduplicated_sorries.json`（6.4 MB，去重）
- `static_100_varied_recent_deduplicated_sorries.json`（125 KB，100 条多样近期 sorry —— **适合冒烟测试**）
- 论文快照 `SorryDB_2601`：2601 条 + 官方 1000 条评估划分（仓库 `data/SorryDB_2601/`）

**排行榜/API（已核实 2026-07-19）**：sorrydb.org 为 SPA；后端 API 存活：`GET https://myapi-754129481175.us-central1.run.app/leaderboard?limit=N` 返回 JSON（rank/agent_id/agent_name/completed_challenges；当前条目尚少，比赛刚起步）。仓库内 `leaderboard_deployment/`（Dockerfile + compose）+ `sorrydb/leaderboard/api/{agents,challenges,leaderboard}.py`（FastAPI + Postgres），可自行部署或注册 agent 参赛。

**关键可复用模块**：`sorrydb/database/sorry.py`（pydantic 数据模型）、`sorrydb/utils/verify_lean_interact.py` 与 `verify.py`（**独立验证器：在原项目里编译候选证明**）、`sorrydb/utils/repl_ops.py`、`sorrydb/strategies/agentic_strategy.py`、`sorrydb/cli/run_{llm,tactic,rfl}_agent.py`（基线 agent）。注意其依赖含 `lean-interact==0.10.5`、langchain/langgraph、modal/morphcloud（云沙箱执行）。

**与 v40 集成建议**：
- 任务拉取模块：直接下载 `deduplicated_sorries.json` 或 SorryDB_2601 评估划分 → 喂给调度器（**S**）
- 验证模块：`pip install sorrydb`，复用其 verifier 或对照实现自己的 `verify_proof()`（**S/M**）
- 排行榜对接：实现其 agent 协议提交结果，获取公开可比成绩（**M**）
- 注意：sorrydb-data 最后更新 2026-01-11，"nightly"更新宣称与实际有滞后（**未证实**是否已恢复）；快照 SorryDB_2601 稳定可用

---

## 2. LeanDojo / LeanDojo-v2 / ReProver

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/lean-dojo/LeanDojo （819★，MIT）｜后继 https://github.com/lean-dojo/LeanDojo-v2 （111★，Apache-2.0） |
| 最新活动 | LeanDojo 最后 commit **2026-01-18："Update README to indicate LeanDojo is deprecated"**；最新 release **v4.20.0（2025-06-13）**；PyPI `lean-dojo` 4.20.0（2025-06-30）；LeanDojo-v2 push 2026-04-26 |
| 验证状态 | **已核实** |

**Issue #250（stdin 空读崩溃，已核实）**：标题 "lean_dojo_repl not working"，2025-07-23 创建，**仍 OPEN，2 条评论，无官方修复**。根因：`Lean4Repl.lean` 的 `loop` 在无输入时解析空字符串 → `failed to parse JSON offset 0`，使 `Dojo.__enter__()` 的 `json.loads(self._read_next_line()[0])` 崩溃。附最小复现（durant42040/lean4-example）。**因仓库已弃用，预计不会修**；社区 workaround = 迁移到 LeanDojo-v2 或改用 repl/LeanInteract/PyPantograph。

**ReProver 独立调用**：检索器为 byt5-small encoder（HF `kaiyuy/leandojo-lean4-retriever-byt5-small`）+ 前缀语料 FAISS 检索，理论上可脱离 Dojo 单独 `transformers` 加载做 premise 检索（LeanSearch v2 论文即以 HTTP/encoder 方式单独跑 ReProver 基线，证实可行）。本环境无法访问 HF API，模型页现状**未证实**；且该检索器基于 LeanDojo Benchmark（Lean v4.3 时代语料），对新 mathlib 覆盖有限。**建议优先用第 4 节的在线前提选择服务，而非自建 ReProver**。

**与 v40 集成建议**：**不建议**新接入 LeanDojo v1（已官宣弃用，仅能跑到 Lean v4.3.0-rc2+ 的老接口）。若需其数据抽取/`Dojo` 交互语义，评估 **LeanDojo-v2**（"end-to-end training/eval/deployment 框架"，活跃）或直接 LeanInteract（**M**）。

---

## 3. 轻量 Lean REPL / 验证工具

### 3a. leanprover-community/repl（Lean 4 REPL）
- URL：https://github.com/leanprover-community/repl ｜ 214★，**Apache-2.0**，push **2026-07-16**（持续跟进 toolchain：最新 bump 到 v4.33.0-rc1）
- 接口：JSON over stdin/stdout，空行分隔命令；command 模式（返回 env/sorries/proofState/messages）、tactic 模式（按 proofState id 下发 tactic）、file 模式（`{"path": ..., "allTactics": true}`）。**原生报告 sorry 及其 goal —— 与 sorry 消解场景天然匹配**
- 需按目标项目 `lean-toolchain` 选用对应版本的 repl；SorryDB 数据集的 `lean_version` 字段即为此设计
- 集成：**S/M**（`lake exe repl` 起子进程，异步管道即可；v40 需为每个任务构建目标仓库）
- 验证状态：**已核实**

### 3b. LeanInteract（pip: lean-interact）—— ★推荐
- URL：https://github.com/augustepoiroux/LeanInteract ｜ 125★，**MIT**，push **2026-07-17**；PyPI `lean-interact` **0.11.5（2026-07-16）**
- 功能：Python 异步友好地封装 repl；支持 Lean `v4.8.0-rc1`–`v4.32.0-rc1`（旧版本 backport 最新 repl 特性）；临时项目/依赖实例化（可直接拉 Mathlib 依赖的 benchmark 环境）；**增量+并行 elaboration（v0.9+）**；声明/info tree 抽取；环境/证明状态 pickling（断点续证明）
- **SorryDB 官方验证器即构建于其上**（`verify_lean_interact.py`），生产验证过
- 集成：作为 v40 的"Lean 执行/验证后端"（`pip install lean-interact`），**S**
- 验证状态：**已核实**

### 3c. PyPantograph（原 lenianivas/Pantograph，已迁移）
- URL：https://github.com/stanford-centaur/PyPantograph ｜ 143★，**Apache-2.0**，push **2026-06-30**；leanprover/Pantograph 为官方镜像（74★，2026-06-15）；论文 arXiv:2410.16429
- 功能：机器对机器 Lean 4 交互：`goal_tactic`（编程式 tactic 执行）、**metavariable coupling 处理**（子目标间依赖，repl 不具备）、整文件 conformity 检查（`check_track`）、常量检查；附 MCTS 证明搜索示例
- 安装：`uv add git+https://github.com/stanford-centaur/PyPantograph`（含 Lean 子模块，需 elan）
- 集成：适合 v40 中需要"搜索式证明器"（MCTS/子目标分解）的路线；成本 **M**
- 验证状态：**已核实**

### 3d. Reservoir（任务源补充）
- https://reservoir.lean-lang.org ｜ HTTP 200（**已核实**）。Lean 官方包注册表；SorryDB 即爬它找活跃 Lean 4 仓库。v40 可用其 API 扩充 sorry 任务池（配合 §1 的 crawler 思路）
- 集成成本：M（需实现 clone+build+cache 管线，或复用 SorryDB 的 `sorrydb/cli/update_db.py`）

---

## 4. 前提选择 / 语义搜索服务（全部在线，2026-07-19 探测 HTTP 200）

| 服务 | 端点（已核实来源） | 认证/费用 | 特点 |
|---|---|---|---|
| **LeanSearch** | `POST https://leansearch.net/search`（JSON body，端点取自 leanprover-community/LeanSearchClient 源码） | 免费、客户端代码无鉴权 | 自然语言→Mathlib 定理；v1 由 frenzymath/LeanSearch（57★，Apache-2.0，2026-01-23）支撑 |
| **LeanSearch v2** | 论文 arXiv:2605.13137（2026-05）称"standard mode 公开 API 可用"；代码 https://github.com/frenzymath/LeanSearch-v2 （17★，Apache-2.0，push 2026-05-18） | 免费（**部分核实** API 形式） | SOTA 检索（nDCG@10 0.62）；另有 reasoning 模式做"全局前提组检索" |
| **LeanStateSearch** | `GET https://premise-search.com/api/search?query=<goal>&results=N&rev=v4.x`（端点取自 LeanSearchClient 源码） | 免费 | **证明状态→前提**，正是 sorry 消解每一步所需；LeanSearch v2 评测中作基线 |
| **LeanExplore** | https://www.leanexplore.com ｜ Python 包 https://github.com/justincasher/lean-explore （75★，Apache-2.0，push 2026-05-07） | 远程 API 需网站注册免费 key；**可下载数据库完全本地自托管** | 语义+BM25+PageRank 混合；索引 Mathlib/Batteries/Std/PhysLean 等；**内置 MCP server**（stdio JSON-RPC，8 个工具），异步 Client，local/remote 双模式 |
| **Moogle.ai** | https://www.moogle.ai 在线（200） | 未证实 | Lean FRO 语义搜索老牌服务；当前 API 接入方式**未证实**（LeanSearchClient 已不含 moogle 后端） |
| **Loogle** | https://loogle.lean-lang.org 在线（200） | 免费 | 语法/模式搜索（按类型签名找引理），与语义搜索互补；LeanSearchClient 支持 |
| **LeanSearchClient** | https://github.com/leanprover-community/LeanSearchClient （34★，Apache-2.0，push 2026-07-16） | — | Lean 侧 `#leansearch`/`#loogle` 语法插件；**其源码即各服务 API 契约的最佳文档** |

**与 v40 集成建议**：前提选择模块 = `httpx.AsyncClient` 并发调 leansearch.net（NL 查询）+ premise-search.com（goal-state 查询），把 top-k 引理名注入 prover prompt；本地可控性要求高时用 LeanExplore local 模式或 MCP server。**成本 S**。均为免费服务，注意加缓存与速率限制。

---

## 5. 多 LLM 编排 / 路由 / 评估框架（2025+ 活跃）

| 框架 | 仓库 | ★ | 最新活动（API） | 许可证 | 适配点 |
|---|---|---|---|---|---|
| **LiteLLM** | https://github.com/BerriAI/litellm | 53.9k | push **2026-07-18** | MIT+企业版混合（GitHub 标 NOASSERTION） | **统一 OpenAI 格式代理 100+ LLM**：DeepSeek/Kimi/LongCat 皆为 OpenAI 兼容端点，LiteLLM 一处配置路由/fallback/重试/**成本追踪** —— v40 模型路由层首选（S） |
| **LangGraph** | https://github.com/langchain-ai/langgraph | 37.6k | push **2026-07-17** | MIT | supervisor 模式（orchestrator LLM 动态调度子 agent）官方范式；持久化状态机适合"证明尝试"长任务；**SorryDB 自身依赖 langgraph**（S/M） |
| **pydantic-ai** | https://github.com/pydantic/pydantic-ai | 18.6k | push **2026-07-18** | MIT | 类型安全 agent 框架，内置 evals；与 v40 的 pydantic 任务模型亲和（S/M） |
| **smolagents** | https://github.com/huggingface/smolagents | 28.4k | push **2026-07-14** | Apache-2.0 | 轻量 code-agent 范式，工具=Lean 验证器调用（S） |
| **AutoGen** | https://github.com/microsoft/autogen | 59.8k | push 2026-04-15 | 代码 MIT/文档 CC-BY（API 检测为 CC-BY-4.0） | 多 agent 会话框架；近 3 月活动放缓（M） |
| ~~RouteLLM~~ | https://github.com/lm-sys/RouteLLM | 5.2k | **push 2024-08-10（停滞）** | Apache-2.0 | 概念可借鉴（成本-质量路由训练），**不建议直接依赖** |

**与 v40 集成建议**：LiteLLM（路由+计费）+ LangGraph supervisor（orchestrator-worker）+ 自建 Lean 验证 tool 节点 = "一个 LLM 做 orchestrator 动态调度其他 LLM"的最短路径。评估侧可借 pydantic-evals 或 LangSmith openevals 记录 pass@k。

---

## 6. DeepSeek / Kimi / LongCat API 现状（2026-07，为模型路由与成本优化依据）

> 来源：价格聚合站（morphllm.com、coworker.ai、techjacksolutions、benchlm.ai、pricepertoken、china-llm，更新日期 2026-06-27 ~ 2026-07-18）与官方仓库/文档。未逐字核对官方定价页的项目标 **部分核实**。

### DeepSeek（platform.deepseek.com，OpenAI 兼容 `https://api.deepseek.com`）
- **当前模型（V4 时代，2026-04-24 起）**：`deepseek-v4-flash`（$0.14/1M 输入 cache-miss、**$0.0028 cache-hit（50× 折扣）**、$0.28 输出；1M 上下文，max output 384K）与 `deepseek-v4-pro`（$0.435/$0.87，cache-hit $0.003625（120×）；原价 $1.74/$3.48，75% 促销 2026-05-22 起转永久）
- **`deepseek-chat` / `deepseek-reasoner` 已是 v4-flash 非思考/思考模式别名，2026-07-24 正式退役**（距今日 5 天）——v40 配置里应立即迁移到 `deepseek-v4-flash` 并在请求体切换 thinking
- 错峰折扣（50–75%）已于 **2025-09-05 终止**；**上下文缓存折扣是主要成本杠杆**（系统提示/工具定义置前且字节级稳定）
- 新账号 5M tokens 免费额度（30 天）
- 状态：**部分核实**（多聚合源一致，引用日期 2026-07-13/14）

### Kimi（Moonshot，`https://api.moonshot.cn/v1` 或 .ai，OpenAI 兼容）
- 名录（2026-07）：`kimi-k3`（新旗舰，$3/$15，cache-hit $0.30，1M 上下文）｜`kimi-k2.6`（旗舰，$0.95/$4，256K）｜**`kimi-k2.7-code`**（2026-06-12 发布，coding/agentic，$0.95/$4 官方，OpenRouter/DeepInfra 低至 $0.74/$3.50；262K 上下文；cache-hit $0.19；1T/32B-active MoE，HF 开放权重 modified MIT；已进入 GitHub Copilot）｜`kimi-k2.5`（$0.6/$3）｜`kimi-k2`（0711，$0.55/$2.20，128K）
- **未发布 Batch API 折扣档**（benchlm.ai 2026-07-18 明示）
- 开源侧：MoonshotAI/Kimi-K2（10.9k★，modified MIT）、Kimi-K2.5（2.1k★）；kimi-cli（9.4k★，Apache-2.0，2026-07-16 活跃）
- 状态：**部分核实**

### LongCat（美团，`https://api.longcat.chat/openai/v1`，OpenAI 兼容；key 于 longcat.chat/platform/api_keys）
- 免费额度：社区实测报道 **每日刷新 500 万 → 5500 万免费 tokens**（2026-02/03 多篇中文技术博客一致；**未证实**官方条款）
- API 模型名录（2026-03 起）：`LongCat-Flash-Chat`、`LongCat-Flash-Thinking`、`LongCat-Flash-Thinking-2601`、`LongCat-Flash-Lite`、`LongCat-Flash-Omni-2603`；pricepertoken 列 LongCat-Flash-Chat $0.00（2026-07-18）
- **LongCat-2.0**：2026-06-30 前后开源（GitHub meituan-longcat/LongCat-2.0，486★，**MIT**，push 2026-07-08）：1.6T MoE/48B active、原生 1M 上下文、LSA 稀疏注意力；权重 HF `meituan-longcat/LongCat-2.0`；OpenRouter 可用；曾以匿名 "Owl Alpha" 在 OpenRouter 跑量
- 状态：免费额度与价格 **部分核实/未证实**；仓库与许可证 **已核实**

### LongCat-Flash-Prover —— 与 v40 直接相关的专项证明模型 ★
- https://github.com/meituan-longcat/LongCat-Flash-Prover ｜ 91★，**MIT**，push 2026-05-09；技术报告 arXiv:2603.21065
- 560B MoE，Lean4 **原生形式推理**（autoformalize/sketch/prove 三能力 + TIR 工具集成推理，直接调 Lean4 编译验证）
- 成绩（自报）：**MiniF2F-Test 97.1%**（72 次采样/题）、ProverBench 70.8%、PutnamBench 41.5%、MathOlympiad-Bench 46.7% —— 开放权重 SOTA
- 权重：HF `meituan-longcat/LongCat-Flash-Prover` + ModelScope 镜像（国内可达性好）
- 与 v40 集成：作为"专家 prover 模型"一路（self-host vLLM/SGLang，或经 OpenRouter 类渠道）；560B 自托管成本高 → 建议先 API 化试用，成本 **M/L**

---

## 7. Kaggle 部署

| 主题 | 结论 | 来源/状态 |
|---|---|---|
| 秘密管理 | `from kaggle_secrets import UserSecretsClient; UserSecretsClient().get_secret("LABEL")`；在 Notebook 编辑器 Add-ons → Secrets 配置；**仅在 Kaggle Notebook 运行时内可用**（本地 import 报 ModuleNotFoundError）；fork 公开 notebook 不会带走 secret | Kaggle 官方公告（product-feedback/114053，2019 起，2024-10 再推）**已核实** |
| 12h 限制 | 单次 session **最长 12 小时，无论如何运行都一样**（Kaggle 员工 Dustin 明示）；交互式另有约 40 分钟空闲超时 → 长任务用 "Save Version → Run All" 后台跑；GPU 配额 **30 小时/周**（2×T4 / P100 / TPU v3-8），周六重置 | kaggle.com 问答帖（权威=A）**已核实** |
| 装 elan/Lean | 现成脚本参考 GitHub `Kaggle/kaggle-api` issue #595：`apt-get install git libgmp-dev cmake ccache clang` → `elan-init.sh -y --default-toolchain leanprover/lean4:vX.Y.Z` → `lake new/update/build`（示例用 v4.7.0，模式至今适用；新 toolchain 需自测） | **部分核实**（脚本真实存在，未实跑验证） |
| 加速建议 | Kaggle 上跑 v40：`pip install lean-interact`（自动管理 Lean 设置）+ SorryDB 的 `lake exe cache get` 拉 mathlib cache；12h 内可完成单仓库 build + 批量 sorry 尝试；用 kaggle_secrets 注入 DeepSeek/Kimi/LongCat key | 推断（基于 §3b/§1 已核实事实） |

---

## Top-5 "立即集成"清单

1. **LeanInteract**（`pip install lean-interact`，MIT，2026-07-16 仍发版）—— v40 的 Lean 执行/验证后端：异步驱动 repl、增量并行 elaboration、临时项目、状态 pickling；SorryDB 官方验证器同款底座。**成本 S，已核实**。
2. **SorryDB 数据集 + 验证器 + 排行榜**（Apache-2.0）—— 真实 sorry 任务源（2601 条快照/1000 条官方评估划分/125KB 冒烟集），pydantic 任务模型与独立 verifier 可直接复用，另有活跃排行榜 API 可做公开对标。**成本 S（数据）/M（参赛），已核实**。
3. **前提选择三件套**：`POST leansearch.net/search` + `GET premise-search.com/api/search` + LeanExplore（local/MCP）—— 免费 HTTP 语义检索，直接提升各 LLM 的引理命中率；纯 httpx 异步接入。**成本 S，已核实**。
4. **LiteLLM 代理层**（MIT 核心，2026-07-18 活跃）—— 统一接 DeepSeek-v4-flash/pro、Kimi k2.7-code/k3、LongCat（均 OpenAI 兼容），自带路由/fallback/成本追踪；配合 DeepSeek 50× 缓存命中折扣与 LongCat 每日免费额度做成本优化。**成本 S，已核实**。
5. **LongCat-Flash-Prover**（MIT 权重，HF+ModelScope）—— 560B Lean4 专项证明模型（MiniF2F 97.1%），作为 v40 多 LLM 阵容中的"形式推理专家"一路，先经 API/OpenRouter 试，必要时自托管。**成本 M/L，已核实**（权重/许可证；成绩为厂商自报）。

> 备选第 6：LangGraph supervisor 范式（orchestrator-worker 编排，SorryDB 同栈），若 v40 编排层尚未定型则优先度可上调。

### 排雷提示
- **勿新接 LeanDojo v1**：已官宣 deprecated（2026-01-18），issue #250（stdin 崩溃）OPEN 无修复；迁移方向 LeanDojo-v2 或 LeanInteract。
- **勿依赖 RouteLLM**：2024-08 停更。
- `deepseek-chat`/`deepseek-reasoner` 模型名 **2026-07-24 退役**，立即切换到 `deepseek-v4-flash` + thinking 开关。
- Moogle.ai 在线但 API 接入方式未证实；LeanSearchClient 已不含其后端。
