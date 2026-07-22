# v40 引擎最终回归验证报告（regression_final）

> 验证人：验证工程师（收尾轮）｜日期：2026-07-19
> 仓库：`/mnt/agents/output/project`（git master @ **`fe0ff91`** "feat(verify): dojo_v2 …"），**源码零改动**（全程只读运行，`git status` 干净，未执行任何 git 写操作）
> 范围：环境重建 → 全量 pytest（含真实 lake + dojo_v2）→ Kaggle bundle 冒烟 → 汇总报告。真实 e2e 不重跑，直接引用已核实的 `e2e_final` 数据。

---

## 0. 环境版本表（全新容器第 0 步重建，全部实测）

| 组件 | 版本 | 来源/说明 |
|---|---|---|
| OS | Debian 12 (bookworm), x86_64, 2 CPU / 4 GB RAM / 23 G 空闲 | 无 root |
| Python | 3.12.12 / pip 25.0.1 | |
| elan | 4.2.3 (`b6cec7e10`, `~/.elan/bin`) | 经 `ghfast.top` 代理下载（直连 GitHub 不可靠） |
| Lean toolchain | **4.20.0**（commit `77cfc4d1a4f6`，`leanprover/lean4:v4.20.0`） | tar.zst + `zstandard` Python 解包 + elan 软链；已 `elan default` 设为全局默认 |
| Lake | 随 4.20.0 工具链 | `lake build` mini 项目实测通过（恰好 11 条 sorry warning） |
| lean-dojo | 4.20.0 | + `python3 /mnt/agents/output/patch_lean_dojo.py` 幂等补丁，输出 **"all patches applied"**（memory×1、fifo×8、kernelfix×1） |
| openai SDK | 2.46.0 | |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | |
| gitpython / zstandard | 3.1.52 / 0.25.0 | |
| dojo trace 缓存 | 本轮新建 | `python3 /mnt/agents/output/trace_noapi.py` → **75.2 s** 完成（`build_deps=False`），缓存于 `~/.cache/lean_dojo/gitpython-lean_mini_project-eab5b625…/lean_mini_project`（容器本地） |
| 样例项目 | `/mnt/agents/output/lean_mini_project` @ `eab5b625` | 11 真实 sorry（Trivial×5 / Medium×4 / Hard×2 故意不可证） |

重建方式：`bash /mnt/agents/output/bootstrap_lean_env.sh`（幂等；第 5 步 sanity 的独立 smoke 脚本有 1 条期望偏差，见 §5 遗留限制 L-7，与 v40 引擎无关）+ `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pytest pytest-asyncio openai lean-dojo==4.20.0 gitpython`。

---

## 1. 全量 pytest 结果（`cd /mnt/agents/output/project && python -m pytest tests/ -q`）

### 1.1 最终结果（dojo trace 缓存就位后）

```
210 passed, 9 warnings in 35.13s        # 复跑确认：210 passed in 35.73s
```

**210/210 全绿，0 failed，0 skipped，两次运行一致。**

### 1.2 过程数字与失败分类

| 阶段 | 结果 | 说明 |
|---|---|---|
| 无 trace 缓存（容器初始） | **199 passed, 11 skipped**, 20.26s | 11 个 skip 全部在 `tests/test_dojo_v2.py`，原因统一为 `mini project not traced yet (run /mnt/agents/output/trace_noapi.py once)` —— **环境性 skip，属预期**（dojo trace 缓存在容器本地 `~/.cache/lean_dojo`，共享盘没有） |
| 重 trace（75.2s）后 | **210 passed, 0 skipped, 0 failed**, 35.13s / 35.73s | dojo_v2 的 11 个交互式 `run_tac` 用例全部真实通过 |
| 对照基线（无工具链容器，历史记录） | 185 passed + 14 环境性 failed + 11 skip | 14 个 failed 为 `test_subprocess_lean.py` 真实 lake 用例在无 `~/.elan` 时的环境性失败；装好工具链后即消 |

- **非环境性失败：0**。本轮无任何需要记录的 traceback。
- 9 条 warnings 均为 `pty.py:95 DeprecationWarning: ... forkpty() may lead to deadlocks`（lean-dojo REPL 经 pty 拉起所致，上游行为，良性）。

---

## 2. e2e 最终成绩（不重跑，引用已核实数据）

来源：`/mnt/agents/output/e2e_final/results/run_20260719_002020.json` + `_summary.txt` + `/mnt/agents/output/e2e_final_console.log`（本轮逐字段复核，与下列数字一致）。

| 指标 | 数值 |
|---|---|
| processed | **11** |
| **solved** | **9 / 11** |
| **verify_pass_rate** | **1.0**（9 个 solved 全部 `verification_passed=true`） |
| wall_time | **249.9 s** |
| tokens_used | **28 200**（逐条落盘可审计；2 564/任务） |
| API 调用（by_provider） | deepseek_a×1 + deepseek_b×38 + kimi×34 = **73 次** |

**by_status**：`SOLVED_RFL × 7` / `SOLVED_SEARCH × 1` / `SOLVED_AGENTIC × 1` / `FAILED_ALL × 2`
**by_solver**：rfl×7、tactic_search×1、axprover_v2×1、orchestrator×2

每任务轨迹（run json 实测）：

| 任务类型 | status | solver | 证明 | verify | tokens | time |
|---|---|---|---|---|---|---|
| Trivial/Medium ×7 | SOLVED_RFL | rfl（规则） | `rfl`/`omega`/`simp` | ✅×7 | 0 | 5.5–25.1 s |
| and_comm_simple（Trivial） | SOLVED_SEARCH | tactic_search | `exact And.symm h` | ✅ | 302 | 32.0 s |
| or_intro_simple（Trivial） | SOLVED_AGENTIC | axprover_v2 | `by exact Or.inl h` | ✅ | 1 482 | 79.2 s |
| impossible_zero_eq_one（Hard，故意不可证） | **FAILED_ALL** | orchestrator | proof=null | ❌ | 10 278 | 196.1 s |
| unprovable_all_even（Hard，故意不可证） | **FAILED_ALL** | orchestrator | proof=null | ❌ | 16 138 | 245.5 s |

**fix 前 → fix 后对比**（fix 前 = `benchmark_results.md` @ `05ef734` 三臂实验；fix = 提交 `a3a78de` round1 修复，含 theorem-shell extraction 即 BUG-2）：

- 总 solved：**7/11 → 9/11**；**Trivial：3/5 → 5/5**。
- 被救回的两个任务（`and_comm_simple` / `or_intro_simple`）正是 fix 前因 BUG-2（LLM 输出包 theorem 外壳、抽取不剥壳）一致失败的两个 Trivial——e2e_final 中二者证明体（`exact And.symm h` / `by exact Or.inl h`）与任务一一对应，修复因果链闭合。
- **防假阳性持续达标**：两个故意不可证 Hard 任务烧满 agentic 停滞耐心后 `FAILED_ALL`、`proof=null`、`verification_passed=false`，全程 0 假阳性。

---

## 3. Kaggle bundle 冒烟证据

| 步骤 | 命令 | 结果 |
|---|---|---|
| 构建 | `python tools/make_kaggle_bundle.py` | `bundle written: dist/v40_kaggle_bundle.py (105860 bytes); py_compile OK`（工具自检） |
| 独立编译 | `python -m py_compile dist/v40_kaggle_bundle.py` | **OK** |
| 临时目录解包冒烟 | `cp` 到 `mktemp -d` → `python -c "import v40_kaggle_bundle"` | **IMPORT OK**，`main` 函数就位 |
| CLI 冒烟 | `python v40_kaggle_bundle.py --help` | 自解包到 `./v40_src/v40_sorry_resolver`（/kaggle 不存在时的 fallback 路径），**完整打印 usage**：`--project-paths/--workers/--verifier {subprocess,dojo,mock}/--wall-clock-budget/--task-limit/--resume/--no-resume/--output-dir/--dry-run/--mock-llm/--log-level` |
| 产物归档 | `cp dist/v40_kaggle_bundle.py /mnt/agents/output/v40_kaggle_bundle.py` | 已拷贝（105 860 B），复 `py_compile` **OK** |

---

## 4. 与 v39 的终极对比表

v39 数字引自 `/mnt/agents/output/audit_performance.md`（run2 实测：12h 超时被杀）；v40 数字引自 `benchmark_results.md`（吞吐口径 Arm C）与本轮 §2 的 e2e_final。

| 指标 | v39 实测 | v40 实测 | 结论 |
|---|---|---|---|
| **真实 solved** | **0**（5 个"solved"全是 mock 启发式假阳性且被导出为 patch） | **9/11**（全部真实 `lake env lean` 编译通过，`verify_pass_rate=1.0`） | 0 → 9，质的修复（v39 P0-3 验证链已修） |
| **假阳性** | **5**（`predicted_success_rate>0.8 且含 "apply"` 即判成功） | **0**（两个故意不可证 Hard 全部 FAILED_ALL、proof=null） | 防假阳性达标 |
| **API 聚合吞吐** | P4 **54 调用/h**；P2/P3 ~350 调用/h | **~4 970 调用/h**（57 调用/41.3 s，4 worker 并行，benchmark_results.md §5） | **vs P4 ≈ 92×**；vs P2/P3 ≈ 14× |
| **每任务均值耗时** | P4 **23.3 min/任务**（22 迭代，全部跑满停滞） | **22.7 s/任务**（249.9 s / 11；最难的不可证 Hard 烧满预算也才 245.5 s） | **≈ 61×**（迭代 22→停滞耐心即停 + 任务级并行） |
| 每任务 LLM 调用 | P4 22 次/任务 | 73 调用/11 任务 ≈ 6.6 次/任务（solved 任务 7 个零 LLM） | ≈ 3.3× 降本 |
| 每任务 tokens | 未落盘（估数百万级/run） | **2 564**（28 200/11，逐条可审计） | 可计量性修复 |
| 验证通路 | mock（Lean 从不编译） | 真实 `lake env lean` 子进程逐候选编译 + 链级统一复核（任何 success 入账前必过 `verify_proof`）；新增 dojo_v2 交互式 `run_tac` 通路（本轮 11 用例全绿） | 达标 |
| 并行度 | 1（全文 0 处 gather，串行 await） | 4 worker 任务级并行（httpx 日志多 provider 交错实测） | 达标 |
| **12h 超时可完成性** | run2 **11h55m 被杀**，P4 仅完成 24/45 配额任务；928 任务外推 ~24–41.6h，**结构性不可行** | 11 任务/249.9 s；同 worker 数线性外推 928 任务 ≈ **5.9 h < 12 h**（且 mini 项目含 2 个必然烧满预算的不可证任务，真实语料均值更低） | **12h 预算内可完成** |
| 断点续跑 | 进程内缓存，重跑重付 | SQLite cache.db + 原子 checkpoint.json（tmp+fsync+os.replace）落盘 | 达标 |
| 成本口径 | run2：1 401 次调用/12h → 0 真实 solved | e2e_final：73 次调用/28 200 tokens/249.9s → 9 真实 solved（按 deepseek 公开价上界估算 < $0.05） | 数量级降本 |

---

## 5. 遗留限制清单

| # | 限制 | 性质 | 影响/缓解 |
|---|---|---|---|
| L-1 | **LongCat EXPLORER key 对 chat 接口无效**（`/models` 200 但 chat 稳定 401） | provider 侧 | health_check 已改为 1-token chat 探针（`llm/client.py:221-254`，BUG-3 已修），启动即可检出；运行时 3×4xx 熔断 + fallback，引擎不崩不伪造 |
| L-2 | **Kimi（moonshot-v1-8k）产出 Lean 3 语法**（`begin...end`），证明生成不可用 | provider/模型侧 | 仅任 CRITIC（评审）角色，e2e 中正常履职；证明生成由 deepseek 承担 |
| L-3 | **dojo trace 缓存容器本地化**（`~/.cache/lean_dojo`） | 环境 | 每个新容器需先跑 `trace_noapi.py`（本轮实测 75.2 s，`build_deps=False`），否则 dojo_v2 用例/通路 skip |
| L-4 | **Dojo 交互通路的上游残留**（env_report §Dojo 阻塞根因 #3：elaboration 期 stdout 响应路由、内核前缀限制） | 上游 lean-dojo 4.20.0 | 本轮 FIFO/kernelfix 补丁后 dojo_v2 全部 11 用例真实通过；subprocess 仍是主验证通路，dojo 为增强通路 |
| L-5 | **验证规模**：e2e 在 11 任务、无依赖 mini 项目上完成 | 覆盖范围 | mathlib 级大项目（trace 代价、import 图）尚未实测；`build_deps=True` trace 在 2C/4G 上不可行（>50 min） |
| L-6 | **不可证 Hard 任务烧满 agentic 预算**（196–245 s/任务）才 FAILED_ALL | 设计行为 | 停滞耐心机制的预期代价；此类任务主导 wall time（本 run 占 ~88%），预算/耐心可配置 |
| L-7 | **独立脚本 `verify_subprocess_smoke.py` 1 条期望偏差**：`exact List.length_append xs ys` 被拒（Lean 4.20.0 core 中该定理参数为 implicit，"function expected"） | smoke 脚本期望过时 | **与 v40 引擎无关**：v40 pytest 210/210 全绿（其内部真实 lake 用例全部通过）；如需消警，把候选改为 `simp` 或 `List.length_append`（不带显式参数） |
| L-8 | **`/mnt/agents/output` 为 9p/portal 挂载，不支持 symlink、IO 慢** | 环境 | BUG-7 已修（`subprocess_lean.py:465-473` symlink 失败回退 copytree/copy2）；仍建议工作目录放本地盘 `/tmp` 以求性能 |
| L-9 | pytest 9 条 `forkpty` DeprecationWarning | 上游 pty 行为 | 良性，不影响结果 |
| L-10 | 已泄露 key 仍建议轮换（v39 沿用至今，产物中均已脱敏） | 安全卫生 | 与引擎正确性无关 |

---

## 6. 结论

- **pytest：210/210 全绿**（199 passed + 11 环境性 skip → trace 后 210 passed，0 failed，两次复跑一致），无非环境性失败。
- **bundle：构建 + py_compile + 临时目录解包 import/--help 冒烟全部通过**，产物已归档 `/mnt/agents/output/v40_kaggle_bundle.py`。
- **e2e（引用已核实数据）：9/11 真实 solved、verify_pass_rate=1.0、0 假阳性**，fix 前后 7/11→9/11、Trivial 3/5→5/5 的因果链闭合。
- **v39 → v40 终极对比全部达标**：吞吐 54 → ~4 970 调用/h（≈92×）、每任务 23.3 min → 22.7 s（≈61×）、真实 solved 0 → 9/11、假阳性 5 → 0、12h 预算内可完成（外推 ≈5.9 h）。
- 仓库全程只读，master 停留于 `fe0ff91`，工作树干净。

**最终判定：v40 回归验证通过（PASS）。**
