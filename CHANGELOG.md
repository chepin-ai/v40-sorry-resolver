# Changelog

All notable changes to the v40 sorry resolver are documented here.

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
