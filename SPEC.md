# SPEC.md — v40 Sorry Resolver（唯一事实源 / Single Source of Truth）

> 本文件是 v40 重构的**契约**。所有实现代理必须严格遵守其中的文件树、接口签名、行为要求。
> 禁止单方面更改接口；发现问题回报主代理。
> 背景：`/mnt/agents/output/audit_correctness.md`（47 条 bug）、`/mnt/agents/output/audit_performance.md`（性能量化）、`/mnt/agents/output/env_report.md`（真实 Lean 环境）必须先读。

## 0. 项目目标

将 v39（单文件、mock 空转、全串行、验证链失效）重构为 v40：**真实验证 + 真异步并发 + 多 LLM 协作/互评估 + 动态自适应编排** 的 Lean 4 sorry 消解引擎。整合 SorryDB（任务源）+ LeanDojo-v2（可选验证通路）+ AxProverBase-v2（agentic 证明）+ LeanProgress-v2（优先级预测）。

硬性非功能要求：
1. **零硬编码密钥**：所有 key 走环境变量 / `.env`（`.env` 必须 gitignore）。提供 `.env.example`。
2. **零虚构模型名**：禁止 `deepseek-v4-flash/pro`。默认模型必须真实存在且启动时 health check 校验；4xx fail-fast + 熔断。
3. **真实验证为默认**：默认 Verifier = subprocess `lake env lean`；mock 仅在显式 `V40_VERIFIER=mock` 时启用，且 mock 结果在报告中标 `[UNVERIFIED]`，mock 的 apply 假阳性启发式必须删除。
4. **真并发**：任务级 worker pool（asyncio.Queue + N workers），禁止 Phase 内 `for task: await` 串行。
5. **预算即一等公民**：全局 wall-clock 预算、每任务时间/token 预算、thinking 单独超时（≥240s）与普通超时（≤60s）解耦、软截止自适应降级。
6. **断点续跑真实可用**：原子写 checkpoint（tmp + os.replace）、resume 合并 prev_results、各 Phase 按 status 过滤、SIGTERM 优雅停机（循环内轮询 shutdown event）。
7. **验证链强制**：任何 `success=True` 必须过统一 `verify()`：词边界 sorry/admit 黑名单 → 真实重编译 → rc==0 且无该定理 sorry warning。所有 solved 数字必须可复现。
8. 每个文件头部 `python -m py_compile` 必须通过；提交前模块自测必须跑通。
9. 代码注释/标识符用英文；用户可见的日志/报告可用中文。

## 1. 文件树（仓库根 = /mnt/agents/output/project/）

```
SPEC.md  README.md  .env.example  .gitignore  pyproject.toml(可选)
v40_sorry_resolver/
├── __init__.py            # 导出公共 API，__version__="40.0.0"
├── config.py              # [M1]
├── models.py              # [M1]
├── cache.py               # [M1]
├── checkpoint.py          # [M1]
├── metrics.py             # [M1]
├── llm/
│   ├── __init__.py
│   ├── client.py          # [M1]
│   └── router.py          # [M1]
├── verify/
│   ├── __init__.py
│   ├── base.py            # [M2]
│   ├── subprocess_lean.py # [M2]
│   ├── dojo.py            # [M2] 可选通路，flag-gated
│   └── mock.py            # [M2] 测试专用
├── progress.py            # [M2] LeanProgress-v2
├── sorrydb.py             # [M2] 任务源：本地项目 sorry 扫描 + 可选 SorryDB API
├── engine/
│   ├── __init__.py
│   ├── orchestrator.py    # [M3] worker pool 编排 + 预算 + 降级 + resume
│   ├── tactic_search.py   # [M3] 有界 beam search
│   ├── axprover.py        # [M3] AxProverBase-v2
│   └── agents.py          # [M3] Prover/Critic/OrchestratorLLM 多智能体
├── cli.py                 # [M3] argparse 真实接线
└── __main__.py            # [M3] python -m v40_sorry_resolver
tests/                     # 各模块代理附带 test_<module>.py
tools/
└── make_kaggle_bundle.py  # [M3] 生成 Kaggle 单文件 bundle（zip→base64 内嵌 + 入口）
```

## 2. 环境与外部事实（来自 env_report.md，必须遵循）

- Lean 4.20.0 经 elan 安装于 `~/.elan/bin`（`lake`, `lean` 可用）；lean-dojo 4.20.0 已装并打补丁（`/mnt/agents/output/patch_lean_dojo.py` 幂等）。
- **默认验证通路 = subprocess**：对目标项目副本执行 `lake env lean <file>`，mini 项目实测 ~0.2s/次。Dojo `run_tac` 通路当前被上游问题阻塞 → `verify/dojo.py` 仅作为 flag-gated 可选通路（`V40_VERIFIER=dojo`），默认不启用，不得影响主通路。
- 样例真实项目：`/mnt/agents/output/lean_mini_project/`（git repo，11 个真实 sorry：Trivial×5 / Medium×4 / Hard×2 故意不可证，toolchain v4.20.0，无 mathlib 依赖）。默认 `lean_project_paths=["/mnt/agents/output/lean_mini_project"]`，可被 CLI/配置覆盖指向用户目标子目录。
- pip 安装用镜像 `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple <pkg>`。
- 沙箱 2CPU/4GB；Kaggle 目标 4CPU/30GB/12h。并发默认值须两端可用。

## 3. 模块契约

### 3.1 [M1] `models.py`

```python
class PriorityLevel(Enum): P0_CRITICAL=0; P1_IMPORTANT=1; P2_MEDIUM=2; P3_LOW=3
class ProofStatus(Enum):
    OPEN; IN_PROGRESS; SOLVED_RFL; SOLVED_LLM_DIRECT; SOLVED_SEARCH; SOLVED_AGENTIC
    FAILED_ALL; MARKED_AXIOM; OPEN_PROBLEM; BUDGET_EXHAUSTED; UNVERIFIED_MOCK
SOLVED_STATUSES: frozenset  # 四种 SOLVED_*

@dataclass
class SorryTask:
    id: str                      # "{file}:{line}:{col}" 的 sha1[:12] 或显式 id
    project_path: str            # Lean 项目根（含 lakefile）
    file_path: str               # 相对 project_path 的 .lean 路径
    line_number: int; column_number: int
    theorem_name: str            # 必填（sorry 所在定理名；扫描器按行号向上最近声明定位，禁止取 context 第一个声明）
    goal_state: str = ""; surrounding_context: str = ""
    priority: PriorityLevel = PriorityLevel.P2_MEDIUM
    status: ProofStatus = ProofStatus.OPEN
    proof: Optional[str] = None
    predicted_steps: int = 0; predicted_success: float = 0.0
    escalation_level: int = 0
    attempts: list[dict] = field(default_factory=list)   # 有界：只保留最近 20 条摘要
    def cache_key(self) -> str   # sha256(project:file:line:col)[:16]
    def to_dict(self) -> dict    # 禁止 asdict 深拷贝外部对象；逐字段构造
    @classmethod from_dict(cls, d) -> "SorryTask"

@dataclass
class ResolutionResult:
    task_id: str; success: bool; status: ProofStatus
    proof: Optional[str] = None; solver: str = ""
    iterations: int = 0; tokens_used: int = 0; time_elapsed: float = 0.0
    remaining_goals: int = -1; verification_passed: bool = False
    unverified: bool = False     # mock 通路置 True
    error: Optional[str] = None
```

### 3.2 [M1] `config.py`

```python
@dataclass
class LLMProviderConfig:
    name: str; base_url: str; api_key: str; model: str
    max_concurrent: int = 4; timeout_s: float = 60.0; thinking_timeout_s: float = 300.0
    enabled: bool = True

@dataclass
class V40Config:
    # 任务源
    lean_project_paths: list[str] = field(default_factory=lambda: ["/mnt/agents/output/lean_mini_project"])
    sorrydb_endpoint: Optional[str] = None       # None = 不联网拉取
    # 验证
    verifier: str = "subprocess"                 # subprocess|dojo|mock（env V40_VERIFIER 可覆盖；mock 仅测试）
    lean_timeout_s: float = 30.0                 # 单次 lean 编译超时
    max_concurrent_lean: int = 4
    # 并发与预算
    num_workers: int = 8                         # 任务级并行 worker 数（Kaggle 可 16）
    wall_clock_budget_s: float = 36000.0         # 全局预算，默认 10h（Kaggle 12h 留 2h 余量）
    per_task_time_budget_s: float = 600.0
    per_task_token_budget: int = 200_000
    soft_deadline_s: float = 32400.0             # 超过则降级（见 3.10）
    # 求解参数（可被 OrchestratorLLM 动态调整，见 StrategyConfig）
    tactic_search_depth: int = 4; tactic_search_width: int = 2
    agentic_max_iterations: int = 8              # v39 的 21-100 → 8
    agentic_stall_patience: int = 3              # 连续 3 轮无改善即停（修 v39 停滞检测语义错误）
    thinking_max_tokens: int = 2048              # v39 8192 → 2048
    escalation_threshold: int = 3                # 跨轮持久化后真实可达
    axiom_quota: int = 45
    # LLM
    providers: dict[str, LLMProviderConfig] = ...  # 由 from_env 填充
    llm_temperature: float = 0.3
    # 存储
    work_dir: str = "./v40_work"                 # cache/checkpoint/results 根
    checkpoint_interval_tasks: int = 10
    @classmethod from_env(cls, env_file: str|None=".env") -> "V40Config"
    def validate(self) -> list[str]              # 返回问题列表；缺 key→相应 provider disabled 并 WARNING
```

环境变量契约（`.env.example` 必须一致）：
```
DEEPSEEK_API_KEY=        # Orchestrator 角色（规划/调度/协调/评估）
DEEPSEEK_API_KEY_2=      # Prover 角色
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_REASONER_MODEL=deepseek-reasoner
KIMI_API_KEY=
KIMI_BASE_URL=https://api.moonshot.cn/v1
KIMI_MODEL=moonshot-v1-8k
LONGCAT_API_KEY=
LONGCAT_BASE_URL=https://api.longcat.chat/openapi/v1
LONGCAT_MODEL=LongCat-Flash-Chat
V40_VERIFIER=subprocess
V40_NUM_WORKERS=8
```
模型默认值必须是真实存在的公开模型名；health check 失败 → 该 provider 自动 disabled（不 crash）。

### 3.3 [M1] `llm/client.py`

```python
@dataclass
class LLMResponse:
    text: str; model: str; provider: str
    prompt_tokens: int; completion_tokens: int; latency_s: float
    from_cache: bool = False; error: Optional[str] = None

class AsyncLLMClient:
    """OpenAI 兼容异步客户端（openai SDK AsyncOpenAI, max_retries=0 由自实现重试接管）。"""
    def __init__(self, cfg: LLMProviderConfig, cache: "Cache|None"=None): ...
    async def generate(self, prompt: str, system_prompt: str|None=None,
                       temperature: float|None=None, max_tokens: int=2048,
                       thinking: bool=False, cache_key: str|None=None) -> LLMResponse: ...
    async def health_check(self) -> bool: ...   # models.list() 或 1-token chat；4xx→False
    def stats(self) -> dict: ...                # calls, errors, tokens, latency p50/p95, breaker_state
    async def close(self) -> None: ...
```
行为要求：
- `temperature=None` 才用默认（修 v39 `or` 吞 0.0 的 bug）。
- 重试仅对 429/5xx/网络错误，指数退避（1s,2s,4s，最多 3 次）；4xx 立即返回 error 不重试；**连续 3 次 4xx → 熔断**（后续调用直接 error，health_check 可复位）。
- thinking=True 时使用 `thinking_timeout_s`（≥240s）并限制 `max_tokens ≤ config.thinking_max_tokens`；reasoning 模型路由（如 deepseek-reasoner）由 Router 决定，client 只做透传。
- cache_key 由调用方给语义前缀，client 内拼 `sha256(model+prompt+system+temperature)`，禁止 `hash()%N`。
- 每次调用记录 metrics（provider/model/latency/tokens/成败）到 `metrics.py` 的全局收集器。
- 连接复用（SDK 内部 httpx pool），`close()` 必须可重入。

### 3.4 [M1] `llm/router.py`

```python
class Role(Enum): ORCHESTRATOR; PROVER; CRITIC; EXPLORER
ROLE_TO_PROVIDER = {"ORCHESTRATOR":"deepseek_a", "PROVER":"deepseek_b", "CRITIC":"kimi", "EXPLORER":"longcat"}
class MultiLLMRouter:
    @classmethod def from_config(cls, cfg: V40Config, cache) -> "MultiLLMRouter": ...
    def client(self, role: Role) -> AsyncLLMClient       # 未启用→fallback 链 CRITIC→PROVER→EXPLORER→ORCHESTRATOR，并 WARNING
    async def health_check_all(self) -> dict[str,bool]   # 并发检查；失败的标记 disabled
    def available_roles(self) -> list[Role]
    def report(self) -> str                              # 各 provider 状态/调用量/成本表
```
角色语义（README 必须写明）：**DeepSeek key1 = Orchestrator**（规划/调度/协调/周期性评估指标并输出策略调整 JSON）；**DeepSeek key2 = Prover**（主力证明生成）；**Kimi = Critic**（证明评审/互评估/lesson 摘要）；**LongCat = Explorer**（tactic 多样性采样/备选路线）。无 key 的角色自动 fallback 并在报告标注。

### 3.5 [M1] `cache.py` / `checkpoint.py` / `metrics.py`

- `Cache`：SQLite(WAL) 持久层 + 有界 LRU 内存层；**单写协程**（asyncio.Queue + 唯一 writer task，修 v39 多写 SQLITE_BUSY 静默丢批）；接口 `async get/set(key, value, namespace="default")`、`async close()`；key 一律 sha256。
- `Checkpoint`：`save(tasks, results, phase, metrics)` 写 `path.tmp` 后 `os.replace`（原子）；`load()` 容错返回 None；task 序列化走 `SorryTask.to_dict`。
- `MetricsCollector`：线程/协程安全的计数器+直方图（list 有界 10k）；`record_llm_call(...)`、`record_task(...)`、`snapshot()`、`render_table()`；每 phase/每 provider 的吞吐、延迟 p50/p95、token、成功率。

### 3.6 [M2] `verify/base.py`

```python
@dataclass
class VerificationResult:
    ok: bool; error: Optional[str]=None; duration_s: float=0.0
    remaining_sorries: int = -1; diagnostics: str = ""
class Verifier(Protocol):
    async def init(self) -> None: ...
    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult: ...
    async def close(self) -> None: ...
def build_verifier(cfg: V40Config) -> Verifier   # 按 cfg.verifier 工厂；mock 需 cfg.verifier=="mock" 显式
```

### 3.7 [M2] `verify/subprocess_lean.py`（**默认通路，核心**）

`SubprocessLeanVerifier`：
1. **文本黑名单先行**：proof 剥注释后词边界正则 `\b(sorry|admit|stop)\b` 命中 → 立即 `ok=False`（修 v39 子串误判）。
2. **单 sorry 替换**：读 `project_path/file_path`，按 `theorem_name`+`line_number` 定位定理块（该块内应恰有 1 个 `sorry`；多 sorry 取含 line_number 的那个）；替换为 `by\n  <proof 各行缩进>`（若 proof 已 `by ` 开头则不重复加）。写入**临时副本目录**（`work_dir/verify_tmp/<hash>/`，完整复制项目或按需 symlink+复制目标文件；保证原项目不被污染、并发安全——每个验证独立目录或文件锁）。
3. `asyncio.create_subprocess_exec("lake","env","lean",file, cwd=tmp_project)`，`timeout=lean_timeout_s`，超时杀进程组。接受条件：`rc==0` 且 stderr/stdout 中该定理的 `declaration uses 'sorry'` warning 消失。
4. 并发：全局 `asyncio.Semaphore(max_concurrent_lean)`；进程泄漏防护（`finally` 确保 kill）。
5. 可选加强（配置 `check_axioms: bool=False`）：在副本文件尾追加 `#print axioms <theorem_name>`，输出含 `sorryAx` 则拒收。
6. 项目副本缓存：同一 project_path 的"干净副本"只建一次（内容寻址 key），每次验证仅覆盖目标文件。
7. 必须内置自检：`verify_proof` 对 `sorry` 原样（即 proof="sorry"）必须返回 False；对 mini 项目 `nat_refl` 用 `rfl` 必须 True（开发时实跑验证，mini 项目已实测 27/27 通路可用）。

### 3.8 [M2] `verify/dojo.py` + `verify/mock.py`

- `LeanDojoVerifier`：`LeanGitRepo(本地路径, commit)` → `Theorem(repo, rel_file, theorem_name)` → `Dojo(thm, timeout, build_deps=False)`；构造前自动执行 `/mnt/agents/output/patch_lean_dojo.py`（幂等）；已知上游缺陷（env_report §Dojo 阻塞）→ 不可用时 `init()` 抛明确异常并建议回退 subprocess；**不得静默降级**。
- `MockVerifier`：仅测试用；`verify_proof` 对 proof 含 "VALID" 标记返回 True 其余 False（**禁止 v39 的 apply 启发式假阳性**）；结果全部标 `unverified=True`。

### 3.9 [M2] `sorrydb.py` + `progress.py`

- `SorryScanner`：扫描 `lean_project_paths`（支持子目录），正则+括号配平定位每个 `sorry` 的定理名（**按行号向上最近 `theorem|lemma` 声明**，修 v39 取 context 第一个声明的 bug）、行列号、所在文件、goal（可经 `lake env lean` 的 warning 或留空由 LLM 从上下文推断）；输出 `list[SorryTask]`。**禁止注入假任务**（v39 P1-9）；扫描为空 → WARNING + 空列表。
- `SorryDBClient`：可选远程 SorryDB（endpoint 配置，失败仅 WARNING 返回 []，不注入假任务）。
- `LeanProgressV2`：`predict(tasks)->tasks` 填充 predicted_steps/success；启发式（目标字符串特征 + 文件/层级元数据）+ 历史统计（从 Cache 读过去 run 的同特征任务成功率，贝叶斯平滑）；**rfl 候选谓词修复**：`predicted_steps <= 4 and priority in (P2,P3)` 或有明确 rfl 特征（自反/定义即约），并有单测断言非空。

### 3.10 [M3] `engine/orchestrator.py`（编排核心）

```python
@dataclass
class StrategyConfig:                 # 可被 OrchestratorLLM 动态调整（见 3.12）
    tactic_search_depth: int; tactic_search_width: int
    agentic_max_iterations: int; thinking_max_tokens: int
    enable_thinking: bool; phase_order: list[str]     # ["rfl","direct","search","agentic"]
    explorer_share: float = 0.3        # search 阶段 Explorer 模型采样占比
    @classmethod from_config(cls, cfg: V40Config) -> "StrategyConfig"
    def degraded(self) -> "StrategyConfig"   # 软截止降级：depth-1,width=1,iter=4,thinking off

class ResolutionPipeline:
    def __init__(self, cfg, router, verifier, cache, checkpoint, metrics, strategy): ...
    async def run(self, tasks: list[SorryTask], resume: bool=True) -> RunReport: ...
```
行为契约：
1. **worker pool**：`asyncio.Queue` 装任务（按 LeanProgress 优先级排序入队），`num_workers` 个 worker 协程循环取任务执行完整 phase 链；worker 内每步先查：`shutdown_event`、`wall_clock 剩余`、`per-task 预算`。软截止后所有新取任务用 `strategy.degraded()`。
2. **Phase 链**（每任务内顺序，任务间并行）：rfl（固定小 tactic 集直接过 verifier，零 LLM）→ direct（Prover 一次生成完整 proof）→ search（beam）→ agentic（AxProverV2）。任一成功即 `verify()` 入账并 short-circuit。escalation_level 在 checkpoint 中持久化，跨 run 累计 ≥3 → MARKED_AXIOM（受 axiom_quota 截断）。
3. **预算执行**：per-task 超时用 `asyncio.wait_for` 包整个 phase 链；超额 → BUDGET_EXHAUSTED 释放 worker。token 预算按 `metrics` 累计判断。
4. **优雅停机**：SIGTERM/SIGINT handler 置 event + 紧急 checkpoint（含当前 results，修 v39 存 {} 的 bug）；worker 循环每轮轮询。
5. **resume**：`run(resume=True)` 读 checkpoint，跳过 `status in SOLVED_STATUSES|MARKED_AXIOM` 的任务，合并 prev_results 计数。
6. `RunReport`：counts by status、by solver、by provider、tokens、wall_time、verify_pass_rate、未触达任务列表；落盘 `work_dir/results/run_<ts>.json` + 人读摘要。
7. 全程日志含 ETA（按最近 10 任务均速估算）。

### 3.11 [M3] `engine/tactic_search.py` + `engine/axprover.py`

- `TacticSearchEngine.search(task, strategy) -> ResolutionResult`：beam search；heapq 元素 `(priority, monotonic_counter, depth, state_fingerprint, proof)`（修 v39 TypeError）；fingerprint = `sha1(state_repr)`；每节点候选 tactic 由 PROVER 与 EXPLORER 按 `explorer_share` 分担生成（温度 0.2/0.5 制造多样性）；每步过 verifier；深度/宽度来自 strategy； visited 去重。
- `AxProverV2.solve(task, strategy) -> ResolutionResult`：循环 ≤ strategy.agentic_max_iterations：
  1. **Propose**：PROVER（enable_thinking 时走 thinking 预算）生成完整 proof；notebook 只保留**最近 3 条 lesson**（每条 ≤200 字符，由 CRITIC 摘要压缩——修 prompt 膨胀）。
  2. **Compile/Verify**：统一 `verifier.verify_proof`。
  3. **Critique（互评估）**：失败时 CRITIC 评审 proof+诊断，输出 ≤200 字符 lesson（错因分类：语法/类型/策略/方向）；成功时 CRITIC 复核 proof 质量（含黑名单复查）。
  4. **停滞**：`stall = 当前轮 - 上次 remaining_sorries 改善轮; stall >= agentic_stall_patience → break`（修 v39 语义错误）；`iterations` 报实际值。
  5. 结果必须 `verification_passed = verify().ok`，禁止自签 True。

### 3.12 [M3] `engine/agents.py`（多智能体协作/互评估/动态自适应）

```python
class CriticAgent:      # Role.CRITIC：lesson 摘要、proof 复核、失败归因
    async def summarize_lesson(self, task, proof, diagnostics) -> str
    async def review_proof(self, task, proof) -> tuple[bool, str]
class OrchestratorLLM:  # Role.ORCHESTRATOR：规划/调度/协调/评估
    async def plan(self, tasks_summary: dict) -> StrategyConfig          # run 开始前：按任务分布给出初始策略
    async def evaluate_and_adjust(self, metrics_snapshot: dict, strategy: StrategyConfig) -> StrategyConfig
    # Pipeline 每 25 个完成任务或每 10 分钟调用一次；输出严格 JSON（schema 见下），解析失败→保持原策略并 WARNING
class EmergenceLog:     # “实时涌现”可观测层：记录策略调整事件、角色贡献变化、cross-eval 一致率
```
evaluate_and_adjust 的 JSON schema：`{"tactic_search_depth":int,"tactic_search_width":int,"agentic_max_iterations":int,"enable_thinking":bool,"explorer_share":float,"rationale":str}`，字段范围 clamp（depth∈[2,6], width∈[1,4], iter∈[3,12], share∈[0,0.6]）。**安全阀**：调整幅度每轮每项最多 ±1（或 ±0.1），防止振荡。

### 3.13 [M3] `cli.py` + `tools/make_kaggle_bundle.py`

- CLI（全部真实接线，修 v39 静默忽略）：`--project-paths`（重复参数，覆盖 lean_project_paths）、`--workers`、`--verifier`、`--wall-clock-budget`、`--task-limit`、`--resume/--no-resume`、`--output-dir`、`--dry-run`（只扫描+health check 不求解）、`--mock-llm`（测试用假 LLM，禁止与真实 key 混用）。
- `make_kaggle_bundle.py`：把包打成 zip→base64 内嵌生成单文件 `dist/v40_kaggle_bundle.py`（解压到 /kaggle/working 后 `main()`）；README 写明 Kaggle 用法（含 12h 预算映射：wall_clock=36000, workers=16）。

## 4. v39 必修 bug 映射（实现时逐条核对，测试覆盖 P0/P1）

| 必修 | 落点模块 |
|---|---|
| 超时错配 P0-2（thinking 独立超时+预算） | client.py, config.py, orchestrator.py |
| 验证链失效 P0-3（统一 verify、黑名单、重编译） | verify/*, 所有 solver 出口 |
| 僵尸线程/死锁 P0-7 | subprocess_lean.py（subprocess 模式天然规避线程池问题） |
| 虚构模型+4xx 重试放大 P0-9 | client.py, router.py |
| rfl 死阶段 P1-1 | progress.py |
| 停滞检测语义 P1-2 | axprover.py |
| escalation 不可达 P1-3 | orchestrator.py |
| 断点续跑 P1-4 / 原子写+asdict P1-5 / SIGTERM P1-6 | checkpoint.py, orchestrator.py, models.py |
| V39_MOCK_MODE/CLI 失效 P1-7、硬编码 key P1-8 | config.py, cli.py |
| 假任务注入 P1-9 | sorrydb.py |
| heapq TypeError P1-10 | tactic_search.py |
| 缓存键碰撞 P2-5、flush 竞争 P2-2 | cache.py, client.py |
| 定理名定位 P2-19、code fence 提取 P2-13、sorry 子串误判 P2-14 | sorrydb.py, axprover/tactic_search 共用 `extract_lean_code()`（放在 engine/__init__.py 或 utils），subprocess_lean.py |
| 统计失真 P2-17（processed=实际触达） | orchestrator.py, metrics.py |

## 5. 验收标准（Stage 4 测试依据）

1. `python -m py_compile` 全部通过；`pytest tests/` 全绿（mock LLM + mini 项目真实 verifier）。
2. mock LLM + 真实 subprocess verifier 端到端跑 mini 项目：流程完整、无 crash、统计一致。
3. 真实 LLM（4 key）health check + 小规模基准产出 `benchmark_results.md`。
4. 真实端到端：mini 项目 11 sorry，真实 LLM 求解，Trivial≥3/5 消解，Hard 不误报 solved（验证链拦截 sorry/admit）。
5. README：架构图、角色分配、环境变量、Kaggle 部署指南（bundle 用法 + 12h 预算配置 + resume）。
