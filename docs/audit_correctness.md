# v39 Lean 4 sorry 自动消解引擎 — 四变体只读正确性审计报告

审计方式：4 个变体全部逐行通读 + `ast.parse`/`py_compile` 编译验证 + 逐对 `diff` 交叉比对 + 文档对照。未修改任何被审计文件。

**文件代号**
- `OPT` = v39_sorry_resolver_kaggle_optimized.py (1793 行，主审计对象，AST OK)
- `KAG` = v39_sorry_resolver_kaggle.py (1732 行，AST OK)
- `FULL` = v39_sorry_resolver_full.py (1734 行，**SyntaxError，无法运行**)
- `FIN` = v39_final_integrated.py (1508 行，AST OK)

---

## 一、执行摘要（13 行）

1. **FULL 根本无法启动**：`FULL:1597`（及 1612/1618/1627/1637/1680/1705）存在跨行字符串字面量，`py_compile` 直接 SyntaxError。文档（v39_sorry_resolver_documentation.md:4-5）声称该文件"1743 行、完整可运行"，与实际 1734 行且不可解析双重矛盾。
2. **KAG 开箱即崩**：已知 `KAG:752-754` 同步方法内调用 async `cache.get_prediction` 未 await → TypeError；本审计另发现其下游还潜伏第二颗雷（`KAG:996` mock 下 `task.dojo_state=None` → `_compile` 必抛 ValueError）。
3. **OPT 是唯一可运行基底**，但其"Kaggle 12h"目标在数学上不可达：`llm_timeout=60s`（OPT:314）< thinking 模式单次 90-180s（OPT:1037-1038 强制 `use_thinking=True`）→ 每次 Pro 调用必然先白等 60s 超时再降级，Phase 4 45 任务 × ~22 轮 × ≥90s ≈ **20+ 小时**，仅 Phase 4 就超预算。
4. **验证链端到端失效**：`VerificationAndIteration.verify`（OPT:1092）定义后从未被调用；`_mathematical_correctness_check` 恒 True（OPT:1117-1118）；Phase 1-3 对获胜 proof 不做 sorry/admit 文本筛查（mock 会拦 sorry，**真实 Lean 只给 warning 照样 ProofFinished**）→ 假阳性不止存在于 mock 路径。
5. **Kaggle 容错叙事不成立**：checkpoint 非原子写（OPT:1237）、恢复时丢弃 `prev_results`（OPT:1730）、Phase 1 不按 status 过滤（OPT:1363）、`_shutdown_event` 在 `resolve_batch` 内从未被轮询（OPT:1683 只在死代码里检查）、tactic 超时留下僵尸线程最终耗尽 4 线程池（OPT:621-628）。
6. **多个宣称特性是死线**：rfl 阶段谓词恒空（OPT:1529-1532，四版本同）、escalation 永远到不了 3 级（OPT:1432-1437）、`axiom_quota` 无人使用、`V39_MOCK_MODE` 环境变量四版本都只写不读、CLI 参数（--tasks/--model/--output/--rounds/--checkpoint-interval）全部静默忽略、`_adjust_concurrency` 定义后从未调用。
7. **配置漂移严重**：硬编码真实形态 API key（OPT:6/357）；`deepseek-v4-flash/pro` 为虚构模型名，真实调用必 404，重试循环把 404 当可重试错误每次调用浪费 3× 退避；`LLM_BASE_URL` 环境变量分支不可达（OPT:359-360 默认值非 None）。
8. FIN/FULL 的 LeanDojo 调用契约全错：`run_tac(theorem, tactic)`（FULL:515/FIN:666，第一个参数应是 TacticState）+ `Theorem(repo, file, goal_state)`（FULL:454/FIN:635，第三参数应是定理名）→ 真实模式 100% 失败；KAG/OPT 已修正此契约。
9. FIN 独有高危设计：`LeanDojoV2Integration` 生成 `"by sorry"` 占位证明且 `_verify_proof` 恒 True（FIN:552-601）→ 一旦接线即批量伪造 solved；anthropic 路径 `response.content[0].text` 在 thinking 开启时必 AttributeError（FIN:757）。
10. 停滞检测语义错误：`iteration > 20 and best_remaining == len(goals)`（OPT:1009，四版本同）实际效果是"第 21 轮后首次持平即退出"，与 max_iterations=100 和文档宣称的"20 次无进展终止"均不符。
11. 统计层面：`results["details"]` 无界增长且每次 checkpoint 全量重写 → O(n²) IO；`total_processed=len(tasks)` 把未触达任务计入"已处理"。
12. 四版本关系：**FULL(不可运行) → FIN(功能堆叠但真实模式全错) → KAG(Kaggle 化但引入 await 回归) → OPT(修复 KAG 回归 + 引入 thinking/缓存/新配置漂移)**。
13. 建议：以 OPT 为基底，先修 P0 的验证链与超时错配，再修 checkpoint/恢复，最后清理死代码与配置漂移（见第六节）。

**计数：P0 = 9 条，P1 = 14 条，P2 = 24 条（合计 47 条，不含已知背景 3 条与 KAG:754 已知 bug）。**

---

## 二、P0 — 阻断运行 / 伪造结果 / 必然失败

| # | 文件:行号 | 代码摘录 | 影响 | 修复方案 |
|---|-----------|----------|------|----------|
| P0-1 | FULL:1597-1598（另 1612,1618,1627,1637,1680,1705） | `print("` ↵ `--- 消解进度摘要 ---")` 裸换行 | **整个文件 SyntaxError，无法 import/运行**；文档却以其为"完整可运行"参考实现 | 全部改为 `print("\n--- ... ---")`；CI 加 `python -m py_compile` 门禁 |
| P0-2 | OPT:314 vs OPT:1036-1038 | `llm_timeout: float = 60.0`；`use_thinking=True, reasoning_effort="max"` | thinking 单次 90-180s > 60s 超时 → **每次 Pro 调用必超时**，attempt 0 异常后才降级 Flash（OPT:736-740）。Phase 4 串行 45 任务 ×(21+1) 轮 ×≥90s ≈ 20-25h；Phase 3 最坏 230×30×2 次调用 ≫ 12h。**Kaggle 12h 目标不可达** | thinking 调用单独设 `llm_thinking_timeout≥240s`；`AsyncOpenAI(timeout=...)` 按调用覆盖；Phase 3/4 用 `asyncio.gather`+Semaphore 并发；加全局 wall-clock 预算并在超预算时跳过低优先级阶段 |
| P0-3 | OPT:1092（死代码）, 1117-1118, 1002, 1463-1519, 908-919 | `async def verify(...)` 无任何调用点；`_mathematical_correctness_check: return True`；AxProver 自签 `verification_passed=True`；`_try_rfl/_llm_direct_prove/tactic search` 仅凭 `run_tactic` success 即置 SOLVED | **验证链端到端失效**。mock 路径：`predicted_success_rate>0.8 and "apply" in tactic`（OPT:659-660）凭空判成功；真实路径：Lean 对 `sorry` 仅 warning 仍 ProofFinished，Phase 1-3 无 sorry/admit 文本筛查（仅 AxProver 的 `_review_proof` 有，OPT:1066）→ 含 sorry 的"证明"会被记为 SOLVED_AI。报表中所有 solved 数字不可信 | 每个 success 分支统一调用 `verify()`；verify 内做①文本黑名单（sorry/admit/native_decide 滥用）②重放编译 ③`#print axioms` 检查不含 `sorryAx`；mock 的 apply 分支删除或改为始终失败 |
| P0-4 | FULL:515 / FIN:666；FULL:451-455 / FIN:635 | `dojo.run_tac(theorem, tactic)`；`Theorem(repo_path, str(file), task.goal_state)` | **LeanDojo 调用契约错误**：`run_tac` 第一参数应为 TacticState 而非 Theorem；Theorem 第三参数应为定理全名而非 goal 文本 → FULL/FIN 真实模式 100% 抛错（被 `except Exception` 吞成"战术失败"假象） | 迁移 KAG/OPT 的 `enter_dojo`/`init_state` 模式（OPT:596-614, 634-649） |
| P0-5 | FULL:897-911 | `if build_ok and len(goals) == 0: return ... SOLVED_AI`（AgenticProver 全程无 Reviewer） | FULL 的 Agentic 对 proposal **不做 sorry 文本检查**（对比 OPT:1066 有检查）→ 真实模式下 LLM 只要回 `"by sorry"` 即被记为证明成功 | 移植 OPT 的 `_review_proof`；并加 P0-3 的统一 verify |
| P0-6 | FIN:552-554, 583-601 | `prove_theorem` 返回 `"by sorry  -- placeholder proof"`；`_apply_tactic` 恒 `"no goals"`；`_verify_proof` 恒 `True` → `attempt_proof` 产出 `success=True, SOLVED_AI` | FIN 独有的**伪造证明工厂**。当前仅被死代码 `_attempt_with_fallback`（FIN:1434）引用，但一旦接线即批量制造含 sorry 的"solved" | 整体删除 `LeanDojoV2Integration` 或把全部占位实现改为显式 `raise NotImplementedError` |
| P0-7 | OPT:621-628 + 675-676 + 1550 | `asyncio.wait_for(loop.run_in_executor(self._executor, ...), timeout=lean_timeout)`；`shutdown(wait=True)` | `wait_for` 超时只取消 future 包装，**线程里 Lean tactic 继续跑**；4 个 worker 被僵尸 tactic 占满后，后续 `run_in_executor` 全部排队 → 流水线事实死锁；`shutdown(wait=True)` 在事件循环内无限期等待 → 进程无法正常退出 | 超时后放弃该 dojo（新建 dojo 而不是复用 state）；executor 独立化并 `shutdown(wait=False, cancel_futures=True)`（3.9+）；shutdown 外包 `run_in_executor` + 总时限 |
| P0-8 | FIN:896-924 / FULL:708-759；FIN:1042 / FULL:1039 | 所有 beam 分支共享同一个 `initial_dojo`；`_compile_and_extract(task, proof)` 传 `task.dojo_obj`（从未被赋值→None）→ `_sync_execute_tactic` 每次新建 `Dojo` 且无人关闭 | FIN/FULL 的 tactic 搜索**状态污染**（候选 B 在候选 A 改过的 state 上跑）；Agentic 每轮迭代泄漏一个 Lean 进程（100 轮 × 45 任务）；`close_dojo` 检查 `task.dojo_obj` 恒 None → 等于没关 | 同 P0-4，迁 OPT 的 dojo 生命周期管理（每次 `enter_dojo`/`close_dojo` 配对，OPT:987/1013） |
| P0-9 | OPT:303-304, 366-368, 1714；OPT:733-743 | `llm_model="deepseek-v4-flash"`, `llm_model_pro="deepseek-v4-pro"`；`except Exception` → 固定 3 次指数退避 | 两个模型名均为虚构，真实 API 返回 4xx model-not-found；重试循环不区分错误类型 → **每次 LLM 调用白等 2s+4s 退避 ×3 次后返回 ERROR**，全 pipeline 在真实模式空转数小时且零产出；mock=True 把这一切完全掩盖（与已知背景呼应但根因在此） | 启动时 `models.list()` 校验模型名；只对 429/5xx/超时重试，4xx 立即 fail-fast；`generate` 增加"连续 N 次 4xx 则全局熔断" |

---

## 三、P1 — 功能失效 / 数据损坏风险 / 真实模式崩溃

| # | 文件:行号 | 代码摘录 | 影响 | 修复方案 |
|---|-----------|----------|------|----------|
| P1-1 | OPT:1529-1532（KAG/FULL/FIN 同逻辑：KAG:1468, FULL:1487, FIN:1269） | `_is_rfl_candidate: complexity_estimate <= 3 and priority in (P2, P3)` | **恒空谓词**：`_predict` 最小输出 base_steps=3（"Is"分支，OPT:822），而 priority_boost 对 P2 +1 步、P3 +2 步（OPT:843-848）→ P2/P3 的 complexity 最小为 4/5 → Phase 1 永远不选任何任务，`rfl_quota=150` 纯摆设；文档 §六却宣称"rfl 消解 150 ✅ 实现" | 谓词改为 `complexity_estimate <= 4` 或对 P2/P3 不加 step_adj；并加单元测试断言 rfl 候选非空 |
| P1-2 | OPT:1009-1011（KAG:956, FULL:924, FIN:1003 同） | `if iteration > 20 and best_remaining == len(goals): break` | 语义错误：`best_remaining` 是历史最小值，当前轮只要"打平历史最好"即退出 → 实际上**第 21 轮后第一次持平就 break**（编译全败时 goals=[err] len=1，iter 21 必触发），并非"连续 20 轮无进展"；配合 max_iterations=100 名存实亡。且 OPT:1016 误报 `iterations=self.config.max_iterations` | 改为 `stall = 迭代计数 - 上次 best_remaining 改善的轮次; if stall >= 10: break`；iterations 报实际值 |
| P1-3 | OPT:1432-1437 + 323 | `if task.escalation_level >= 3: MARKED_AXIOM else: escalation_level += 1` | `resolve_batch` 单次通过内每个任务在 Phase 4 只被访问一次 → escalation_level 单次运行最多到 1 → **MARKED_AXIOM 分支不可达**；`axiom_quota=45` 无任何引用。文档 §2.7"失败 3 次升格公理"不成立 | 跨轮持久化 escalation_level（checkpoint 已带该字段，需多轮复用同一 task 列表）；在 Phase 4 内对失败任务原地重试 N 次；使用 axiom_quota 截断 |
| P1-4 | OPT:1728-1733, 1740-1742, 1363 | `tasks, prev_results, phase = checkpoint_data` 后 `prev_results`/`phase` 再未被使用；`emergency_save: checkpoint.save(tasks, {}, ...)`；Phase 1 `rfl_tasks = [t for t in tasks if self._is_rfl_candidate(t)]` 无 status 过滤 | **断点续跑名不副实**：①恢复后 results 计数器/明细全部清零（紧急保存本就存 `{}`）②不按 phase 跳过已完成阶段 ③Phase 1 对已 SOLVED 任务重复执行（双重计数）④kaggle preemption 场景下恢复只能保住 task.status | `resolve_batch(tasks, resume=checkpoint_data)`：合并 prev_results；各阶段过滤器加 `t.status == OPEN`；按 phase 跳过 |
| P1-5 | OPT:190-198 + 1230-1238 | `to_dict: d = asdict(self)` 之后再 `d.pop("dojo_obj"...)`；checkpoint 列表推导在 try 之外；`open(path,"w")` 直接覆写 | ①`asdict` 会 **deepcopy** theorem_obj/dojo_obj/dojo_state —— 真实 Dojo 含 subprocess/pipe，deepcopy 抛异常且发生在 try 之外 → checkpoint.save 把异常抛进 `resolve_batch`（OPT:1376 等调用点无保护）→ 批处理中途崩溃；②非原子写：SIGKILL 落在 dump 中途 → 截断 JSON → `load()` 失败 → 全部重来 | 用 `{f.name: getattr(self,f.name) for f in fields(self) if f.name not in (...)}` 替代 asdict；写 `path.tmp` 后 `os.replace` |
| P1-6 | OPT:1644-1663, 1683, 1745 | `_shutdown_event` 只在 `SorryResolutionOrchestrator.run`（未被 main 使用）中 `is_set()` 轮询；main 路径 `resolve_batch` 从不检查 | **SIGTERM 优雅关机失效**：信号回调能 set event、能紧急保存，但批处理循环不响应，Kaggle SIGKILL 到来前任务不会停；紧急保存 results={} 又加剧 P1-4 | `resolve_batch` 每个任务循环开头检查 `_shutdown_event.is_set()` → 保存并 return；紧急保存传入当前 results |
| P1-7 | OPT:1787（KAG:1726, FULL:1728, FIN:1503 同）; OPT:1776-1790 | `os.environ["V39_MOCK_MODE"] = "1"` —— 全代码库无任何读取点；`--tasks/--model/--output/--rounds/--no-patches/--checkpoint-interval` 解析后从未使用 | 文档 §3.2 明确教用户 `export V39_MOCK_MODE=1`，**四版本全部不生效**；CLI 承诺的调参全部静默无效（用户以为改了模型/轮数其实没改） | `V39Config.__post_init__` 读 `os.environ.get("V39_MOCK_MODE")`；`main(args)` 真正接收并使用 CLI 参数 |
| P1-8 | OPT:6, 357 | docstring 与 fallback 均硬编码 `sk-8c0c461a…[REDACTED]` | 真实形态密钥入库/随文件分发 → 泄露即被盗刷；且只要 openai 包存在，无 env 的用户会静默用这把共享 key 计费 | 删除硬编码；无 key 时强制 mock 并报错提示；轮换该 key |
| P1-9 | OPT:1151-1156 | `except Exception: ... tasks.extend(self._mock_db_tasks())` | SorryDB 查询**任何**失败（含 httpx 未安装的 ImportError）都注入 50 个假任务，即使用户只扫本地真实项目 → 假任务混入真实流水线，污染 solved 统计与 checkpoint | 仅在显式 mock_mode 且无 custom_projects 时才注入；否则返回空列表并 WARNING |
| P1-10 | OPT:900-925 | `heapq.heappush(beam, (new_priority, depth+1, new_state, new_proof))`，元素为 `TacticState` | `(priority, depth)` 并列时 heapq 比较第 3 元素 → `TacticState < TacticState` **TypeError 整个搜索崩溃**（真实模式常见：两个候选同 depth、同 remaining 数）；`_state_fingerprint` 用 `hash(pp)%1e7` 也有碰撞误判 | 元组第 2 位放单调计数器 `(priority, counter, depth, state, proof)`；fingerprint 用 `hashlib.sha1(pp.encode()).hexdigest()` |
| P1-11 | KAG:585-588 + 993-997 | mock 分支 `return mock_dojo, mock_dojo` 但未 `task.dojo_state = ...`；`_compile: init_state = task.dojo_state; if init_state is None: raise ValueError` | KAG 独有第二颗雷：即使修掉 754 的 TypeError，mock 模式下首个 agentic 任务在 `_compile` 必抛 ValueError → Phase 4 崩 → main 捕获后批失败（OPT 已在 OPT:599-600 修复） | 移植 OPT 的 mock `enter_dojo` 赋值 |
| P1-12 | FIN:724-730, 942-943 | `generate` 单次调用无 try/except、无重试；`_generate_tactics` 直接 await | FIN 的 LLM 调用**零容错**：任何网络抖动/限流异常沿 search→resolve_batch 上抛 → 整批崩溃；与 OPT 的 3 次退避相比是明显退化 | 移植 OPT 的重试/退避（但保留 P0-9 的错误分类） |
| P1-13 | FIN:750-758 | `thinking={"type":"enabled",...}` + `content = response.content[0].text` | anthropic 开启 thinking 后 `content[0]` 是 ThinkingBlock（无 `.text`）→ **AttributeError 必崩**；模型名 `claude-opus-4-5-20251101` 硬编码 | 遍历 `response.content` 取 `block.type=="text"`；模型名入配置 |
| P1-14 | FIN:388-389 + 1317-1320（OPT:1585, FULL:1607 同类） | FIN 的 `scan_all` 查询失败仅 warning **不注入 mock** → tasks=[]；`print_progress: 100*solved/total` | FIN 在 SorryDB 不可达且无本地项目时 `total=0` → **ZeroDivisionError** 崩在收尾打印；OPT/FULL 仅在任务源为空时同病 | `if total: ...` 守卫；scan_all 空结果提前退出 |

---

## 四、P2 — 阻塞点 / 资源泄漏 / 逻辑瑕疵 / 配置漂移

| # | 文件:行号 | 问题 | 影响与修复 |
|---|-----------|------|-----------|
| P2-1 | OPT:806 | `asyncio.create_task(self.cache.set_prediction(...))` 无引用、无 await、无异常回收 | fire-and-forget：循环提前结束丢写入、任务被 GC 前可能未完成（"Task was destroyed but it is pending"）、异常无人观测。修复：改为直接 `await`（预测写本就很快）或维护 `self._pending: set` 并在 close 时 `gather` |
| P2-2 | OPT:471-493 | `_batch_queue` 跨协程无锁 append+clear；多个并发 flush 在 executor 里并行写同一 SQLite（WAL 单写者）→ `SQLITE_BUSY` 被 except 吞掉 → **批次静默丢失** | flush 全程持 `self._lock`；或单连接 + `asyncio.Queue` 单写协程 |
| P2-3 | OPT:556 vs 616-632 | Semaphore 只包住最便宜的 `init_theorem`，`enter_dojo`/`run_tactic`（真正昂贵的 Lean 交互）完全绕开 | 并发限制名存实亡（当前串行流水线无体感，一旦并行化即失控）。修复：把 semaphore 移到 run_tactic/enter_dojo |
| P2-4 | OPT:546-551 | `_adjust_concurrency` 定义后 0 调用点；且只改 `config.max_concurrent_lean`，已创建的 Semaphore/ThreadPool 不受影响 | 文档宣称的"内存自适应并发"完全不生效。修复：删除或实现为动态 Semaphore（如 `asyncio.BoundedSemaphore` + 手动 release/acquire 差值） |
| P2-5 | OPT:951, 1498 | `cache_key=f"tac:{task.id}:{hash(prompt) % 100000}:{temp}"` | 取模 1e5 生日碰撞（~300 个 prompt 即 50% 碰撞）→ **A 目标的缓存 tactic 被 B 目标命中**；`hash()` 跨进程随机也使缓存不可复现。修复：sha256(prompt) 全值或 16 位 hex |
| P2-6 | OPT:759-760 | `temperature or self.config.llm_temperature`、`max_tokens or ...` | 显式传 `temperature=0.0`/`max_tokens=0` 被 falsy 吞掉改回默认。修复：`x if x is not None else default` |
| P2-7 | OPT:733-743 | 重试循环捕获所有 Exception（含 400/401/404 不可重试错误） | 每个确定性错误白等 2+4s（乘上调用次数就是小时级）。修复：按 `openai.APIStatusError.status_code` 分类重试 |
| P2-8 | OPT:359-360 | `if self.llm_base_url is None:` —— 但字段默认值已是 `"https://api.deepseek.com/v1"` | `LLM_BASE_URL` 环境变量**永远读不到**（死分支）。修复：字段默认 None，在 post_init 里 `env or 默认值` |
| P2-9 | OPT:1193-1209 | `subprocess.run(["grep",...])` 在 async 方法内同步执行；`int(lineno)` 无保护 | 大仓库 grep 阻塞事件循环秒级以上；grep 输出含二进制/非常规行时 ValueError。修复：`asyncio.create_subprocess_exec`；`try: int(...)` |
| P2-10 | OPT:701-707 | `_get_session` 创建 aiohttp session 但 `generate` 只用 openai client；session 仅被 close 引用 | 死代码 + 若被调用则泄漏连接（当前未调用）。修复：删除或真正用于 SorryDB/httpx 调用 |
| P2-11 | OPT:651-661 + 934-939 | mock `run_tactic` 返回的 state 恒为 `None` → `_state_fingerprint` 全为 `"none:{depth}"` → `visited` 把同层兄弟全部剪枝 | mock 下 beam search 退化为单链（每层只活 1 个节点），搜索结果与真实模式行为不可比。修复：mock 返回伪状态对象（如 tactic 序列哈希） |
| P2-12 | OPT:258, 285 | `psutil.cpu_percent(interval=0.1)` 在 async 上下文同步阻塞 0.1s | 每次 `log_status` 卡循环 100ms。修复：`interval=None`（非阻塞采样）或 run_in_executor |
| P2-13 | OPT:1043-1050, 1503 | `_extract_lean_code` 只认 ` ```lean\n `；`_llm_direct_prove` 只 strip 引号/反引号，不处理 fenced block | LLM 返回 ` ```lean4 ` / 带散文时提取失败 → tactic 变成 `"by ```lean\n..."` 白编一轮。修复：统一宽松正则 ` ```(?:lean4?)?\s*\n(.*?)``` `，两路径共用 |
| P2-14 | OPT:1066-1072, 1124 | `"sorry" in proof.lower()`、`"admit"/"stop"` 子串匹配 | 注释/标识符含这些子串（如 `-- sorry about this`、`readmit`）→ 误判拒收合法证明。修复：词边界正则 `\bsorry\b` 并剔除注释 |
| P2-15 | OPT:1550-1551 | `self.lean.shutdown()`（`wait=True` 阻塞调用）在 async `shutdown` 里直接执行 | 正常退出也阻塞事件循环；与 P0-7 叠加时永久挂起。修复：`await loop.run_in_executor(None, self._executor.shutdown, True)` + 超时 |
| P2-16 | OPT:1762-1764 | `run_in_executor(None, lambda: json.dump(..., open(final_path,"w"), ...))` | 文件句柄永不关闭（依赖 GC）；lambda 内异常成为 unraisable。修复：抽成 `_sync_save_json`（OPT:1543 已有）复用 |
| P2-17 | OPT:1450-1451, 1444-1448 | `total_processed = len(tasks)`；剩余 OPEN 前 38 个直接 `OPEN_PROBLEM`，其余计入 `failed` | "已处理"含从未尝试的任务；"开放问题"纯按配额切，无任何判定依据 → 指标误导。修复：processed=实际进入过任一阶段的任务数；open_problem 需满足"agentic 失败且 escalation 满" |
| P2-18 | OPT:1669-1705 | `SorryResolutionOrchestrator`：①1692 `predict_and_prioritize` 在 OPT 已是 async 却未 await（新回归，死代码路径）②self.prioritizer/verifier 各自新建 ResolutionCache/LeanDojoExecutor 与主系统双写同一 DB 文件 ③每轮 `scan_all` 重建任务 → metrics 跨轮重复计数 | 该类 main 未使用但保留即隐患：1692 若被启用即 `TypeError: cannot unpack non-iterable coroutine`（与 KAG:754 同款）。修复：删除该类或改为复用主系统单例并补 await |
| P2-19 | OPT:582-594 | `_infer_theorem_name` 取 context 中**第一个** `theorem|lemma|def|instance|example` 名 | context 含多个声明时必取错（sorry 常在后面的定理里）→ 真实模式 init 错定理。修复：按 `line_number` 向上最近声明定位 |
| P2-20 | OPT:1102-1115 | `_lean_compiler_check`：`enter_dojo` 覆盖 `task.dojo_obj` 前不关闭旧 dojo；except 路径不 close | 若 verify 被启用（P0-3 修复后）即引入 dojo 泄漏。修复：`try/finally: close_dojo` + 进入前检查 |
| P2-21 | OPT:1644, 453, 481 等 | 模块级 `asyncio.Event()`（import 时创建）；大量 `asyncio.get_event_loop()` | 3.10+ 下 Event 惰性绑定尚可，但 import 期创建属反模式；`get_event_loop()` 在协程内应用 `get_running_loop()`（语义明确、避免 DeprecationWarning） | 
| P2-22 | OPT:402, 51-57 | import 期 `setup_logging(force=True)` 重置 root logging；`get_kaggle_paths()` 死代码 | import 副作用污染宿主应用日志；死函数误导。修复：移入 `main()`/删除 |
| P2-23 | OPT:1568, 文档:4-5 | banner 仍写 `API: DeepSeek (deepseek-coder)`；文档写 FULL"1,743 行/完整可运行" | 与 v4-flash 实际配置、FULL 实际 1734 行且 SyntaxError 的事实矛盾 → 配置/文档漂移。修复：banner 从 config 取值；文档重写 |
| P2-24 | FIN:787-798 / FULL:24,65 / KAG:695 | FIN：`__init__` 里同步 `AutoModel.from_pretrained("lean-dojo/LeanProgress-v1")`（阻塞+可能联网下载），而 `predict_remaining_steps` 恒返回 (5,0.85)/(6,0.70) 且无人调用；FULL：`import pickle`、`import aiofiles` 从未使用；KAG：`AsyncOpenAI` 未设 `max_retries=0` → SDK 默认 2 次 × 自实现 3 次 = 最多 9 次/调用 | FIN 启动即可能卡数分钟下载一个根本不会用的模型；KAG 重试放大。修复：模型加载改惰性+可选；删未用 import；KAG 补 `max_retries=0` |

---

## 五、四版本差异矩阵

### 5.1 谱系与定位

```
FULL (v39_sorry_resolver_full.py, 2026-06-22)          ← 文档对应版本，但 SyntaxError 不可运行
  └─ FIN (v39_final_integrated.py)                     ← 功能堆叠版：+LeanDojoV2 +Anthropic +transformers +SorryDB 评估
       └─ KAG (v39_sorry_resolver_kaggle.py)           ← Kaggle 化：+监控/检查点/信号/补丁/WAL 缓存/CLI；引入 await 回归
            └─ OPT (v39_sorry_resolver_kaggle_optimized.py)  ← 最新最全：修 KAG 回归 +V4 模型/thinking/LLM 缓存/降级
```

**最新最全 = OPT**（唯一语法可运行且 LeanDojo 契约正确、且修复了 KAG 两处 P0 的版本），但它也引入了独有的新配置漂移（虚构 V4 模型、thinking 超时错配、OPT:1692 死代码路径新 await 回归）。

### 5.2 关键能力对照

| 能力 | FULL | FIN | KAG | OPT |
|---|---|---|---|---|
| 语法可运行 | ❌ SyntaxError:1597 | ✅ | ✅ | ✅ |
| LeanDojo 调用契约 | ❌ run_tac(theorem)/Theorem(goal_state) | ❌ 同左 | ✅ state 传递正确 | ✅ 同左 |
| LeanDojo mock 回退类 | ❌ 无（ImportError 后裸奔） | ❌ 无 | ✅ _Mock* 全套 | ✅ 同左 |
| ResolutionCache | 同步阻塞 | 同步阻塞 | async+WAL（引入 754 bug） | async+WAL+批量 flush（修复） |
| `predict_and_prioritize` | 同步+同步缓存 ✅ | 同步+同步缓存 ✅ | 同步×async 缓存 ❌ TypeError:754 | async+await ✅（但 OPT:1692 死路径漏 await） |
| LLM 重试 | ❌ 单次+ERROR 串 | ❌ 单次且异常上抛 | ✅ 3 次退避（无错误分类） | ✅ 3 次退避+Pro→Flash 降级 |
| LLM 内存缓存 | ❌ | ❌ | ❌ | ✅（但 key 有 P2-5 碰撞） |
| thinking 模式 | ❌ | Anthropic thinking（会崩 P1-13） | ❌ | ✅ DeepSeek extra_body（但 P0-2 超时错配） |
| 检查点/信号 | ❌ | ❌ | ✅（实现有 P1-4/5/6） | ✅ 同左 |
| 资源监控(psutil) | ❌ | ❌ | ✅（调节逻辑是死代码 P2-4） | ✅ 同左 |
| 补丁导出 | ❌ | ❌ | ✅ | ✅ |
| Reviewer sorry 检查 | ❌（P0-5） | ✅ _review_proof | ✅ | ✅ |
| VerificationAndIteration | ❌ 无此类 | ✅ 定义但未被调用+math 恒 True | ✅ 同左 | ✅ 同左 |
| 硬编码 API key | ❌（仅 env） | ❌（仅 env） | ✅ sk-8c0c... | ✅ 同左 |
| mock enter_dojo 设置 dojo_state | n/a（无此结构） | n/a（无此结构） | ❌ → P1-11 潜伏崩溃 | ✅ 修复 |
| tactic search 默认 (depth/width/iter) | 5/3/(≤30) | 5/3/(≤30) | 5/3/50 | 4/2/30 |
| 每节点候选 tactic 数 | 3 (0.3/0.4/0.5) | 3 | 3 | 2 (0.2/0.35) |
| Agentic 引擎名 | AgenticProver（无 Reviewer） | AxProverBaseSolver | AxProverBaseSolver | AxProverBaseSolver（+thinking+Pro 模型） |

### 5.3 各版本独有 bug（其余共有 bug 见 P 表标注）

- **FULL 独有**：P0-1 SyntaxError（7 处跨行字符串）；P0-5 Agentic 无 sorry 审查；`import pickle/aiofiles` 死 import；LLM 把异常包成 `"ERROR: ..."` 字符串但 `AgenticProver._propose` 不检查 → 把错误文本当证明编译 100 轮（FULL:1006-1014）。
- **FIN 独有**：P0-6 LeanDojoV2 伪造证明工厂；P1-12 LLM 零重试异常上抛；P1-13 anthropic thinking AttributeError；P1-14 无 mock 兜底 → ZeroDivision；`LeanDojoV2Integration.trace_repository` 只拼路径不 trace（FIN:543-546）；`httpx.get` 无 timeout（FIN:405）；全文 `chr(10)` 代替 `\n` 的混淆写法。
- **KAG 独有**：已知 `KAG:752-754` 协程未 await TypeError；P1-11 mock dojo_state 未赋值 → Phase 4 ValueError（OPT 已双双修复）。
- **OPT 独有**：P0-2 llm_timeout/thinking 错配；P0-9 虚构 v4 模型+升级逻辑只认精确字符串 `"deepseek-coder"`（OPT:366）；P2-5 hash 缓存键；P2-18 之①（OPT:1692 死路径新 await 回归）；P2-8 LLM_BASE_URL 死分支。

### 5.4 四版本共有 bug（同一段代码复制四次）

P1-1 rfl 死阶段、P1-2 停滞检测、P1-3 escalation 死锁、P1-7 V39_MOCK_MODE/CLI 参数、P0-3 之验证缺失（FULL 无 verifier 类，其余三版 verify 不被调用且 math 恒 True）、mock `_mock_execute_tactic`/`_mock_generate` 启发式全同（apply 分支假阳性）、`print_progress` 除零、`create_mock_tasks` 数据全同（FULL/KAG/OPT 用 Unicode ∀∃⊢≃≅≫；FIN 被 ASCII 化为 `forall/exists/|-/~=/>>`——注意 FIN 的 `_predict` 关键词匹配也随之改为英文词，两套启发式不等价）。

---

## 六、重构建议清单（按优先级排序）

**第一梯队（不修则结果不可信 / 跑不完）**
1. 接通验证链：所有 `result.success=True` 分支强制过 `verify()`；实现真正的 `_mathematical_correctness_check`（重放编译 + `#print axioms` 无 `sorryAx` + 词边界 sorry/admit 黑名单）；删除 mock 的 apply 假阳性分支（OPT:659-660）。
2. 修时间预算：thinking 调用超时 ≥240s 与 `llm_timeout` 解耦；给 `resolve_batch` 加全局 wall-clock 预算（如 10h）与阶段级预算；Phase 2/3/4 改 `asyncio.gather`+Semaphore 并发（当前全串行是 12h 不可达的最大结构性原因，与 P0-2 叠加）。
3. 模型名落地：删除虚构 `deepseek-v4-flash/pro` 与硬编码 key；启动时校验模型可用性；重试按 HTTP 状态码分类，4xx fail-fast 并熔断。
4. 修 Lean 资源生命周期：超时即废弃 dojo 新建（P0-7）；FIN/FULL 迁移 OPT 的 dojo 管理（P0-4/P0-8）；heapq 加单调计数器（P1-10）。

**第二梯队（Kaggle 容错闭环）**
5. checkpoint：原子写（tmp+rename）、`to_dict` 不用 asdict deepcopy、恢复时合并 prev_results 并按 phase/status 跳过（P1-4/P1-5）。
6. 优雅停机：`resolve_batch` 每任务轮询 `_shutdown_event`；紧急保存带当前 results（P1-6）。
7. 修死线：rfl 谓词（P1-1）、escalation/axiom_quota（P1-3）、V39_MOCK_MODE 与 CLI 参数真正生效（P1-7）、SorryDB 失败不注入假任务（P1-9）。

**第三梯队（卫生）**
8. 删除或修复死代码：`SorryResolutionOrchestrator`（OPT:1669-1705）、`_adjust_concurrency`、`_get_session`、`get_kaggle_paths`、`LeanDojoV2Integration`、FIN 的 transformers 加载。
9. 缓存与状态：LLM cache key 改 sha256、批量 flush 加锁、memory_cache 加 LRU 上限（OPT:413/687）。
10. 工程化：`py_compile`+ruff+mypy 进 CI（可立即捕获 FULL 的 SyntaxError 与 KAG:754 这类未 await）；四个文件收敛为单一事实源（当前是四份漂移副本，P 表中共有的 10+ 条 bug 都是复制粘贴产物）；文档按 OPT 重写并删除虚构性能数字（"pass@32 78%"、"75.8% 预测准确率"、"消解率 91% ✅"均无实现支撑）。

---

*审计完成。除已知背景 3 条与 KAG:754 已知 bug 外，本报告全部为本次深挖新发现；行号已逐一对照源文件核验。*
