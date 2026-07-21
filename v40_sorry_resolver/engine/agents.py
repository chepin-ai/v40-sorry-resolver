"""Multi-agent collaboration layer (SPEC 3.12).

- CriticAgent (Role.CRITIC): lesson summarization, proof cross-review,
  failure attribution.
- OrchestratorLLM (Role.ORCHESTRATOR): planning / scheduling / periodic
  evaluation with strict-JSON strategy adjustments (clamped + per-round
  safety valve to prevent oscillation).
- EmergenceLog: observability of emergent behavior — strategy adjustment
  events, role contribution changes, cross-eval agreement rate; persisted as
  ``work_dir/results/emergence_<ts>.jsonl``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

from v40_sorry_resolver.models import SorryTask
from v40_sorry_resolver.llm.router import Role
# Reuse the verify layer's comment stripper + word-boundary blacklist so the
# Critic judges exactly like the verifier (N-12: a proof whose comment says
# `-- no sorry here` was wrongly rejected by the Critic).
from v40_sorry_resolver.verify.subprocess_lean import _BLACKLIST_RE, _strip_comments

logger = logging.getLogger("v40.agents")

_LESSON_CHARS = 200

# evaluate_and_adjust JSON schema (SPEC 3.12) with clamp ranges.
_ADJUST_SCHEMA = (
    '{"tactic_search_depth":int,"tactic_search_width":int,'
    '"agentic_max_iterations":int,"enable_thinking":bool,'
    '"explorer_share":float,"rationale":str}'
)
_CLAMP_RANGES = {
    "tactic_search_depth": (2, 6),
    "tactic_search_width": (1, 4),
    "agentic_max_iterations": (3, 12),
}
_INT_VALVE = 1       # max +/-1 per round for integer knobs
_FLOAT_VALVE = 0.1   # max +/-0.1 per round for explorer_share
_SHARE_RANGE = (0.0, 0.6)


def _extract_json(text: str) -> Optional[dict]:
    """Tolerant JSON extraction: whole text, then first {...} block."""
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


class CriticAgent:
    """CRITIC role: lesson summarization / proof review / failure attribution."""

    def __init__(self, router, metrics=None, emergence=None, retriever=None):
        self.router = router
        self.metrics = metrics
        self.emergence = emergence
        # Optional premise retriever (frontier_atp Top-8 #6); None = disabled.
        self.retriever = retriever

    async def summarize_lesson(
        self, task: SorryTask, proof: str, diagnostics: str
    ) -> str:
        """Compress a failure into one <=200-char lesson with attribution."""
        system = (
            "You are a strict Lean 4 proof critic. Reply with EXACTLY ONE "
            "line, at most 200 characters, in the form "
            "'CATEGORY: lesson' where CATEGORY is one of "
            "syntax/type/strategy/direction."
        )
        premises_block = await self._retrieve_premises(task)
        prompt = (
            f"Theorem {task.theorem_name}.\n"
            f"Failed proof attempt:\n{(proof or '(empty)')[:1200]}\n"
            f"Verifier diagnostics:\n{(diagnostics or '(none)')[:1200]}\n"
            f"{premises_block}"
            "Summarize the root cause as one lesson for the next attempt."
        )
        try:
            resp = await self.router.client(Role.CRITIC).generate(
                prompt,
                system_prompt=system,
                temperature=0.2,
                max_tokens=128,
                cache_key=None,
            )
            text = "" if getattr(resp, "error", None) else getattr(resp, "text", "")
            lesson = self._clean_lesson(text)
            if lesson:
                return lesson
        except Exception as exc:
            logger.debug("critic summarize failed: %s", exc)
        return self._heuristic_lesson(diagnostics)

    async def propose_alternative(
        self,
        task: SorryTask,
        lessons: Optional[list] = None,
        last_error: str = "",
    ) -> str:
        """Dynamic replanning (frontier_atp Top-8 #5; BFS-Prover-V2/Hilbert/
        Delta-Prover): when the prover is stalled, output ONE alternative
        high-level plan — an *approach switch* (different lemma path, different
        strategy family: induction/contradiction/term-mode/simp-set/...), not
        just another lesson. Injected into the next round's system prompt.

        Always returns a non-empty plan (heuristic fallback when the LLM is
        unavailable) so the replan is never a no-op.
        """
        system = (
            "You are a strict Lean 4 proof critic. The current proof approach "
            "is STALLED. Propose ONE alternative high-level approach — an "
            "approach switch: a different lemma path or a different strategy "
            "family (e.g. induction on another variable, proof by "
            "contradiction, term-mode construction, a simp/omega/decide "
            "automation-first attempt, or a different decomposition). Reply "
            "with at most 3 short lines, <=400 characters total, starting "
            "with 'APPROACH SWITCH:'."
        )
        lesson_lines = []
        for entry in (lessons or [])[-3:]:
            # notebook entries are (lesson, raw_diagnostics) pairs or strings
            lesson_lines.append(str(entry[0] if isinstance(entry, tuple) else entry))
        prompt = (
            f"Theorem {task.theorem_name}.\n"
            f"Goal: {(task.goal_state or '(unknown)')[:400]}\n"
            f"Recent lessons from failed attempts:\n"
            + ("\n".join(f"- {l}" for l in lesson_lines) or "- (none)")
            + f"\nLast error: {(last_error or '(none)')[:300]}\n"
            "Propose the alternative high-level plan now."
        )
        try:
            resp = await self.router.client(Role.CRITIC).generate(
                prompt,
                system_prompt=system,
                temperature=0.4,
                max_tokens=256,
                cache_key=None,
            )
            text = "" if getattr(resp, "error", None) else getattr(resp, "text", "")
            plan = self._clean_plan(text)
            if plan:
                return plan
        except Exception as exc:
            logger.debug("critic propose_alternative failed: %s", exc)
        return self._heuristic_plan(task, lessons)

    @staticmethod
    def _clean_plan(text: str) -> str:
        if not text or not text.strip():
            return ""
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        plan = "\n".join(lines[:3])[:400]
        if not plan.upper().startswith("APPROACH SWITCH"):
            plan = "APPROACH SWITCH: " + plan
        return plan[:400]

    @staticmethod
    def _heuristic_plan(task: SorryTask, lessons: Optional[list]) -> str:
        return (
            "APPROACH SWITCH: abandon the current tactic chain; try a "
            "different strategy family — e.g. automation first "
            "(simp/omega/decide), induction on the main variable, or a "
            "term-mode proof — and route the key step through a different "
            "lemma than before."
        )[:400]

    async def review_proof(self, task: SorryTask, proof: str) -> tuple[bool, str]:
        """Cross-review a verified proof: local blacklist re-check + LLM review."""
        # Local blacklist re-check first (defense in depth, no LLM needed).
        if _BLACKLIST_RE.search(_strip_comments(proof or "")):
            return False, "blacklist: proof contains sorry/admit/stop"
        system = (
            "You are a strict Lean 4 proof reviewer. Reply with JSON only: "
            '{"approved": true|false, "reason": "<=120 chars"}.'
        )
        prompt = (
            f"Theorem {task.theorem_name}.\n"
            f"Proof under review (already compiles):\n{(proof or '')[:2000]}\n"
            "Review quality: no sorry/admit, no circularity, proves the stated "
            "theorem. Reply with the JSON verdict only."
        )
        try:
            resp = await self.router.client(Role.CRITIC).generate(
                prompt,
                system_prompt=system,
                temperature=0.0,
                max_tokens=128,
                cache_key=None,
            )
            text = "" if getattr(resp, "error", None) else getattr(resp, "text", "")
            data = _extract_json(text)
            if data is not None and "approved" in data:
                approved = bool(data.get("approved"))
                reason = str(data.get("reason", ""))[:_LESSON_CHARS]
                return approved, reason or ("approved" if approved else "rejected")
            logger.warning("critic review unparseable; defaulting to approve")
            return True, "review unparseable (default approve)"
        except Exception as exc:
            logger.warning("critic review failed (%s); defaulting to approve", exc)
            return True, f"review unavailable ({exc})"

    async def _retrieve_premises(self, task: SorryTask) -> str:
        """Optional premise-retrieval prompt block (frontier_atp Top-8 #6).

        Fires only when a retriever is wired (config ``retrieval_enabled``)
        and the goal mentions mathlib-style constants; any failure degrades
        to an empty block — retrieval never blocks the solving flow.
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
            logger.debug("critic premise retrieval unavailable: %s", exc)
            return ""
        if not premises:
            return ""
        return "Related Mathlib lemmas (retrieved):\n" + "".join(
            f"- {name}\n" for name in premises
        )

    @staticmethod
    def _clean_lesson(text: str) -> str:
        if not text:
            return ""
        line = text.strip().splitlines()[0].strip() if text.strip() else ""
        return line[:_LESSON_CHARS]

    @staticmethod
    def _heuristic_lesson(diagnostics: str) -> str:
        d = (diagnostics or "").lower()
        if "type mismatch" in d or "has type" in d:
            cat = "type"
        elif "unexpected token" in d or "parse" in d or "syntax" in d:
            cat = "syntax"
        elif "timeout" in d or "maxrecdepth" in d or "whnf" in d:
            cat = "strategy"
        else:
            cat = "direction"
        snippet = (diagnostics or "no diagnostics").strip().splitlines()
        tail = snippet[0][:140] if snippet else "no diagnostics"
        return f"{cat}: {tail}"[:_LESSON_CHARS]


class OrchestratorLLM:
    """ORCHESTRATOR role: plan, evaluate metrics, adjust strategy (strict JSON)."""

    def __init__(self, router, base_strategy=None, emergence=None, metrics=None):
        self.router = router
        self._base_strategy = base_strategy
        self.emergence = emergence
        self.metrics = metrics

    async def plan(self, tasks_summary: dict):
        """Produce the initial strategy from the task distribution."""
        from v40_sorry_resolver.engine.orchestrator import StrategyConfig

        base = self._base_strategy or StrategyConfig.from_config(
            _NullConfig()
        )
        system = (
            "You are the orchestrator of a Lean 4 sorry-resolution pipeline. "
            "Reply with ONE JSON object only, schema: " + _ADJUST_SCHEMA
        )
        prompt = (
            "Task distribution summary:\n"
            + json.dumps(tasks_summary, ensure_ascii=False, default=str)[:2000]
            + "\nCurrent strategy:\n"
            + json.dumps(base.to_dict(), ensure_ascii=False)
            + "\nPropose the initial strategy as strict JSON (fields clamped to "
            "depth[2,6], width[1,4], iter[3,12], share[0,0.6])."
        )
        try:
            resp = await self.router.client(Role.ORCHESTRATOR).generate(
                prompt, system_prompt=system, temperature=0.2, max_tokens=512,
                cache_key=None,
            )
            text = "" if getattr(resp, "error", None) else getattr(resp, "text", "")
            data = _extract_json(text)
            if data is None:
                logger.warning("orchestrator plan: JSON parse failed; keeping base strategy")
                self._log_adjustment(base, base, "plan parse failure")
                return base
            new_strategy = self._apply_adjustment(base, data)
            self._log_adjustment(base, new_strategy, str(data.get("rationale", ""))[:200])
            return new_strategy
        except Exception as exc:
            logger.warning("orchestrator plan failed (%s); keeping base strategy", exc)
            return base

    async def evaluate_and_adjust(self, metrics_snapshot: dict, strategy):
        """Periodic evaluation; adjust strategy with clamp + safety valve."""
        system = (
            "You are the orchestrator of a Lean 4 sorry-resolution pipeline. "
            "Reply with ONE JSON object only, schema: " + _ADJUST_SCHEMA
        )
        prompt = (
            "Metrics snapshot:\n"
            + json.dumps(metrics_snapshot or {}, ensure_ascii=False, default=str)[:2500]
            + "\nCurrent strategy:\n"
            + json.dumps(strategy.to_dict(), ensure_ascii=False)
            + "\nEvaluate throughput/success and propose adjustments as strict "
            "JSON (fields clamped to depth[2,6], width[1,4], iter[3,12], "
            "share[0,0.6])."
        )
        try:
            resp = await self.router.client(Role.ORCHESTRATOR).generate(
                prompt, system_prompt=system, temperature=0.2, max_tokens=512,
                cache_key=None,
            )
            text = "" if getattr(resp, "error", None) else getattr(resp, "text", "")
            data = _extract_json(text)
            if data is None:
                # Parse failure: keep the current strategy unchanged + WARNING.
                logger.warning(
                    "evaluate_and_adjust: JSON parse failed; strategy unchanged"
                )
                self._log_adjustment(strategy, strategy, "parse failure - unchanged")
                return strategy
            new_strategy = self._apply_adjustment(strategy, data)
            self._log_adjustment(
                strategy, new_strategy, str(data.get("rationale", ""))[:200]
            )
            return new_strategy
        except Exception as exc:
            logger.warning("evaluate_and_adjust failed (%s); strategy unchanged", exc)
            return strategy

    # ------------------------------------------------------------ internals

    @staticmethod
    def _apply_adjustment(strategy, data: dict):
        """Clamp ranges + per-round safety valve (+/-1 int, +/-0.1 share)."""
        from v40_sorry_resolver.engine.orchestrator import StrategyConfig

        def int_knob(key: str, current: int) -> int:
            lo, hi = _CLAMP_RANGES[key]
            try:
                v = int(data.get(key, current))
            except (TypeError, ValueError):
                v = current
            v = max(lo, min(hi, v))
            return max(current - _INT_VALVE, min(current + _INT_VALVE, v))

        depth = int_knob("tactic_search_depth", strategy.tactic_search_depth)
        width = int_knob("tactic_search_width", strategy.tactic_search_width)
        iters = int_knob("agentic_max_iterations", strategy.agentic_max_iterations)

        try:
            share = float(data.get("explorer_share", strategy.explorer_share))
        except (TypeError, ValueError):
            share = strategy.explorer_share
        share = max(_SHARE_RANGE[0], min(_SHARE_RANGE[1], share))
        share = max(
            strategy.explorer_share - _FLOAT_VALVE,
            min(strategy.explorer_share + _FLOAT_VALVE, share),
        )

        enable_thinking = data.get("enable_thinking", strategy.enable_thinking)
        return StrategyConfig(
            tactic_search_depth=depth,
            tactic_search_width=width,
            agentic_max_iterations=iters,
            thinking_max_tokens=strategy.thinking_max_tokens,
            enable_thinking=bool(enable_thinking),
            phase_order=list(strategy.phase_order),
            explorer_share=round(share, 3),
        )

    def _log_adjustment(self, old, new, rationale: str) -> None:
        if self.emergence is None:
            return
        try:
            self.emergence.strategy_adjustment(
                old.to_dict() if hasattr(old, "to_dict") else {},
                new.to_dict() if hasattr(new, "to_dict") else {},
                rationale,
            )
        except Exception:  # pragma: no cover - defensive
            pass


class _NullConfig:
    """Fallback defaults when no base strategy is available."""

    tactic_search_depth = 4
    tactic_search_width = 2
    agentic_max_iterations = 8
    thinking_max_tokens = 2048


class EmergenceLog:
    """Emergent-behavior observability: strategy adjustments, role
    contributions, cross-eval agreement rate. Persisted as JSONL.

    Disk writes are BATCHED (N-10): ``record`` only appends to an in-memory
    pending buffer; lines are flushed every ``_FLUSH_EVERY`` events or on an
    explicit ``flush()`` (the pipeline flushes at run end), keeping
    synchronous file IO off the event-loop hot path.
    """

    _FLUSH_EVERY = 32

    def __init__(self, work_dir: Optional[str] = None):
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._path: Optional[str] = None
        self._pending: list[str] = []
        if work_dir:
            try:
                results_dir = os.path.join(work_dir, "results")
                os.makedirs(results_dir, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                self._path = os.path.join(results_dir, f"emergence_{ts}.jsonl")
            except Exception as exc:
                logger.warning("emergence log path unavailable: %s", exc)
                self._path = None

    def record(self, kind: str, **data: Any) -> None:
        event = {"ts": time.time(), "kind": kind}
        event.update(data)
        with self._lock:
            self.events.append(event)
            if self._path:
                self._pending.append(json.dumps(event, ensure_ascii=False, default=str))
                if len(self._pending) >= self._FLUSH_EVERY:
                    self._flush_locked()

    def flush(self) -> None:
        """Write any buffered events to disk (safe to call anytime)."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._path or not self._pending:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(self._pending) + "\n")
            self._pending.clear()
        except Exception as exc:
            logger.debug("emergence flush failed: %s", exc)

    def strategy_adjustment(self, old: dict, new: dict, rationale: str) -> None:
        self.record(
            "strategy_adjustment", old=old, new=new, rationale=rationale[:200]
        )

    def role_contribution(
        self, role: str, solver: str, task_id: str, success: bool
    ) -> None:
        self.record(
            "role_contribution",
            role=role,
            solver=solver,
            task_id=task_id,
            success=success,
        )

    def cross_eval(self, task_id: str, agree: bool) -> None:
        self.record("cross_eval", task_id=task_id, agree=bool(agree))

    def agreement_rate(self) -> float:
        evals = [e for e in self.events if e.get("kind") == "cross_eval"]
        if not evals:
            return 1.0
        return sum(1 for e in evals if e.get("agree")) / len(evals)

    def summary(self) -> dict:
        self.flush()  # readers of the JSONL expect it complete up to now
        roles: dict[str, dict[str, int]] = {}
        for e in self.events:
            if e.get("kind") != "role_contribution":
                continue
            role = str(e.get("role", "unknown"))
            bucket = roles.setdefault(role, {"success": 0, "total": 0})
            bucket["total"] += 1
            if e.get("success"):
                bucket["success"] += 1
        return {
            "events": len(self.events),
            "strategy_adjustments": sum(
                1 for e in self.events if e.get("kind") == "strategy_adjustment"
            ),
            "cross_eval_agreement_rate": round(self.agreement_rate(), 4),
            "role_contributions": roles,
            "path": self._path,
        }
