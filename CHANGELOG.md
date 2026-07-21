# Changelog

All notable changes to the v40 sorry resolver are documented here.

## [Unreleased] — standalone bundle R2 hardening (2026-07-21, g-docs)

Documentation for fixes shipped in `f71814a` + `3975a82` (master), verified
end-to-end from the raw GitHub URL in a bare sandbox (fresh `$HOME`, no
elan/Lean/pip deps):

### Fixed

- **R2 download resilience** (`v40_standalone.py` `_download`): TCP-connect and
  every blocking read are now capped by `connect_timeout=30s`, so a half-dead
  mirror that hangs the handshake is abandoned within ~30s instead of blocking
  the full per-attempt timeout (v39 12h-hang class). Verified live: direct
  GitHub mirror produced `urlopen error timed out` / `2.0 KiB/s < 8 KiB/s`
  stall aborts in ~30-35s per attempt before the run fell through and
  completed via Range resume.
- **Dependency-free `--help`** (`v40_standalone.py`): CLI help text is embedded
  at bundle build time, so `--help` works in a bare environment before any
  bootstrap (previously importing the package pulled third-party deps and the
  help path itself could fail on a naked Kaggle kernel). Verified: exit 0 on a
  fresh `$HOME` with no pip packages.
- **CLI `--verifier` choices synced with `VALID_VERIFIERS`**: help/argparse
  choices are now generated from the single source of truth
  `("subprocess", "dojo", "repl", "hybrid", "lean_interact", "mock")` instead
  of a drifted hardcoded list. Verified: `--help` output matches
  `config.VALID_VERIFIERS` exactly.

### Known issues (verified 2026-07-21)

- **Complete-cache 416 edge** (`_download`): if a previous run was killed
  *after* the Lean tarball finished downloading but *before* the toolchain
  install completed, the next run sends `Range: bytes=<full-size>-`, every
  mirror answers `HTTP 416`, and bootstrap aborts with "all download mirrors
  failed". Workaround: delete `~/.cache/v40/downloads/lean-*-linux.tar.zst`
  (or the whole `~/.cache/v40` dir) and re-run. Partial caches resume
  correctly via `206 Partial Content`; only the 100%-complete cache trips.

## [Unreleased] — agentic roadmap (2026-07-21, feat-roadmap-agentic)

Roadmap items from `LOCAL_GUIDE.md` §7 + `frontier_atp.md` Top-8 #2/#4/#5,
triggered by the 2026-07-21 Kaggle mathlib scan (CategoryTheory `Basic.lean:149`
"sorry" was inside a comment/string — 0 real sorries is the *legitimate*
mathlib CI state).

### Added

- **Comment/string-aware sorry scanner** (`sorrydb.py`): `_strip_comments` now
  also blanks string-literal bodies (positions preserved, so line/column still
  point at the real file); nameless `example : P := ...` and
  `instance : C := ...` declarations are collected as sorry containers
  (synthesized stable names `example_<line>`/`instance_<line>`); a sorry inside
  a `def` is recorded but logged with a WARNING (the verification path splices
  by theorem/lemma); `SorryScanner.last_stats` reports
  files/declarations/sorries scanned. A mathlib-style file whose "sorry"s live
  in comments/strings now yields 0 tasks with an explanatory INFO, not
  alarmist WARNINGs.
- **0-sorry graceful CLI exit** (`cli.py`): when the task source yields zero
  sorries the CLI prints "该项目未发现 sorry（若目标是 mathlib 等 CI 强制无
  sorry 的库属正常）" plus project stats (files/declarations scanned) and exits
  0 immediately — no health check, no verifier init.
- **CLI `--sorrydb` / `--project` wiring** (`cli.py`): `--sorrydb <path|URL>`
  switches the task source to `SorryDBClient.load()` (mutually exclusive with
  `--project-paths`); `--project` is now an alias of `--project-paths` (the
  form used by the Kaggle command line).
- **APOLLO sub-lemma decomposition** (`engine/decompose.py`, new;
  `engine/axprover.py`; frontier_atp Top-8 #4, arXiv:2505.05758): after ≥2
  consecutive agentic failures (config `apollo_enabled`, default on) the PROVER
  decomposes the goal into ≤`apollo_max_sublemmas` (default 3)
  `have h_i : P_i := by sorry` sub-lemmas + a closing skeleton. Each sub-lemma
  is verified **in isolation** (a synthetic `<parent>_apollo_<h_i>` theorem in
  a throwaway copy of the source file — the original is never touched and temp
  files are cleaned up); failed sub-lemmas are re-proven individually with the
  remaining budget (`apollo_sublemma_retries`, default 2) and may be decomposed
  one recursive level (`apollo_recursive`, default on). Only when every
  sub-lemma verifies is the skeleton reassembled and the full proof verified
  end-to-end.
- **Shared lemma cache** (`engine/lemma_cache.py`, new; frontier_atp Top-8 #5,
  BFS-Prover-V2 shared Subgoal Cache): `LemmaCache` over the persistent
  `Cache`, key = sha256 of the whitespace-normalized goal, value = verified
  proof + metadata. One instance per pipeline run is shared across workers;
  sub-lemma / direct / search / agentic successes are all written, and every
  task checks the cache *before* proving — a hit short-circuits the whole
  phase chain (still subject to the mandatory re-verification, v39 P0-3).
  Config `lemma_cache_enabled` (default on).
- **CRITIC approach-switch replanning** (`engine/agents.py`,
  `engine/axprover.py`; frontier_atp Top-8 #5 dynamic replanning): when the
  agentic loop stalls (`≥ agentic_stall_patience` rounds without improvement),
  the CRITIC now proposes an alternative high-level plan — an approach switch
  (different lemma path / strategy family) — which is injected into the next
  round's system prompt and recorded as a `replan:` notebook lesson, up to
  `replan_max` (default 2) times per task before the loop really breaks.

### Changed

- `tests/test_axprover.py::test_stall_breaks_at_patience` now pins
  `replan_max = 0` / `apollo_enabled = False` to keep testing the pre-roadmap
  stall semantics (the new defaults intentionally extend the loop with
  replanning).

## [Unreleased] — verification infrastructure (2026-07)

Verification-layer roadmap items 1-2 from `LOCAL_GUIDE.md` §7 (Kimina Lean
Server resident-REPL pattern + LeanInteract backend). Regression-safe: the
default verifier remains `subprocess`; new backends are opt-in via
`V40_VERIFIER`.

### Added

- **Resident REPL pool** (`verify/repl_pool.py`): `ReplPool(project_path,
  size)` with asyncio-condition `acquire(task)`/`release(session)`; affinity
  binding keyed by `(project, file, theorem)` so consecutive candidates of
  one theorem reuse the same REPL (import head stays elaborated — saves the
  per-candidate spawn+elaborate overhead). **Memory guard**: a background
  sweep reads `/proc/<pid>/status` VmRSS over each session's process tree and
  poisons sessions above `repl_max_rss_mb` (default 1500) — idle sessions are
  evicted and rebuilt warm, checked-out ones dropped on release. This is the
  workaround for the "REPL has no memory limit" known limitation (Lean 4.20
  ignores `-Dweak.max_memory`). `close()` is idempotent and kills whole
  process trees plus an `/proc/*/environ` orphan sweep (the
  `kill_descendants` reparent race), so no REPL processes leak.
- **Hybrid dual-channel verifier** (`verify/hybrid.py`,
  `V40_VERIFIER=hybrid`): tactic-level probing (goal states, stepwise
  `run_tactic`) goes through the REPL pool; final judgement stays with
  `SubprocessLeanVerifier` whole-file compilation. The REPL verdict is
  computed concurrently as a witness — agreement counters detect REPL
  protocol drift; the subprocess verdict is always authoritative.
- **LeanInteract third backend** (`verify/lean_interact.py`,
  `V40_VERIFIER=lean_interact`): SPEC 3.6 Verifier on the
  LeanInteract/repl stack (SorryDB's official verifier base,
  `pip install lean-interact`). Reuses the SPEC 3.7 splice machinery; one
  REPL `Command` per candidate in a fresh env (no redeclaration), incremental
  elaboration caches the shared prefix. Missing package / init failure raises
  `LeanInteractUnavailableError` with install instructions — never silent
  fallback. `V40_LEAN_INTERACT_REPL_GIT` overrides the REPL git URL for
  GitHub-restricted networks.
- **Config**: `repl`/`hybrid`/`lean_interact` added to `VALID_VERIFIERS` and
  the `build_verifier` factory; new `V40Config.repl_pool_size` (default 2)
  and `V40Config.repl_max_rss_mb` (default 1500), env-overridable via
  `V40_REPL_POOL_SIZE` / `V40_REPL_MAX_RSS_MB`.
- **Tests**: `tests/test_repl_pool.py` (8, real dojo: concurrent-acquire
  mutual exclusion, affinity reuse counting, memory-guard trigger via fake
  RSS data, leak cleanup), `tests/test_lean_interact.py` (8: missing-package
  error text + real protocol conformance), `tests/test_hybrid.py` (6, real
  mini project end-to-end).

## [Unreleased] — frontier integration (2026-07)

Integrates the **verified actionable items** from the 2026-07 frontier research
(`frontier_atp.md` Top-8, `frontier_resources.md` Top-5). Every item is
regression-safe by default; all network behavior in tests is mocked.

### Added

- **SorryDB real dataset intake** (`sorrydb.py`): `SorryDBClient` now pulls real
  SorryDB snapshots — `{"repos": [...], "sorries": [...]}` JSON documents or
  JSONL files — from **local file paths** (or `file://`) and **remote URLs**
  alike, mapping the SorryDB pydantic schema
  (`repo{remote,branch,commit,lean_version}`, `location{path,start_line,...}`,
  `debug_info{goal,url}`, `id`) onto `SorryTask` with missing-field tolerance.
  Empty payloads / failures still log a WARNING and return `[]` — fake tasks are
  never injected (v39 P1-9). Legacy flat entries remain supported.
- **SorryDB anti-cheat verification protocol** (`verify/subprocess_lean.py`):
  with `V40Config.sorrydb_mode=True`, `verify_proof` additionally asserts
  (1) the target theorem's sorry count drops by exactly 1, and (2) the theorem
  statement text is unchanged by the splice; (3) with `check_axioms=True` the
  existing `#print axioms` sorryAx rejection completes the 3-part protocol
  (frontier_atp §5.1).
- **Verifier-guided repair loop** (`engine/axprover.py`): the agentic notebook
  now stores `(lesson, raw_diagnostics)` pairs — the CRITIC's ≤200-char lesson
  plus the verifier's raw Lean diagnostics truncated to ~500 chars — and both
  are injected into the next propose prompt (frontier_atp Top-8 #2; iterative
  correction >> resampling).
- **Length-normalized beam tie-break** (`engine/tactic_search.py`): equal-priority
  beam candidates are now ordered by `alpha * log(L)` (L = accumulated proof
  length in tokens, BFS-Prover arXiv:2502.03438). New config
  `search_length_norm_alpha: float = 0.1`; `alpha = 0` reproduces the previous
  FIFO tie-break exactly.
- **Premise retrieval tool** (`engine/retrieval.py`, new): unified async client
  `search_premises(query, top_k=5) -> list[str]` over leansearch.net
  (`POST /search`) and premise-search.com (`GET /api/search`); 8s per-source
  timeout, failures degrade to `[]` + WARNING and never block the solving flow.
  `CriticAgent` / `AxProverV2` optionally inject retrieved Mathlib lemma names
  when the goal mentions mathlib-style constants, gated by
  `retrieval_enabled: bool = False` (see README "前沿技术落点").
- **Cost-aware three-tier budgets** (`config.py`, `progress.py`,
  `engine/orchestrator.py`): `BudgetTier` enum (LIGHT/STANDARD/DEEP);
  `LeanProgressV2.budget_tier` buckets tasks by `predicted_steps`
  (≤3 LIGHT / ≤8 STANDARD / >8 DEEP); `StrategyConfig.for_tier(tier)` presets
  (LIGHT depth2/width1/iter3/no-thinking; STANDARD = current defaults; DEEP
  depth5/width3/iter10/thinking on). The orchestrator picks the tier preset per
  task; a real OrchestratorLLM dynamic adjustment still takes priority
  (frontier_atp Top-8 #8).

### Changed

- **DeepSeek V4 model migration** (`config.py`, `llm/client.py`,
  `llm/router.py`, `.env.example`, README): new defaults `deepseek-v4-flash`
  (chat) / `deepseek-v4-pro` (reasoning) — the legacy aliases `deepseek-chat` /
  `deepseek-reasoner` retire on **2026-07-24** (frontier_resources §6, verified).
  Health check is now a two-stage probe: on primary-model failure the legacy
  alias is probed once and adopted (with a migration WARNING) if it answers.
- `tests/test_config.py` / `tests/test_axprover.py`: assertions updated to the
  new defaults (V4 model names) and the new `(lesson, raw_diagnostics)`
  notebook pair structure.

### Notes

- `V40Config` gained explicit `check_axioms: bool = False` (previously an
  implicit optional attribute consumed by the subprocess verifier).
- `BudgetTier` is exported from the package root.
