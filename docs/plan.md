# plan.md — v39/v40 Sorry Resolver 总执行计划

## v1 阶段（已完成 ✅）
- Stage 1 双路审计 → audit_correctness.md（47 bug）、audit_performance.md（量化瓶颈）
- Stage 2 真实 Lean 环境 → env_report.md、lean_mini_project（11 真实 sorry）、subprocess 验证通路 27/27
- Stage 3 SPEC 驱动三代理并行构建 v40 → 合并集成（修复 2 处 snapshot await）→ 153 tests 绿
- Stage 4 真实验证 → 170/170 全绿；三臂真实 e2e：7/11 真实消解、vpr=1.0、Hard 零假阳性；vs v39 吞吐 ≈92×
- Stage 5 独立评审 REQUEST_CHANGES（P0×1/P1×5）→ fix-round1 全修 + README/requirements → 185 tests 绿，已合并 master

## v2 阶段（本轮）

### Stage A — 最终回归验证（接续上轮唯一遗留线索）[coder/verifier]
- 重建 Lean 4.20 工具链（ghfast 代理路径）；全量 pytest 199/199（含 14 个真实 lake 用例）
- 真实 e2e 复测（4 key，mini 项目）：验证 theorem-shell 修复后 Trivial 消解率从 3/5 提升；Hard=0、vpr=1.0 保持
- 重建 Kaggle bundle 并冒烟；产出 regression_final.md

### Stage B — 前沿调研（并行 explore，纯只读）
- B1：2025-2026 Lean4 ATP 前沿（DeepSeek-Prover-V2/V2.5、Kimina-Prover、Goedel-Prover-V2、BFS-Prover、Seed-Prover、Apollo；RMaxTS、agentic 证明循环、test-time 扩展策略）→ 可落地技术清单
- B2：开源资源/GitHub（SorryDB 真实数据集与 API、LeanDojo 生态/ReProver 前提选择、proof 修复/验证工具；用用户提供的 GitHub key 搜星标/issue）→ 可集成资源清单
- 产出 frontier_report.md（两路合并，每项标注：来源/日期/可落地性/接入模块）

### Stage C — 核心攻坚：Dojo 交互式验证通路（mission impossible）[coder]
- 完成 lean-dojo REPL FIFO 补丁（env_report §Dojo 阻塞第 3 点：响应走 FIFO + ProofFinished 协议漂移修复），或自研 Lean4Repl 直驱客户端
- 验收：mini 项目 nat_refl 经交互式 run_tac 拿到真实 ProofFinished；失败则交付"逐步文件级 tactic 验证"替代通路 + 根因技术报告
- 产出 dojo_breakthrough.md + 代码（verify/dojo_v2.py 或补丁集）

### Stage D — 前沿成果持久化集成（依赖 B）[coder]
- 将 B 的可落地项集成进架构/代码：提示词策略模板、搜索算法增强、前提选择/检索增强、SorryDB 真实数据集接入、自适应策略参数扩展
- 全部经测试验证，不得引入回归

### Stage E — 总验收 + 交付整合（主代理）
- 全量最终回归；清理 worktree/债务清单；融合全部沙箱产物打包
- LOCAL_GUIDE.md（本地实现/测试/验证详细指引，含外部配合事项）
- 最终报告 .md + .docx（docx 技能）；Kaggle bundle 终版

## v2 全部完成 ✅（2026-07-19）
- A 回归：258/258 全绿；e2e 9/11、vpr=1.0（regression_final.md / final_verification.md）
- B 前沿：frontier_atp.md / frontier_resources.md（全部带来源+日期）
- C 攻坚：Dojo 交互通路攻破（dojo_breakthrough.md，路径1成功）
- D 集成：SorryDB真实数据集/诊断回填/长度归一化/前提检索/三档预算/V4迁移 已合并 master
- E 交付：LOCAL_GUIDE.md + FINAL_REPORT.md/.docx + 源码tarball + Kaggle bundle(120270B)

## v3 阶段（2026-07-21，本轮）
驱动：Kaggle 实测证据（mathlib 0-sorry 正确处理 + 注释 sorry 误检 + lake 缺失阻断）+ LOCAL_GUIDE §7 路线图 + 用户要求（自包含单文件 / GitHub 托管运行 / 沙箱完整 e2e）
- F1 [coder] Agentic 路线图：扫描器注释/字符串感知（修 Basic.lean:149 类误检）、example/instance 支持、0-sorry 友好退出、CLI --sorrydb 接线、APOLLO 子引理分解、共享 lemma 缓存+失败重规划 || branch feat-roadmap-agentic
- F2 [coder] 验证基建：常驻 REPL 池（dojo_v2 会话池+内存守卫+文件亲和预热，破 REPL 内存限制）+ LeanInteract 第三验证后端 || branch feat-verification-infra
- F3 [coder]（依赖 F1+F2 合并）：自包含单文件 v2（自装 elan/Lean/pip/补丁、kaggle_secrets 探测、内嵌 mini 项目 --self-test、--project 别名、github:owner/repo[/subdir][@ref] 任务源）+ GitHub 建仓推送 + raw URL 冒烟
- G [verifier]：沙箱完整端到端（模拟 Kaggle 裸环境自举 + github: 源 + 全量 pytest + 文档更新）
