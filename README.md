# v40 Sorry Resolver

面向 Lean 4 项目的异步 `sorry` 消解引擎：多 LLM 角色编排 + worker-pool 真并发 +
真实子进程验证链 + 原子 checkpoint 断点续跑。设计契约见 `SPEC.md`。

## 架构图

```
                        ┌────────────────────────────────────────────┐
                        │                  CLI 入口                   │
                        │  python -m v40_sorry_resolver (cli.py)      │
                        │  参数覆盖 → 扫描 sorry → 启动健康检查门      │
                        └───────────────┬────────────────────────────┘
                                        │ tasks
                                        ▼
                        ┌────────────────────────────────────────────┐
                        │     ResolutionPipeline (orchestrator.py)    │
                        │  asyncio.PriorityQueue + N 个 worker 协程   │
                        │  每任务相位链：rfl → direct → search →      │
                        │  agentic；成功一律过 verifier 复核才入账     │
                        │  预算：per-task 时间/token + 全局 wall +    │
                        │  soft deadline 降级；SIGTERM 优雅停机       │
                        └───┬───────────┬───────────┬────────┬───────┘
                            │           │           │        │
              ┌─────────────┘           │           │        └──────────────┐
              ▼                         ▼           ▼                       ▼
   ┌──────────────────┐   ┌────────────────────┐   ┌──────────────┐   ┌─────────────┐
   │  TacticSearch     │   │  AxProverV2        │   │ CriticAgent  │   │OrchestratorLLM│
   │  BFS tactic 搜索  │   │  提议→验证→ critique│   │ 评审/lesson  │   │ 规划+周期评估 │
   │  (PROVER/EXPLORER)│   │  每任务独立 notebook│   │  (CRITIC)    │   │(ORCHESTRATOR)│
   └────────┬─────────┘   └─────────┬──────────┘   └──────┬───────┘   └──────┬──────┘
            │                       │                     │                  │
            └───────────────┬───────┴─────────────────────┴──────────────────┘
                            ▼
              ┌─────────────────────────────┐        ┌────────────────────────┐
              │  MultiLLMRouter (llm/)       │        │ Verifier (verify/)      │
              │  角色→provider 路由+fallback │        │ subprocess(默认)/dojo/  │
              │  熔断器 / 1-token 健康探针   │        │ mock(仅测试,UNVERIFIED) │
              │  thinking→reasoner 模型路由  │        │ 词边界黑名单+拼接重编译 │
              └──────────────┬──────────────┘        └───────────┬────────────┘
                             ▼                                   ▼
              DeepSeek×2 / Kimi / LongCat              lake env lean (子进程)
                             │
                             ▼
        Cache(SQLite) / Checkpoint(原子写,可 resume) / MetricsCollector /
        EmergenceLog(批量落盘) / RunReport(run_*.json + summary)
```

## 四角色分配（SPEC 3.4）

| provider（配置键） | 角色 | 职责 | 默认模型 |
|---|---|---|---|
| DeepSeek key1（`deepseek_a`） | **ORCHESTRATOR** | 规划 / 调度 / 协调 / 周期性评估指标并输出策略调整 JSON | `deepseek-chat` |
| DeepSeek key2（`deepseek_b`） | **PROVER** | 主力证明生成（direct/search/agentic 提议）；`thinking=True` 时路由到 `deepseek-reasoner` | `deepseek-chat` |
| Kimi（`kimi`） | **CRITIC** | 证明评审 / 互评估 / lesson 摘要 | `moonshot-v1-8k` |
| LongCat（`longcat`） | **EXPLORER** | tactic 多样性采样 / 备选路线 | `LongCat-2.0` |

无 key（或启动健康检查失败）的角色按 CRITIC→PROVER→EXPLORER→ORCHESTRATOR 链
自动 fallback 并打 WARNING；4xx 连续 3 次熔断。启动时对所有 provider 做
**1-token chat 生成探针**（不是只看 `/models`——LongCat 曾出现 `/models` 200 但
chat 401 的假阳性），全部失败且未用 `--mock-llm` 时直接报错退出。

## 环境变量（照 `.env.example`）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek key1 = ORCHESTRATOR | （空=禁用） |
| `DEEPSEEK_API_KEY_2` | DeepSeek key2 = PROVER | （空=禁用） |
| `DEEPSEEK_BASE_URL` | DeepSeek 端点 | `https://api.deepseek.com/v1` |
| `DEEPSEEK_MODEL` | DeepSeek 聊天模型 | `deepseek-chat` |
| `DEEPSEEK_REASONER_MODEL` | thinking 调用的推理模型 | `deepseek-reasoner` |
| `KIMI_API_KEY` / `KIMI_BASE_URL` / `KIMI_MODEL` | CRITIC | `https://api.moonshot.cn/v1` / `moonshot-v1-8k` |
| `LONGCAT_API_KEY` / `LONGCAT_BASE_URL` / `LONGCAT_MODEL` | EXPLORER | `https://api.longcat.chat/openai/v1` / `LongCat-2.0` |
| `V40_VERIFIER` | 验证后端 `subprocess`/`dojo`/`mock` | `subprocess` |
| `V40_NUM_WORKERS` | worker 协程数 | `8` |

真实环境变量优先于 `.env` 文件；`.env` 已 gitignore，源码零硬编码密钥。

## 本地运行

```bash
pip install -r requirements.txt
# 真实验证需要 Lean 工具链：安装 elan + 项目对应 toolchain（如 leanprover/lean4:v4.20.0）
cp .env.example .env   # 填入 4 个 API key

python -m v40_sorry_resolver --help
python -m v40_sorry_resolver --dry-run            # 扫描 + 健康检查，不求解
python -m v40_sorry_resolver \
    --project-paths /path/to/lean_project \
    --workers 8 --wall-clock-budget 36000         # 正式求解（默认 resume）
python -m pytest tests/ -q                        # 测试（mock LLM，无网络）
```

产物在 `--output-dir`（默认 `./v40_work`，Kaggle 上自动切 `/kaggle/working/v40_work`）：
`cache.db`（SQLite LLM 缓存）、`checkpoint.json`（原子写）、`results/run_*.json` +
`_summary.txt`、`results/emergence_*.jsonl`。`--no-resume` 忽略旧 checkpoint；
`--task-limit N` 限制任务数；`--mock-llm` 全角色确定性假 LLM（与真实 key 严格隔离）。

## Kaggle 部署指南

```bash
python tools/make_kaggle_bundle.py   # 生成 dist/v40_kaggle_bundle.py（单文件，自解包）
```

Kaggle notebook 中：

```python
# 1) 依赖自举（bundle 只内嵌本包；缺依赖会 fail-fast 并提示安装命令）
!pip install -q "openai>=2.46,<3" "httpx>=0.28,<1"
# 2) Lean 工具链：在镜像中预装 elan + 项目 toolchain（见 bootstrap_lean_env.sh 的做法）
# 3) 把 API key 配进 Kaggle Secrets 对应环境变量，然后 12h 预算映射运行：
import v40_kaggle_bundle
v40_kaggle_bundle.main([
    "--project-paths", "/kaggle/input/my-lean-project",
    "--workers", "16",                 # 12h 预算映射（SPEC 3.13）
    "--wall-clock-budget", "36000",    # 10h，预留 2h 给构建/收尾
])
```

- **预算映射**：`workers=16`、`wall_clock_budget_s=36000`（soft deadline 默认 32400s
  后切换降级策略）；断网/超时被 SIGTERM 时会先做紧急 checkpoint 再退出。
- **resume**：默认开启。同一 output-dir 重跑自动跳过已 SOLVED/MARKED_AXIOM 任务、
  合并历史结果与 escalation 计数；换新任务集或想重来时加 `--no-resume`。

## 验证通路（SPEC 3.6–3.8）

| 后端 | 用途 | 说明 |
|---|---|---|
| `subprocess`（默认） | 生产 | 每次候选在独立临时目录拼接后跑 `lake env lean` 真实编译；词边界黑名单（剥注释后匹配 `sorry`/`admit`/`stop`）先行；超时杀整个进程组；不支持 symlink 的文件系统自动回退 copy |
| `dojo` | 实验（flag-gated） | LeanDojo 通路 v1；上游阻塞时 `init()` 显式抛 `DojoUnavailableError`，绝不静默降级 |
| `dojo_v2` | 交互式 tactic 级（可用） | `LeanDojoV2Verifier`（`verify/dojo_v2.py`）：LeanDojo 交互 `run_tac` 通路，真实状态级验证（初始 goal → 逐步 tactic → 内核复核的 ProofFinished）。需先跑 `python3 /mnt/agents/output/patch_lean_dojo.py`（双向 FIFO + 内核前缀修复，幂等）并 trace 一次目标仓库（`python3 /mnt/agents/output/trace_noapi.py`）。除 SPEC `Verifier` 协议外另暴露 `open_task(task)`/`run_tactic(task, state_id, tactic)` 供搜索/agent 逐步验证；e2e 证据 `python3 /mnt/agents/output/dojo_e2e_proof.py`，根因链见 `/mnt/agents/output/dojo_breakthrough.md` |
| `mock` | **仅测试** | 只认 `VALID` 标记；经此通路的结果全部 `unverified=True`，报告与 run json 标注 `[UNVERIFIED]` |

任何相位的“成功”都必须再过一次统一 `verifier.verify_proof` 复核才入账——没有
这条链路的“ solved”一律视为假阳性。

## 局限性

- 验证即编译：LLM 生成的不可信文本会以当前用户权限真实编译（半可信威胁模型），
  生产部署建议对 verify 子进程加 rlimit/只读挂载；Kaggle 上注意隔离。
- LeanDojo 旧通路 v1 被上游阻塞（详见 env_report），默认勿用；交互式需求走
  `dojo_v2`（已修复，见 dojo_breakthrough.md）：单会话串行、每任务一个 lean
  进程（~0.8 GB RSS），大规模并发需自行限流并复用会话。
- 搜索/agentic 长相位内部不轮询 shutdown，SIGTERM 后最坏延迟 ≈ 单任务时间预算
  （默认 600s）；token 预算按相位边界结算，单相位内可短暂超支。
- Kimi（moonshot-v1-8k）基准中倾向输出 Lean 3 语法，不适合直接生成 Lean 4 证明，
  其 CRITIC 评审角色不受影响。
- ` sorry` 定位基于文本扫描（非 elaborator 级），极端嵌套/宏生成的 sorry 可能漏扫。
