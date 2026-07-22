# v40 sorry 消解引擎 — 合并后真实环境全量验证报告（T1）

> 验证人：系统验证工程师（T1 角色）｜日期：2026-07-18
> 仓库：`/mnt/agents/output/project`（git master `05ef734`，合并后最新，**源码零改动**）
> 全部数字来自真实运行日志/结果文件，路径见 §8 证据索引；API key 全部脱敏（前 8 位+省略号）。

---

## 0. 环境版本表（全新容器第 0 步重建）

| 组件 | 版本 | 来源/说明 |
|---|---|---|
| OS | Debian 12, x86_64, 2 CPU / 4 GB RAM | 无 root |
| Python | 3.12.12 / pip 25.0.1 | |
| elan | 4.2.3 (`~/.elan/bin`) | 经 `ghfast.top` 代理下载（直连 GitHub 超时） |
| Lean toolchain | **4.20.0** (`leanprover/lean4:v4.20.0`) | tar.zst 手动解包 + elan 软链 |
| lean-dojo | 4.20.0 | + `patch_lean_dojo.py` 幂等补丁（memory flag + REPL FIFO） |
| openai SDK | 2.46.0 | |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | |
| gitpython | 3.1.52 | |
| 样例项目 | `/mnt/agents/output/lean_mini_project` | 11 真实 sorry（Trivial×5/Medium×4/Hard×2 故意不可证），`lake build` ✅ 恰好 11 条 sorry warning |

**环境偏差记录（必须）**：`/mnt/agents/output` 是 portal/9p 挂载，**不支持 symlink**（`ln -s` → `EPERM Operation not supported`，实测）。引擎的 `SubprocessLeanVerifier._materialize_run` 依赖 `os.symlink`，因此 verify_tmp 若放在 output-dir 下会整体崩溃。→ 所有运行的工作目录放在本地盘 `/tmp`，运行结束后原样拷贝产物到 `/mnt/agents/output/e2e_run1/`。pytest 不受影响（用 `tmp_path`）。该限制本身记为 BUG-7。

---

## 1. 合并后全量测试（含真实 Lean，未 deselect）

```
$ cd /mnt/agents/output/project && python -m pytest tests/ -q
........................................................................ [ 42%]
........................................................................ [ 84%]
..........................                                               [100%]
170 passed in 12.36s        # 复跑确认：170 passed in 11.91s
```

**结果：170/170 全绿**（含 `test_subprocess_lean.py` 真实 `lake env lean` 用例）。无需修任何测试配置，未建 `t1-test-fixes` 分支。

---

## 2. 真实多 LLM 健康检查与基准

### 2.1 配置修正（仅 .env，未动源码）

- `LONGCAT_BASE_URL` 默认 `https://api.longcat.chat/openapi/v1` **404**（openresty），官方文档实际端点为 `https://api.longcat.chat/openai/v1`；默认模型 `LongCat-Flash-Chat` 该平台已不支持，`/models` 实测仅剩 `LongCat-2.0`。→ 在 `.env`（已 gitignore）中修正为 `LONGCAT_BASE_URL=https://api.longcat.chat/openai/v1`、`LONGCAT_MODEL=LongCat-2.0`。默认值过时记为 BUG-6。
- key（脱敏）：deepseek_a=`sk-8c0c4...`，deepseek_b=`sk-14601...`，kimi=`sk-vlUxZ...`，longcat=`ak_20J2O...`。

### 2.2 健康检查（引擎自身 `MultiLLMRouter.health_check_all()`）

| provider | 角色 | 模型 | health_check_all |
|---|---|---|---|
| deepseek_a | ORCHESTRATOR | deepseek-chat | ✅ true |
| deepseek_b | PROVER | deepseek-chat | ✅ true |
| kimi | CRITIC | moonshot-v1-8k | ✅ true |
| longcat | EXPLORER | LongCat-2.0 | ✅ true（但见下方警告） |

原始结果：`/mnt/agents/output/health_check_result.json` = `{"deepseek_a": true, "deepseek_b": true, "kimi": true, "longcat": true}`。

> ⚠️ **LongCat 假象**：`GET /models` 对该 key 返回 200，但 `POST /chat/completions` 稳定返回 **401 `{"error":{"code":"invalid_api_key","message":"无效的AppId: ak_20J2O...","type":"authentication_error"}}`**（curl ×3 复现；注意：LongCat 服务端会在错误消息中原样回显完整 key，产物已做脱敏替换）。即该 key 对生成接口无效；health check 因 `/models` 可用而误报健康（BUG-3）。基准与 e2e 中 LongCat 的实际生成全部失败，数字如实记录。

### 2.3 基准（脚本 `/mnt/agents/output/bench_llm.py`，结果 `bench_llm_results.json`）

每 provider：3× 相同短 prompt（"用 Lean 4 证明 ∀ n:Nat, n=n，只输出 tactic"）+ 1× 中等 prompt（证 `n + 0 = n`）。**质量判定用真实 Lean**：抽取候选 tactic 拼进 mini 项目编译（`nat_refl` / `add_zero_custom`），编译过才算"可用"。无 LLM 缓存，每次调用真实打 API。

| provider | API 成功 | 短 prompt 延迟 avg/min/max | 短 prompt tokens(in+out) | **Lean 实测可用** | 中等 prompt | 中等延迟 | **中等 Lean 可用** |
|---|---|---|---|---|---|---|---|
| deepseek_a (ORCH) | 3/3 | 1.12 / 0.98 / 1.31 s | 63+82 | **2/3** | ✅ induction | 0.80 s | ✅ |
| deepseek_b (PROV) | 3/3 | 0.77 / 0.69 / 0.84 s | 63+77 | **3/3** | ✅ induction | 0.99 s | ✅ |
| kimi (CRIT) | 3/3 | 3.80 / 3.21 / 4.96 s | 78+570 | **0/3** | ❌ Lean3 语法 | 0.49 s | ❌ |
| longcat (EXPL) | 0/3 | —（401，3 次后熔断器打开） | 0+0 | — | ❌ 401 | — | — |

关键观察：
- **DeepSeek 两 key 输出质量高但常包 theorem 外壳**（"只输出 tactic" 仍给 `theorem ... := by rfl`）；bench 的多候选抽取能救回（2-3/3 可用），但引擎内 `extract_lean_code` 救不回 → 直接关联 e2e 中两个 Trivial 任务失败（BUG-2，见 §5/§7）。
- **Kimi(moonshot-v1-8k) 产出 Lean 3 语法**（`begin...end`、`refl`、`intros n,` 带逗号），Lean 4.20 全拒；对"证明生成"角色不可直接用（其 CRITIC 角色是评审而非生成，e2e 中正常履职）。
- **Orchestrator 策略 JSON 能力（SPEC §3.12）**：给 deepseek_a 假 metrics snapshot + 当前 strategy → 返回**可解析 JSON，6 字段类型全对、范围全在 clamp 内**（depth=5∈[2,6], width=3∈[1,4], iter=10∈[3,12], share=0.4∈[0,0.6]），rationale 针对 stuck 任务给出合理调整理由。延迟 2.65s，306+155 tokens。**通过**。
- 成本控制：每 provider ≤5 次调用（deepseek_a 4+1 策略），全程 17 次。

---

## 3. 真实端到端：三臂对照实验

公共参数：CLI 入口（经 shim 包装，见下）`--project-paths /mnt/agents/output/lean_mini_project --workers 4 --wall-clock-budget 1800 --no-resume`，verifier=subprocess（真实 `lake env lean`），agentic 迭代默认 8，task 集相同（11 个）。
**CLI 不支持 per-task 预算参数**（实测 `cli.py` 无此 flag）→ 保持默认 600s；wall-clock 1800s 由 CLI 生效（日志确认 `wall budget 1800s`）。

**两处必要的非源码干预（均已记录，未改仓库任何文件）：**
1. `/mnt/agents/output/run_cli.py` shim：运行时 monkeypatch 绕过 BUG-1（CLI 调 `SorryScanner.scan()` 不传 paths 必崩，见 §7）；Arm B 额外经 `V40_SHIM_DISABLE_EVAL=1` 把周期评估间隔拉到 10⁹（近似无动态调整）。
2. 工作目录在 `/tmp`（§0 的 symlink 限制），产物事后拷贝到 `/mnt/agents/output/e2e_run1/{armA,armB,armC}/`。

| 臂 | 配置 | solved | wall | API 调用（按 httpx 日志计） | tokens（run json） | verify_pass_rate |
|---|---|---|---|---|---|---|
| **A** 单 LLM | 仅 deepseek_b；CRITIC/EXPLORER/ORCH 全部 fallback→deepseek_b（日志有 3 条 fallback WARNING） | **7/11** | **56.4 s** | **74**（deepseek×74） | **10 751** | **1.0** |
| **B** 多 LLM 无动态 | 4 key 全开 + 周期评估禁用 | **7/11** | **34.7 s** | **56**（ds×37 + kimi×16 + longcat×3 全 401） | **7 591** | **1.0** |
| **C** 多 LLM + 动态编排 | 4 key 全开 + 默认评估间隔（25 任务/600s） | **7/11** | **41.3 s** | **57**（ds×38 + kimi×16 + longcat×3 全 401） | **9 665** | **1.0** |

**每臂每任务轨迹**（三臂 status 完全一致；时间为 Arm C，完整版见各臂 run json）：

| theorem | 难度 | status（三臂相同） | solver | verification_passed | tokens | time |
|---|---|---|---|---|---|---|
| nat_refl | Trivial | SOLVED_RFL | rfl(规则) | ✅ | 0 | 0.9s |
| one_plus_one | Trivial | SOLVED_RFL | rfl | ✅ | 0 | 0.9s |
| list_length_append_simple | Trivial | SOLVED_RFL | rfl(simp) | ✅ | 0 | 1.2s |
| and_comm_simple | Trivial | FAILED_ALL | orchestrator | ❌ | 1 177 | 13.2s |
| or_intro_simple | Trivial | FAILED_ALL | orchestrator | ❌ | 1 054 | 13.7s |
| add_zero_custom | Medium | SOLVED_RFL | rfl | ✅ | 0 | 1.6s |
| add_comm_custom | Medium | SOLVED_RFL | rfl(omega) | ✅ | 0 | 4.3s |
| mul_two | Medium | SOLVED_RFL | rfl(omega) | ✅ | 0 | 3.3s |
| list_map_id | Medium | SOLVED_RFL | rfl(simp) | ✅ | 0 | 2.0s |
| **impossible_zero_eq_one** | **Hard(不可证)** | **FAILED_ALL** | orchestrator | ❌ | 3 192 | 35.3s |
| **unprovable_all_even** | **Hard(不可证)** | **FAILED_ALL** | orchestrator | ❌ | 4 242 | 39.0s |

**结论解读：**
- 三臂 solved 集相同（7 个全部由零 LLM 的规则阶段rfl/simp/omega 解出），两个 LLM 依赖的 Trivial 任务因 BUG-2（抽取不吃 theorem 外壳）在三臂中一致失败——**该引擎缺陷均匀作用于三臂，不影响对照公平性**。
- Arm B vs C：本任务集（11 任务、wall<60s）下周期评估的触发条件（≥25 任务 或 ≥600s）在 C 中仅因 BUG-5（`_last_eval_ts` 初始 0）在首个任务完成时触发 1 次（空 metrics，策略未变），之后不再触发；emergence 日志：B 有 1 条 strategy_adjustment（仅初始 plan），C 有 2 条（plan + 1 次即时 evaluate）。**动态编排在小规模 run 中无实际影响；三臂差异主要体现在成本/延迟**。
- 成本对比：多 LLM 臂（B/C）比单 LLM 臂（A）tokens 少 10-29%、wall 快 27-38%（kimi 分担 CRITIC/课程总结调用；Critic 复核由 kimi 承担后 deepseek 负载下降）。Arm C 比 B 多 1 次 orchestrator 评估调用（ds×38 vs 37）和相应 tokens（9 665 vs 7 591，含 rationale 长文本）。
- **API 故障如实记录**：LongCat 3 次 401 后熔断器打开（日志：`provider 'longcat' circuit breaker OPEN after 3 consecutive 4xx (last=401)`），之后 EXPLORER 调用立即返回 breaker_open 错误，引擎未崩溃、未伪造——符合降级预案，未降级为双臂（其余 3 provider 全程健康）。

---

## 4. Hard 任务防假阳性证据（验收核心）

引用 Arm C run json `/mnt/agents/output/e2e_run1/armC/results/run_20260718_225210.json`（A/B 臂同构）：

```json
{"theorem": "impossible_zero_eq_one", "status": "FAILED_ALL", "success": false,
 "verification_passed": false, "proof": null, "solver": "orchestrator",
 "error": "LeanMiniProject/Hard.lean:9:44: error: unexpected token 'theorem'; expected '{' or tactic
           LeanMiniProject/Hard.lean:9:42: error: unsolved goals  ⊢ 0 = 1 ..."}
{"theorem": "unprovable_all_even", "status": "FAILED_ALL", "success": false,
 "verification_passed": false, "proof": null, "solver": "orchestrator",
 "error": "... LeanMiniProject/Hard.lean:14:55: error: unexpected token 'theorem' ...
           error: unsolved goals  n : Nat ⊢ n % 2 = 0 ..."}
```

防假阳性机制（代码证据 + 实测双重确认）：
1. `_phase_chain` 统一复核（orchestrator.py，`# NO success is booked without verifier.verify_proof here`）：任何 phase 宣称的 success 必须再过一次真实 `verify_proof`，不过则 `success=False` 级联到下一 phase——v39 的 mock 判成功路径在 v40 中不存在。
2. 三臂 run json 中 7 个 solved 全部 `verification_passed=true` 且 `verify_pass_rate=1.0`；2 个 Hard 任务在三臂 × 全阶段（规则/direct/search/agentic 各轮候选）从未通过验证，最终 `FAILED_ALL`、`proof=null`。
3. Arm A 中 unprovable_all_even 的 LLM 候选（`apply` 不等式路线）被真实 Lean 拒绝：`tactic 'apply' failed, failed to unify 1 ≠ 0 with ¬n % 2 = 1`（run json diagnostics）。

---

## 5. 与 v39 实测数据（audit_performance.md）对比

| 指标 | v39 run2 实测 | v40 实测（Arm C，含真实 Lean 验证） | 倍数/结论 |
|---|---|---|---|
| 真实 solved | **0**（2166 调用/14.3h，5 个 mock 假阳性） | **7/11**（全部真实编译通过） | 0 → 7，质的修复（v39 P0-3 已修） |
| 假阳性 | 5/5（mock 启发式判成功并导出 patch） | **0**（Hard×2 三臂全部 FAILED_ALL） | 防假阳性达标 |
| API 聚合吞吐 | P4 54 调用/h；P2/P3 ~350 调用/h | **4 970 调用/h**（57 调用/41.3s，4 worker 并行） | **vs P4 ≈ 92×；vs P2/P3 ≈ 14×** |
| 单任务串行耗时 | P4 均值 **23.3 min**/任务（22 迭代） | 最难任务（Hard，烧满停滞耐心）**35-39 s**；LLM 任务 13s | **≈ 36-40×**（迭代 22→停滞 3 即停 + 并行） |
| 每任务 LLM 调用 | P4 22 次/任务 | agentic 失败任务 ≤ ~12 次（4 迭代×prover+critic 等），57 调用/11 任务 ≈ **5.2 次/任务** | ≈ 4× 降本 |
| 每任务 tokens | v39 未落盘（估数百万级/run） | **879 tokens/任务**（9 665/11），tokens 逐条落盘可审计 | 可计量性修复 |
| 验证通路 | mock（Lean 从不编译） | 真实 `lake env lean` 子进程，每次候选都编译 | 达标 |
| 并行度 | 1（全文 0 处 gather） | 4 worker 任务级并行（实测 httpx 日志多 provider 交错） | 达标 |
| 断点续跑 | 进程内缓存，重跑重付 | SQLite cache.db + checkpoint.json 落盘（各臂产物含 checkpoint） | 达标 |

**成本口径**：三臂合计 187 次真实调用 / ~28k tokens；按 deepseek-chat 公开价（输入 $0.27/M、输出 $1.10/M）估算**全程 API 成本 < $0.05**；单臂每任务 ≈ 690-977 tokens。v39 达 0 真实 solved 烧了 1401 次调用/12h。

---

## 6. 验收门逐条结论

| # | 验收门 | 结论 | 证据 |
|---|---|---|---|
| 1 | 全量 pytest 170/170 绿 | ✅ **通过**（170/170，两次：12.36s / 11.91s，未 deselect 任何用例，未改测试配置） | §1 |
| 2 | ≥3 provider health check 通过 | ✅ **通过**：deepseek_a/deepseek_b/kimi 真实生成健康；longcat `/models` 通过但 chat 稳定 401（精确错误已记录：`invalid_api_key / 无效的AppId`，BUG-3） | §2.2，`health_check_result.json` |
| 3 | Arm C：Trivial 真实 solved ≥3/5；Hard solved=0；verify_pass_rate=1.0 | ✅ **通过**：Trivial solved **3/5**（nat_refl、one_plus_one、list_length_append_simple，全部 verification_passed=true）；另外 2 个 Trivial 因 BUG-2 失败（引擎缺陷，已记录）；**Hard solved=0**；**verify_pass_rate=1.0** | §3/§4，armC run json |
| 4 | 数字来自真实运行日志，路径列出，key 脱敏 | ✅ **通过**：全部产物在 `/mnt/agents/output/e2e_run1/`、`bench_llm_results.json`、`health_check_result.json`；日志已扫描确认无 key 泄漏 | §8 |

---

## 7. 发现的源码 bug 清单（按严重级；均未修，仅记录）

| # | 严重级 | 位置 | 现象与证据 |
|---|---|---|---|
| BUG-1 | **P0** | `v40_sorry_resolver/cli.py:220` | `scanner.scan()` 未传 `paths`，而真实 `SorryScanner.scan(self, paths)` 必填 → **真实 CLI 入口 100% 启动即崩**（`TypeError: missing 1 required positional argument: 'paths'`）。conftest stub（`def scan(self)`，tests/conftest.py:513）与真实签名不一致，掩盖了该 bug；无 CLI 集成测试。本次 e2e 靠仓外 shim 绕过。 |
| BUG-2 | **P1** | `v40_sorry_resolver/engine/__init__.py extract_lean_code` | 三级抽取对"```lean 块内含完整 `theorem ... := by ...`"不剥外壳（level-3 的 `by` 回退只在无 fenced 块时触发）。真实 DeepSeek 高频输出该格式 → 拼接到 `by sorry` 位必报 `unexpected token 'theorem'` → **and_comm_simple / or_intro_simple 这类一次 `exact` 可证的任务在三臂全部 FAILED_ALL**（run json error 铁证）。prompt 说 "complete Lean 4 proof" 也诱导模型给完整定理。 |
| BUG-3 | **P2** | `llm/client.py health_check` | `/models` 200 即判健康；LongCat key `/models` 可用但 chat 401（`invalid_api_key/无效的AppId`）→ 健康检查假阳性，运行时才发现（3×401 熔断）。建议健康检查统一用 1-token chat 探针。 |
| BUG-7 | **P2** | `verify/subprocess_lean.py _materialize_run` | 硬用 `os.symlink`，在不支持 symlink 的文件系统（本环境 portal/9p 挂载）上验证整体崩溃（`OSError: [Errno 95]`）；verify_tmp 位置不可配。建议失败时回退 copy。 |
| BUG-4 | **P3** | `llm/client.py:411` vs `cli.py:247` | 客户端把 LLM 指标记到 `get_global_metrics()`（模块级单例），pipeline 却用 CLI 新建的 `MetricsCollector()` → run json `counts_by_provider` 恒为 `{}`（三臂实测）。 |
| BUG-5 | **P3** | `engine/orchestrator.py:216` | `_last_eval_ts = 0.0` 初始化 → 首个任务完成时 `monotonic()-0 ≥ 600` 恒真，立即触发一次空 metrics 的 `evaluate_and_adjust`（Arm A/C 各白烧 1 次 orchestrator 调用）。 |
| BUG-6 | **P3** | `config.py` / `.env.example` | LongCat 默认 base_url `openapi/v1`（404，正确为 `openai/v1`）、默认模型 `LongCat-Flash-Chat`（平台已下线，仅 `LongCat-2.0`）→ 开箱即用失败。 |

另记录非 bug 观察：CLI 无 per-task 预算 flag（任务书允许"若支持"）；`soft_deadline_s` 默认 32400 > 本次 wall 1800，validate() 正确告警（校验器工作正常）。

---

## 8. 证据索引（全部为真实运行产物）

| 内容 | 路径 |
|---|---|
| pytest 全量 | 命令输出见 §1（两次均 170 passed） |
| 健康检查 | `/mnt/agents/output/health_check_result.json`（脚本 `health_check.py`） |
| LLM 基准 | `/mnt/agents/output/bench_llm_results.json`（脚本 `bench_llm.py`，工作区 `/tmp/bench_work`） |
| Arm A 产物 | `/mnt/agents/output/e2e_run1/armA/`（`results/run_20260718_224158.json` + `_summary.txt` + `emergence_*.jsonl` + `console.log` + `checkpoint.json`） |
| Arm B 产物 | `/mnt/agents/output/e2e_run1/armB/`（`results/run_20260718_224930.json` 等） |
| Arm C 产物 | `/mnt/agents/output/e2e_run1/armC/`（`results/run_20260718_225210.json` 等） |
| CLI shim | `/mnt/agents/output/run_cli.py`（BUG-1 绕过 + Arm B 评估禁用，未触仓库源码） |
| API 调用计数口径 | 各臂 `console.log` 中 `POST .../chat/completions` 行逐条计数 |
| v39 对照数据 | `/mnt/agents/output/audit_performance.md` |
| 环境重建 | `/mnt/agents/output/env_report.md` + `bootstrap_lean_env.sh`（elan 改用 ghfast 代理） |

**API key 脱敏声明**：所有报告/日志中 key 仅出现前 8 位（`sk-8c0c4...`/`sk-14601...`/`sk-vlUxZ...`/`ak_20J2O...`）；已对 `/mnt/agents/output/e2e_run1/` 全文扫描确认无完整 key。
