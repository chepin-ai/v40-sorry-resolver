# v40 Sorry Resolver — 本地实现 / 测试 / 验证详细指引

> 版本：v40.0（2026-07-19）｜仓库：`project/`（git master）｜Kaggle 单文件：`v40_kaggle_bundle.py`
> 本指引面向**你的本地机器 / Kaggle / 任意 Linux 环境**的复现、部署与持续迭代。

---

## 1. 交付物总览

| 路径 | 内容 |
|---|---|
| `project/` | v40 完整源码（git 仓库，含 224+ 测试、README、CHANGELOG、SPEC） |
| `v40_kaggle_bundle.py` | Kaggle 单文件 bundle（自解包 + CLI 入口） |
| `lean_mini_project/` | 验证用真实 Lean 4 项目（11 个真实 sorry，toolchain v4.20.0） |
| `patch_lean_dojo.py` | lean-dojo 4.20.0 幂等补丁（FIFO 双向 + ProofFinished 修复） |
| `bootstrap_lean_env.sh` | 环境一键脚本（幂等、版本锁定） |
| `verify_subprocess_smoke.py` / `dojo_e2e_proof.py` / `trace_noapi.py` | 三条验证通路的独立冒烟脚本 |
| `audit_correctness.md` / `audit_performance.md` | v39 审计（47 bug + 性能量化） |
| `benchmark_results.md` / `regression_final.md` / `final_verification.md` | 实测基准与回归证据 |
| `frontier_atp.md` / `frontier_resources.md` | 前沿调研（已集成项与路线图的依据） |
| `env_report.md` / `dojo_breakthrough.md` | 环境事实与 Dojo 攻坚根因链 |

## 2. 本地环境搭建（逐步）

### 2.1 系统要求
Linux x86_64，≥4GB RAM（跑 dojo_v2 交互验证建议 ≥8GB；单 REPL ≈0.8GB RSS），Python 3.10+，无 root 也可。

### 2.2 Lean 4 工具链
```bash
# 方式 A（直连可达时）：
curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y --default-toolchain none
# 方式 B（GitHub 受限时，经代理）：
curl -L https://ghfast.top/https://github.com/leanprover/elan/releases/download/v4.2.3/elan-x86_64-unknown-linux-gnu.tar.gz | tar xz && ./elan-init -y --default-toolchain none
export PATH="$HOME/.elan/bin:$PATH"
elan toolchain install leanprover/lean4:v4.20.0   # 或直接下载 tar.zst + 软链（见 env_report.md）
elan default leanprover/lean4:v4.20.0
```
也可直接 `bash bootstrap_lean_env.sh`（幂等，含全部上述逻辑）。

### 2.3 Python 依赖
```bash
pip install -r project/requirements.txt          # 网络受限时加 -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install lean-dojo==4.20.0 gitpython zstandard # 可选：仅 dojo_v2 通路需要
python3 patch_lean_dojo.py                        # 可选：装完 lean-dojo 后必跑（幂等）
```

### 2.4 验证环境可用（3 分钟自检）
```bash
cd lean_mini_project && lake build               # 应过，恰好 11 条 'declaration uses sorry' warning
python3 ../verify_subprocess_smoke.py            # subprocess 通路：期望全 PASS
python3 ../trace_noapi.py                        # dojo 通路：trace（~75-300s，免 GitHub API）
python3 ../dojo_e2e_proof.py                     # dojo 通路：rfl → 'no goals'，rc=0
```

## 3. 配置（.env）

```bash
cp project/.env.example project/.env
```
| 变量 | 角色 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | **Orchestrator**（规划/调度/协调/评估/动态重构策略） | 你的 key1 |
| `DEEPSEEK_API_KEY_2` | **Prover**（主力证明生成） | 你的 key2 |
| `KIMI_API_KEY` | **Critic**（评审/互评估/lesson 摘要） | moonshot key |
| `LONGCAT_API_KEY` | **Explorer**（tactic 多样性采样） | 注意：你提供的 test key 对 chat 返回 401 `invalid_api_key/无效的AppId`，需向 LongCat 申请有效 chat key；缺失时自动 fallback，不阻塞 |
| `DEEPSEEK_MODEL` / `DEEPSEEK_REASONER_MODEL` | — | 默认 `deepseek-v4-flash` / `deepseek-v4-pro`；旧别名 `deepseek-chat`/`deepseek-reasoner` **2026-07-24 退役**，引擎启动时两阶段探测自动回退 |
| `V40_VERIFIER` | — | `subprocess`（默认，推荐）/ `dojo` / `mock`（仅测试） |
| `V40_NUM_WORKERS` | — | 本地 4-8；Kaggle 16 |

> **安全必做**：v39 曾把 `sk-8c0c461a…` 硬编码进源码并随文件分发——该 key 视同泄露，请立即在 DeepSeek 控制台**轮换/作废**，Kaggle 侧用 `kaggle_secrets`（`from kaggle_secrets import UserSecretsClient`）注入，不要再写进任何会被分享的文件。

## 4. 运行

### 4.1 本地 CLI
```bash
cd project
python -m v40_sorry_resolver --help
# 对你自己的目标领域子目录实测（用户诉求"调整 lean_project_paths 到目标子目录"）：
python -m v40_sorry_resolver \
  --project-paths /path/to/your_lean_project/目标子目录 \
  --workers 8 --wall-clock-budget 36000 --output-dir ./runs/run1
# 先体检再求解（扫描 + 4 provider 健康检查，不调 LLM 求解）：
python -m v40_sorry_resolver --project-paths ... --dry-run
# 断点续跑（Kaggle 抢占/本地中断后）：
python -m v40_sorry_resolver --project-paths ... --resume
```
前提：目标项目能 `lake build`（含 sorry warning 没关系）；sorry 扫描器按"向上最近 theorem/lemma 声明"定位定理名。

### 4.2 SorryDB 真实数据集
```python
from v40_sorry_resolver.sorrydb import SorryDBClient
tasks = SorryDBClient(endpoint_or_path).load()   # 支持本地 JSON/JSONL 快照或远程 URL；格式见 frontier_resources.md
```
开启 `sorrydb_mode`（config）后 verify 执行防作弊协议：sorry 恰减 1 + statement 不变 + 可叠加 `#print axioms` 无 sorryAx。

### 4.3 Kaggle
1. 新建 Notebook → Add Data 上传 `v40_kaggle_bundle.py`（或直接粘贴到 cell）
2. Add-ons → Secrets 配置四个 key（名称同 .env 变量）
3. 首 cell：`!pip install -q openai && !curl -sSf https://elan.lean-lang.org/elan-init.sh | sh -s -- -y` 及 toolchain 安装（详见 README §Kaggle；12h 预算映射 workers=16 / wall_clock=36000 / soft_deadline=32400 已内置自适应降级）
4. `!python v40_kaggle_bundle.py --project-paths <你的项目> --workers 16`
- 实测外推：当前架构 100 任务 ≈0.6-3h，928 任务 ≈5.9h < 12h 上限（regression_final.md §对比表）

### 4.4 三条验证通路选择
| 通路 | 何时用 | 说明 |
|---|---|---|
| `subprocess`（默认） | 一切场景 | 整文件编译判定，~0.2s/次，防假阳性黑名单 |
| `dojo`（dojo_v2） | 需要 tactic 级状态/逐步交互时 | 需 lean-dojo+补丁+trace；单 REPL 0.8GB，并发 ≤核数 |
| `mock` | 仅单元测试 | 结果标 [UNVERIFIED]，不进统计 |

### 4.5 自包含单文件与 GitHub 运行（v40_standalone.py，推荐入口）

`v40_standalone.py` 是**全自举单文件**（≈190KB）：内嵌完整引擎源码 + lean_mini_project + CLI help，裸 Python 3.10+ 环境即可运行——自动装 Lean 4.20.0 工具链（elan 优先，失败回退 ghfast 代理直连 tarball，30s 连接/读超时 + 低速镜像熔断 + Range 断点续传）、自动 pip 装 openai/httpx/zstandard（tuna 镜像兜底）。

```bash
# 一键下载即跑（零成本自检，mock LLM，真实 Lean 验证）：
curl -sL -o v40_standalone.py https://raw.githubusercontent.com/chepin-ai/v40-sorry-resolver/master/v40_standalone.py
python3 v40_standalone.py --self-test
# 期望输出：solved ≥7/11、verify_pass_rate 1.00、Hard 拒收 2/2 → SELF-TEST PASS
# 首次需下载 ~364MB Lean tarball，属正常；半死镜像会在 ~30s 内切换，不会卡死

# 真实求解：github: 任务源（浅克隆，直连失败自动走 ghfast；子目录/@ref 可选）
python3 v40_standalone.py \
  --project github:chepin-ai/v40-sorry-resolver/examples/lean_mini_project \
  --workers 4 --wall-clock-budget 1500 --output-dir ./runs/gh_run
```

**Kaggle 正确姿势**（对照 v39 失败场景）：
1. **二选一**：Add Data 上传 `v40_standalone.py` 后 `!python v40_standalone.py ...`；或 notebook cell 直接 `%run` 前先 `!curl -sL -O <raw URL>`。**不需要**手动装 elan/Lean/pip 依赖（bundle 全自举；§4.3 第 3 步的手动安装对单文件是可选冗余）。
2. **Secrets 注入**：Add-ons → Secrets 配置 `DEEPSEEK_API_KEY` / `DEEPSEEK_API_KEY_2` / `KIMI_API_KEY` / `LONGCAT_API_KEY`；bundle 启动时自动读取 env / kaggle_secrets / .env 三个来源，**无 key 时拒绝真实运行**（只允许 --mock-llm / --self-test），不会空转。
3. **工具链复用**：首次 bootstrap 后工具链落在 `~/.v40/toolchains`（或 elan），同会话后续运行秒级跳过；下载缓存 `~/.cache/v40/downloads/` 支持被杀后续传（注意：缓存文件**完整**但工具链未装完的极端情形下重跑会报 416——删掉该缓存文件即可，属已知边缘）。
4. **mathlib 0-sorry 属正常**：若 `--project` 指向 mathlib（或任何无 sorry 的项目），扫描结果 0 任务、正常退出，不是故障——v39 的"verifier init failed"才是故障；v40 裸环境会自动补齐工具链。请把任务源指向**含真实 sorry 的项目**（如上面的 examples/lean_mini_project 或你自己的项目子目录）。

## 5. 测试与验证

```bash
cd project
python -m pytest tests/ -q                    # 全量（含真实 Lean 用例；缺工具链时那两个文件自动环境性失败/skip）
python -m pytest tests/ -q -W error::RuntimeWarning   # 严格模式（async 纪律）
```
分层：M1 核心 97 项｜M2 验证/扫描 44 项｜M3 引擎 29 项｜fix 轮 +29 项｜frontier 轮 +48 项｜dojo_v2 11 项。验收标准与 e2e 复现步骤见 `regression_final.md`。

## 6. 需要你（本地/外部）配合的事项

1. **轮换泄露的 DeepSeek key1**（见 §3 安全必做）——最高优先级。
2. **LongCat chat key**：当前 `ak_20J2OE9o…` 对 chat 端点 401（`/models` 却 200，属服务端账号/AppId 问题），需申请有效 key；引擎对其余三角色无依赖阻塞。
3. **目标领域项目**：把你的真实 Lean 项目路径（可精确到子目录）传给 `--project-paths`；项目须可 `lake build`。若依赖 mathlib：首次 `lake exe cache get` + build 需 1-3h（一次性），dojo trace 另需 0.5-2h（一次性，缓存复用）。
4. **GitHub token**（你提供的 `ghp_KoWpt…`）：仅调研/trace 远程 repo 时用于提高 API 限额；本地 mini 项目走 GitPython 不需要；如分享给他人请先撤销。
5. **算力**：Kaggle 12h 硬上限 + 30h/周 GPU 配额；本地跑 mathlib 级项目建议 8C16G+。

## 7. 持续迭代路线（技术债与下一步）

**已清理的债**：v39 全部 47 条 bug（9 P0/14 P1/24 P2）核销；LeanDojo#250 通路攻破；模型名迁移预案。
**遗留限制**（详见 regression_final.md §遗留）：dojo REPL 无内存上限（4.20 忽略 -Dweak.max_memory）；trace 缓存容器本地化；mathlib 规模未实测；SorryDBClient 尚未接 CLI 参数。
**下一步建议**（按 ROI 排序，依据 frontier_atp.md Top-8）：
1. 常驻 REPL 池 + import 头 LRU（Kimina Lean Server 模式，验证吞吐再提 1.5-2×）
2. LeanInteract 作为第三验证后端（SorryDB 官方栈，pip lean-interact）
3. APOLLO 式失败子引理隔离-重证-重组（采样预算 ÷100 量级收益）
4. Planner-Prover 共享子目标缓存 + 失败重规划（BFS-V2/Hilbert 收敛架构）
5. LongCat-Flash-Prover 560B 本地/云端部署为形式推理专家路（权重 MIT 已开源）

## 8. 故障排除 FAQ

| 症状 | 处置 |
|---|---|
| `lake: command not found` | `export PATH="$HOME/.elan/bin:$PATH"`，写进 shell rc |
| pip 下载极慢/超时 | 全程加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`；GitHub 资源走 `https://ghfast.top/` 前缀 |
| dojo 用例 skip "not traced" | 跑 `python3 trace_noapi.py`（缓存随容器，重建后需重跑） |
| LLM 全 4xx | 检查 key 是否已轮换/欠费；引擎会熔断并 WARNING，不会空转烧钱 |
| Kaggle 断点 | `--resume` 从 checkpoint 恢复（已 SOLVED 自动跳过、计数合并） |
| 验证全 False | 先跑 §2.4 自检；确认目标项目 `lake build` 独立可过 |
