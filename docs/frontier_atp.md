# Lean 4 自动定理证明前沿调研（2025–2026）

- 调研日期：2026-07-19（所有来源均于该日访问核实）
- 服务对象：v40 多 LLM Lean 4 sorry 消解引擎（Orchestrator=DeepSeek / Prover=DeepSeek / Critic=Kimi / Explorer=LongCat；验证=subprocess `lake env lean` 默认 + lean-dojo 可选；策略=rfl → LLM 直证 → beam tactic 搜索 → AxProverBase-v2 agentic 循环）
- 说明：数字均为原文声明值；未能核实的标 **未证实**。

---

## 1. 开源证明模型

### 1.1 DeepSeek-Prover-V2（DeepSeek-AI）
- 来源：arXiv:2504.21801（2025-04-30，v2 2025-07-18）；https://github.com/deepseek-ai/DeepSeek-Prover-V2；权重 HF `deepseek-ai/DeepSeek-Prover-V2-7B` 与 `-671B`（可下载）。
- 核心思想：DeepSeek-V3 将难题分解为子目标（proof sketch + sorry 占位），7B 模型递归证子目标，合成"informal CoT + 完整 Lean 证明"冷启动数据，再做二元奖励 RL。**Whole-proof 生成**，不是 tactic 模式。
- Prompt 格式（官方 README，已核实）：
  ```
  Complete the following Lean 4 code:
  ```lean4
  <import Mathlib ... theorem ... := by sorry>
  ```
  Before producing the Lean 4 code ..., provide a detailed proof plan outlining the main proof steps and strategies.
  ```
  即 CoT 模式=先出证明计划再给完整代码；非 CoT 模式只要求补全代码。
- 实测：671B miniF2F-test 88.9%（pass@8192），PutnamBench 49/658，ProverBench-AIME 6/15；7B miniF2F 82.0%（官方 README 图）。
- v40 落地：7B 权重可直接作为 Prover 角色本地/推理端点模型，prompt 模板照抄官方（whole-proof + proof plan）；其"子目标分解→7B 证子目标→合并"pipeline 与 v40 的 AxProverBase 循环同构，可借鉴其数据格式沉淀成功证明为 memory。成本：低（HF 权重 + 现成模板）。

### 1.2 Kimina-Prover（Project Numina × Kimi/Moonshot）
- 来源：arXiv:2504.11354（Preview，2025-04）；正式版 72B 权重已发布：HF `AI-MO/Kimina-Prover-72B`（及 Preview-Distill-7B/1.5B）。发布日期精确日 **未证实**（HF 页存在，第三方聚合页 2025-11 收录）。
- 核心思想：纯 RL 长 CoT，不用外部搜索；模型内部"结构化形式推理模式"（自然语言推理与 Lean tactic 块交错），训练用 Kimina Lean Server 做验证。
- 实测：Preview-72B miniF2F 80.7%（pass@8192，当时 SOTA，超 BFS-Prover 72.95%）；正式版 72B：84.0%@pass32（第三方聚合页，权威度中等）；2026-07 综述表记 92.2%（2025-07，高预算）。
- 修正了 miniF2F 中 8 个不可证/错误命题并发布修正版数据集（HF AI-MO）。
- v40 落地：Critic=Kimi 角色与 Kimina 同源——可用 Kimina-Distill-7B 作为"证明专用 Prover 模型"补充 DeepSeek；其"推理块+tactic 块交错"输出格式适合作为 Prover 的输出 schema（便于 Critic 定位错误步骤）。成本：低-中。

### 1.3 Goedel-Prover-V2（Princeton/NVIDIA 等）
- 来源：arXiv:2508.03613（2025-08-05）；权重 HF `Goedel-LM/Goedel-Prover-V2-8B` / `-32B`，另有 `Goedel-Formalizer-V2-8B`、`Goedel-Autoformalizer-8B`（代码+数据全开源）。
- 核心思想：expert iteration + 三招：(1) 阶梯难度合成数据；(2) **verifier-guided self-correction**——把 Lean 编译器报错喂回模型串行改证（每轮改一次）；(3) model averaging 防止多样性塌缩。RL 用混合 GRPO（去组归一化、clip-higher、动态采样仅保留 pass rate ∈ (0,0.75] 的题）。
- 实测：8B miniF2F pass@32 = 84.6%（超过 671B 的 DSP-V2 同指标）；32B 标准模式 88.1%@pass32、自我纠错模式 90.4%；PutnamBench 86 题@pass184（开源第一）。第三方复现：self-correction 把 token 开销放大 ~60×（EconProver 论文）。
- v40 落地：**self-correction 协议可直接移植**：verify 失败时把编译器 error+proof state 附在下一轮 prompt，串行修正而非重采样——v40 axprover.py 的 propose→verify→critic 循环加上"带报错续写"模式即可。推荐采样配置（官方）：temperature 0.7、max_tokens 30000、pass@32。成本：低。

### 1.4 BFS-Prover / BFS-Prover-V2（字节 Seed）
- 来源：V1 arXiv:2502.03438（2025-02，开源权重）；V2 arXiv:2509.06493（2025-09）。
- 核心思想（V1）：证明 best-first tree search（不用 MCTS/value function）即可 scale——(1) expert iteration 每轮过滤掉 beam search 已可解的题；(2) 用编译器报错自动标注的 state-tactic 对做 DPO；(3) **长度归一化打分**鼓励深路径。
- 核心思想（V2）：训练侧多轮 off-policy RL（多阶段 expert iteration + 周期性重训破平台期）；推理侧 **Planner-Prover 多智能体树搜索**：通用推理 LLM 当 Planner 分解子目标，多个 Prover agent 并行证，共享 Subgoal Cache，失败触发动态重规划。
- 实测：V1 miniF2F 72.95%（7B）；V2 miniF2F 95.08%、ProofNet 41.4%（均为当时 SOTA 级）。
- v40 落地：tactic_search.py 的 beam 搜索打分改为 BFS-Prover 式 `Σ log p(a_t|s_t) / L^α`（α∈[0,1] 长度归一）；Planner(DeepSeek-Orchestrator) + Prover 并行 + 共享子目标缓存正是 v40 多角色架构的直接升级路径。成本：打分函数=极低；planner 架构=中。

### 1.5 Seed-Prover / Seed-Prover 1.5（字节 Seed）
- 来源：arXiv:2507.23726（2025-07-31）；1.5: arXiv:2512.17260（2025-12-19）。权重**未开源**。
- 核心思想：**lemma-style whole-proof**——证明中显式引理可复用；基于 Lean 反馈、已证引理、自我总结做**迭代精化**；三档 test-time 策略（light/medium/heavy，按难度分配算力）。1.5 把工具调用（Lean、检索）直接训练进模型（agentic RL），TTS 工作流衔接自然语言证明与形式化。
- 实测：Seed-Prover 证 78.1%（121/155）历届 IMO 形式化题，miniF2F 饱和（99.6%），PutnamBench >50%；IMO 2025 参赛证出 5/6（金牌线）。1.5：PutnamBench 88%、Fate-H 80%、Fate-X 33%、Putnam 2025 9 小时解 11/12。
- v40 落地：不可直接用权重，但其"三档预算按难度分配"可写进 Orchestrator 动态策略 JSON（简单题走 light=rfl+直证，medium=beam，heavy=agentic 循环）；"已证引理入库供后续复用"对应 axprover memory。成本：低（策略层）。

### 1.6 LongCat-Flash-Prover（美团 LongCat）
- 来源：arXiv:2603.21065（2026-03）；权重 HF `meituan-longcat/LongCat-Flash-Prover`（560B MoE，开源），项目页 github.com/meituan-longcat/LongCat-Flash-Prover。
- 核心思想：Native Formal Reasoning——把自动形式化、sketch、proving 三能力合一；agentic 工具集成 RL（HisPO 算法）；定理一致性与合法性检测防 reward hacking。输出含 lemma-style sketch 与 whole-proof 两形态。
- 实测：miniF2F-test 97.1%（仅 72 次推理预算/题）；ProverBench 70.8%、PutnamBench 41.5%（≤220 attempts），宣称开源权重 SOTA。
- v40 落地：v40 Explorer 已是 LongCat——若有 API 访问该 Prover 变体，可直接替换/增强 Prover 角色；其 sketch 输出格式可作 Explorer→Prover 的中间表示。成本：中（560B 自托管贵，走 API 则低）。

### 1.7 其他开源 prover（简记）
- **Leanabell-Prover-V2**（快手，arXiv:2507.08649）：DSP-V2/Kimina-Distill-7B 基础上 Lean 反馈 RL，miniF2F 78.2%（7B）。
- **InternLM2.5-StepProver**（arXiv:2410.15700）：大规模 expert iteration + critic-guided 搜索，miniF2F 65.9%。
- **STP**（Dong & Ma，arXiv 2025-02）：self-play"猜想+证明"双角色迭代，miniF2F 65.0%——self-play 角色分工对 v40 Explorer（出题/猜想）有参考价值。
- **Prover Agent**（Baba et al. 2025-10）：8B + lemma 引导 agent 协调，miniF2F 88.1%（据 2026-07 综述表，arXiv 2607.07779；原文编号未核实）。
- 2026-07 综述 miniF2F 排行（arXiv:2607.07779，访问于 2026-07-19）：Hilbert 99.2% / Seed-Prover 99.6% / Delta-Prover 95.5% / Goedel-V2-32B 94.8% / Kimina-72B 92.2%。

---

## 2. 证明搜索算法（2025+）

### 2.1 RMaxTS（DeepSeek-Prover-V1.5，arXiv:2408.08152）
- 思想：MCTS + RMax 内在奖励（展开产生新节点即给奖励），解决证明搜索稀疏奖励下的探索不足；truncate-and-resume 把树搜索嵌入 whole-proof 生成。代码随 V1.5 开源。
- v40 落地：若 beam 升级为 MCTS，用"新状态产生=内在奖励 1"的 RMaxTS 规则可避免早熟收敛；成本中。注：2025 后社区共识是**简单 BFS（长度归一）在 LLM prover 上不输 MCTS**（BFS-Prover 论文核心结论），优先做 BFS 变体。

### 2.2 长度归一化 BFS（BFS-Prover，arXiv:2502.03438；REAL-Prover 复用，arXiv:2505.20613）
- 打分：`score(node) = Σ_{t=0}^{s-1} log p(a_t|s_t) / L^α`，L 为路径长，α∈[0,1]。REAL-Prover 用同一打分 + LeanSearch-PS 检索增强，ProofNet 23.7%（仅 SFT 即超 DSP-V1.5-RL+RMaxTS）。
- v40 落地：tactic_search.py 改打分函数即可（v40 已有每步 logprob 的话改动 <50 行）。成本：极低。**Top 候选。**

### 2.3 Verifier-guided self-correction / 迭代精化（Goedel-V2；SorryDB 论文；EconProver）
- 实测（SorryDB，arXiv:2603.02668）：相同调用次数下迭代纠错 ≫ 并行采样——Gemini Flash 3 agentic 30.3% vs 其 pass@32 仅 20.5%；EconProver：Goedel-V2+IR 达 86.0% 但 token ×60。
- v40 落地：axprover.py critic 环节把 verify 的 stderr/error 原文+失败前 proof state 结构化进下一轮 prompt；设串行修正上限（Goedel 用 ≤4 轮量级，SorryDB 用 16）。成本：低。

### 2.4 证明修复（proof repair）
- **APOLLO**（arXiv:2505.05758，v5 2025-11）：编译器定位失败子引理→隔离→自动求解器+低 top-K LLM 重证→重组复验。实测：Goedel-Prover-SFT 采样预算从 25,600 降到几百即 57.6%→65.6%；o3-mini/o4-mini 3–7%→>40%；<8B 模型 miniF2F 84.9%（2025-08 时 SOTA）。
- **APRIL**（arXiv:2602.02990，ICLR VerifAI 2026）：260k "错误证明+编译器诊断+修复推理 trace" 数据集（HF `uw-math-ai/APRIL`）；微调后单发修复率 Goedel-V2-8B 15.5%→34.6%。
- v40 落地：axprover.py 增加"修复模式"：失败后不整证重生成，而是抽取 error 行号对应子目标单独重证再拼回；APRIL 数据可作 Critic 的 few-shot 库。成本：中。

### 2.5 ProofAug / ERP（arXiv:2501.18310，ICML 2025）
- 思想：对 LLM 初稿找**最大兼容半证明（MCSP）**，sorry 空洞交给 ATP/启发式方法填，ERP 递归证中间子目标，大幅减少采样数。Isabelle 实现为主。
- v40 落地：beam 搜索时把"已验证前缀"固定，只对首个 sorry 之后增量搜索——避免整证重复采样。成本：中。

### 2.6 LeanProgress（arXiv:2502.17925，v3 2026-01；并入 LeanDojo-v2）
- 思想：训练模型预测当前 proof state 距完成还剩几步（准确率 75.8%），用于 best-first 搜索的节点排序；Mathlib4 上 +3.8%（长证明收益更大）。
- v40 落地：**与 v40 progress.py 天然契合**——用"剩余步数估计"作为 beam 宽度/预算分配信号与 kill 准则。成本：低-中（有开源实现）。

---

## 3. Agentic 证明架构（2025–2026）

### 3.1 Aristotle（Harmonic，arXiv:2510.01346，2025-10-01）
- 架构：三大件——(1) 高度并行 Monte Carlo Graph Search（大 transformer 当 policy+value，以 proof state+历史+informal proof 为条件）；(2) lemma-based informal 推理管线（生成自然语言证明→拆引理→形式化→按形式反馈迭代）；(3) 几何引擎。200B+ 参数 + 大规模并行 + 多轮反馈迭代 + test-time training。
- 实测：IMO 2025 金牌线（5/6，形式化 Lean 证明）。**提供公共 API**（harmonic.fun，2025-09 起 early access；2026 年第三方项目报告"项目期内免费"，可脚本提交 lemma 自动回写）。
- v40 落地：若获 API key，可作为 heavy 档外援（类似 v40 调第三方求解器）；其"informal sketch → MCGS 填正式证明"的两层结构验证 v40 的 Explorer→Prover 分工方向。成本：低（API 调用）。

### 3.2 Numina-Lean-Agent（Project Numina，arXiv:2601.14027）
- 架构：**直接用通用 coding agent（Claude Code）当形式数学推理器** + Numina-Lean-MCP 工具集：lean-lsp-mcp（LSP 驱动验证/目标查看）、LeanDex（语义定理检索，leandex.projectnumina.ai）、Informal Prover、Discussion Partner（外部 LLM 讨论）。
- 实测：Putnam 2025 **12/12**（Claude Opus 4.5，全程串行无并行；多数题 ~$50，A5 ~$1000）；关键消融：**迭代精化策略 ≫ 同预算独立采样**（B4 题 5 轮收敛 vs 独立采样 10 轮失败）。
- v40 落地：这是"通用 LLM+MCP 工具"路线的最佳公开范本——v40 的 4 角色可平移为 MCP 工具调用协议；再次确认 critic/verify 反馈闭环优于 pass@N。代码+解答开源（github.com/project-numina/numina-lean-agent）。成本：低-中。

### 3.3 Hilbert（arXiv:2509.22819，2025-09；v2 2026-03）
- 架构：informal LLM + 专用 prover LLM + 形式验证器 + **语义定理检索器**四组件；递归分解+验证反馈改证。
- 实测：miniF2F 99.2%（+6.6pt 超此前公开最佳）、PutnamBench 462/660（70.0%）——公开模型最佳。
- v40 落地：四组件与 v40 四角色一一对应（Explorer≈informal LLM、Prover≈prover LLM、verify≈verifier、检索器待补），其"递归分解只对 prover 解不动的题触发"的闸门策略可直接抄。成本：低-中。

### 3.4 Delta-Prover（字节，arXiv:2507.15225，2025-07-21）
- 架构：通用 LLM（不微调）+ reflective decomposition + 迭代修复 + 自建 DSL 管理子问题。
- 实测：miniF2F-test 95.9%（agent 框架，超过所有当时含微调的方法）；test-time scaling 律显著优于 Best-of-N。
- v40 落地：证明"不微调通用模型+好 agent 结构"路线成立——v40 全通用模型阵容方向正确；其 DSL 式子问题管理（子目标状态机）可用于 axprover memory 结构。成本：中。

### 3.5 BFS-Prover-V2 Planner-Prover（arXiv:2509.06493，见 1.4）
- 多智能体要点：共享 Subgoal Cache（状态：pending/proving/proven）、集中并行（所有 prover 聚焦当前瓶颈子目标）、动态重规划。对 v40 agents.py 是最具体的工程模板。

### 3.6 其他 2025–2026 系统（简记）
- **AX-Prover**（arXiv:2510.12787）：多 agent 工具化证明，跨数学与量子物理；PutnamBench 91 题（据 2604.18587 排行榜）。
- **Mechanic**（arXiv:2603.24465）：sorrifier 驱动分解工作流，Gemini 统一 reasoner/verifier/prover + Kimina Server 验证 + LeanDex/Loogle 检索。
- **Goedel-Architect**（arXiv:2606.06468）：blueprint（引理依赖图）生成→并行证引理→结构化诊断回写→blueprint 精化。
- **Leanstral**（Mistral，2026）：面向 proof engineering 的开源 agent。
- **AlphaProof**（Google DeepMind，Nature 2025）：RL+AlphaZero 式；Test-time 强化——背景参考。

---

## 4. 前提选择 / 检索增强

### 4.1 LeanSearch v2（arXiv:2605.13137，2026-05）
- 双模式：标准模式=层次化 informalized Mathlib 语料 + embedding→reranker 两段管线（nDCG@10 0.62 vs 次优 0.53）；推理模式=sketch→检索→过滤→judge 迭代循环，69 题研究级基准上 46.1% 命中 ground-truth 引理组（vs 推理检索器 38.0%、premise-selection 基线 9.3%）。固定 prover 下换用 LeanSearch v2：证成率 20% vs 次优 16% vs 无检索 4%。
- **可用性：标准模式公开 API——https://leansearch.net/（免费端点，论文明示）；代码/数据开源**（github.com/frenzymath/LeanSearch-v2）。
- v40 落地：Prover/Critic 增加"按自然语言+形式语句检索 Mathlib 引理"工具调用，把 top-K 引理名塞进 prompt（SorryDB 论文同样用 LeanSearch 做 agentic 工具）。成本：低（HTTP）。

### 4.2 LeanStateSearch / premise-search.com（Tao et al. 2025）
- 输入=proof state（目标+假设），HTTP API 返回 top-100 引理；服务端语料在 premise-search.com。在 LeanSearch v2 对比中属于 premise-selection 基线（明显弱于推理式检索）。
- v40 落地：适合 tactic 级 beam 搜索每步的引理建议。成本：低。**注意其 API 长期可用性未证实。**

### 4.3 LeanDex（Project Numina）
- leandex.projectnumina.ai，语义检索 Mathlib 定理/定义；Numina-Lean-Agent 与 Mechanic 均在用。v40 落地同 4.1，可作为备选/并用端点。成本：低。

### 4.4 其他检索工具
- **Loogle**（loogle.lean-lang.org）：按类型签名/模式搜 Mathlib——社区标准工具，免费。
- **LeanFinder**（HF Space，gradio `/retrieve` 端点）：自然语言→Mathlib 声明。
- **ReProver 谱系**（LeanDojo，NeurIPS 2023）：byt5 检索+beam；2026 对比中已明显落后，不建议新投入。
- SorryDB 论文警示：agent 存在**过度依赖检索**的失败模式——有时直接构造证明项更优；建议工具调用设预算上限。

---

## 5. SorryDB 与基准生态

### 5.1 SorryDB（arXiv:2603.02668，ICML 2026；https://sorrydb.org）
- 现状：从 Reservoir 注册的 78 个活跃 Lean 项目自动挖 sorry（含 repo/commit/Lean 版本/行列位置），prop-valued 过滤+去重；**持续更新**。2026-01 快照 SorryDB_2601 = 1000 题（总池 5663）；网站有 leaderboard 与评测管线（替换 sorry→原项目内编译验证）。
- 关键实测（原文）：单发/并行采样全部输给迭代式；最佳单系统=Gemini Flash 3 agentic（ReAct+LeanSearch 工具，≤16 轮）30.3%；**所有 prover 合集 35.7%（互补性强）**；确定性 tactic（`grind`/`simp`/`norm_num`…）能解决一部分 LLM 解不了的题；专用 prover（Kimina/Goedel-V2）在真实项目 sorry 上泛化弱（竞赛题训练分布+Lean 版本错配）。
- 验证协议细节（值得照抄）：替换 sorry 后须满足 (1) 编译通过；(2) sorry 数恰减 1；(3) 其余 sorry 的 proof goal 不变；并警示 `sorryAx` 作弊面（建议配合 leanchecker/SafeVerify 类工具）。
- v40 落地：sorrydb.py 直接接入其快照/API 做持续回归基准（抗污染）；verify/* 增加上述 3 条完整性检查 + axiom 审计（`#print axioms` 拒绝 sorryAx）。成本：低-中。

### 5.2 LeanDojo 生态与 issue #250（已核实）
- **issue #250**（github.com/lean-dojo/LeanDojo/issues/250，2025-07-23，仍 open）：`lean_dojo_repl not working`——Lean4Repl.lean 的 `loop` 在 stdin 无输入/被重定向时读到空串，JSON 解析 fatal 崩溃（`failed to parse JSON offset 0: unexpected end of input`），`Dojo.__enter__` 炸 `DojoCrashError`。
- 维护者 yangky11 于 2025-08-09 回复：与 **issue #211**（Lean ≥v4.12.0 起 tactic 交互故障，根源是 Lean v4.12 引入的 IO 行为变化，Zulip 线程"Weird IO Behavior Introduced in v4.12.0"）同源，"will not be solved anytime soon"。**即：上游不修，无官方补丁。**
- 社区解法（实际采用）：
  1. **改用 LeanDojo-v2**（Hsiang et al., NeurIPS MATH-AI 2025 workshop；PyPI `lean-dojo-v2`）：proof search 改走 **Pantograph**（arXiv:2410.16429）而非 stdin REPL，绕开该 bug；另含 LeanAgent 终身学习、HF/External prover、LeanProgress。
  2. **改用 Lean REPL 生态替代**：官方 `leanprover-community/repl` + **LeanInteract**（github.com/augustepoiroux/LeanInteract，Poiroux et al. 2025；SorryDB 官方验证栈用的就是它）、**Kimina Lean Server**、leanclient / lean-lsp-mcp。
  3. 工程规避：保证子进程 stdin 为打开的 pipe 且永不提前 EOF（不重定向 stdin、不用 `</dev/null`）——权宜之计，非根治（推断）。
- v40 落地：v40 默认 `lake env lean` 整文件编译判定**不受影响**；若启用 tactic 级交互，跳过 lean-dojo 直接用 LeanInteract/Pantograph。成本：低。

### 5.3 基准动态
- **miniF2F-v2**（arXiv:2511.03108；github.com/roozbeh-yz/miniF2F_v2）：修正 300+ 条 Lean 语句（含 v1 中 16 条不可证命题），分 v2s/v2c 两版；端到端（autoformalize+prove）有效准确率 v1 仅 ~40% vs v2 ~70%；指出文献中 autoformalization 准确率被 LLM 评委严重高估（97%→人审 66%）。**v40 自评建议迁移到 v2**。
- Kimina 版 miniF2F 修正（8 题）在 HF AI-MO 仓库。
- **PutnamBench**：排行榜剧变——2026 年 ALEPH（Logical Intelligence）宣称 668/672（$1400 预算，博客，未同行评议）；Seed-Prover 1.5 88%（581/660）；Hilbert 462；开源最佳 Goedel-V2-32B 86 题（pass@184）。
- **ProofNet**：Lean 4 版 186 题 test（DSP-V2 划分）；最佳公开值 BFS-Prover-V2 41.4%。
- 新基准（背景）：CombiBench、FATE 系列（arXiv:2511.02872）、ProverBench（325 题，DSP-V2 发布）、FormalProofBench、MathlibPR（arXiv:2605.07147）。

---

## 6. 实用工程技巧

### 6.1 验证通路的轻量替代（不依赖 lean-dojo）
| 方案 | 来源 | 特点 | 吞吐证据 |
|---|---|---|---|
| **Kimina Lean Server**（REST API + REPL 池 + import 头 LRU 缓存） | arXiv:2504.21230；github.com/project-numina/kimina-lean-server | 多 REPL 进程并行；同 import 头复用热环境；Python client `check()` 批量提交；infotree 抽取 tactic/state | 9419 条证明验证比次优基线快 1.5–2×；缓存使命中验证 0.099s→0.051s（1.94×）；64 核 ~100 次验证/秒（Kimina 论文） |
| **LeanInteract** | github.com/augustepoiroux/LeanInteract（2025） | Python 库包装 Lean REPL，支持并行；SorryDB 官方验证栈 | SorryDB 论文全线使用 |
| **Lean REPL 裸用** | github.com/leanprover-community/repl | JSON-over-stdio；tactic 模式可分步取 proof state（`:= sorry` 触发 Sorry record 拿 goal） | LeanSearch v2 用它抽取 proof state（69/69 成功） |
| **Pantograph** | arXiv:2410.16429 | Lean 4 proof state 的 M2M 接口，支持树搜索；LeanDojo-v2 的交互后端 | LeanDojo-v2 |
| **lean-lsp-mcp / leanclient** | github.com/oOo0oOo/lean-lsp-mcp；github.com/o0o0o0o/leanclient | LSP 驱动：诊断、goal 查看、tactic 建议、库搜索（LeanSearch/Loogle 内置） | Numina-Lean-Agent、VML 项目（3194 次 LSP 工具调用） |
| **Axle**（云 API，Lean FRO 系） | arXiv:2606.26442（2026-06） | 无状态 REST：编译+逐声明元数据+AST；多 Lean 版本；吞吐与 Kimina Server 相当 | 原文性能对比 |

- v40 落地：**verify/* 升级为常驻 REPL 池**：当前每候选一次 `lake env lean` 冷启动（import Mathlib 即 10s+ 量级——推断）；Kimina Server 模式（import 头作 LRU key 复用预热环境）可带来 ~2× 吞吐且免去重复 import。tactic 级需求用 LeanInteract 的 Sorry-record goal 抽取。成本：低-中（Docker 起服务即可）。

### 6.2 大规模批处理成本/吞吐实践
- **EconProver**（arXiv:2509.12603，腾讯）：token 级成本核算——**并行采样效率 > 长 CoT 串行**（PS=8：63.1% vs SS 58.6%，token 更省）；pass 数 >64 后边际收益骤降（64→128 仅 +1.1%）；动态 CoT 切换（简单题 direct、难题才 CoT）达全 CoT 99.7% 性能而仅 15% token；EconProver-GD 整体仅 12% 成本达可比性能。
- **成本-质量路由**（arXiv:2606.04883）：agentic prover 的动作路由（重采样 vs 终止 vs 降级模型）以成本约束优化成功率——思路与 v40 Orchestrator 动态策略 JSON 完全对齐。
- Numina-Lean-Agent 成本披露：多数 Putnam 题 ~$50，难题 $300–1000/题——为 v40 预算规划提供锚点。
- Harmonic 博客 "Running Lean at Scale"（2025-09，harmonic.fun）：大规模运行 Lean 的工程经验（未深入核实细节）。
- v40 落地：progress.py 的 metrics 增加 per-theorem token 成本维度；Orchestrator 按难度给 CoT/非 CoT、pass 数上限（>64 不加）；verify 批量提交。成本：低。

---

## Top-8 可落地项（按 预期收益 × 落地容易度 排序）

| # | 落地项 | 依据（收益证据） | v40 接入模块 | 成本 |
|---|---|---|---|---|
| 1 | **验证层换常驻 REPL 池 + import 头 LRU 缓存**（Kimina Lean Server / LeanInteract 模式），替代每候选 `lake env lean` 冷启动 | 1.5–2× 验证吞吐（arXiv:2504.21230）；缓存 1.94× | `verify/*` | 低 |
| 2 | **Verifier-guided 串行修复循环**：编译错误+proof state 回填下一轮 prompt，上限 ~4–16 轮，替代整证重采样 | Goedel-V2 88.1→90.4%；SorryDB：agentic 30.3% vs pass@32 20.5%；APOLLO 修复采样预算 ÷100 | `engine/axprover.py` | 低 |
| 3 | **长度归一化 BFS 打分** `Σ log p / L^α` 替换/增强 beam 评分 | BFS-Prover 72.95%（7B，纯 BFS 超 MCTS 系）；REAL-Prover 复用 | `engine/tactic_search.py` | 极低 |
| 4 | **失败子引理隔离-重证-重组**（APOLLO 模式），而非全证明重来 | <8B miniF2F 84.9%；o4-mini 7%→40%+ | `engine/axprover.py`（+`engine/tactic_search.py` 取子目标） | 中 |
| 5 | **Planner-Prover 分解 + 共享子目标缓存 + 动态重规划**（BFS-Prover-V2/Hilbert/Delta-Prover 共识架构；只对直证失败题触发） | BFS-V2 95.08%；Hilbert 99.2%；Delta 95.9% | `engine/agents.py` | 中 |
| 6 | **Mathlib 检索工具接入**：leansearch.net 免费 API（+ LeanDex/Loogle 备选），top-K 引理注入 Prover prompt；工具调用设预算上限防过度依赖 | LeanSearch v2：换检索器证成率 16%→20%（vs 无检索 4%）；SorryDB agentic 标配 | `engine/agents.py` | 低 |
| 7 | **SorryDB 快照接入 + 防作弊验证协议**（sorry 数恰减 1、其余 goal 不变、`#print axioms` 拒 sorryAx），替换 miniF2F-v1 为 v2 自评 | SorryDB_2601（1000 题，动态抗污染）；miniF2F-v1 有 16 题不可证 | `sorrydb.py` + `verify/*` | 低-中 |
| 8 | **成本感知动态预算**：按难度分 light/medium/heavy 档（Seed-Prover 三档）；pass 上限 ~64（EconProver 边际收益拐点）；token 成本纳入 Orchestrator metrics | EconProver：12% 成本达可比性能；PS>SS 同成本；Numina $50–1000/题锚点 | `progress.py`（+ `engine/agents.py` 策略 JSON） | 低 |

备选候补：LeanProgress 剩余步数预测作 beam 排序（→`progress.py`/`tactic_search.py`）；LeanDojo→LeanDojo-v2/Pantograph 迁移（仅当启用 tactic 级交互时必需，issue #250/#211 上游不修）；RMaxTS 内在奖励（若未来上 MCTS）；APRIL 修复数据集微调专用修复小模型；Aristotle/LongCat-Flash-Prover API 作 heavy 档外援。

---

## 附：对 v40 架构的三点总体判断
1. **"迭代反馈 > 堆采样"是 2025–2026 最一致的实证结论**（SorryDB、Numina-Lean-Agent 消融、Goedel-V2、APOLLO 四方互证）——v40 的 propose→verify→critic 循环方向正确，值得把预算从 pass@N 向带反馈轮次倾斜。
2. **"分解+缓存+重规划"是 agentic 证明的收敛架构**（BFS-V2/Hilbert/Delta/Goedel-Architect 独立收敛到同一形态）——v40 四角色应向"Planner(分解)-Prover(并行证)-共享 lemma 缓存-Critic(重规划)"固化。
3. **验证与检索是免费的性能**：REPL 池化 ~2× 吞吐、引理检索 +4~16pt 证成率、sorry 验证协议防 reward hacking，均为纯工程、零训练收益。
