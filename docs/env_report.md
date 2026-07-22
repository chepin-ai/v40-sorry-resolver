# 环境报告 — 真实 Lean 4 + LeanDojo 验证环境（ENV 阶段产出）

### 环境版本
| 组件 | 版本 | 说明 |
|---|---|---|
| OS | Debian 12 (bookworm), x86_64, 2 CPU / 4 GB RAM / 23 G 空闲 | 无 root（apt 不可用） |
| Python | 3.12.12 / pip 25.0.1 | |
| elan | 4.2.3 (`~/.elan/bin`) | 经 GitHub releases 直接下载二进制安装（`--default-toolchain none`） |
| Lean | **4.20.0**（主用），4.15.0（备用） | 手动下载 tar.zst + 软链到 `~/.elan/toolchains/leanprover--lean4---v4.20.0`，elan shim 正常解析 |
| Lake | 5.0.0-1165156 (4.15) / 对应 4.20 | |
| lean-dojo | 4.20.0 | PyPI 仅此一版；已打补丁（见下） |

### 网络实况（重要）
- `pypi.org` 可达但 **`files.pythonhosted.org` 被墙/极慢** → 必须用镜像：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ...`（ray 74 MB 等才能装上）
- `release.lean-lang.org` 可达，但工具链下载实际 302 跳转到 GitHub asset，**极慢（~24 KB/s）** → 用代理 `https://ghfast.top/https://github.com/...`（~3.8 MB/s，同一 origin 文件）
- `github.com` 间歇性 000/403/200；`raw.githubusercontent.com`、`api.github.com`（有 60 次/时匿名限额）、`codeload.github.com` 基本可用
- 沙箱**无 zstd 二进制且无 root** → 用 `pip install zstandard` + Python tarfile 解包

### 样例项目 /mnt/agents/output/lean_mini_project
- `lakefile.toml` + `lean-toolchain`（`leanprover/lean4:v4.20.0`），**无 mathlib/无任何依赖**
- 11 个真实 sorry：`Trivial.lean`×5（`nat_refl`、`one_plus_one`、`and_comm_simple`、`or_intro_simple`、`list_length_append_simple`）、`Medium.lean`×4（`add_zero_custom`、`add_comm_custom`、`mul_two`、`list_map_id`）、`Hard.lean`×2（**故意不可证**：`impossible_zero_eq_one`、`unprovable_all_even`）
- git commit `eab5b625`；`lake build` ✅ 通过，恰好 11 条 `declaration uses 'sorry'` warning

### 通路结论
| 通路 | 状态 | 证据 |
|---|---|---|
| **subprocess `lake env lean <file>`** | ✅ **可用** | `verify_subprocess_smoke.py`：**27/27 PASS, rc=0**。每次编译 ~0.2 s。正确接受有效证明（`rfl`/`decide`/`simp`/`omega`/`exact ⟨h.2,h.1⟩`/归纳），正确拒绝错误证明（含两个不可证定理的全部 8 个候选）。附带发现：`n+0=n` 在 core 中定义即约（`rfl` 可证）、`omega` 可证 `n=n`——引擎定预期时需注意 |
| **lean_dojo trace** | ✅ 可用（259 s，须 `build_deps=False`） | 缓存于 `~/.cache/lean_dojo/gitpython-lean_mini_project-eab5b625…/lean_mini_project`（含 `.ast.json`×4 + 已编译 `Lean4Repl.olean`）。`build_deps=True` 会连 toolchain 1521 个模块一起 trace（2 核/4 GB 上 >50 min，不可行） |
| **lean_dojo Dojo `run_tac`** | ❌ 阻塞（见下） | 初始 goal JSON 能产出（`{"tacticState":"n : Nat\n⊢ n = n","sid":0,"error":null}`），但交互走不通 |

### Dojo 阻塞的根因分析（对后续引擎最重要）
1. **`--memory` 被拒**（已修）：Lean 4.20.0 的严格选项校验（`reparseOptions`，`Lean/Language/Lean.lean:308`）在 imports 可解析后拒绝 `--memory=N`（`max_memory` 已非注册选项）。→ 补丁改为 `-Dweak.max_memory=N`（未知时静默忽略）。
2. **elaboration 期间 stdin 被重定向到 /dev/null**（已修）：即上游 issue **lean-dojo/LeanDojo#250**（4.21 也中招；实测 4.15/4.20 同样）。`lean_dojo_repl` tactic 在编译期读 stdin 必得 EOF → `[fatal] failed to parse JSON …` → `DojoCrashError: Unexpected exit code: 1`。→ 补丁：`Lean4Repl.lean` 改从 `$LEAN_DOJO_REQ_FIFO`（FIFO）读请求、`dojo.py` 建 FIFO 写请求。**已验证 REPL 能处理请求**（调试日志记录了初始 goal 与 `rfl` 响应）。
3. **剩余未修（需进一步改 Lean4Repl.lean）**：
   - elaboration 成功路径下，REPL 写向 stdout 的响应到不了进程真实 fd 1（响应被吞；需把响应也改走 FIFO，绕开 elaboration 期 stdout）
   - `rfl` 收官时内核报错 `cannot add declaration [anonymous] … restricted to the prefix nat_refl`（4.20 的环境前缀限制导致 ProofFinished 判定协议漂移）
   - 补丁均为幂等：`python3 /mnt/agents/output/patch_lean_dojo.py`
4. **Do NOT 用 Lean ≤4.19 配 lean-dojo 4.20.0**：其 `ExtractData.lean` 需要 typed `Parser.parseHeader`（4.20 才有；4.15 实测编译失败）。

### 已知限制
- 4 GB 内存：`TACTIC_MEMORY_LIMIT` 默认 32 g（`-D` 形式下 4.20 上实为忽略）；被 kill 的后台 trace 会留下孤儿 `ExtractData` 进程拖垮整机，需清理
- Dojo `__exit__` 的 `kill_descendants` 对经 `lake env` 启动后被 reparent 的 lean 进程有清理竞态（观察到泄漏的 REPL 进程）——引擎并发跑多个 Dojo 时须自行回收
- GitHub API 匿名限额易触顶 → 用**本地 git repo** 构造 `LeanGitRepo(path, commit)`（走 GitPython，不碰 API）
- `bootstrap_lean_env.sh` 已写好但**未端到端复跑**；`verify_dojo_smoke.py` 中 Dojo 用例会超时

### 接入建议（给 sorry 消解引擎）
- **主通路用 subprocess**：将候选证明替换目标定理的 `sorry`（每个定理块恰一个 sorry，按 `theorem <name>` 定位），整文件 `lake env lean <file>`，rc=0 且无该定理 sorry warning = 接受。无需 trace、无需 Dojo，稳定快速
- 若要用 Dojo：`LeanGitRepo(本地路径, commit)` → `Theorem(repo, rel_file, name)` → `Dojo(thm, timeout, build_deps=False)`；先跑补丁脚本；并预留解决上述第 3 点的工作量
- trace 缓存键 = repo url+commit；改 toolchain/lean-dojo 后需清 `~/.cache/lean_dojo` 重 trace

### 磁盘产物
- `/mnt/agents/output/lean_mini_project/`（git 仓库，11 sorry，`lake build` 过）
- `/mnt/agents/output/verify_subprocess_smoke.py` ✅（27/27 PASS）
- `/mnt/agents/output/verify_dojo_smoke.py`（trace 模式可用；Dojo 用例暂超时）
- `/mnt/agents/output/patch_lean_dojo.py`（幂等，已应用：memory×2、FIFO×4 处补丁）
- `/mnt/agents/output/bootstrap_lean_env.sh`（幂等、版本锁定）
