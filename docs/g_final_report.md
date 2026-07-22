# v40 自包含单文件 — 终极沙箱端到端验证报告（独立验证员 G）

> 日期：2026-07-22（沙箱本地时间）｜对象：`v40_standalone.py` @ master `3975a82`
> 方法：对抗式验证——不只跑通，主动尝试证伪（独立重编译证明、错误证明注入、缓存边界探测、全新 `$HOME` 裸环境）。
> raw bundle 与本地仓库文件 md5 完全一致：`0a0549799bc304b38dca93e3ce6ab552`（三处比对：raw URL / 仓库 / output 副本）。

---

## 一、三个验收结论

| # | 验收项 | 结论 | 关键数字 |
|---|---|---|---|
| 1 | 全量 pytest | **PASS** | **334 passed / 6 skipped / 0 failed**，62.7s；复跑一次 334/6（64.4s），确定性一致 |
| 2 | 裸环境自举 e2e（`HOME=/tmp/ghome1` + raw URL） | **PASS** | SELF-TEST PASS（exit 0），solved 7/11（≥7），vpr=1.00，Hard 拒收 2/2 |
| 3 | 真实 LLM 的 Kaggle 路径复现（github: 源 + 4 key） | **PASS** | solved **8/11**，vpr=100%，wall 199.1s，tokens 39,808，3/4 provider 在线 |

**总评：三项全部通过。** 另发现 1 个真实边缘 bug（完整缓存 416，见 §6-F1）和 1 个外部 key 失效（longcat 401，非代码问题）。

---

## 二、pytest 全量

- 命令：`cd /mnt/agents/output/project && python -m pytest tests/ -q`（环境：elan + Lean 4.20.0 手装至 `~/.elan/toolchains`，lean-dojo==4.20.0 + `patch_lean_dojo.py` 全补丁，`trace_noapi.py` trace 成功 77.8s）
- 结果：**334 passed, 6 skipped, 24 warnings in 62.70s**；二次复跑 334/6 in 64.35s。
- 对基线 308 passed/31 skipped：**+26 passed、-25 skipped**——原 31 skip 中 25 个是缺工具链/dojo 的环境性 skip，本环境自举后全部转为真实 passed。
- 现存 6 skipped 全部为 `tests/test_lean_interact.py`（可选第三后端 lean-interact 包未安装，环境性 skip，属 LOCAL_GUIDE §7 路线图项，非失败）。
- 无任何非环境性失败，无 traceback 需要报告。

## 三、裸环境自举 e2e（核心）

**设置**：`env -i HOME=/tmp/ghome1 PATH=/usr/local/bin:/usr/bin:/bin`（无 elan/Lean/openai/zstandard），`curl -sL -o /tmp/dl_v40.py <raw URL>`，`cd /tmp && python3 /tmp/dl_v40.py --self-test`。

**透明性声明**：本沙箱 GitHub 全镜像单流限速 ~80-90KB/s（364MB 需 ~70 分钟）。采用两轮制：
- **Run 1（零预置，纯冷启动）**：真实观察到 elan 路线失败处理与直连镜像超时后，人工终止（此时下载进度 ~11%）。
- **Run 2（预置 362,000,000/364,296,011 字节 = 99.37% 的部分缓存）**：续跑完整流程至 SELF-TEST PASS。最后 2.2MB 由 bundle 自己的下载器经 Range 续传完成，文件最终精确 364,296,011 字节并成功解包安装。
- 另以并行分块下载（24 流，~5.5MB/s 聚合，90s 完成）取得完整 tarball，zstd/tar 完整性校验通过（5,849 members，`bin/lake` 在位），用于缓存预置与独立验证。

**Run 1/2 日志中观察到的 R2 行为（真实证据）**：
1. elan 路线：`elan-init.sh` 2s 下载成功 → 脚本内部 curl 直连 github.com 挂死 **131.7s 后自行报错**（这是 elan 官方脚本行为，非 bundle 代码路径）→ bundle 正确降级：`elan route unavailable; trying direct toolchain download (ghfast proxy)`。
2. 直连镜像每次尝试在 **~30-35s 内被放弃**：`urlopen error timed out`（connect/read 30s 上限生效）与 `2.0 KiB/s < 8 KiB/s`（低速熔断生效）交替出现——**R2 修复确认生效，半死镜像不会吃满整个 timeout**。
3. Range 续传生效：`resume @362000000` → 服务器 206 → 缓存增长至 362,065,536 → 最终 364,296,011。
4. zstandard 缺失 → bundle 自动 pip 安装（tuna）；openai/httpx 缺失 → 自动安装。
5. 无 key 环境正确拒绝真实运行：`only --mock-llm and --self-test are allowed`。

**结果**（Run 2，自举→PASS 全程 ~6.8 分钟，其中 elan 绕行 ~2.3min + 下载续传 ~3.5min + 流水线 24.2s）：
```
solved: 7/11 (baseline >= 7)        ✓
verify_pass_rate: 1.00 (baseline)   ✓
Hard rejections: 2 (baseline 2)     ✓
SELF-TEST PASS
```
by_status: `SOLVED_RFL 7 / FAILED_ALL 4`（mock LLM 下 4 个非 rfl 任务失败属预期，其中恰含 2 个 Hard 假命题）。
**暖环境复跑**（Run 3）：`lake found ... skip install` 秒级跳过，17.9s 流水线，SELF-TEST PASS，**exit code = 0**。

## 四、真实 LLM 的 Kaggle 路径复现

**命令**：`export` .env 四 key 后 `python /tmp/dl_v40.py --project github:chepin-ai/v40-sorry-resolver/examples/lean_mini_project --workers 4 --wall-clock-budget 1500 --output-dir /tmp/g_real_run`（HOME=/tmp/ghome1，工具链已暖）。

| 指标 | 值 |
|---|---|
| solved | **8/11**（mock 为 7；真实 LLM 多解 1 题） |
| verify_pass_rate | **100.00%** |
| wall_time | **199.1s**（预算 1500s 内） |
| tokens_used | **39,808** |
| LLM 调用 | by_provider: deepseek_a 1 / deepseek_b 46 / **kimi 37**；HTTP 200 ×87 |
| by_status | SOLVED_RFL 7 / SOLVED_SEARCH 1 / FAILED_ALL 3 |
| Hard 拒收 | 2/2（`3df76c748054`、`647ade435dfe` 均 Hard.lean 假命题，FAILED_ALL） |

过程证据：
- **github: 源克隆**：直连 github.com 131s 失败 → 自动切换 `ghfast.top` 前缀克隆成功 → 扫描子目录得 11 个 sorry 任务。
- **健康检查**：longcat key（`ak_20J2OE…`）chat 探测 401 `invalid_api_key/无效的AppId` → provider 正确禁用，不阻塞（与 LOCAL_GUIDE §6-2 已知事项一致；其余 3 key 全部在线）。
- 第 4 个失败任务 `7219c54e0340` 为 Trivial.lean 中非 rfl 可解题（搜索/预算内未命中），属求解能力边界，非验证缺陷；自我评估基线只要求 ≥7。

**与用户原失败命令的差异点**：用户原命令指向 mathlib——mathlib CI 强制无 sorry，扫描 0 任务**正常退出**（bundle 有 0-sorry 优雅退出路径，打印中文提示并 exit 0，不做 verifier init）；本次改用含 11 个真实 sorry 的 `examples/lean_mini_project`，即有真实任务源。v39 的 "verifier init failed" 在 v40 裸环境中不复存在（工具链全自举）。

## 五、对抗式独立验证（不采信管道自报）

1. **证明独立重编译**：从 checkpoint 提取 SOLVED_SEARCH 证明（`and_comm_simple` → `exact And.symm h`），在管道外用同一工具链 `lake env lean` 编译 → **通过**（仅剩其他 sorry 警告）。
2. **错误证明注入**：同一定理改为 `exact h` → Lean 报 `type mismatch`；Hard `0=1` 注入 `rfl` → `tactic 'rfl' failed`。**验证器不是橡皮图章**。
3. **验证闸门时序证据**：checkpoint `attempts` 显示该题 direct 阶段候选曾被 `verification re-check failed` 拒收，search 阶段才通过——先验后收成立。
4. **--help 裸环境**：全新 HOME、零依赖下 `--help` exit 0（build-time 内嵌帮助修复生效）。
5. **verifier choices 同步**：`--help` 显示 `{subprocess,dojo,repl,hybrid,lean_interact,mock}` 与 `config.VALID_VERIFIERS` 逐项一致。

## 六、发现与已知问题

- **F1（真实边缘 bug，低危）**：`_download` 对**已 100% 完整**的缓存文件会发送 `Range: bytes=<full>-`，所有镜像回 **HTTP 416**，重试耗尽后 `RuntimeError: all download mirrors failed`（已实测复现）。触发窗口：上一轮在"下载完成→工具链安装完成"之间被杀。**部分缓存不受影响**（206 续传正常）。规避：删除 `~/.cache/v40/downloads/` 后重跑。建议：`_download` 前置比较文件大小与远端 Content-Length，相等则直接返回。
- **F2（外部，非代码）**：longcat chat key 401 失效（`/models` 可达但 chat 鉴权失败），引擎正确处理为禁用。
- **F3（环境观察）**：本沙箱 github.com 直连完全不可达、ghfast/gh-proxy 单流 ~80-90KB/s；bundle 的多级降级在此极端网络下仍完成自举，侧面压测了弹性设计。Kaggle 真实网络直连可达，elan 路线会一次成功。

## 七、与 v39 原始故障的闭环对照

| v39 故障 | v40 闭环证据（本轮实测） |
|---|---|
| **12h 超时**（半死镜像挂死下载） | R2：connect/read 30s 上限 + 8KiB/s 低速熔断 + 镜像降级链，日志实测每次 ~30-35s 放弃；全局 `--wall-clock-budget 1500` 下真实运行 199s 收敛 |
| **mock 空转**（无 key 也"跑"，烧钱/假跑） | 无 key 时 bundle 拒绝真实运行（日志：`only --mock-llm and --self-test are allowed`）；有 key 时真实 API 调用可计量（87×HTTP 200，39,808 tokens，三 provider 分项计数） |
| **lake 缺失 → verifier init failed** | 裸 HOME 全自举：elan 失败→代理 tarball→自动装 zstandard/openai→`~/.v40/toolchains` 落盘→SELF-TEST PASS；暖环境秒级跳过 |

## 八、文档改动清单

1. `/mnt/agents/output/LOCAL_GUIDE.md`：新增 **§4.5「自包含单文件与 GitHub 运行」**——raw URL 一键命令、`--self-test` 验收基线、`github:` 任务源、Kaggle 正确姿势（上传 bundle 或 curl raw URL、Secrets 注入、mathlib 0-sorry 属正常、416 边缘规避）。
2. 仓库 `CHANGELOG.md`（**g-docs 分支**，commit `6c6d771`，master 未动）：新增「standalone bundle R2 hardening」条目——R2 超时帽、dependency-free --help、verifier-choices 同步三项修复的文档化 + F1 已知问题记录。

## 九、未覆盖范围（诚实声明）

- 364MB 全量下载的**首字节到末字节完全由 bundle 自身完成**的场景未跑满（沙箱单流限速，70 分钟成本）；以 Run 1 冷启动 ~11% + Run 2 Range 续传收尾 + 完整文件独立校验组合覆盖，下载器各代码路径（直连、代理、熔断、续传、解包）均有真实日志证据。
- dojo/repl/hybrid/lean_interact 验证后端本轮未在 e2e 中逐项跑（e2e 用默认 subprocess）；dojo 通路环境侧已冒烟（trace_noapi 77.8s 成功）。
- mathlib 规模项目未实测（遗留限制，与 LOCAL_GUIDE §7 一致）。
- Kaggle Secrets 读取路径以 env 注入等价模拟，未在真实 Kaggle kernel 内运行。
