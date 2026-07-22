# v39→v40 Sorry Resolver：分析·审计·重构·验证·迭代 终态报告

**日期**：2026-07-19 ｜ **交付**：`project/`（v40 源码）、`v40_kaggle_bundle.py`、`LOCAL_GUIDE.md`、全套审计/基准/验证证据

---

## 1. 执行摘要

v39 在 Kaggle 上 12 小时超时的根因不是"算力不足"，而是**验证链全假 + 全串行编排 + thinking 模式延迟膨胀**三重结构性缺陷：mock 验证下整个流水线数学上不可能产出任何真实证明（实测真实 solved = 0），且 Phase 4 单任务成本 23-53 分钟 × 串行 → 全量需 24-41 小时。本轮以 SPEC 驱动的多代理工程将系统重构为 v40：**真实 subprocess Lean 验证（默认）+ 攻破 lean-dojo 交互式通路（tactic 级）+ 4 角色多 LLM 真异步并发 + Orchestrator LLM 动态自适应策略 + SorryDB/LeanDojo-v2(dojo_v2)/AxProverBase-v2/LeanProgress-v2 全整合**。

**终极对比（全部实测，证据见 benchmark_results.md / regression_final.md）**：

| 指标 | v39（12h 超时运行） | v40（合并终态） | 变化 |
|---|---|---|---|
| 真实 solved | 0（5 个 mock 假阳性） | **9/11**（verify_pass_rate=1.0） | 0 → 真实可复核 |
| 不可证任务假阳性 | 有（mock apply 启发式） | **0**（2 个 Hard 全拒，附 Lean 诊断） | 假阳性清零 |
| 吞吐 | 54 调用/h（Phase 4） | ~4,970 调用/h | **≈92×** |
| 单任务均耗 | 23.3 min | 22.7 s（9 solved / 249.9s / 28,200 tokens） | **≈61×** |
| 928 任务外推 | 24-41h（>12h 上限 2-3.5×） | **≈5.9h（<12h）** | 数学上可达 |
| 并发 | 0 处 gather，纯串行 | worker pool + 4 角色并行（实测 3.32×/4 workers） | 真异步 |
| 验证 | mock 自签 `verification_passed=True` | 统一 verify：黑名单→真实重编译→warning 归因；可选 `#print axioms` 无 sorryAx | 端到端可信 |
| 测试 | 0 | **210+48 项全绿**（含真实 Lean 与 dojo 交互用例） | — |

## 2. 审计结论（Stage 1-2）

- **正确性审计**（audit_correctness.md）：4 个变体 47 条 bug（9 P0/14 P1/24 P2）。最要害：验证链端到端失效（`verify()` 从未被调用、math 检查恒 True、真实 Lean 对 sorry 仅 warning 照样 ProofFinished → 假阳性不限于 mock）；`llm_timeout=60s` < thinking 单次 90-180s 的必然超时错配；虚构模型名 `deepseek-v4-flash/pro`（当时）+ 4xx 盲重试放大；checkpoint 非原子写 + 恢复丢 results；rfl 阶段谓词恒空；escalation 永不可达；CLI 参数全静默忽略；硬编码真实 key。谱系结论：OPT 是唯一可运行基底。
- **性能审计**（audit_performance.md）：降速拐点贡献度 = thinking 延迟膨胀 ~60% + 全链路串行 ~30% + 无预算 22 轮迭代 ~10%；API 限速贡献为 0（2166 次全 200 OK，0 次 429）；内存自适应节流从未触发（死代码）。
- **环境**（env_report.md）：Lean 4.20.0 + lean-dojo 4.20.0 就绪；subprocess `lake env lean` 判定通路 27/27；Dojo 交互通路被上游 #250 等三处根因阻塞（后被 Stage C 攻破，见 §5）。

## 3. v40 架构（Stage 3，SPEC 契约 + 三代理并行 + worktree 隔离）

```
任务源: SorryScanner(本地项目/子目录) + SorryDBClient(真实快照, 防作弊协议)
   │  LeanProgressV2 优先级 + 成本三档预算(LIGHT/STANDARD/DEEP)
   ▼
ResolutionPipeline: asyncio.PriorityQueue × N workers（真并发，非 Phase 串行）
   │  每任务 phase 链 short-circuit：
   │  rfl规则(零LLM) → LLM直证(Prover) → beam搜索(Prover+Explorer,长度归一化)
   │  → AxProverV2(Propose→真实Verify→Critic归因→原始Lean诊断回填→压缩Memory)
   ▼
MultiLLMRouter: Orchestrator=DeepSeek#1(规划/调度/协调/评估→策略JSON,安全阀clamp)
              Prover=DeepSeek#2  Critic=Kimi(互评估)  Explorer=LongCat(多样性+前提检索)
   ▼
Verifier: SubprocessLean(默认, 黑名单+重编译+warning归因) | DojoV2(tactic级交互) | Mock(仅测试,[UNVERIFIED])
预算: 全局wall-clock + 软截止降级 + 每任务时间/token + thinking独立超时(≥240s)
容错: 原子checkpoint + resume合并 + SIGTERM优雅停机 + escalation跨run持久化→MARKED_AXIOM
```

47 条 v39 bug 全部核销（独立评审复核：0 未修 / 2 部分修复已在 fix-round1 补齐 / 16 修复 / 5 架构移除）。

## 4. 实测验证（Stage 4-5，真实 key + 真实 Lean）

- **测试**：合并终态全量 pytest 全绿（dojo 分支全量 210 passed + frontier 净增 48；含真实 `lake env lean` 与 dojo 交互用例；`-W error::RuntimeWarning` 严格模式零警告）。
- **真实 e2e**（mini 项目 11 真实 sorry，4 角色全开）：**9/11 solved**（SOLVED_RFL×7 / SEARCH×1 / AGENTIC×1），Trivial 5/5，2 个故意不可证 Hard 全拒，verify_pass_rate=1.0，249.9s / 28,200 tokens。三臂对照（单 LLM vs 多 LLM vs 多 LLM+动态编排）：多 LLM 臂 tokens 省 10-29%、wall 快 27-38%。
- **独立评审 → 修复迭代**：REQUEST_CHANGES（P0 CLI 启动崩、P1 metrics 边界/notebook 并发污染/健康门/[UNVERIFIED] 缺失等）→ fix-round1 全部修复并补 29 项测试 + README/requirements。
- **4 provider 实测**：DeepSeek×2、Kimi 健康；LongCat test key 对 chat 401（服务端 AppId 无效，需用户换 key；引擎自动 fallback 不阻塞）。

## 5. 核心攻坚：Dojo 交互式通路（mission impossible → 已破）

三根因全部源码级定位并修复（dojo_breakthrough.md）：① elaboration 期 stdin 被 `withIsolatedStreams` 换成空 buffer（#250）→ 请求走 FIFO；② stdout 响应在 cmdline elaboration 内被捕获永不回放 → 响应走第二 FIFO + nonblock/select 读；③ 4.20 `Elab.async` 前缀限制拒匿名 `addDecl` → `unlockAsync` 上 `addDeclCore` 真内核复核。验收：`nat_refl` 经 `run_tac("rfl")` 拿到真实 `'no goals'`；不可证定理的 `rfl` 返回真实 LeanError。封装为 `verify/dojo_v2.py`（SPEC Verifier 协议 + tactic 级接口），测试 11 项真实通过。

## 6. 前沿成果持久化（Stage B→D，2026-07-19 调研核实）

调研（frontier_atp.md / frontier_resources.md，全部条目带来源与访问日期）→ 已集成 7 项：SorryDB 真实数据集客户端 + 防作弊协议（sorry 恰减 1/statement 不变/拒 sorryAx）；验证器引导修复循环（原始 Lean 诊断回填，2025-2026 最强一致结论）；长度归一化搜索打分；leansearch/premise-search 前提检索（默认关，可开）；成本三档预算；DeepSeek V4 模型迁移 + 旧别名自动回退（**旧别名 2026-07-24 退役**，已内置预案）；README 前沿落点表 + CHANGELOG。排雷：LeanDojo v1 已官宣 deprecated（我们用自研 dojo_v2 补丁 + subprocess，不押注上游）；RouteLLM 停更不接入。路线图（LeanInteract 后端/常驻 REPL 池/APOLLO 子引理分解/LongCat-Flash-Prover 560B）见 LOCAL_GUIDE §7。

## 7. 遗留限制与风险（诚实清单）

1. dojo REPL 单进程 RSS ≈0.8GB 且 4.20 下 `-Dweak.max_memory` 被忽略 → 并发 ≤核数，长跑需进程巡检；
2. lean-dojo trace 缓存容器本地化，新环境需重 trace（mini ~75s，mathlib 级 0.5-2h 一次性）；
3. mathlib 规模项目未做端到端实测（mini 项目无外部依赖）；
4. SorryDBClient 已实现但未接 CLI 参数（python API 可用）；
5. mini 项目 11 任务样本量小，动态编排（Orchestrator 调整）在大规模下收益待实测；
6. 你提供的 LongCat key 对 chat 无效（401）；v39 硬编码 key 已泄露，**必须轮换**（LOCAL_GUIDE §3/§6）。

## 8. 复现与部署

完整本地/Kaggle 步骤、外部配合事项、FAQ：见 **`LOCAL_GUIDE.md`**。一句话：`bash bootstrap_lean_env.sh` → 配 `.env`（4 key）→ `python -m v40_sorry_resolver --project-paths <你的项目子目录> --workers 8`；Kaggle 用 `v40_kaggle_bundle.py` + Secrets。

---
*本报告全部数字可溯源至 /mnt/agents/output/ 下的运行日志与 run json；无编造数据。*
