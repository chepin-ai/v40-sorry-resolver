"""Tests for v40_sorry_resolver.models (SPEC 3.1)."""

import hashlib

import pytest

from v40_sorry_resolver.models import (
    MAX_ATTEMPTS_KEPT,
    PriorityLevel,
    ProofStatus,
    ResolutionResult,
    SOLVED_STATUSES,
    SorryTask,
)


def make_task(**overrides):
    kwargs = dict(
        id="task-1",
        project_path="/proj",
        file_path="Mini/Basic.lean",
        line_number=10,
        column_number=4,
        theorem_name="nat_refl",
    )
    kwargs.update(overrides)
    return SorryTask(**kwargs)


class TestEnums:
    def test_priority_values(self):
        assert PriorityLevel.P0_CRITICAL.value == 0
        assert PriorityLevel.P1_IMPORTANT.value == 1
        assert PriorityLevel.P2_MEDIUM.value == 2
        assert PriorityLevel.P3_LOW.value == 3

    def test_proof_status_members(self):
        expected = {
            "OPEN",
            "IN_PROGRESS",
            "SOLVED_RFL",
            "SOLVED_LLM_DIRECT",
            "SOLVED_SEARCH",
            "SOLVED_AGENTIC",
            "FAILED_ALL",
            "MARKED_AXIOM",
            "OPEN_PROBLEM",
            "BUDGET_EXHAUSTED",
            "UNVERIFIED_MOCK",
        }
        assert {s.name for s in ProofStatus} == expected

    def test_solved_statuses_is_frozenset_of_four(self):
        assert isinstance(SOLVED_STATUSES, frozenset)
        assert len(SOLVED_STATUSES) == 4
        assert all(s.name.startswith("SOLVED_") for s in SOLVED_STATUSES)
        assert ProofStatus.OPEN not in SOLVED_STATUSES
        assert ProofStatus.MARKED_AXIOM not in SOLVED_STATUSES


class TestSorryTask:
    def test_cache_key_contract(self):
        task = make_task()
        expected = hashlib.sha256(
            b"/proj:Mini/Basic.lean:10:4"
        ).hexdigest()[:16]
        assert task.cache_key() == expected
        assert len(task.cache_key()) == 16

    def test_make_id_contract(self):
        expected = hashlib.sha1(b"Mini/Basic.lean:10:4").hexdigest()[:12]
        assert SorryTask.make_id("Mini/Basic.lean", 10, 4) == expected

    def test_to_dict_field_by_field(self):
        task = make_task(
            priority=PriorityLevel.P0_CRITICAL,
            status=ProofStatus.SOLVED_SEARCH,
            proof="rfl",
            escalation_level=2,
        )
        task.add_attempt({"phase": "rfl", "ok": False})
        d = task.to_dict()
        assert d["id"] == "task-1"
        assert d["project_path"] == "/proj"
        assert d["file_path"] == "Mini/Basic.lean"
        assert d["line_number"] == 10
        assert d["column_number"] == 4
        assert d["theorem_name"] == "nat_refl"
        assert d["priority"] == "P0_CRITICAL"
        assert d["status"] == "SOLVED_SEARCH"
        assert d["proof"] == "rfl"
        assert d["escalation_level"] == 2
        assert d["attempts"] == [{"phase": "rfl", "ok": False}]
        # No unexpected keys leaked by a generic asdict.
        assert set(d) == {
            "id",
            "project_path",
            "file_path",
            "line_number",
            "column_number",
            "theorem_name",
            "goal_state",
            "surrounding_context",
            "priority",
            "status",
            "proof",
            "predicted_steps",
            "predicted_success",
            "escalation_level",
            "attempts",
        }

    def test_to_dict_does_not_alias_attempts(self):
        task = make_task()
        task.add_attempt({"n": 1})
        d = task.to_dict()
        d["attempts"].append({"n": 2})
        assert len(task.attempts) == 1

    def test_roundtrip(self):
        task = make_task(
            goal_state="⊢ n = n",
            surrounding_context="theorem nat_refl ...",
            priority=PriorityLevel.P3_LOW,
            status=ProofStatus.BUDGET_EXHAUSTED,
            predicted_steps=5,
            predicted_success=0.75,
        )
        task.add_attempt({"phase": "direct", "ok": True})
        clone = SorryTask.from_dict(task.to_dict())
        assert clone.to_dict() == task.to_dict()

    def test_from_dict_tolerates_missing_fields(self):
        task = SorryTask.from_dict({})
        assert task.id  # auto-derived, non-empty
        assert task.priority is PriorityLevel.P2_MEDIUM
        assert task.status is ProofStatus.OPEN
        assert task.attempts == []
        assert task.proof is None

    def test_from_dict_tolerates_unknown_status(self):
        task = SorryTask.from_dict({"status": "NOT_A_STATUS", "priority": "NOPE"})
        assert task.status is ProofStatus.OPEN
        assert task.priority is PriorityLevel.P2_MEDIUM

    def test_from_dict_derives_id_when_absent(self):
        task = SorryTask.from_dict(
            {"file_path": "A.lean", "line_number": 3, "column_number": 7}
        )
        assert task.id == SorryTask.make_id("A.lean", 3, 7)

    def test_attempts_bounded(self):
        task = make_task()
        for i in range(MAX_ATTEMPTS_KEPT + 15):
            task.add_attempt({"i": i})
        assert len(task.attempts) == MAX_ATTEMPTS_KEPT
        # Most recent entries are kept.
        assert task.attempts[-1] == {"i": MAX_ATTEMPTS_KEPT + 14}
        assert task.attempts[0] == {"i": 15}

    def test_from_dict_truncates_attempts(self):
        d = make_task().to_dict()
        d["attempts"] = [{"i": i} for i in range(50)]
        task = SorryTask.from_dict(d)
        assert len(task.attempts) == MAX_ATTEMPTS_KEPT

    def test_from_dict_rejects_non_dict(self):
        with pytest.raises(TypeError):
            SorryTask.from_dict(["not", "a", "dict"])


class TestResolutionResult:
    def test_defaults(self):
        r = ResolutionResult(
            task_id="t", success=False, status=ProofStatus.FAILED_ALL
        )
        assert r.proof is None
        assert r.solver == ""
        assert r.iterations == 0
        assert r.tokens_used == 0
        assert r.time_elapsed == 0.0
        assert r.remaining_goals == -1
        assert r.verification_passed is False
        assert r.unverified is False
        assert r.error is None

    def test_roundtrip(self):
        r = ResolutionResult(
            task_id="t1",
            success=True,
            status=ProofStatus.SOLVED_LLM_DIRECT,
            proof="rfl",
            solver="llm_direct",
            iterations=1,
            tokens_used=123,
            time_elapsed=1.5,
            remaining_goals=0,
            verification_passed=True,
        )
        clone = ResolutionResult.from_dict(r.to_dict())
        assert clone.to_dict() == r.to_dict()

    def test_from_dict_tolerant(self):
        r = ResolutionResult.from_dict({"task_id": "x"})
        assert r.task_id == "x"
        assert r.success is False
        assert r.status is ProofStatus.OPEN
