"""LeanProgressV2 predictor tests (SPEC 3.9), incl. the rfl-candidate fix.

v39 P1-1: the rfl predicate was vacuously empty (``complexity <= 3`` was never
satisfied for P2/P3 tasks). The repaired predicate ``predicted_steps <= 4 and
priority in (P2,P3) OR reflexivity feature`` must yield a **non-empty** candidate
set on the mini project — asserted directly here.
"""
from __future__ import annotations

import pytest

from v40_sorry_resolver.models import PriorityLevel, SorryTask
from v40_sorry_resolver.progress import LeanProgressV2


def _task(name, goal, priority=PriorityLevel.P2_MEDIUM) -> SorryTask:
    return SorryTask(
        id=name,
        project_path="/p",
        file_path="F.lean",
        line_number=1,
        column_number=1,
        theorem_name=name,
        goal_state=goal,
        priority=priority,
    )


# ----------------------------------------------------------------- prediction
def test_predict_fills_steps_and_success(mini_tasks):
    tasks = LeanProgressV2().predict(list(mini_tasks))
    assert len(tasks) == 11
    for t in tasks:
        assert t.predicted_steps > 0
        assert 0.0 <= t.predicted_success <= 1.0


def test_rfl_candidates_non_empty_on_mini_project(mini_tasks):
    """The core regression assertion for v39 P1-1."""
    pred = LeanProgressV2()
    tasks = pred.predict(list(mini_tasks))
    candidates = pred.rfl_candidates(tasks)
    assert candidates, "rfl candidate set must be non-empty (v39 P1-1 fix)"
    names = {t.theorem_name for t in candidates}
    assert "nat_refl" in names
    assert "one_plus_one" in names


def test_rfl_candidates_have_small_steps(mini_tasks):
    pred = LeanProgressV2()
    tasks = pred.predict(list(mini_tasks))
    for t in pred.rfl_candidates(tasks):
        assert t.predicted_steps <= 4


def test_reflexivity_feature_overrides_priority():
    pred = LeanProgressV2()
    # P0 priority but `m = m` is reflexive -> still an rfl candidate.
    hard = _task("hard_refl", "m = m", priority=PriorityLevel.P0_CRITICAL)
    pred.predict([hard])
    assert pred.is_rfl_candidate(hard) is True


def test_non_rfl_high_priority_not_candidate():
    pred = LeanProgressV2()
    t = _task("ac", "a + b = b + a", priority=PriorityLevel.P1_IMPORTANT)
    pred.predict([t])
    assert pred.is_rfl_candidate(t) is False


def test_false_literal_equation_low_success():
    pred = LeanProgressV2()
    false_task = _task("bad", "0 = 1", priority=PriorityLevel.P0_CRITICAL)
    pred.predict([false_task])
    assert false_task.predicted_success < 0.2
    assert false_task.predicted_steps > 4


def test_computed_equation_not_flagged_false():
    """`1 + 1 = 2` must be a cheap compute goal, not a false statement."""
    pred = LeanProgressV2()
    t = _task("ok", "1 + 1 = 2")
    pred.predict([t])
    assert t.predicted_success > 0.5
    assert t.predicted_steps <= 4
    assert pred.is_rfl_candidate(t) is True


# ------------------------------------------------------- history (Bayesian)
class _FakeCache:
    """Minimal async get/set cache exposing historical success stats."""

    def __init__(self, data):
        self._data = data

    async def get(self, key, namespace="default"):
        return self._data.get((namespace, key))


class _BrokenCache:
    async def get(self, key, namespace="default"):
        raise RuntimeError("cache backend down")


def _hist_key(bucket):
    from v40_sorry_resolver.progress import _HISTORY_NS

    return (_HISTORY_NS, f"{_HISTORY_NS}:feat:{bucket}")


@pytest.mark.asyncio
async def test_history_blends_success_with_bayesian_smoothing():
    # eq_arith historically poor: 1 win / 10 trials.
    data = {_hist_key("eq_arith"): {"wins": 1, "trials": 10}}
    pred = LeanProgressV2(cache=_FakeCache(data))
    await pred.load_history()
    t = _task("arith", "n * 2 = n + n", priority=PriorityLevel.P1_IMPORTANT)
    pred.predict([t])
    pure = LeanProgressV2().predict([_task("arith2", "n * 2 = n + n")])[0]
    # Blended success should be pulled below the pure-heuristic value.
    assert t.predicted_success < pure.predicted_success
    assert t.predicted_steps == pure.predicted_steps  # steps unaffected by history


@pytest.mark.asyncio
async def test_predict_works_without_cache_pure_heuristic():
    pred = LeanProgressV2(cache=None)
    await pred.load_history()  # no-op, must not raise
    tasks = pred.predict([_task("x", "n = n")])
    assert tasks[0].predicted_success > 0.5


@pytest.mark.asyncio
async def test_predict_fault_tolerant_to_broken_cache():
    pred = LeanProgressV2(cache=_BrokenCache())
    await pred.load_history()  # must swallow the backend error
    tasks = pred.predict([_task("x", "n = n")])
    assert tasks[0].predicted_success > 0.5  # fell back to pure heuristic


@pytest.mark.asyncio
async def test_history_interface_missing_falls_back():
    pred = LeanProgressV2(cache=object())  # no .get attribute
    await pred.load_history()
    tasks = pred.predict([_task("x", "1 + 1 = 2")])
    assert tasks[0].predicted_steps <= 4
