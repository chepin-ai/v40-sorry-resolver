"""AxProverBase-v2: agentic propose -> verify -> critique loop (SPEC 3.11).

Anti-bloat fixes vs v39:
- notebook keeps only the last 3 lessons, each <=200 chars (CRITIC-compressed);
- stall detection semantics fixed: ``stall = current_iter - last_improve_iter``,
  break when ``stall >= agentic_stall_patience``;
- ``iterations`` reports the actual number of loop iterations;
- ``verification_passed`` always comes from ``verifier.verify_proof`` — never
  self-signed (v39 P0-3).

Verifier-guided repair (frontier_atp Top-8 #2; Goedel-V2/SorryDB/APOLLO/
Numina-Lean-Agent all confirm iterative correction >> resampling): the
notebook stores ``(lesson, raw_diagnostics)`` pairs — the CRITIC's compressed
lesson *plus* the verifier's raw Lean diagnostics (truncated to ~500 chars) —
and both are injected into the next propose prompt.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from v40_sorry_resolver.models import ProofStatus, ResolutionResult, SorryTask
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine import extract_lean_code
from v40_sorry_resolver.engine.agents import CriticAgent

logger = logging.getLogger("v40.axprover")

_MAX_LESSONS = 3
_LESSON_CHARS = 200
# Raw verifier diagnostics kept per notebook entry (frontier_atp Top-8 #2).
_DIAG_CHARS = 500


class AxProverV2:
    def __init__(
        self,
        router,
        verifier,
        critic: Optional[CriticAgent] = None,
        metrics=None,
        cfg=None,
        emergence=None,
        retriever=None,
        lemma_cache=None,
    ):
        self.router = router
        self.verifier = verifier
        self.critic = critic if critic is not None else CriticAgent(router)
        self.metrics = metrics
        self.cfg = cfg
        self.emergence = emergence
        # Optional premise retriever (frontier_atp Top-8 #6); None = disabled.
        self.retriever = retriever
        # Shared goal->proof cache (frontier_atp Top-8 #5); None = disabled.
        self.lemma_cache = lemma_cache
        # APOLLO sub-lemma decomposer (Top-8 #4); created lazily per solve so
        # tests can inject a stub via self.decomposer.
        self.decomposer = None
        # Introspection mirror of the LAST completed solve's notebook only;
        # the working notebook is a solve() local (per-task isolation, N-3).
        # Entries are (lesson, raw_diagnostics) pairs (frontier_atp Top-8 #2).
        self.notebook: list[tuple[str, str]] = []

    def _stall_patience(self) -> int:
        return int(getattr(self.cfg, "agentic_stall_patience", 3) or 3)

    def _replan_max(self) -> int:
        return max(0, int(getattr(self.cfg, "replan_max", 2) or 0))

    def _apollo_enabled(self) -> bool:
        return bool(getattr(self.cfg, "apollo_enabled", True))

    def _get_decomposer(self):
        if self.decomposer is None:
            from v40_sorry_resolver.engine.decompose import ApolloDecomposer

            self.decomposer = ApolloDecomposer(
                self.router, self.verifier, lemma_cache=self.lemma_cache, cfg=self.cfg
            )
        return self.decomposer

    async def solve(self, task: SorryTask, strategy) -> ResolutionResult:
        t0 = time.monotonic()
        max_iter = max(1, int(strategy.agentic_max_iterations))
        patience = max(1, self._stall_patience())
        prover = self.router.client(Role.PROVER)

        # Per-task notebook (N-3): local state, bounded to the last
        # _MAX_LESSONS (lesson, raw_diagnostics) pairs; never shared across
        # tasks. Raw diagnostics are the verifier's own Lean output (truncated
        # to ~500 chars) so the next round sees the real compiler feedback
        # alongside the CRITIC's lesson (frontier_atp Top-8 #2).
        notebook: list[tuple[str, str]] = []

        tokens_used = 0
        iterations = 0
        best_remaining: Optional[int] = None
        last_improve_iter = 0  # iteration 0 is the baseline
        last_error: Optional[str] = None
        consec_fail = 0  # consecutive failed rounds (APOLLO trigger)
        apollo_attempted = False
        replans_used = 0
        active_plan = ""  # CRITIC approach-switch plan (Top-8 #5 replanning)

        # Shared lemma cache (frontier_atp Top-8 #5): a verified proof for
        # this exact goal may already exist from another worker/phase.
        cached = await self._cache_lookup(task)
        if cached:
            hit_result = await self._try_cached_proof(task, cached, t0)
            if hit_result is not None:
                self.notebook = list(notebook)
                return hit_result

        async def maybe_replan(iter_idx: int) -> bool:
            """Stall handler: CRITIC approach-switch replan instead of an
            immediate break (dynamic replanning, frontier_atp Top-8 #5).
            Returns True when a replan was injected and the loop should
            continue; False when the loop must break."""
            nonlocal replans_used, last_improve_iter, active_plan
            if replans_used >= self._replan_max():
                return False
            replans_used += 1
            try:
                plan = await self.critic.propose_alternative(
                    task, notebook, last_error or ""
                )
            except Exception as exc:  # pragma: no cover - defensive
                plan = f"APPROACH SWITCH: critic unavailable ({exc})"
            active_plan = plan
            self._push_lesson(notebook, f"replan: {plan}"[:_LESSON_CHARS], "")
            last_improve_iter = iter_idx  # give the new approach a fresh budget
            logger.info(
                "axprover %s: stall -> approach-switch replan %d/%d",
                task.id, replans_used, self._replan_max(),
            )
            return True

        for i in range(max_iter):
            iterations = i + 1

            # APOLLO sub-lemma decomposition (frontier_atp Top-8 #4): after
            # >=2 consecutive failures, isolate-and-reprove sub-lemmas instead
            # of resampling the whole proof.
            if (
                not apollo_attempted
                and consec_fail >= 2
                and self._apollo_enabled()
            ):
                apollo_attempted = True
                apollo_proof = await self._apollo_attempt(task, strategy, notebook)
                if apollo_proof:
                    approved, note = await self.critic.review_proof(task, apollo_proof)
                    if self.emergence is not None:
                        self.emergence.cross_eval(task.id, agree=bool(approved))
                    if approved:
                        self.notebook = list(notebook)
                        return ResolutionResult(
                            task_id=task.id,
                            success=True,
                            status=ProofStatus.SOLVED_AGENTIC,
                            proof=apollo_proof,
                            solver="axprover_v2_apollo",
                            iterations=iterations,
                            tokens_used=tokens_used,
                            time_elapsed=time.monotonic() - t0,
                            remaining_goals=0,
                            # grounded in the decomposer's final verify_proof
                            verification_passed=True,
                        )
                    last_error = f"critic rejected apollo proof: {note}"
                    self._push_lesson(
                        notebook, f"critic: {note}"[:_LESSON_CHARS], ""
                    )
                else:
                    self._push_lesson(
                        notebook, "apollo: decomposition/reassembly failed", ""
                    )

            proof, toks, llm_error = await self._propose(
                task, prover, strategy, notebook, plan=active_plan
            )
            tokens_used += toks
            if llm_error is not None:
                last_error = llm_error
                consec_fail += 1
                await self._add_lesson(task, "", llm_error, notebook)
                if i - last_improve_iter >= patience:
                    if await maybe_replan(i):
                        continue
                    break
                continue
            if not proof:
                last_error = "empty proof"
                consec_fail += 1
                await self._add_lesson(
                    task, "", "empty proof extracted from LLM reply", notebook
                )
                if i - last_improve_iter >= patience:
                    if await maybe_replan(i):
                        continue
                    break
                continue

            try:
                vr = await self.verifier.verify_proof(task, proof)
            except Exception as exc:
                last_error = f"verifier error: {exc}"
                consec_fail += 1
                await self._add_lesson(task, proof, last_error, notebook)
                if i - last_improve_iter >= patience:
                    if await maybe_replan(i):
                        continue
                    break
                continue

            rem = getattr(vr, "remaining_sorries", -1)
            if isinstance(rem, int) and rem >= 0:
                if best_remaining is None or rem < best_remaining:
                    best_remaining = rem
                    last_improve_iter = i
                    consec_fail = 0

            if getattr(vr, "ok", False):
                # Success path: CRITIC cross-review (mutual evaluation).
                approved, note = await self.critic.review_proof(task, proof)
                if self.emergence is not None:
                    self.emergence.cross_eval(task.id, agree=bool(approved))
                if approved:
                    await self._cache_store(task, proof)
                    self.notebook = list(notebook)  # introspection mirror
                    return ResolutionResult(
                        task_id=task.id,
                        success=True,
                        status=ProofStatus.SOLVED_AGENTIC,
                        proof=proof,
                        solver="axprover_v2",
                        iterations=iterations,
                        tokens_used=tokens_used,
                        time_elapsed=time.monotonic() - t0,
                        remaining_goals=0,
                        verification_passed=True,  # grounded in vr.ok above
                    )
                last_error = f"critic rejected: {note}"
                consec_fail += 1
                self._push_lesson(notebook, f"critic: {note}"[:_LESSON_CHARS], "")
            else:
                diagnostics = getattr(vr, "diagnostics", "") or getattr(
                    vr, "error", ""
                ) or "verification failed"
                last_error = str(diagnostics)[:300]
                consec_fail += 1
                await self._add_lesson(task, proof, str(diagnostics), notebook)

            # Stall semantics (v39 P1-2 fix): consecutive rounds without
            # remaining_sorries improvement; before giving up, let the CRITIC
            # switch the approach (frontier_atp Top-8 #5 dynamic replanning).
            if i - last_improve_iter >= patience:
                if await maybe_replan(i):
                    continue
                logger.info(
                    "axprover %s: stall %d >= patience %d -> break",
                    task.id,
                    i - last_improve_iter,
                    patience,
                )
                break

        self.notebook = list(notebook)  # introspection mirror
        return ResolutionResult(
            task_id=task.id,
            success=False,
            status=ProofStatus.FAILED_ALL,
            solver="axprover_v2",
            iterations=iterations,
            tokens_used=tokens_used,
            time_elapsed=time.monotonic() - t0,
            remaining_goals=best_remaining if best_remaining is not None else -1,
            verification_passed=False,
            error=last_error or "agentic loop exhausted",
        )

    # ------------------------------------------------------------ internals

    async def _apollo_attempt(
        self, task: SorryTask, strategy, notebook: list[tuple[str, str]]
    ) -> Optional[str]:
        """Run APOLLO decomposition (Top-8 #4); never raises."""
        try:
            return await self._get_decomposer().attempt(
                task, strategy=strategy, notebook=notebook
            )
        except Exception as exc:
            logger.info("axprover %s: apollo attempt failed: %r", task.id, exc)
            return None

    async def _cache_lookup(self, task: SorryTask) -> Optional[str]:
        """Shared-lemma-cache probe before any proving (Top-8 #5)."""
        if self.lemma_cache is None:
            return None
        goal = (task.goal_state or "").strip()
        if not goal:
            return None
        try:
            hit = await self.lemma_cache.get(goal)
        except Exception as exc:
            logger.debug("lemma cache lookup failed: %r", exc)
            return None
        if hit and isinstance(hit, dict) and hit.get("proof"):
            return str(hit["proof"])
        return None

    async def _try_cached_proof(
        self, task: SorryTask, proof: str, t0: float
    ) -> Optional[ResolutionResult]:
        """A cache hit is only a candidate: re-verify + critic review before
        booking success (no self-signed successes, v39 P0-3)."""
        try:
            vr = await self.verifier.verify_proof(task, proof)
        except Exception as exc:
            logger.info("cached proof verify error on %s: %r", task.id, exc)
            return None
        if not getattr(vr, "ok", False):
            logger.info(
                "axprover %s: cached proof failed re-verification; solving fresh",
                task.id,
            )
            return None
        approved, note = await self.critic.review_proof(task, proof)
        if self.emergence is not None:
            self.emergence.cross_eval(task.id, agree=bool(approved))
        if not approved:
            logger.info("axprover %s: cached proof critic-rejected: %s", task.id, note)
            return None
        logger.info("axprover %s: lemma cache hit -> short-circuit", task.id)
        return ResolutionResult(
            task_id=task.id,
            success=True,
            status=ProofStatus.SOLVED_AGENTIC,
            proof=proof,
            solver="axprover_v2_cache",
            iterations=0,
            tokens_used=0,
            time_elapsed=time.monotonic() - t0,
            remaining_goals=0,
            verification_passed=True,  # grounded in vr.ok above
        )

    async def _cache_store(self, task: SorryTask, proof: str) -> None:
        if self.lemma_cache is None:
            return
        goal = (task.goal_state or "").strip()
        if not goal or not (proof or "").strip():
            return
        try:
            await self.lemma_cache.put(
                goal, proof, meta={"solver": "axprover_v2", "task_id": task.id}
            )
        except Exception as exc:
            logger.debug("lemma cache store failed: %r", exc)

    async def _propose(
        self,
        task: SorryTask,
        prover,
        strategy,
        notebook: list[tuple[str, str]],
        plan: str = "",
    ):
        thinking = bool(getattr(strategy, "enable_thinking", False))
        max_tokens = (
            int(getattr(strategy, "thinking_max_tokens", 2048))
            if thinking
            else 2048
        )
        lessons_block = ""
        if notebook:
            # Each entry: CRITIC lesson + the raw verifier diagnostics that
            # produced it (verifier-guided repair, frontier_atp Top-8 #2).
            parts = []
            for lesson, raw_diag in notebook[-_MAX_LESSONS:]:
                entry = f"- {lesson}"
                if raw_diag:
                    entry += f"\n  Raw verifier diagnostics: {raw_diag}"
                parts.append(entry)
            lessons_block = (
                "Recent lessons (avoid repeating these mistakes):\n"
                + "\n".join(parts)
            )
        premises_block = await self._retrieve_premises(task)
        prompt = (
            f"Theorem {task.theorem_name} (file {task.file_path}, "
            f"line {task.line_number}).\n"
            f"Goal: {task.goal_state or '(infer from context)'}\n"
            f"Context:\n{(task.surrounding_context or '')[:2000]}\n"
            f"{premises_block}"
            f"{lessons_block}\n"
            "Produce a complete Lean 4 proof. Respond with Lean code only, "
            "inside a ```lean fenced block."
        )
        system_prompt = (
            "You are an expert Lean 4 prover. Output only Lean code. "
            "Never use sorry/admit."
        )
        if plan:
            # Dynamic replanning (frontier_atp Top-8 #5): the CRITIC's
            # approach-switch plan steers this round away from the stalled
            # approach.
            system_prompt += (
                "\nThe previous approach stalled. Follow this alternative "
                "high-level plan (APPROACH SWITCH) unless clearly "
                "inapplicable:\n" + plan
            )
        try:
            resp = await prover.generate(
                prompt,
                system_prompt=system_prompt,
                temperature=0.3,
                max_tokens=max_tokens,
                thinking=thinking,
                cache_key=None,
            )
        except Exception as exc:
            return "", 0, f"llm error: {exc}"
        if getattr(resp, "error", None):
            return "", 0, f"llm error: {resp.error}"
        tokens = int(getattr(resp, "prompt_tokens", 0) or 0) + int(
            getattr(resp, "completion_tokens", 0) or 0
        )
        return extract_lean_code(getattr(resp, "text", "") or ""), tokens, None

    async def _retrieve_premises(self, task: SorryTask) -> str:
        """Optional premise-retrieval prompt block (frontier_atp Top-8 #6).

        Only fires when a retriever is wired (config ``retrieval_enabled``)
        AND the goal mentions mathlib-style constants. Any failure degrades to
        an empty block — retrieval never blocks the solving flow.
        """
        if self.retriever is None:
            return ""
        try:
            from v40_sorry_resolver.engine.retrieval import has_mathlib_constant

            goal = task.goal_state or ""
            if not has_mathlib_constant(goal):
                return ""
            premises = await self.retriever.search_premises(goal, top_k=5)
        except Exception as exc:
            logger.debug("premise retrieval unavailable: %s", exc)
            return ""
        if not premises:
            return ""
        return "Related Mathlib lemmas (retrieved):\n" + "".join(
            f"- {name}\n" for name in premises
        )

    async def _add_lesson(
        self,
        task: SorryTask,
        proof: str,
        diagnostics: str,
        notebook: list[tuple[str, str]],
    ) -> None:
        try:
            lesson = await self.critic.summarize_lesson(task, proof, diagnostics)
        except Exception as exc:
            lesson = f"unknown: critic unavailable ({exc})"
        self._push_lesson(notebook, lesson, diagnostics)

    @staticmethod
    def _push_lesson(
        notebook: list[tuple[str, str]], lesson: str, raw_diagnostics: str = ""
    ) -> None:
        notebook.append(
            (str(lesson)[:_LESSON_CHARS], str(raw_diagnostics or "")[:_DIAG_CHARS])
        )
        del notebook[:-_MAX_LESSONS]
