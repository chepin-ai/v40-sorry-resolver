"""AxProverBase-v2: agentic propose -> verify -> critique loop (SPEC 3.11).

Anti-bloat fixes vs v39:
- notebook keeps only the last 3 lessons, each <=200 chars (CRITIC-compressed);
- stall detection semantics fixed: ``stall = current_iter - last_improve_iter``,
  break when ``stall >= agentic_stall_patience``;
- ``iterations`` reports the actual number of loop iterations;
- ``verification_passed`` always comes from ``verifier.verify_proof`` — never
  self-signed (v39 P0-3).
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


class AxProverV2:
    def __init__(
        self,
        router,
        verifier,
        critic: Optional[CriticAgent] = None,
        metrics=None,
        cfg=None,
        emergence=None,
    ):
        self.router = router
        self.verifier = verifier
        self.critic = critic if critic is not None else CriticAgent(router)
        self.metrics = metrics
        self.cfg = cfg
        self.emergence = emergence
        # Bounded notebook: last 3 lessons, each <=200 chars.
        self.notebook: list[str] = []

    def _stall_patience(self) -> int:
        return int(getattr(self.cfg, "agentic_stall_patience", 3) or 3)

    async def solve(self, task: SorryTask, strategy) -> ResolutionResult:
        t0 = time.monotonic()
        max_iter = max(1, int(strategy.agentic_max_iterations))
        patience = max(1, self._stall_patience())
        prover = self.router.client(Role.PROVER)

        tokens_used = 0
        iterations = 0
        best_remaining: Optional[int] = None
        last_improve_iter = 0  # iteration 0 is the baseline
        last_error: Optional[str] = None

        for i in range(max_iter):
            iterations = i + 1
            proof, toks, llm_error = await self._propose(task, prover, strategy)
            tokens_used += toks
            if llm_error is not None:
                last_error = llm_error
                await self._add_lesson(task, "", llm_error)
                if i - last_improve_iter >= patience:
                    break
                continue
            if not proof:
                last_error = "empty proof"
                await self._add_lesson(task, "", "empty proof extracted from LLM reply")
                if i - last_improve_iter >= patience:
                    break
                continue

            try:
                vr = await self.verifier.verify_proof(task, proof)
            except Exception as exc:
                last_error = f"verifier error: {exc}"
                await self._add_lesson(task, proof, last_error)
                if i - last_improve_iter >= patience:
                    break
                continue

            rem = getattr(vr, "remaining_sorries", -1)
            if isinstance(rem, int) and rem >= 0:
                if best_remaining is None or rem < best_remaining:
                    best_remaining = rem
                    last_improve_iter = i

            if getattr(vr, "ok", False):
                # Success path: CRITIC cross-review (mutual evaluation).
                approved, note = await self.critic.review_proof(task, proof)
                if self.emergence is not None:
                    self.emergence.cross_eval(task.id, agree=bool(approved))
                if approved:
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
                self._push_lesson(f"critic: {note}"[:_LESSON_CHARS])
            else:
                diagnostics = getattr(vr, "diagnostics", "") or getattr(
                    vr, "error", ""
                ) or "verification failed"
                last_error = str(diagnostics)[:300]
                await self._add_lesson(task, proof, str(diagnostics))

            # Stall semantics (v39 P1-2 fix): consecutive rounds without
            # remaining_sorries improvement.
            if i - last_improve_iter >= patience:
                logger.info(
                    "axprover %s: stall %d >= patience %d -> break",
                    task.id,
                    i - last_improve_iter,
                    patience,
                )
                break

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

    async def _propose(self, task: SorryTask, prover, strategy):
        thinking = bool(getattr(strategy, "enable_thinking", False))
        max_tokens = (
            int(getattr(strategy, "thinking_max_tokens", 2048))
            if thinking
            else 2048
        )
        lessons_block = ""
        if self.notebook:
            lessons_block = "Recent lessons (avoid repeating these mistakes):\n" + "\n".join(
                f"- {l}" for l in self.notebook[-_MAX_LESSONS:]
            )
        prompt = (
            f"Theorem {task.theorem_name} (file {task.file_path}, "
            f"line {task.line_number}).\n"
            f"Goal: {task.goal_state or '(infer from context)'}\n"
            f"Context:\n{(task.surrounding_context or '')[:2000]}\n"
            f"{lessons_block}\n"
            "Produce a complete Lean 4 proof. Respond with Lean code only, "
            "inside a ```lean fenced block."
        )
        try:
            resp = await prover.generate(
                prompt,
                system_prompt=(
                    "You are an expert Lean 4 prover. Output only Lean code. "
                    "Never use sorry/admit."
                ),
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

    async def _add_lesson(self, task: SorryTask, proof: str, diagnostics: str) -> None:
        try:
            lesson = await self.critic.summarize_lesson(task, proof, diagnostics)
        except Exception as exc:
            lesson = f"unknown: critic unavailable ({exc})"
        self._push_lesson(lesson)

    def _push_lesson(self, lesson: str) -> None:
        self.notebook.append(str(lesson)[:_LESSON_CHARS])
        del self.notebook[:-_MAX_LESSONS]
