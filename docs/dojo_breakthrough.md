# Dojo Breakthrough — lean-dojo 交互式 `run_tac` 验证通路攻坚报告

**结果：路径 1（FIFO 双向补丁 + ProofFinished 前缀修复）成功。**
Lean 4.20.0 + lean-dojo 4.20.0 上实现真实 tactic 级/状态级验证：初始 goal →
逐步 `run_tac` → **内核复核的 ProofFinished**。全程可复跑：
`python3 /mnt/agents/output/dojo_e2e_proof.py`（3 门全过，~3s）；
`python3 /mnt/agents/output/verify_dojo_smoke.py`（**17/17 PASS**）；
项目测试 `tests/test_dojo_v2.py` 11 项 + 全量 210 项通过。

---

## 1. 根因链（每条附证据）

### RC1 `--memory=N` 被 4.20 拒绝（此前已修）
`reparseOptions` 严格校验（`Lean/Language/Lean.lean:308`），`max_memory` 不再是
注册选项。→ 补丁 `-Dweak.max_memory=N`（未知时静默忽略；注意这同时意味着
**内存上限实际失效**，见 §4）。

### RC2 elaboration 期 stdin 被掏空（上游 #250；此前已修）
实证（本次容器，pid 1860 `lake env lean … TrivialManual.lean` 运行中）：
```
/proc/1860/fd/0 -> /dev/null
```
源码层机制不止 C++ 驱动：4.20 的 elaboration 被
`IO.FS.withIsolatedStreams` 包裹（`Lean/Elab/Command.lean:320`
`wrapAsyncAsSnapshot`），stdin 被换成**空 buffer**
（`Init/System/IO.lean:1656-1670`：`withStdin (Stream.ofBuffer bIn)`）。
→ REPL tactic 读 stdin 必得 EOF。修复：请求改走 `$LEAN_DOJO_REQ_FIFO`。

### RC3 elaboration 期 stdout 被捕获不回放（本次定位+修复）
**现象**：打过 RC2 补丁后，Dojo 初始化 300s 超时，pexpect 缓冲为空。
**实证**（手工复现，`LeanMiniProject/TrivialManual.lean` 内嵌
`lean_dojo_repl` tactic）：
- 进程已打开请求 FIFO（`/proc/1860/fd/3 -> /tmp/req.fifo`）——证明
  `initializeTacticRepl` 与 `printResponse` **已经执行**；
- 但 fd 1 指向的真实文件 **0 字节**（`/proc/1860/fd/1 -> /tmp/manual_out.txt`）。
**机制**：`withIsolatedStreams` 把 stdout/stderr 换成内存 buffer
（`Init/System/IO.lean:1656`），内容只在**该 command/task 结束时**回放
（`CoreM.lean:486`、`Command.lean:320`、`Language/Lean.lean:728`）。
REPL 在 elaboration 内死循环到 `exit` 才结束 → 捕获的输出**永不回放**。
对照实验：`example : True := by myprint; trivial`（elaboration 立即完成）
的 `IO.println` 能到 fd 1——输出被延迟回放而非丢弃，这解释了为何此前偶尔
"见过"初始 goal 的假象。
**修复**：响应改走第二条 FIFO `$LEAN_DOJO_RESP_FIFO`（`printResponse` →
`responseStream`，`IO.FS.Stream.putStrLn` + `flush`）；`dojo.py` 用
`os.open(O_RDONLY|O_NONBLOCK)` + `select` 带 deadline 读行。

### RC4 ProofFinished 收官撞上 4.20 环境前缀限制（本次定位+修复）
**现象**：`rfl` 后内核报
`cannot add declaration [anonymous] … restricted to the prefix nat_refl`。
**机制**：4.20 cmdline 默认开 async elaboration
（`Lean/Elab/Frontend.lean:152-153` 设 `Elab.async`）；在定理 `nat_refl` 的
async 分支里 env 带 `asyncCtx`，`AsyncContext.mayContain` 只放行
`declPrefix` 前缀下的新声明（`Lean/Environment.lean:410-411`）。
`Lean.addDecl`（`Lean/AddDecl.lean:80`）在 async 下走
`addConstAsync`（`Environment.lean:959-965`），sync 路径也有
`addDeclCore` 的前缀检查（`Environment.lean:667-673`）——REPL
`validateProof` 的匿名 `Declaration.thmDecl` 必然被拒。
**修复**：不复用 `addDecl`；改为在 `(← getEnv).unlockAsync`
（`Environment.lean:638`，官方注释即"忘记 async 上下文限制"）上直接调
`Lean.Environment.addDeclCore env0 maxHb decl none`，匹配
`Except Kernel.Exception` 拿成败。仍是**真内核复核**（`addDeclCheck`
extern 走 kernel type checker），检查用的 env 用完即弃，不污染当前分支。

---

## 2. 修复方案（diff 摘要；幂等脚本 `patch_lean_dojo.py` v2）

**`lean_dojo/interaction/Lean4Repl.lean`**（site-packages，trace 时随
`trace.py` 拷入被 trace 仓库；补丁脚本会把缓存里的副本同步并
`lake build Lean4Repl` 重建 olean）：
1. `printResponse` → 经缓存句柄写 `$LEAN_DOJO_RESP_FIFO`（未设置回退 stdout）；
2. `loop` 经 `requestStream` 读 `$LEAN_DOJO_REQ_FIFO`（RC2，此前已有）；
3. `validateProof` 内核检查 → `unlockAsync` + `Environment.addDeclCore`
   （RC4）；其余逻辑（parse/goals/`hasSorry`/mvar 检查）不动。

**`lean_dojo/interaction/dojo.py`**：
1. spawn 处建 `req.fifo`+`resp.fifo`，导两个环境变量，`resp_fd` 非阻塞打开；
2. `_read_next_line` 在 FIFO 模式下走 `_read_resp_fifo_line`
   （`select` + 行缓冲 + timeout→`DojoTacticTimeoutError`，EOF→`EOFError`）；
3. `__exit__` 关闭两个 FIFO fd/文件与临时目录；
4. `--memory` → `-Dweak.max_memory`（RC1，此前已有）。

协议不变量保持：行首 `REPL> ` 前缀、`{"sid","cmd"}` 请求/
`{sid,tacticState,error}` 响应、`tacticState=="no goals"` ⇒ ProofFinished、
`proof contains sorry` ⇒ ProofGivenUp。`run_tac` 判定链无需改动。

---

## 3. e2e 证据（原始 JSON 收发摘录，`dojo_e2e_proof.py`）

```
GATE 1 nat_refl:  init sid=0  "n : Nat\n⊢ n = n"
  {"sid": 0, "cmd": "rfl"}
  → {'tacticState': 'no goals', 'sid': 1, 'error': None}        # ProofFinished

GATE 2 impossible_zero_eq_one:  init "⊢ 0 = 1"
  {"sid": 0, "cmd": "rfl"}
  → {'error': "tactic 'rfl' failed, the left-hand side\n  0\nis not
     definitionally equal to the right-hand side\n  1\n⊢ 0 = 1"}  # 正确拒绝

GATE 3 and_comm_simple（状态级逐步）:
  {"sid": 0, "cmd": "apply And.intro"} → sid=1 "case left … ⊢ b  case right … ⊢ a"
  {"sid": 1, "cmd": "exact h.2"}       → sid=2 "case right … ⊢ a"
  {"sid": 2, "cmd": "exact h.1"}       → 'no goals'              # ProofFinished
  旁证：从 sid=1 重放错误 tactic "exact h.1" → type mismatch 错误（状态树正确）
```

- `verify_dojo_smoke.py`：9 可证定理全部 ProofFinished（含多行
  `induction … with` 整块 tactic），2 不可证定理 × 4 候选全部拒绝
  （`simp` 把 `0=1` 化归 `⊢ False` 属合法中间态，非成功）——**17/17 PASS**。
- 边缘：`sorry` ⇒ ProofGivenUp；`exact Eq.refl n` ⇒ ProofFinished。
- 单会话冷启动 ~0.6s（trace 缓存命中后），17 会话共 ~11s。

## 4. 剩余限制与运维注意点

1. **内存**：`-Dweak.max_memory` 形式下 4.20 实际忽略该选项——REPL 进程
   **无真实内存上限**。实测单 REPL 进程 RSS ≈ 0.8 GB（ps：`820848 KB`）；
   4 GB 机器上并发 Dojo 需自行限流（建议 ≤2-3 并发）并复用会话。
2. **并发模型**：每会话一个 `lake env lean` 进程，协议串行（一问一答）；
   `dojo_v2` 内部用 `asyncio.Lock` 串行化同 verifier 的交互。
3. **进程回收**：`kill_descendants` 对经 `lake env` 启动后被 reparent 的
   lean 进程存在竞态（env_report 已记）；`dojo_v2` 在 timeout/crash 时
   `drop_session` 强制 teardown。长跑引擎应定期核对
   `ps aux | grep "lake env lean"` 兜底。
4. **trace 缓存**：键 = repo url+commit，缓存在 `~/.cache/lean_dojo`（不在
   共享盘）；重装 lean-dojo 后必须重跑 `patch_lean_dojo.py`（它会同步并重建
   缓存仓库里的 Lean4Repl.olean）；全新容器先 `trace_noapi.py`（免 GitHub
   API；api.github.com 匿名限额 60/h 极易 403，trace 尾部解析 lean4 依赖
   元数据会撞限，本报告环境实测退避 321s 后才恢复）。
5. **消息通道**：FIFO 模式下响应的 `message` 字段恒为空（elaboration 期
   stdout/stderr 被捕获不回放，诊断信息拿不到）；错误文本走 `error` 字段，
   不受影响。
6. **判定语义**：ProofFinished 以 REPL 的 `validateProof` 全链为准（defeq
   核对 + 无 sorry + 无 mvar + 内核 `addDeclCore` 复核）；`sorry` 战术本身
   不报错，收官时才拒（ProofGivenUp）——与上游语义一致。
7. **`where` 结尾定理 / prelude 文件**仍为上游既有不支持项（DojoInitError）。

## 5. 产物清单

| 产物 | 说明 |
|---|---|
| `/mnt/agents/output/patch_lean_dojo.py` | 幂等补丁 v2（memory + 请求 FIFO + 响应 FIFO + 内核前缀修复 + trace 缓存同步） |
| `/mnt/agents/output/dojo_e2e_proof.py` | 验收门 e2e（3 门 + 原始 JSON），rc=0 |
| `/mnt/agents/output/verify_dojo_smoke.py` | 全量 17 会话冒烟（本次全过） |
| `/mnt/agents/output/trace_noapi.py` | 免 GitHub API 的 trace 驱动 |
| `project@v40_sorry_resolver/verify/dojo_v2.py` | `LeanDojoV2Verifier`（SPEC Verifier + `open_task`/`run_tactic` 战术级接口） |
| `project@tests/test_dojo_v2.py` | 11 项真实 Lean 测试（缺工具链/未 trace 自动 skip） |
| 分支 `dojo-breakthrough`（commit `fe0ff91`） | 集成提交 |
