# A2 性能 / 并发 / 成本审计报告 — v39 Sorry Resolver 三次 Kaggle 运行

> 审计范围：3 份运行日志 + 主代码 `v39_sorry_resolver_kaggle_optimized.py`（其余变体对照）+ `final_state.json`。
> 方法：只读。用 Python 正则解析日志时间戳（`YYYY-MM-DD HH:MM:SS | LEVEL | logger | msg`），按 10 分钟/1 小时桶统计 `httpx` HTTP 调用；以 orchestrator 的 Phase 日志为阶段边界；以相邻 HTTP 200 日志间隔近似单次调用时延（mock 模式下本地处理为纳秒级，间隔≈纯 API 时延，见 §3.4 证据）。所有数字均可回溯到日志时间戳或代码行号。

---

## 1. 执行摘要

- **三次运行真实 solved 数 = 0**。全部 5 个"solved"（run1: SORRY_0009/0073；run2: SORRY_0017/0073/0048）均为 mock 启发式假阳性（`predicted_success_rate>0.8 且 tactic 含 "apply"` 即判成功，代码 651-660 行），且被当作 patch 导出。run2 烧掉 **1401 次真实 API 调用 / 11h55m**，产出 3 个假证明。
- **降速拐点实测 = 起跑后 156.2 min（2h36m）**，与用户报告的"2h40m 后"一致。拐点与 **Phase 4（Agentic）入口 00:06:47 精确重合**：小时吞吐 23 时 **358 次/h → 01 时 29 次/h（-92%）**，与观测值完全一致。
- **根因 Top3**：① 全链路串行编排（5 个 Phase 全是 `for task: await`，全文 0 处 `asyncio.gather`）；② Phase 4 thinking 模式延迟膨胀（`deepseek-v4-pro + use_thinking=True + reasoning_effort="max" + max_tokens=8192`，单次调用中位 70s、峰值 298s，是 flash 的 ~9-15 倍）；③ 每任务 22 轮串行迭代无预算（停滞 break 固定在 iteration 21，45 个配额任务需 17.5h，超 12h 预算 1.5 倍）。API 限速贡献为 **0**（2166 次调用全部 200 OK，0 次 429）。
- **全量 928 任务外推：按现有配额设计需 ~24h / ~3250 次调用（串行、且验证仍是 mock）；若 928 全量流经 P2+P3 则需 ~41.6h / ~9342 次调用** —— 均远超 Kaggle 12h。当前架构在 12h 内最多完成 ~24 个 Phase-4 深度任务。
- **v40 核心目标**：任务级并行 ≥16（4 LLM key × 4 worker），P4 单任务 23.3min → ≤5min，每任务调用 31 → ≤15 次，100 个 mock-free 任务总墙钟 ≤3h（理论可达 <1h，见 §7）。

---

## 2. 三次运行总览（实测）

| 运行 | 代码版本 | 起止时间 | 墙钟 | HTTP 调用 | mock | 结果 |
|---|---|---|---|---|---|---|
| run1 | kaggle_optimized | 07-16 05:08:27 → 07:29:15 | 2h20m48s | 765 | Lean mock=True，LLM 真实 | P2 0/100；P3 跑到 84/100 被截断；2 个 mock 假阳性（SORRY_0009/0073） |
| run2 | kaggle_optimized | 07-17 21:30:34 → 07-18 09:25:52 | 11h55m18s（12h 超时被杀） | 1401 | 同上 | P2 0/100；P3 2/100（假）；P4 24/45 任务，1 假阳性（SORRY_0048）；**Phase 4 未跑完即超时** |
| run3a | final_integrated | 07-17 09:17:44 → 09:17:45 | **1.08s** | 0（LLM 也 mock） | 全 mock | 100 任务全流程 1.08s，0/100，15900 mock-token（≈1590 次 mock 调用） |
| run3b | kaggle.py | 07-17 09:24:43 | 启动即崩 | 0 | — | `TypeError: cannot unpack non-iterable coroutine object`（kaggle.py:753-754，`predict_and_prioritize` 为同步函数却调用 async `ResolutionCache.get_prediction` 未 await；optimized 版 801 行已修为 `await`） |

`final_state.json` 与 run3a 吻合（time_elapsed=1.08s，solved 全 0，marked_open=38，failed=62）。注意任务本身为合成占位数据（`goal_state="forall (x : alpha), x = x"`、`commit_hash="abc123"`），非真实 SorryDB 抓取。

---

## 3. 吞吐曲线与降速拐点

### 3.1 run2（12h）10 分钟粒度 HTTP 调用数

```
21:30-23:50  Phase2+Phase3  ████████████████ 42-71 次/10min（均值 ~58 → ~350/h）
00:00        ██████████ 54
00:10        █████ 24          ← 00:06:47 进入 Phase 4，拐点
00:20-03:50  ██ 3-9 次/10min（~30/h，最低 18/h）
04:20-06:00  ████ 10-36 次/10min（flash 回退窗口，见 §4.2）
06:10-09:20  █ 6-7 次/10min（~38/h）
```

小时桶（验证用户口径）：**21 时 188 / 22 时 308 / 23 时 358 / 00 时 104 / 01 时 29 / 02 时 24 / 03 时 27 / 04 时 65 / 05 时 144 / 06 时 62 / 07 时 39 / 08 时 38 / 09 时 15**。
→ 23 时 358 → 01 时 29 = **-91.9%**，拐点在 00:06:47（起跑后 156.2 min = 2h36m）。

### 3.2 run1（2h20m）10 分钟粒度

```
05:08-07:28  41-82 次/10min（均值 ~51 → ~324/h），全程无降速
```
run1 只跑了 P2 + P3（flash 非 thinking），**从未进入 Phase 4，因此从未降速** —— 反向佐证降速源于 Phase 4 的调用模式而非 API 侧累积限速。

### 3.3 各 Phase 墙钟 / 调用 / 延迟（run2 实测）

| Phase | 窗口 | 墙钟 | 调用数 | 调用/任务 | 任务数 | 速率 | 单次调用间隔（中位/p90） | solved |
|---|---|---|---|---|---|---|---|---|
| P1 rfl | 21:30:35 | ~0s | 0 | 0 | 0 个候选（`_is_rfl_candidate` 无一命中） | — | — | 0/0 |
| P2 llm_direct | 21:30:35→21:47:38 | **17.1 min** | 101 | 1 | 100 | 355/h | 8s / 22s | **0/100** |
| P3 tactic | 21:47:38→00:06:47 | **139.2 min** | 796 | 7.96 ≈ 8（4 轮 beam × 2 温度，代码 947 行 `temps=[0.2,0.35]`） | 100 | 343/h | 8s / 21s | 2/100（皆 mock 假阳性） |
| P4 agentic | 00:06:47→09:25:52（被杀） | **559.1 min = 9.32h** | 506 | **22**（=迭代 0..21，1 调用/迭代） | **24 / 45 配额** | **54/h** | **70s / 137s** | 1/24（mock 假阳性） |

run1 对照：P2 17.5min/101 调用/0-100；P3 123.3min/665 调用/84 任务（≈88s/任务，与 run2 的 83.5s/任务一致）。两次运行 P2/P3 指标高度可复现。

### 3.4 Phase 4 任务级时间线（run2，全部 24 个任务）

| 任务 | 开始 | 耗时(min) | 迭代 | 任务 | 开始 | 耗时(min) |
|---|---|---|---|---|---|---|
| SORRY_0000 | 00:06:47 | 6.7 | 22 | SORRY_0048 | 05:36:09 | 2.5（6 迭代"解出"，mock 假阳性） |
| SORRY_0056 | 00:13:26 | 12.9 | 22 | SORRY_0064 | 05:38:40 | 5.9 |
| SORRY_0028 | 00:26:19 | 42.8 | 22 | SORRY_0072 | 05:44:33 | 6.4 |
| SORRY_0084 | 01:09:04 | 45.0 | 22 | SORRY_0080 | 05:50:57 | 7.0 |
| SORRY_0049 | 01:54:06 | **53.5** | 22 | SORRY_0088 | 05:57:56 | 6.0 |
| SORRY_0021 | 02:47:38 | 52.9 | 22 | SORRY_0096 | 06:03:54 | 10.4 |
| SORRY_0077 | 03:40:31 | 48.7 | 22 | SORRY_0004 | 06:14:18 | 31.8 |
| SORRY_0008 | 04:29:11 | 12.9 | 22 | SORRY_0012 | 06:46:08 | 32.8 |
| SORRY_0016 | 04:42:05 | 14.9 | 22 | SORRY_0020 | 07:18:54 | 34.4 |
| SORRY_0024 | 04:56:58 | 11.3 | 22 | SORRY_0036 | 07:53:17 | 34.8 |
| SORRY_0032 | 05:08:18 | 10.7 | 22 | SORRY_0044 | 08:28:07 | 33.9 |
| SORRY_0040 | 05:18:58 | 17.2 | 22 | SORRY_0052 | 09:02:00 | 23.9（12h 被杀） |

**平均 23.3 min/任务，且全部 22 迭代无一真实收敛**（22/23 个跑满迭代后 "Stagnation detected at iteration 21"）。三个延迟 regime（单次调用间隔分布）：

| Regime | 窗口 | n | 中位 | 均值 | p90 | 解读 |
|---|---|---|---|---|---|---|
| W1 | 00:06–04:29 | 155 | **124s** | 101.6s | 149s | pro+thinking 正常工作，单任务耗时时长从 6.7min 单调爬升到 53.5min |
| W2 | 04:29–06:14 | 227 | **15s** | 27.8s | 85s | 3 次 "Pro model failed, falling back to Flash: Request timed out"（00:36/04:48/05:27）落在此窗口前后，flash 回退使速度 ×6 |
| W3 | 06:14–09:25 | 123 | **93s** | 93.4s | 103s | pro+thinking 恢复，稳定 ~90s/调用（SORRY_0012 连续 22 次全部 87–100s） |

### 3.5 "间隔≈API 时延"的有效性

mock 模式下 `init_theorem/enter_dojo/run_tactic` 全部短路（代码 554-556、651-660），`_review_proof/_summarize_lesson` 为纯字符串操作。run3a 证明：**100 任务全流程（含 45×22 次 prover 迭代）在全 mock 下仅 1.08s → 本地编排开销 ~10ms/任务 ≈ 墙钟的 0.01%**。故 run1/run2 的墙钟 99.9% 是 API 等待。

---

## 4. 降速根因归因（按贡献度排序）

> 贡献度为专家估算，基于反事实拆解：P4 墙钟 559min = 24 任务 × 22 迭代 × 单次延迟。若单次延迟保持 flash 的 8s（其余不变）→ P4 仅 ~70min（消除 ~87% 墙钟）；若任务并行 16（其余不变）→ P4 ~103min；若迭代 22→8（其余不变）→ P4 仍需 ~10h。三者相乘，单改迭代上限救不了场。

### 根因 ①（贡献 ~60%）：Phase 4 thinking 模式单次调用延迟膨胀 9–15 倍

证据链（代码行号均为 kaggle_optimized.py）：
- `_propose_proof`（1032-1039 行）：`model=llm_model_pro`（"deepseek-v4-pro"，304 行）、`use_thinking=True`（1037）、`reasoning_effort=llm_reasoning_effort`（**"max"**，305 行）、`max_tokens=min(8192, 64000//8)=8192`（1035、317 行）。
- 实测：P4 调用间隔中位 70s（W1 124s / W3 93s），vs P2/P3 flash 中位 8s → **8.75–15.5×**。P4 内 51%（259/505）的调用 >60s，最大 298s。
- 每次迭代 prompt 结构恒定（1023 行 notebook 只取 last-5），**输入 prompt 无显著膨胀**；膨胀发生在**生成侧**——effort=max + 8192 token 上限让模型每次产出长思维链。所以准确说不是"prompt 膨胀"而是"**thinking 生成预算膨胀**"。
- 同一代码/同一 prompt 形状下，单任务耗时从 6.7min（00:06）爬升到 53.5min（01:54）再回落（04:29 后）再回升（06:14 后）——与北京时间早高峰/午间/下午的服务端负载曲线一致。**属 API 服务端时延波动（推断，日志无法直接证明），但串行设计把该波动 100% 传导为总时长**。

### 根因 ②（贡献 ~30%）：全链路串行编排，零任务级并行

- `resolve_batch` 五个 Phase 全部是 `for i, task in enumerate(...): result = await ...`（1365、1383、1402、1422、1446 行）；`TacticSearchEngine.search` 的 beam 循环（903-925）与 `_generate_tactics` 的温度循环（948-951）、`AxProverBaseSolver.solve` 的迭代循环（992-1011）同样是串行 await。
- **全文 0 处 `asyncio.gather` / 0 处任务级 `create_task`**（唯一的 create_task 在 806 行，是 fire-and-forget 的缓存写入）。名义并发设施：`asyncio.Semaphore(4)`（542 行）+ `ThreadPoolExecutor(4)`（543 行）——**只包 Lean 进程操作，而 Lean 在 mock 下是空操作**；LLM 调用无任何并发。
- 结果：吞吐被钉死在 `1/单次调用延迟`：flash 时 ~350/h（8s），pro-thinking 时 ~54/h（66s）。12h 预算被换算成"24/45 个 P4 任务"。

### 根因 ③（贡献 ~10%）：每任务 22 轮无预算迭代 + 停滞判据失效

- 停滞 break 条件 `iteration > 20 and best_remaining == len(goals)`（1009 行）：mock 下 goals 永远不变 → **每个任务必然跑满 22 轮**（日志 23/24 任务 "Stagnation at iteration 21" 证实）。`max_iterations=100`（318 行）名存实亡，而失败结果里 `iterations=self.config.max_iterations`（1016 行）又虚报为 100（度量 bug）。
- 22 调用 × 70s 中位 = 23.3min/任务 × 45 配额 = **17.5h > 12h**，结构上注定超时。
- 无每任务/每 Phase 时间预算、无 token 预算、无全局软截止；唯一超时是 LLM 客户端 `llm_timeout`。**注意配置漂移**：代码默认 60s（314 行），但 run2 中 51% 的 P4 调用 >60s 且全程仅 3 次 "Request timed out"、最大成功调用 298s → 实际生效 timeout ≥ ~300s（运行时被改大；否则应产生数百次超时重试警告，而日志中一条都没有）。60s 硬超时 + 重试本可砍掉一半 P4 浪费。

### 被排除的因素（实测贡献 = 0）

| 假设 | 证据 | 结论 |
|---|---|---|
| API 限速/配额 | run1+run2 共 **2166 次调用全部 200 OK，0 次 429/5xx**，无 rate_limit 字样 | ❌ 无限速；是"慢"不是"拒" |
| 输入 prompt 膨胀 | notebook 仅保留 last-5（1023 行），单条 lesson ~150 字符（1080 行截断）；输入尺寸近似恒定 | ❌ 输入不膨胀；膨胀在 thinking 生成侧 |
| 内存自适应节流 | 三次运行内存 3.6–4.2%（702–885MB/32100MB），阈值 80%（245 行）；**0 条 throttling 警告** | ❌ 从未触发；且该机制只会 sleep(5) 减速（1366-1367 等行）与收缩 Lean 信号量（546-551 行），本来也加不了速 |
| 本地 CPU/编排开销 | run3a 全流程 1.08s/100 任务 | ❌ ≈0.01%，可忽略 |
| mock 空转（作为降速因） | mock 验证是即时的 | ❌ 不贡献降速；但它**把所有 2166 次调用的收益归零**（见 §5） |

---

## 5. 成本 / 收益核算

### 5.1 实测账本

| 运行 | API 调用 | 墙钟 | 名义 solved | mock 假阳性 | **真实 solved** | 调用/真实 solved |
|---|---|---|---|---|---|---|
| run1 | 765 | 2h20m | 2 | 2 | **0** | ∞ |
| run2 | 1401 | 11h55m | 3 | 3 | **0** | ∞ |
| run3a | 0（1590 次 mock 调用 / 15900 mock-token） | 1.08s | 0 | 0 | 0 | — |
| 合计 | **2166** | ~14.3h | 5 | 5 | **0** | — |

token 消耗：日志从不落盘 `tokens_consumed`（results 里汇总但 stdout 无记录），无法实测；按 max_tokens=8192/4096 上限与 22 轮/任务估算，run2 的 P4 约 506 次 × (输入 ~1-2k + 思维链数千) —— 量级 **数百万 token**，须以 v40 的 usage 落盘来核实。

### 5.2 每 Phase 边际收益（run2）

| Phase | 调用 | 真实 solved | 边际收益 | 评价 |
|---|---|---|---|---|
| P2 LLM direct | 101 | 0 | 0 | flash 直证对这些 (∞,1)-范畴任务无效，但成本仅 17min，可保留作廉价过滤器 |
| P3 tactic search | 796 | 0 | 0 | 8 调用/任务 × 100 任务换 0 真实解；beam 深 4 × 宽 2 在 mock 下是纯空转 |
| P4 agentic | 506 | 0 | 0 | 9.3h 烧在 24 个任务上，22 轮迭代全部停滞 |

### 5.3 928 全量外推（现有设计、串行、mock 验证不计 Lean 编译时间）

按配额设计（rfl 150 / llm_direct 420 / tactic 230 / agentic 45 / axiom 45 / open 38，318-324 行）与实测单任务成本：

| 阶段 | 任务数 | 单任务实测 | 墙钟 | 调用 |
|---|---|---|---|---|
| P2 | 420 | 10.2s / 1 调用 | 1.2h | 420 |
| P3 | 230 | 83.5s / 8 调用 | 5.3h | 1840 |
| P4 | 45 | 23.3min / 22 调用 | **17.5h** | 990 |
| **合计（配额方案）** | | | **≈24.0h** | **≈3250** |
| 若 928 全量流过 P2+P3（无配额截断）+ P4 45 | | | **≈41.6h** | **≈9342** |

**结论：即便验证继续免费（mock）、API 永不限速，当前架构跑完 928 配额方案也需约 2 个 Kaggle 12h 会话；按 run2 实际达成的 P4 速率（2.58 任务/h），仅 45 个深度任务就需 17.4h。** 若再叠加真实 Lean 编译（每 tactic 秒级~分钟级），缺口更大。

---

## 6. 并发模型审计：名义 vs 实际

### 6.1 名义设施（代码存在）

| 设施 | 位置 | 实际作用域 |
|---|---|---|
| `asyncio.Semaphore(max_concurrent_lean=4)` | 542 行 | 仅 `init_theorem`（558 行）等 Lean 进程操作；mock 下为空操作 |
| `ThreadPoolExecutor(max_workers=4)` | 543 行 | 仅 offload 同步 Lean/SQLite/文件 IO（559、606、621、1282 行等） |
| `aiohttp.TCPConnector(limit=10, limit_per_host=5)` | 704-706 行 | `_get_session` 从未被任何 generate 路径使用（死代码；实际走 `openai.AsyncOpenAI`，691 行） |
| 内存自适应并发 `_adjust_concurrency` | 546-551 行 | 只调 Lean 信号量；从未触发（内存 ≤4.2%） |

### 6.2 实际执行时序（run2，实测）

```
单事件循环、单 LLMClient、单 DeepSeek key、任何时刻至多 1 个 in-flight 请求：

21:30 ──P1(0s)──P2──┬─task0: [1×flash ~10s]─task1: [1×flash]─ … ─task99──21:47
                    │   串行 for-await（1383 行），任务间无重叠
21:47 ──P3──────────┼─task0: [8×flash 串行]─task1: [8×flash]─ … ─task99──00:06
                    │   beam 内 2 温度串行（948 行）→ run_tactic 串行（911 行）
00:06 ──P4──────────┼─SORRY_0000: [22×pro-thinking 串行 70s/次] ─SORRY_0056: [22×] ─…
                    │   迭代 i 的 _propose_proof 完成 → mock 编译(0s) → 迭代 i+1
09:25 ──(12h 被杀)──┴─ SORRY_0052 第 ~15 迭代中；45 配额只完成 24
```

CPU 证据：三次运行启动快照 CPU = 2.5% / 0.0% / 0.0%（4 vCPU 中 ≈0.1 核在用），与"全程阻塞在网络等待"一致。Kaggle 30GB 内存用 0.7-0.9GB（2.3-2.9%）。**4CPU/30GB 的 >95% 算力闲置**；真实 LeanDojo 环境反而需要这些核（Lean 编译是多进程负载），当前串行模型连 Lean 侧的 4 并发信号量都喂不饱。

---

## 7. v40 优化目标设计（12h 预算内完成 100 个 mock-free 任务）

### 7.1 量化目标表（"当前值"全部为本次实测）

| # | 指标 | 当前值（实测） | v40 目标 | 实现手段 |
|---|---|---|---|---|
| 1 | 任务级并行度 | **1**（0 处 gather；1422 行串行 for） | **≥16 worker**（4 LLM key × 4；可配） | asyncio worker pool + 每 key 令牌桶限速 + 每 worker 完整 prover 循环 |
| 2 | P4 单任务墙钟 | **23.3 min**（6.7–53.5） | **≤5 min** | 迭代上限 22→**8**；重复 state-fingerprint 早停；单调用硬超时 60s（`asyncio.wait_for`） |
| 3 | P4 单调用延迟 | 中位 70s / p90 137s / max 298s | flash ≤25s；thinking-low ≤60s | thinking 预算 8192→**2048 token**；effort "max"→**"low/auto"**；effort=max 仅留给 P0 且仅在 2 轮廉价尝试失败后 |
| 4 | 每任务 LLM 调用 | 1 + 8 + 22 = **31** | **≤15**（1 + 6 + 8） | P3 beam 3 迭代 × 2 温度；SQLite 持久化 LLM 缓存（现为进程内 dict，687 行，重启即失效、断点续跑重复付费） |
| 5 | 每任务 token 预算 | 未计量（日志无 token 字段） | **≤30k tokens/任务**，usage 逐条落盘 | generate() 返回即写 metrics 表；超预算即降级/跳过 |
| 6 | Phase 超时 | 无 | P1 30s/任务；P2 25s/调用；P3 90s/任务；P4 300s/任务；全局软截止 | deadline scheduler，见 7.2 |
| 7 | 有效吞吐（聚合） | P2/P3 ~350 调用/h；P4 54/h | **≥2000 调用/h** | 4 key 并行 + 延迟隐藏（单 key 故障自动切，现有 pro→flash 回退 736-740 行推广为 key 级路由） |
| 8 | 100 任务总墙钟 | >12h（run2 未完成） | **≤3h**（含真实 Lean 验证余量） | 1-6 项叠加 |
| 9 | 真实验证 | mock=True（假阳性 5/5） | 真实 LeanDojo/subprocess lean，mock 仅限单测 | 消除 651-660 行启发式判成功路径；patch 导出前必须真实编译通过 |
| 10 | 资源利用 | CPU ≤2.5%、内存 ≤4.2%，节流 0 次 | CPU ≥50%（Lean 编译吃满 4 核）；monitor 从"只会减速"改为**双向调并发** | 资源充裕时升 worker 数，紧张时降 |
| 11 | 断点续跑成本 | LLM 缓存进程内，resume 全量重付 | resume 0 重复调用 | SQLite 响应缓存（cache_key 已存在：dir:/tac:/ax: 前缀）+ checkpoint 记录已完成调用 |
| 12 | 配置一致性 | timeout 默认 60s vs 实际 ≥298s 漂移；模型硬编码改写（366-368 行）；API key 硬编码（357 行，**交付前必须移除/脱敏**） | 配置单一来源 + 启动时打印生效值 | pydantic 校验 + env 注入 |

### 7.2 软截止降级规则（T = 12h 硬截止）

| 时点 | 条件（实测指标驱动） | 动作 |
|---|---|---|
| T-6h | 完成率投影 <60% | P4 迭代 8→4；thinking effort →low；P3 beam 宽 2→1 |
| T-3h | 仍有未启动的 P4 任务 | 停止接收新 P4；在跑的跑完；其余任务走 P2/P3 flash 快速通道 |
| T-1h | 队列非空 | 排空：不再发起新任务；导出已验证 patch；写最终 checkpoint |
| T-15min | — | 硬停：results/patch/metrics 全量落盘 |

### 7.3 最小并行度与多 LLM 理论吞吐上限

**最小并行度（100 任务 / 12h）**：v40 预算下串行工作量 = 100 × (1×25s + 6×10s + 8×35s) ≈ **10.1h** → 理论最小并行 = ⌈10.1/12⌉ = **1**，但无真实 Lean 编译余量；考虑 Lean 编译（每 tactic 数秒-分钟，待 Stage-2 实测）与 API 波动，**最低推荐 4（≈2.5h 完成），目标 16（≈40min + Lean 时间）**。

**多 LLM（2× DeepSeek + Kimi + LongCat）并行理论上限**：4 key × 保守 8 并发 = 32 in-flight（实测单调用延迟：flash 8s / thinking-low ~30s / thinking-max ~100s）：

| 负载类型 | 聚合吞吐上限 | 相当于 run2 P4 速率（54/h）的倍数 |
|---|---|---|
| 全 flash（8s） | 32/8s = 4 rps = **14400 调用/h** | 267× |
| 全 thinking-low（30s） | 1.07 rps = **3840/h** | 71× |
| 全 thinking-max（100s） | 0.32 rps = **1150/h** | 21× |

即：即使全部任务走最贵的 thinking 路径，4-key 并行也能在 **~1.3h 内完成 100 任务 × 15 调用 = 1500 次调用**；瓶颈将从 LLM 转移到真实 Lean 编译吞吐（`max_concurrent_lean=4` 需上调并配合 30GB 内存按 ~1-2GB/Lean 进程测算容量）。上限约束：各 provider 的账号级 RPM（未在日志中观测到任何 429，实测余量未知，需 Stage-4 基准实测标定）。

---

## 8. 证据索引（关键行号 / 日志锚点）

- 串行循环：kaggle_optimized.py 1365（P1）、1383（P2）、1402（P3）、1422（P4）、1446（P5）；`TacticSearchEngine.search` 903-925；`_generate_tactics` 948-951；`AxProverBaseSolver.solve` 992-1011。
- thinking 配置：305（effort=max）、317（thinking_budget=64000）、1035-1039（pro 模型 + use_thinking + 8192 tokens）。
- 停滞/迭代：1009-1011（iteration>20 break → 22 轮）、1016（iterations 虚报 100）。
- mock 假阳性：651-660（`predicted_success_rate>0.8 and "apply" in tactic` → True）；假阳性日志：run1 06:08:11 SORRY_0009、06:20:33 SORRY_0073；run2 22:33:36 SORRY_0017、22:43:18 SORRY_0073、05:38:40 SORRY_0048；均伴随 `Patch exported`。
- 并发设施：542-543（Semaphore/ThreadPoolExecutor 仅 Lean）、704-706（aiohttp 连接池死代码）、806（唯一 create_task=缓存写入）。
- 资源：三次启动快照 CPU 2.5%/0%/0%，Mem 740/885/702MB of 32100MB；`should_throttle` 260-267 行，0 次触发。
- run3b 崩溃：kaggle.py 753-754（同步函数内未 await `get_prediction`）→ traceback 见 run3 日志 09:24:43；optimized 版 801 行已修为 await。
- 配额：318-324 行（max_iterations=100、rfl 150/llm 420/tactic 230/agentic 45/axiom 45/open 38）。
- 安全：357 行硬编码 DeepSeek API key（报告与 v40 交付中须脱敏并改 env 注入）。

*分析工具：一次性 Python 脚本（内存中解析，未落盘临时文件）；日志解析正则 `^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (\w+)\s*\| ([\w\.]+)\s*\| (.*)$`，run1/2/3 分别解析出 966/1693/316 个结构化事件。*
