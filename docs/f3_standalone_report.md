# F3 — v40 自包含单文件 + GitHub 托管 验收报告

日期：2026-07-21　执行：release-engineering subagent　Token 全程脱敏（`ghp_gL3l…` 仅存于进程环境，未落盘；每次 push 后 `.git/config` grep 复核 = 0）

## 1. 产出物

| 项 | 值 |
|---|---|
| GitHub 仓库 | https://github.com/chepin-ai/v40-sorry-resolver （public，default branch `master`） |
| description | Multi-LLM agentic Lean 4 sorry resolver — real verification, async swarm, self-contained |
| HEAD commit | `be21e51`（ffdc388 → a07d44d → b93fccf → 54abd23 → 8807f22 → cd792b0 → 93c9534 → 387f752 → be21e51，共 9 个提交全部 push 成功） |
| raw URL | https://raw.githubusercontent.com/chepin-ai/v40-sorry-resolver/master/v40_standalone.py |
| 生成器 | `tools/make_standalone_bundle.py`（已提交） |
| 单文件 | `dist/v40_standalone.py` → 仓库根 `v40_standalone.py` → `/mnt/agents/output/v40_standalone.py`（190,083 B，三者字节一致；raw 200 且 cmp IDENTICAL） |
| vendored 示例 | `examples/lean_mini_project/`（7 文件，无 .git/.lake） |
| README | 顶部「一键运行（自包含单文件）」节：raw 下载 + `--self-test` + `github:` 用法 + Kaggle 单元格 |
| 新增测试 | `tests/test_standalone_bundle.py`，36 个（生成器/解包/github 解析/密钥优先级/自测评分/停滞检测/幂等复用） |

## 2. 单文件引导行为（task 1 逐条验收）

1. **环境自举**：`ensure_lean()` 三级幂等——PATH 有 lake 跳过 → 暖目录（`~/.elan/bin`、`~/.v40/toolchains/lean/bin`）冷 PATH 秒级复用 → 安装。安装：elan 官方脚本（300s 停滞上限）→ 手动链（releases 直连 ⇄ ghfast 代理；**停滞检测**（<8KiB/s/30s 弃权）、**Content-Length 截断检测 + Range 断点续传**（每镜像 6 次）、**稳定缓存目录** `~/.cache/v40/downloads/` 支持跨进程续跑）；无 zstd 时 pip zstandard + Python tarfile（`filter="tar"`，py<3.12 回退）。pip：openai+httpx 必装，lean-dojo+GitPython 仅 `--verifier dojo/repl/hybrid/lean_interact`；tuna 镜像→官方源回退；**user-site 部分安装打捞**（pip 非零退出也刷新 sys.path 重试 import）。`--skip-bootstrap` 全关。
2. **密钥自举**：env > kaggle_secrets（ImportError 静默）> 同目录 .env（引号/注释解析）。全无 → WARNING，仅放行 `--mock-llm`/`--self-test`/`--dry-run`/`--help`。
3. **任务源**：`--project`=`--project-paths`（CLI 原生别名）；`github:owner/repo[/subdir][@ref]` → `git clone --depth 1`（GITHUB_TOKEN→x-access-token；失败切 ghfast clone）→ codeload zip 兜底 → subdir 扫描。
4. **`--self-test`**：内嵌 mini 项目 → 全流程（默认 --mock-llm + 真实 subprocess verifier；`--real-llm` 可选）→ 扫描器反解 Hard 任务 id（task id 是哈希）→ 对照基线（≥7/11、vpr=1.0、2 Hard 拒收）→ 退出码 0/1。
5. **0-sorry 友好退出**：实测 rc=0 +「该项目未发现 sorry…」+ 项目统计。Kaggle：CLI 原生 `/kaggle/working/v40_work`。

## 3. 沙箱冒烟记录（task 3）

### 3.1 全新 HOME 真实自举自测 —— 流水线 PASS + 全自举机制逐项实证
命令：`HOME=/tmp/fakehome1 python dist/v40_standalone.py --self-test`。

- **自举机制全链路实证**（4 次 fakehome 运行 + 1 次真实 HOME 运行合计覆盖）：
  elan 脚本可达但安装器硬编码 github releases（本容器直连 000）→ 300s 上限判败 → 手动链直连停滞检测（0.2KiB/s/连接超时）→ **ghfast 代理成功下载完整 364.3MB（真实 HOME 运行，171s）→ Python zstandard+tarfile 解包 → 装入 `~/.v40/toolchains/lean` → 后续运行暖目录秒级复用**；fakehome 下 pip user-site 安装 openai/httpx/zstandard 成功（打捞逻辑生效，run-3/4 无依赖告警）；**截断检测（255M/264M/340M of 364.3MB 三次）与 Range 断点续传（`resume @255016104`/`@264125608`/`@340228368`）按设计工作，跨进程缓存续跑（run-4 直接从未完成字节继续）**。
- **流水线自测结果（自举完成的工具链上）**：`--self-test` → **SELF-TEST PASS，退出码 0**：
  `solved: 7/11 (baseline >= 7)`、`verify_pass_rate: 1.00`、`Hard rejections: 2 (baseline 2)`——两个假命题（0=1、∀n n%2=0）全部被真实 `lake env lean` 验证拒收。
- **残留确认项**：turn 结束时 fakehome run-4 处于「直连 attempt 4 connect 挂起」状态（340/364MB 已续传就位，仅剩 24MB），其后 ghfast 续传→解包→流水线为该代码路径第三次重复，前两段均已分别实证成功；组合运行的最终打印未能在步数预算内等到。挂起根因：urllib connect 无独立短超时（对死镜像最多挂到 socket 超时），已列入残留风险 R2。

### 3.2 raw URL + --help + github: 任务源 —— 全部通过
- raw（经 ghfast 前缀；本容器 raw.githubusercontent 直连不可达）：**HTTP 200，190,083 B，与本地 cmp IDENTICAL**；GitHub API 复核文件在 master 根目录。
- `python /tmp/v40_dl.py --help` → 完整 CLI usage，rc=0。
- `python /tmp/v40_dl.py --project github:chepin-ai/v40-sorry-resolver/examples/lean_mini_project --dry-run`：暖目录复用 lake（秒级）→ 直连 clone 失败（131s 超时）→ **ghfast clone 成功** → subdir 扫描 → `[dry-run] tasks=11 verifier=subprocess`（Hard×2 P0 / Medium×4 P1 / Trivial×5 P2 全列出）→ rc=0。

### 3.3 全量 pytest
`PATH=~/.v40/toolchains/lean/bin:$PATH python -m pytest tests/ -q` → **308 passed, 31 skipped, 0 failed**（14.8s）。
（无 lake 时 259 passed + 14 failed，全部 subprocess_lean 环境依赖；自举后全绿。31 skipped 为 dojo/repl/SorryDB-网络等既有门禁。）

## 4. 冒烟三结论

1. **自举自测**：自举机制（elan 回退/代理下载/断点续传/解包/暖目录幂等/pip 打捞）全链路实证；`--self-test` 在自举工具链上 **PASS（7/11、vpr=1.0、Hard 拒收 2/2、rc=0）**；fakehome 从零组合重跑在 turn 结束时处于续传挂起等待（见 3.1 残留确认项）。
2. **github: 任务源端到端**：raw 200 字节一致；`--help` OK；`github:…/examples/lean_mini_project --dry-run` 经 ghfast clone 成功扫出 11 任务，rc=0。**通过**。
3. **pytest**：**308 passed / 0 failed / 31 skipped**（期望 ~304 区间，新增 36 全绿）。**通过**。

## 5. 推送与脱敏证据

- 9 次 push 均经 `https://x-access-token:ghp_gL3l…@ghfast.top/https://github.com/…git`（github.com 直连被墙），每次后立即 `git remote set-url` 回无 token URL，`grep -c ghp_ .git/config` = 0。
- API 复核：repo public、description 正确、根含 v40_standalone.py / examples / tools / tests。

## 6. 残留风险

- **R1** ghfast.top 为第三方公益代理，速率波动大（364MB：171s ~ 50min+），且会截断长连接——已用截断检测+Range 续传+稳定缓存+多尝试缓解，极端网络下冷启动仍可能失败（会 WARNING 并提示 --skip-bootstrap 手动装）。
- **R2** urllib connect 无独立短超时：对"半死"直连镜像单次 attempt 可挂起至 socket 超时（实测 >14min），拉长最坏自举时间；后续可改为首选 ghfast 或对 connect 单独设 30s 上限（threading/socket 层）。
- **R3** elan 官方路由在 github 被墙环境必失败（其安装器硬编码 github releases），冷启动固定多花 ~5min；Kaggle/海外网络无此开销。
- **R4** self-test solved=7 恰为基线边界（两次实测均 7/11）；更慢机器上 tactic 搜索超时可能减少 solved 数。
- **R5** `--help` 需要 openai 可 import（bundle 不含三方库）；自举过的环境无此问题。
