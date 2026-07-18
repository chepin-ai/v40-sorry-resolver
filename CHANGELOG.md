# Changelog

All notable changes to the v40 sorry resolver are documented here.

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
