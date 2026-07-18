"""Async worker-pool orchestrator (SPEC 3.10).

Fixes v39's worst structural defect: every phase used ``for task: await``
serial loops. Here tasks flow through an ``asyncio.PriorityQueue`` consumed by
``num_workers`` worker coroutines; each worker runs the full phase chain
(rfl -> direct -> search -> agentic) per task with hard per-task time/token
budgets, a global wall-clock budget, a soft deadline that switches new tasks
to a degraded strategy, cross-run escalation persistence, SIGTERM/SIGINT
graceful shutdown, and checkpoint resume.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from v40_sorry_resolver.models import (
    ProofStatus,
    ResolutionResult,
    SOLVED_STATUSES,
    SorryTask,
)
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine import extract_lean_code, maybe_await
from v40_sorry_resolver.engine.agents import CriticAgent, EmergenceLog, OrchestratorLLM
from v40_sorry_resolver.engine.axprover import AxProverV2
from v40_sorry_resolver.engine.tactic_search import TacticSearchEngine

logger = logging.getLogger("v40.orchestrator")

# Fixed zero-LLM tactic set for the rfl phase (core Lean 4 tactics only).
RFL_TACTICS: tuple[str, ...] = ("rfl", "trivial", "decide", "simp", "omega")

_TERMINAL_SKIP = frozenset(SOLVED_STATUSES) | {ProofStatus.MARKED_AXIOM}

# OrchestratorLLM periodic evaluation cadence (SPEC 3.12).
_EVAL_EVERY_TASKS = 25
_EVAL_EVERY_S = 600.0


@dataclass
class StrategyConfig:
    """Dynamic strategy; may be adjusted by OrchestratorLLM (SPEC 3.12)."""

    tactic_search_depth: int
    tactic_search_width: int
    agentic_max_iterations: int
    thinking_max_tokens: int
    enable_thinking: bool
    phase_order: list[str] = field(
        default_factory=lambda: ["rfl", "direct", "search", "agentic"]
    )
    explorer_share: float = 0.3

    @classmethod
    def from_config(cls, cfg: Any) -> "StrategyConfig":
        return cls(
            tactic_search_depth=int(getattr(cfg, "tactic_search_depth", 4)),
            tactic_search_width=int(getattr(cfg, "tactic_search_width", 2)),
            agentic_max_iterations=int(getattr(cfg, "agentic_max_iterations", 8)),
            thinking_max_tokens=int(getattr(cfg, "thinking_max_tokens", 2048)),
            enable_thinking=True,
            phase_order=["rfl", "direct", "search", "agentic"],
            explorer_share=0.3,
        )

    def degraded(self) -> "StrategyConfig":
        """Soft-deadline degradation: depth-1, width=1, iter=4, thinking off."""
        return StrategyConfig(
            tactic_search_depth=max(2, self.tactic_search_depth - 1),
            tactic_search_width=1,
            agentic_max_iterations=4,
            thinking_max_tokens=self.thinking_max_tokens,
            enable_thinking=False,
            phase_order=list(self.phase_order),
            explorer_share=self.explorer_share,
        )

    def to_dict(self) -> dict:
        return {
            "tactic_search_depth": self.tactic_search_depth,
            "tactic_search_width": self.tactic_search_width,
            "agentic_max_iterations": self.agentic_max_iterations,
            "thinking_max_tokens": self.thinking_max_tokens,
            "enable_thinking": self.enable_thinking,
            "phase_order": list(self.phase_order),
            "explorer_share": self.explorer_share,
        }


@dataclass
class RunReport:
    counts_by_status: dict[str, int] = field(default_factory=dict)
    counts_by_solver: dict[str, int] = field(default_factory=dict)
    counts_by_provider: dict[str, int] = field(default_factory=dict)
    tokens_used: int = 0
    wall_time_s: float = 0.0
    processed: int = 0
    solved: int = 0
    verify_pass_rate: float = 0.0
    untouched: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "counts_by_status": self.counts_by_status,
            "counts_by_solver": self.counts_by_solver,
            "counts_by_provider": self.counts_by_provider,
            "tokens_used": self.tokens_used,
            "wall_time_s": round(self.wall_time_s, 3),
            "processed": self.processed,
            "solved": self.solved,
            "verify_pass_rate": round(self.verify_pass_rate, 4),
            "untouched": self.untouched,
            "results": self.results,
        }

    def render_summary(self) -> str:
        lines = [
            "=== v40 run summary ===",
            f"wall_time: {self.wall_time_s:.1f}s  processed: {self.processed}  "
            f"solved: {self.solved}  verify_pass_rate: {self.verify_pass_rate:.2%}",
            f"tokens_used: {self.tokens_used}",
            f"by_status: {self.counts_by_status}",
            f"by_solver: {self.counts_by_solver}",
        ]
        if self.counts_by_provider:
            lines.append(f"by_provider: {self.counts_by_provider}")
        if self.untouched:
            lines.append(f"untouched({len(self.untouched)}): {self.untouched[:10]}")
        return "\n".join(lines)


def _result_to_dict(r: ResolutionResult) -> dict:
    status = r.status
    return {
        "task_id": r.task_id,
        "success": r.success,
        "status": status.name if isinstance(status, ProofStatus) else str(status),
        "proof": r.proof,
        "solver": r.solver,
        "iterations": r.iterations,
        "tokens_used": r.tokens_used,
        "time_elapsed": round(float(r.time_elapsed), 3),
        "remaining_goals": r.remaining_goals,
        "verification_passed": r.verification_passed,
        "unverified": r.unverified,
        "error": r.error,
    }


def _result_from_any(obj: Any) -> Optional[ResolutionResult]:
    """Tolerantly rebuild a ResolutionResult from an object or dict."""
    if obj is None:
        return None
    if isinstance(obj, ResolutionResult):
        return obj
    if not isinstance(obj, dict):
        return None
    try:
        status_raw = obj.get("status", "OPEN")
        status = (
            status_raw
            if isinstance(status_raw, ProofStatus)
            else ProofStatus[status_raw]
            if isinstance(status_raw, str) and status_raw in ProofStatus.__members__
            else ProofStatus.OPEN
        )
        return ResolutionResult(
            task_id=str(obj.get("task_id", "")),
            success=bool(obj.get("success", False)),
            status=status,
            proof=obj.get("proof"),
            solver=str(obj.get("solver", "")),
            iterations=int(obj.get("iterations", 0) or 0),
            tokens_used=int(obj.get("tokens_used", 0) or 0),
            time_elapsed=float(obj.get("time_elapsed", 0.0) or 0.0),
            remaining_goals=int(obj.get("remaining_goals", -1)),
            verification_passed=bool(obj.get("verification_passed", False)),
            unverified=bool(obj.get("unverified", False)),
            error=obj.get("error"),
        )
    except Exception:  # pragma: no cover - defensive
        return None


class ResolutionPipeline:
    """SPEC 3.10 worker-pool resolution pipeline."""

    def __init__(self, cfg, router, verifier, cache, checkpoint, metrics, strategy):
        self.cfg = cfg
        self.router = router
        self.verifier = verifier
        self.cache = cache
        self.checkpoint = checkpoint
        self.metrics = metrics
        self.strategy = strategy

        self.shutdown_event = asyncio.Event()
        self._results: dict[str, ResolutionResult] = {}
        self._tasks: list[SorryTask] = []
        self._processed = 0
        self._started_ids: set[str] = set()
        self._resumed_ids: set[str] = set()
        self._recent_durations: deque[float] = deque(maxlen=10)
        self._marked_axiom_count = 0
        self._completed_since_eval = 0
        self._last_eval_ts = 0.0
        self._eval_task: Optional[asyncio.Task] = None
        self._start_wall = 0.0
        self._start_loop = 0.0
        self._signals_installed: list[int] = []

        work_dir = getattr(cfg, "work_dir", None)
        self.emergence = EmergenceLog(work_dir)
        self.critic = CriticAgent(router, metrics=metrics, emergence=self.emergence)
        self.orchestrator_llm = OrchestratorLLM(
            router, base_strategy=strategy, emergence=self.emergence, metrics=metrics
        )
        self.search_engine = TacticSearchEngine(router, verifier, metrics=metrics)
        self.axprover = AxProverV2(
            router,
            verifier,
            critic=self.critic,
            metrics=metrics,
            cfg=cfg,
            emergence=self.emergence,
        )

    # ------------------------------------------------------------------ API

    async def run(self, tasks: list[SorryTask], resume: bool = True) -> RunReport:
        self._start_wall = time.time()
        self._start_loop = asyncio.get_running_loop().time()
        self._tasks = list(tasks)
        self._install_signal_handlers()
        try:
            if resume:
                await self._resume_merge(self._tasks)

            pending = [t for t in self._tasks if t.status not in _TERMINAL_SKIP]
            self._resumed_ids = {
                t.id for t in self._tasks if t.status in _TERMINAL_SKIP
            }
            # Cross-run escalation: tasks already at threshold become axioms.
            still_pending = []
            for t in pending:
                if t.escalation_level >= self._escalation_threshold():
                    self._mark_axiom(t)
                else:
                    still_pending.append(t)
            pending = still_pending

            # Initial planning by the OrchestratorLLM (best effort).
            await self._plan(pending)

            queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
            counter = itertools.count()
            for task in sorted(pending, key=self._priority_key):
                queue.put_nowait((self._priority_key(task), next(counter), task))
            num_workers = max(1, int(getattr(self.cfg, "num_workers", 8)))
            for _ in range(num_workers):
                queue.put_nowait(((999, 999.0, 999), next(counter), None))

            logger.info(
                "pipeline start: %d pending tasks, %d workers, wall budget %.0fs",
                len(pending),
                num_workers,
                self._wall_budget(),
            )
            workers = [
                asyncio.create_task(self._worker(i, queue), name=f"v40-worker-{i}")
                for i in range(num_workers)
            ]
            await asyncio.gather(*workers, return_exceptions=True)

            # Drain queue: anything left was never touched (budget/shutdown).
            untouched = set()
            while not queue.empty():
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    break
                if item and item[2] is not None:
                    task = item[2]
                    if task.status not in _TERMINAL_SKIP:
                        untouched.add(task.id)

            if self._eval_task is not None:
                try:
                    await asyncio.wait_for(self._eval_task, timeout=30.0)
                except Exception:
                    pass

            await self._save_checkpoint(phase="finished")
            report = await self._build_report(untouched)
            await self._persist_report(report)
            self.emergence.record(
                "run_finished",
                processed=report.processed,
                solved=report.solved,
                wall_time_s=round(report.wall_time_s, 2),
            )
            return report
        finally:
            self._remove_signal_handlers()

    def request_shutdown(self) -> None:
        """Public shutdown trigger (CLI / tests)."""
        self.shutdown_event.set()

    # -------------------------------------------------------- setup helpers

    def _wall_budget(self) -> float:
        return float(getattr(self.cfg, "wall_clock_budget_s", 36000.0))

    def _soft_deadline(self) -> float:
        return float(getattr(self.cfg, "soft_deadline_s", 32400.0))

    def _per_task_time_budget(self) -> float:
        return float(getattr(self.cfg, "per_task_time_budget_s", 600.0))

    def _per_task_token_budget(self) -> int:
        return int(getattr(self.cfg, "per_task_token_budget", 200_000))

    def _escalation_threshold(self) -> int:
        return int(getattr(self.cfg, "escalation_threshold", 3))

    def _axiom_quota(self) -> int:
        return int(getattr(self.cfg, "axiom_quota", 45))

    def _wall_remaining(self) -> float:
        return self._wall_budget() - (
            asyncio.get_running_loop().time() - self._start_loop
        )

    def _soft_deadline_passed(self) -> bool:
        return (
            asyncio.get_running_loop().time() - self._start_loop
        ) >= self._soft_deadline()

    @staticmethod
    def _priority_key(task: SorryTask):
        """LeanProgress-based priority: level, then success desc, steps asc."""
        prio = getattr(task, "priority", None)
        level = getattr(prio, "value", 2) if prio is not None else 2
        return (
            int(level),
            -float(getattr(task, "predicted_success", 0.0) or 0.0),
            int(getattr(task, "predicted_steps", 0) or 0),
        )

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._on_signal, sig)
                self._signals_installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            # Non-main thread (e.g. under pytest) or unsupported platform.
            self._signals_installed = []

    def _remove_signal_handlers(self) -> None:
        if not self._signals_installed:
            return
        try:
            loop = asyncio.get_running_loop()
            for sig in self._signals_installed:
                loop.remove_signal_handler(sig)
        except Exception:  # pragma: no cover - defensive
            pass
        self._signals_installed = []

    def _on_signal(self, sig) -> None:  # pragma: no cover - signal path
        logger.warning("received %s -> graceful shutdown", sig.name)
        self.shutdown_event.set()
        try:
            asyncio.get_running_loop().create_task(self._emergency_save())
        except Exception:
            pass

    async def _emergency_save(self) -> None:
        """Emergency checkpoint carrying CURRENT results (v39 P1-4 fix)."""
        try:
            await self._save_checkpoint(phase="emergency")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("emergency checkpoint failed: %s", exc)

    # ------------------------------------------------------------- resume

    async def _resume_merge(self, tasks: list[SorryTask]) -> None:
        """Merge checkpoint state: skip solved/marked, restore escalation."""
        if self.checkpoint is None:
            return
        try:
            data = await maybe_await(self.checkpoint.load())
        except Exception as exc:
            logger.warning("checkpoint load failed (%s); starting fresh", exc)
            return
        prev_tasks, prev_results = self._normalize_checkpoint(data)
        if not prev_tasks and not prev_results:
            return

        for task in tasks:
            prev = prev_tasks.get(task.id)
            if prev is None:
                continue
            prev_status = prev.get("status")
            prev_esc = int(prev.get("escalation_level", 0) or 0)
            task.escalation_level = max(task.escalation_level, prev_esc)
            prev_attempts = prev.get("attempts") or []
            if prev_attempts:
                task.attempts = (list(prev_attempts) + task.attempts)[-20:]
            if prev_status in _TERMINAL_SKIP:
                task.status = prev_status
                task.proof = prev.get("proof")

        merged = 0
        for r in prev_results:
            res = _result_from_any(r)
            if res is not None and res.task_id and res.task_id not in self._results:
                self._results[res.task_id] = res
                merged += 1
        self._marked_axiom_count = sum(
            1 for r in self._results.values() if r.status == ProofStatus.MARKED_AXIOM
        )
        skipped = sum(1 for t in tasks if t.status in _TERMINAL_SKIP)
        logger.info(
            "resume: merged %d prev results, %d tasks already terminal (skipped)",
            merged,
            skipped,
        )

    @staticmethod
    def _normalize_checkpoint(data: Any) -> tuple[dict[str, dict], list]:
        """Accept dict or tuple checkpoint payloads; return (tasks_by_id, results).

        Task entries may be SorryTask objects or plain dicts; everything is
        normalized to plain dicts with ProofStatus values.
        """
        if not data:
            return {}, []
        if isinstance(data, dict):
            raw_tasks = data.get("tasks") or []
            raw_results = data.get("results") or []
        elif isinstance(data, (tuple, list)) and len(data) >= 2:
            raw_tasks, raw_results = data[0], data[1]
        else:
            return {}, []

        def task_to_plain(t: Any) -> Optional[dict]:
            if isinstance(t, SorryTask):
                return {
                    "id": t.id,
                    "status": t.status,
                    "proof": t.proof,
                    "escalation_level": t.escalation_level,
                    "attempts": list(t.attempts),
                }
            if isinstance(t, dict):
                tid = t.get("id") or t.get("task_id")
                if not tid:
                    return None
                status = t.get("status", ProofStatus.OPEN)
                if isinstance(status, str) and status in ProofStatus.__members__:
                    status = ProofStatus[status]
                elif not isinstance(status, ProofStatus):
                    status = ProofStatus.OPEN
                return {
                    "id": tid,
                    "status": status,
                    "proof": t.get("proof"),
                    "escalation_level": int(t.get("escalation_level", 0) or 0),
                    "attempts": t.get("attempts") or [],
                }
            return None

        if isinstance(raw_tasks, dict):
            iterable = raw_tasks.values()
        else:
            iterable = raw_tasks
        tasks_by_id: dict[str, dict] = {}
        for t in iterable:
            plain = task_to_plain(t)
            if plain:
                tasks_by_id[plain["id"]] = plain

        if isinstance(raw_results, dict):
            results = list(raw_results.values())
        else:
            results = list(raw_results)
        return tasks_by_id, results

    # -------------------------------------------------------------- phases

    async def _worker(self, wid: int, queue: asyncio.PriorityQueue) -> None:
        while True:
            # Loop-head checks (SPEC 3.10.1): shutdown, then wall clock.
            if self.shutdown_event.is_set():
                return
            if self._wall_remaining() <= 0:
                logger.info("worker %d: wall-clock budget exhausted", wid)
                return
            item = await queue.get()
            try:
                task = item[2]
                if task is None:  # sentinel -> clean exit
                    return
                if self.shutdown_event.is_set():
                    task.status = ProofStatus.OPEN
                    return
                if self._wall_remaining() <= 0:
                    return
                strategy = self.strategy
                if self._soft_deadline_passed():
                    strategy = strategy.degraded()
                self._started_ids.add(task.id)
                task.status = ProofStatus.IN_PROGRESS
                result = await self._run_task(task, strategy)
                await self._account(task, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # worker must never die silently
                logger.exception("worker %d: unexpected error: %s", wid, exc)
                if item and item[2] is not None:
                    t = item[2]
                    t.status = ProofStatus.FAILED_ALL
                    await self._account(
                        t,
                        ResolutionResult(
                            task_id=t.id,
                            success=False,
                            status=ProofStatus.FAILED_ALL,
                            solver="orchestrator",
                            error=f"worker error: {exc}",
                        ),
                    )
            finally:
                queue.task_done()

    async def _run_task(self, task: SorryTask, strategy: StrategyConfig) -> ResolutionResult:
        """Per-task hard time budget around the whole phase chain."""
        self._processed += 1  # only tasks that actually enter the chain
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._phase_chain(task, strategy),
                timeout=self._per_task_time_budget(),
            )
        except asyncio.TimeoutError:
            logger.warning("task %s: per-task budget exceeded", task.id)
            result = ResolutionResult(
                task_id=task.id,
                success=False,
                status=ProofStatus.BUDGET_EXHAUSTED,
                solver="orchestrator",
                error="per_task_time_budget exceeded",
            )
        result.time_elapsed = time.monotonic() - t0
        return result

    async def _phase_chain(self, task: SorryTask, strategy: StrategyConfig) -> ResolutionResult:
        tokens_used = 0
        last_error: Optional[str] = None
        for phase in strategy.phase_order:
            if self.shutdown_event.is_set():
                task.status = ProofStatus.OPEN  # interrupted -> retried on resume
                return ResolutionResult(
                    task_id=task.id,
                    success=False,
                    status=ProofStatus.OPEN,
                    solver="orchestrator",
                    tokens_used=tokens_used,
                    error="shutdown during phase chain",
                )
            if tokens_used >= self._per_task_token_budget():
                return ResolutionResult(
                    task_id=task.id,
                    success=False,
                    status=ProofStatus.BUDGET_EXHAUSTED,
                    solver="orchestrator",
                    tokens_used=tokens_used,
                    error="per_task_token_budget exceeded",
                )
            result = await self._run_phase(phase, task, strategy)
            if result is None:
                continue
            tokens_used += result.tokens_used
            if not result.success:
                last_error = result.error
                self._record_attempt(task, phase, False, result.error)
                continue
            # Unified verification re-check before accounting (v39 P0-3):
            # NO success is booked without verifier.verify_proof here.
            try:
                vr = await self.verifier.verify_proof(task, result.proof or "")
            except Exception as exc:
                vr = None
                last_error = f"verifier error: {exc}"
            if vr is not None and getattr(vr, "ok", False):
                result.verification_passed = True
                result.tokens_used = tokens_used
                task.status = result.status
                task.proof = result.proof
                self._record_attempt(task, phase, True, None)
                return result
            result.success = False
            result.verification_passed = False
            last_error = last_error or "verification re-check failed"
            self._record_attempt(task, phase, False, "verification re-check failed")
        return ResolutionResult(
            task_id=task.id,
            success=False,
            status=ProofStatus.FAILED_ALL,
            solver="orchestrator",
            tokens_used=tokens_used,
            error=last_error or "all phases exhausted",
        )

    async def _run_phase(
        self, phase: str, task: SorryTask, strategy: StrategyConfig
    ) -> Optional[ResolutionResult]:
        if phase == "rfl":
            return await self._phase_rfl(task)
        if phase == "direct":
            return await self._phase_direct(task, strategy)
        if phase == "search":
            return await self.search_engine.search(task, strategy)
        if phase == "agentic":
            return await self.axprover.solve(task, strategy)
        logger.warning("unknown phase %r skipped", phase)
        return None

    async def _phase_rfl(self, task: SorryTask) -> Optional[ResolutionResult]:
        """Zero-LLM phase: fixed small tactic set straight through verifier."""
        for tactic in RFL_TACTICS:
            if self.shutdown_event.is_set():
                return None
            try:
                vr = await self.verifier.verify_proof(task, tactic)
            except Exception as exc:
                logger.debug("rfl verify error on %s: %s", task.id, exc)
                continue
            if getattr(vr, "ok", False):
                return ResolutionResult(
                    task_id=task.id,
                    success=True,
                    status=ProofStatus.SOLVED_RFL,
                    proof=tactic,
                    solver="rfl",
                    verification_passed=True,
                )
        return None

    async def _phase_direct(
        self, task: SorryTask, strategy: StrategyConfig
    ) -> Optional[ResolutionResult]:
        """Prover one-shot full-proof generation; chain-level verify decides."""
        client = self.router.client(Role.PROVER)
        prompt = (
            f"Theorem {task.theorem_name} (file {task.file_path}, "
            f"line {task.line_number}).\n"
            f"Goal: {task.goal_state or '(infer from context)'}\n"
            f"Context:\n{(task.surrounding_context or '')[:2000]}\n"
            "Output a complete Lean 4 proof for this theorem. "
            "Respond with Lean code only (a ```lean fenced block)."
        )
        try:
            resp = await client.generate(
                prompt,
                system_prompt="You are an expert Lean 4 proof assistant.",
                temperature=0.2,
                max_tokens=2048,
                cache_key=f"direct:{task.cache_key()}",
            )
        except Exception as exc:
            return ResolutionResult(
                task_id=task.id,
                success=False,
                status=ProofStatus.FAILED_ALL,
                solver="llm_direct",
                error=f"llm error: {exc}",
            )
        if getattr(resp, "error", None):
            return ResolutionResult(
                task_id=task.id,
                success=False,
                status=ProofStatus.FAILED_ALL,
                solver="llm_direct",
                error=str(resp.error),
            )
        proof = extract_lean_code(getattr(resp, "text", "") or "")
        tokens = int(getattr(resp, "prompt_tokens", 0) or 0) + int(
            getattr(resp, "completion_tokens", 0) or 0
        )
        if not proof:
            return ResolutionResult(
                task_id=task.id,
                success=False,
                status=ProofStatus.FAILED_ALL,
                solver="llm_direct",
                tokens_used=tokens,
                error="empty proof from LLM",
            )
        return ResolutionResult(
            task_id=task.id,
            success=True,
            status=ProofStatus.SOLVED_LLM_DIRECT,
            proof=proof,
            solver="llm_direct",
            tokens_used=tokens,
        )

    # ----------------------------------------------------------- accounting

    def _mark_axiom(self, task: SorryTask) -> None:
        if self._marked_axiom_count < self._axiom_quota():
            task.status = ProofStatus.MARKED_AXIOM
            self._marked_axiom_count += 1
            res = ResolutionResult(
                task_id=task.id,
                success=False,
                status=ProofStatus.MARKED_AXIOM,
                solver="orchestrator",
                error=f"escalation_level>={self._escalation_threshold()} persisted",
            )
        else:  # quota truncation (v39: quota was never used)
            task.status = ProofStatus.FAILED_ALL
            res = ResolutionResult(
                task_id=task.id,
                success=False,
                status=ProofStatus.FAILED_ALL,
                solver="orchestrator",
                error="axiom_quota exhausted",
            )
        self._results[task.id] = res
        logger.info(
            "task %s -> %s (escalation %d, axioms %d/%d)",
            task.id,
            task.status.name,
            task.escalation_level,
            self._marked_axiom_count,
            self._axiom_quota(),
        )

    async def _account(self, task: SorryTask, result: ResolutionResult) -> None:
        if not result.success and result.status not in (
            ProofStatus.OPEN,  # interrupted by shutdown: keep OPEN for resume
        ):
            task.escalation_level += 1
            if task.escalation_level >= self._escalation_threshold():
                self._mark_axiom(task)
                result = self._results[task.id]
            else:
                task.status = result.status
        elif result.success:
            task.status = result.status
        self._results[task.id] = result
        task.attempts = task.attempts[-20:]

        self._recent_durations.append(result.time_elapsed)
        self._record_metrics(task, result)
        self.emergence.record(
            "task_done",
            task_id=task.id,
            solver=result.solver,
            success=result.success,
            status=result.status.name,
            tokens=result.tokens_used,
        )
        self.emergence.role_contribution(
            role=self._solver_role(result.solver),
            solver=result.solver,
            task_id=task.id,
            success=result.success,
        )

        done = len(self._started_ids)
        total = done + max(0, self._pending_estimate())
        avg = (
            sum(self._recent_durations) / len(self._recent_durations)
            if self._recent_durations
            else 0.0
        )
        eta = avg * max(0, total - done)
        logger.info(
            "task %s: %s via %s (%.2fs) | processed=%d eta~%.0fs tokens=%d",
            task.id,
            result.status.name,
            result.solver,
            result.time_elapsed,
            self._processed,
            eta,
            result.tokens_used,
        )

        interval = int(getattr(self.cfg, "checkpoint_interval_tasks", 10) or 10)
        if interval > 0 and self._processed % interval == 0:
            await self._save_checkpoint(phase="solve")

        self._completed_since_eval += 1
        now = time.monotonic()
        if (
            self._completed_since_eval >= _EVAL_EVERY_TASKS
            or now - self._last_eval_ts >= _EVAL_EVERY_S
        ) and (self._eval_task is None or self._eval_task.done()):
            self._completed_since_eval = 0
            self._last_eval_ts = now
            self._eval_task = asyncio.create_task(self._evaluate())

    def _pending_estimate(self) -> int:
        return max(0, len(self._tasks) - len(self._started_ids) - len(self._resumed_ids))

    @staticmethod
    def _solver_role(solver: str) -> str:
        return {
            "rfl": "rule",
            "llm_direct": "PROVER",
            "tactic_search": "PROVER+EXPLORER",
            "axprover_v2": "PROVER+CRITIC",
            "orchestrator": "ORCHESTRATOR",
        }.get(solver, solver or "unknown")

    def _record_metrics(self, task: SorryTask, result: ResolutionResult) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record_task(
                task_id=task.id,
                status=result.status.name,
                solver=result.solver,
                success=result.success,
                tokens_used=result.tokens_used,
                time_elapsed=result.time_elapsed,
            )
        except Exception as exc:
            logger.debug("metrics.record_task failed: %s", exc)

    @staticmethod
    def _record_attempt(
        task: SorryTask, phase: str, success: bool, error: Optional[str]
    ) -> None:
        task.attempts.append(
            {
                "phase": phase,
                "success": success,
                "error": (error or "")[:200] or None,
                "ts": time.time(),
            }
        )
        task.attempts = task.attempts[-20:]

    # --------------------------------------------------- strategy adaptation

    async def _plan(self, pending: list[SorryTask]) -> None:
        if not pending:
            return
        summary = {
            "total": len(pending),
            "by_priority": {},
            "avg_predicted_steps": 0.0,
            "avg_predicted_success": 0.0,
        }
        for t in pending:
            name = getattr(getattr(t, "priority", None), "name", "P2_MEDIUM")
            summary["by_priority"][name] = summary["by_priority"].get(name, 0) + 1
        if pending:
            summary["avg_predicted_steps"] = sum(
                float(getattr(t, "predicted_steps", 0) or 0) for t in pending
            ) / len(pending)
            summary["avg_predicted_success"] = sum(
                float(getattr(t, "predicted_success", 0) or 0) for t in pending
            ) / len(pending)
        try:
            new_strategy = await self.orchestrator_llm.plan(summary)
            if new_strategy is not None:
                self.strategy = new_strategy
        except Exception as exc:
            logger.warning("orchestrator plan failed (%s); keeping base strategy", exc)

    async def _evaluate(self) -> None:
        try:
            snapshot = (
                await maybe_await(self.metrics.snapshot())
                if self.metrics is not None and hasattr(self.metrics, "snapshot")
                else {}
            )
            new_strategy = await self.orchestrator_llm.evaluate_and_adjust(
                snapshot, self.strategy
            )
            if new_strategy is not None:
                self.strategy = new_strategy
        except Exception as exc:
            logger.warning("evaluate_and_adjust failed (%s); strategy unchanged", exc)

    # ------------------------------------------------------------ checkpoint

    async def _save_checkpoint(self, phase: str) -> None:
        if self.checkpoint is None:
            return
        try:
            await maybe_await(
                self.checkpoint.save(
                    self._tasks,
                    list(self._results.values()),
                    phase,
                    self.metrics,
                )
            )
            logger.debug("checkpoint saved (phase=%s, %d results)", phase, len(self._results))
        except Exception as exc:
            logger.error("checkpoint save failed: %s", exc)

    # --------------------------------------------------------------- report

    async def _build_report(self, untouched: set[str]) -> RunReport:
        wall = time.time() - self._start_wall
        counts_status: dict[str, int] = {}
        counts_solver: dict[str, int] = {}
        tokens = 0
        solved = 0
        verified = 0
        results_json = []
        for r in self._results.values():
            sname = r.status.name if isinstance(r.status, ProofStatus) else str(r.status)
            counts_status[sname] = counts_status.get(sname, 0) + 1
            if r.solver:
                counts_solver[r.solver] = counts_solver.get(r.solver, 0) + 1
            tokens += int(r.tokens_used or 0)
            if r.success:
                solved += 1
                if r.verification_passed:
                    verified += 1
            results_json.append(_result_to_dict(r))
        counts_provider: dict[str, int] = {}
        if self.metrics is not None and hasattr(self.metrics, "snapshot"):
            try:
                snap = await maybe_await(self.metrics.snapshot()) or {}
                counts_provider = dict(snap.get("by_provider") or {})
            except Exception:
                counts_provider = {}
        report = RunReport(
            counts_by_status=counts_status,
            counts_by_solver=counts_solver,
            counts_by_provider=counts_provider,
            tokens_used=tokens,
            wall_time_s=wall,
            processed=self._processed,
            solved=solved,
            verify_pass_rate=(verified / solved) if solved else 0.0,
            untouched=sorted(untouched),
            results=results_json,
        )
        return report

    async def _persist_report(self, report: RunReport) -> None:
        work_dir = getattr(self.cfg, "work_dir", None)
        if not work_dir:
            return
        try:
            results_dir = os.path.join(work_dir, "results")
            os.makedirs(results_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(results_dir, f"run_{ts}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
            with open(
                os.path.join(results_dir, f"run_{ts}_summary.txt"), "w", encoding="utf-8"
            ) as fh:
                fh.write(report.render_summary() + "\n")
        except Exception as exc:
            logger.error("report persist failed: %s", exc)
