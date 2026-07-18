"""LeanProgress-v2: priority/effort predictor for sorry tasks (SPEC 3.9).

``predict(tasks)`` fills ``predicted_steps`` / ``predicted_success`` from
goal-string features + file/level metadata, optionally blended with historical
success rates (Bayesian smoothing) read from the run ``Cache``.

rfl-candidate fix (v39 P1-1): the old predicate ``complexity <= 3 and priority in
(P2,P3)`` was *vacuously empty* because the predictor never emitted <= 3 for P2/P3
tasks. The repaired predicate is::

    predicted_steps <= 4 and priority in (P2, P3)   OR   reflexivity feature

A unit test asserts the candidate set is non-empty on the mini project.

History is optional: ``load_history()`` reads the async cache into an in-memory
stats table; when the cache/interface is absent the predictor falls back to a
pure heuristic (fault-tolerant, per spec).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .models import PriorityLevel, SorryTask  # SPEC 3.1 contract (provided by M1)

logger = logging.getLogger(__name__)

_HISTORY_NS = "progress"

# rfl predicate constants (SPEC 3.9).
RFL_MAX_STEPS = 4
RFL_PRIORITIES = (PriorityLevel.P2_MEDIUM, PriorityLevel.P3_LOW)

_EQ_RE = re.compile(r"^(?P<lhs>.+?)\s*=\s*(?P<rhs>.+?)$")
_NUM_LIT_RE = re.compile(r"^[\d\s+\-*/%()·]+$")
_CONNECTIVE_RE = re.compile(r"[∧∨→↔¬]|/\\|\\/|->|<->")
_INDUCTION_HINT_RE = re.compile(r"\b(List|map|append|length|fold|Nat\.succ|induction)\b|\+\+")
_FORALL_EXISTS_RE = re.compile(r"[∀∃]|forall|exists")


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


class LeanProgressV2:
    """Heuristic + optional-history predictor."""

    def __init__(
        self,
        cache=None,
        prior_success: float = 0.5,
        prior_strength: float = 8.0,
    ) -> None:
        self._cache = cache
        self._prior_success = float(prior_success)
        self._prior_strength = float(prior_strength)
        # bucket -> {"wins": int, "trials": int}
        self._stats: dict[str, dict[str, int]] = {}

    # -------------------------------------------------------------- history
    async def load_history(self, buckets: Optional[list[str]] = None) -> None:
        """Best-effort load of per-bucket success stats from the async cache.

        Fault-tolerant: any missing interface / error leaves the pure-heuristic
        fallback in place (empty stats table).
        """
        cache = self._cache
        get = getattr(cache, "get", None)
        if get is None:
            return
        for bucket in (buckets or list(_BUCKETS)):
            key = f"{_HISTORY_NS}:feat:{bucket}"
            try:
                val = get(key, namespace=_HISTORY_NS)
                if hasattr(val, "__await__"):
                    val = await val
                if isinstance(val, dict) and "trials" in val:
                    self._stats[bucket] = {
                        "wins": int(val.get("wins", 0)),
                        "trials": int(val.get("trials", 0)),
                    }
            except Exception as exc:  # never let history break prediction
                logger.warning("LeanProgressV2: history read failed for %s: %r", key, exc)

    # ------------------------------------------------------------- predict
    def predict(self, tasks: list[SorryTask]) -> list[SorryTask]:
        """Fill predicted_steps / predicted_success (synchronous, SPEC 3.9)."""
        for task in tasks:
            bucket, steps, success = self._heuristic(task)
            task.predicted_steps = steps
            task.predicted_success = self._blend_success(bucket, success)
        return tasks

    def _blend_success(self, bucket: str, heuristic_success: float) -> float:
        st = self._stats.get(bucket)
        if not st or st.get("trials", 0) <= 0:
            return round(heuristic_success, 3)
        trials = st["trials"]
        wins = st.get("wins", 0)
        # Beta posterior mean with prior (prior_success, prior_strength).
        alpha = self._prior_success * self._prior_strength
        beta = (1.0 - self._prior_success) * self._prior_strength
        smoothed = (wins + alpha) / (trials + alpha + beta)
        # Shrink toward the heuristic by the amount of evidence.
        weight_h = self._prior_strength
        blended = (heuristic_success * weight_h + smoothed * trials) / (weight_h + trials)
        return round(blended, 3)

    # ---------------------------------------------------------- rfl predicate
    def is_rfl_candidate(self, task: SorryTask) -> bool:
        """Repaired rfl predicate (SPEC 3.9; v39 P1-1 fix)."""
        if self._has_rfl_feature(task.goal_state):
            return True
        steps = task.predicted_steps
        if steps <= 0:  # not predicted yet -> compute heuristically
            _, steps, _ = self._heuristic(task)
        return steps <= RFL_MAX_STEPS and task.priority in RFL_PRIORITIES

    def rfl_candidates(self, tasks: list[SorryTask]) -> list[SorryTask]:
        return [t for t in tasks if self.is_rfl_candidate(t)]

    # ------------------------------------------------------------ heuristic
    @staticmethod
    def _has_rfl_feature(goal: str) -> bool:
        """Reflexivity / definitional feature: an ``X = X``-shaped goal."""
        g = _norm_ws(goal)
        m = _EQ_RE.match(g)
        if not m:
            return False
        lhs, rhs = m.group("lhs").strip(), m.group("rhs").strip()
        return bool(lhs) and lhs == rhs

    def _heuristic(self, task: SorryTask) -> tuple[str, int, float]:
        goal = _norm_ws(task.goal_state)
        # Reflexive equation -> rfl.
        if self._has_rfl_feature(goal):
            return "eq_refl", 2, 0.95
        eq = _EQ_RE.match(goal)
        if eq:
            lhs, rhs = eq.group("lhs").strip(), eq.group("rhs").strip()
            # Two *different* bare integer literals -> likely a false statement
            # (e.g. `0 = 1`). A compound expression like `1 + 1` is NOT a bare
            # literal, so `1 + 1 = 2` correctly falls through to eq_compute.
            if self._is_int_lit(lhs) and self._is_int_lit(rhs):
                if int(lhs) != int(rhs):
                    return "eq_false", 8, 0.05
                return "eq_compute", 2, 0.95
            # Numeral computation (e.g. `1 + 1 = 2`) -> rfl/decide.
            if _NUM_LIT_RE.match(lhs) and _NUM_LIT_RE.match(rhs):
                return "eq_compute", 2, 0.9
            if _INDUCTION_HINT_RE.search(goal):
                return "eq_inductive", 5, 0.5
            if _CONNECTIVE_RE.search(goal):
                return "eq_prop", 3, 0.8
            # Generic Nat arithmetic -> omega-able.
            if re.search(r"[+\-*/%]", goal):
                return "eq_arith", 4, 0.7
            return "eq_other", 4, 0.6
        if _CONNECTIVE_RE.search(goal):
            return "prop", 3, 0.8
        if _INDUCTION_HINT_RE.search(goal):
            return "inductive", 5, 0.5
        steps = 6 + (1 if _FORALL_EXISTS_RE.search(goal) else 0)
        return "other", steps, 0.4

    @staticmethod
    def _is_int_lit(s: str) -> bool:
        """True iff ``s`` is a single bare integer literal (no ops/vars)."""
        return bool(re.fullmatch(r"\d+", s.strip()))


_BUCKETS = (
    "eq_refl", "eq_compute", "eq_false", "eq_inductive", "eq_prop",
    "eq_arith", "eq_other", "prop", "inductive", "other",
)
