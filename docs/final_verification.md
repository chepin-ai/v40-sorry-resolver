# v40 引擎合并终态 — 独立全量复核报告

- **复核对象**: `/mnt/agents/output/project`, git master = `1f3adb0295b90096e78291d95c25dcad1a9d3b2f`（dojo-breakthrough + frontier-integration 合并终态），工作树干净（`git status --porcelain` 无输出）。
- **复核方式**: 只读 + 跑测试 + 真实 Lean 抽查；未改任何代码，未执行任何 git 写操作。
- **复核时间**: 2026-07-18/19 UTC（容器全新重建）。

## 1. 环境重建（按 env_report.md 复现，全部成功）

| 组件 | 版本 | 说明 |
|---|---|---|
| OS / Python | Linux x86_64 (Debian 12 容器) / Python 3.12.12 | 无 root |
| elan | 4.2.3 | 经 ghfast 代理装到 `~/.elan/bin`（`--default-toolchain none`） |
| Lean | **4.20.0**（commit 77cfc4d1a4f6, Release） | ghfast 代理下载 tar.zst（364 MB）+ zstandard 解包 + 软链 `leanprover--lean4---v4.20.0`；`lean --version` / `lake --version`(5.0.0-77cfc4d) 实测正常 |
| pip（tuna 镜像） | pytest 9.1.1, pytest-asyncio 1.4.0, openai 2.46.0, lean-dojo 4.20.0, gitpython 3.1.52 | |
| lean-dojo 补丁 | `python3 /mnt/agents/output/patch_lean_dojo.py` → "all patches applied"（memory + FIFO×7 + kernelfix 共 10 处） | |
| dojo trace | `python3 /mnt/agents/output/trace_noapi.py` → **74.1 s 完成**（预期 ~75 s），缓存于 `~/.cache/lean_dojo/gitpython-lean_mini_project-eab5b625bdd2323795d7b755def9c8e490749caf/lean_mini_project`，4/4 文件 trace，Lean4Repl 编译通过 | |

## 2. 全量 pytest

```
cd /mnt/agents/output/project && python -m pytest tests/ -q
258 passed, 9 warnings in 34.97s
```

- 收集数独立复核：`--collect-only` = **258 collected** → **258 passed / 0 failed / 0 skipped / 0 error**。
- 期望 "≥258 passed 0 failed"（基线 dojo 210 + frontier 净增 48 = 258）→ **精确命中**。
- 9 条 warning 均为 `pty.forkpty` DeprecationWarning（dojo_v2 测试固有，非失败）。

## 3. sorrydb_mode 防作弊协议 — 真实 Lean 抽查（非 mock）

方法：`V40Config(sorrydb_mode=True, check_axioms=True, lean_timeout_s=90)` + `SubprocessLeanVerifier`，任务用生产代码 `SorryScanner` 扫出的 mini 项目 `nat_refl`（`LeanMiniProject/Trivial.lean:8:3`），真实跑 `lake env lean`（注意：项目自带单测 test_sorrydb.py 均 mock 掉 `_run_lean`，本抽查为真实工具链端到端）。

| # | 检查 | 结果 |
|---|---|---|
| A | 合法 `rfl` 证明（sorrydb_mode + check_axioms 全开） | **通过**：`ok=True`，0.26 s 真实编译；目标定理 sorry 警告消失（remaining_sorries=4 为同文件其他定理），`#print axioms` 无 sorryAx —— 三段协议（count−1 / statement 不变 / 无 sorryAx）全过 |
| B | statement 改一字（拼接后内容 `n = n` → `n = m`，调同一 `_sorrydb_integrity_check`） | **被拒**：`VerificationError: theorem statement modified by proof splice (before='theorem nat_refl (n : Nat) : n = n', after='... n = m')` |
| B2（附加） | 内容未变（sorry 数未 −1） | **被拒**：`sorry count must drop by exactly 1 (before=1, after=1)` |
| C | proof 含 sorry（`"sorry"`、`"by sorry"`） | **均被拒**：`proof contains blacklisted keyword (sorry/admit/stop)`（黑名单在编译前命中） |
| B0（对照） | 诚实拼接（rfl）过 integrity check | 通过（无异常），证明不是"一律拒" |

隔离性附加验证：抽查后 mini 项目 `git status` 干净，`Trivial.lean` 第 8 行 sorry 原样保留 —— 验证器未污染原项目。

## 4. Kaggle bundle 复跑

```
python tools/make_kaggle_bundle.py   # 重建，内置 py_compile OK
python -m py_compile dist/v40_kaggle_bundle.py   # OK
cp dist/v40_kaggle_bundle.py /mnt/agents/output/v40_kaggle_bundle.py
```

- **sha256**: `9d1ca72a662f18b41d96a4817845dc12674f433ccecf3764cf084eb86941534a`（dist 与 output 副本一致）
- **大小**: 120 270 bytes
- 内容对抗性复核：bundle 为 base64 zip 内嵌整个 `v40_sorry_resolver` 包（故明文 grep 不到 sorrydb_mode）；解包后 **25 个 .py 与 master 逐字节 sha256 一致（0 mismatch）**，内嵌 `verify/subprocess_lean.py` 含 `sorrydb_mode` 与 `_sorrydb_integrity_check`，全部解包文件 py_compile 通过。

## 5. 结论：**PASS**

- pytest：258/258 passed，0 failed/skipped，与合并基线（210+48）精确一致；git HEAD 与干净度符合声明。
- sorrydb_mode 三段防作弊协议在**真实 Lean 4.20.0** 下抽查三项全符：合法 rfl 通过、statement 改一字被拒、proof 含 sorry 被拒（附加：sorry 计数未降也被拒、诚实拼接不误伤、原项目零污染）。
- bundle 重建成功、可编译、哈希稳定，内嵌包与 master 逐字节一致。
- 环境按 env_report.md 完全可复现（trace 74.1 s ≈ 预期 75 s）。

未发现任何与声明不符之处。
