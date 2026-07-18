"""Tests for v40_sorry_resolver.checkpoint (SPEC 3.5)."""

import json
import os
from pathlib import Path

from v40_sorry_resolver.checkpoint import Checkpoint
from v40_sorry_resolver.models import (
    PriorityLevel,
    ProofStatus,
    ResolutionResult,
    SorryTask,
)


def make_tasks():
    t1 = SorryTask(
        id="t1",
        project_path="/proj",
        file_path="A.lean",
        line_number=1,
        column_number=1,
        theorem_name="foo",
        priority=PriorityLevel.P1_IMPORTANT,
        status=ProofStatus.SOLVED_RFL,
        proof="rfl",
        escalation_level=2,
    )
    t1.add_attempt({"phase": "rfl", "ok": True})
    t2 = SorryTask(
        id="t2",
        project_path="/proj",
        file_path="B.lean",
        line_number=20,
        column_number=5,
        theorem_name="bar",
        status=ProofStatus.OPEN,
    )
    return [t1, t2]


class TestSaveLoad:
    def test_roundtrip(self, tmp_path):
        cp = Checkpoint(str(tmp_path / "ckpt.json"))
        tasks = make_tasks()
        results = [
            ResolutionResult(
                task_id="t1",
                success=True,
                status=ProofStatus.SOLVED_RFL,
                proof="rfl",
                solver="rfl",
                verification_passed=True,
            )
        ]
        cp.save(tasks, results, phase="direct", metrics={"tasks": 1})
        loaded = cp.load()
        assert loaded is not None
        assert loaded["phase"] == "direct"
        assert loaded["metrics"] == {"tasks": 1}
        assert [t.id for t in loaded["tasks"]] == ["t1", "t2"]
        restored = loaded["tasks"][0]
        assert restored.status is ProofStatus.SOLVED_RFL
        assert restored.priority is PriorityLevel.P1_IMPORTANT
        assert restored.escalation_level == 2
        assert restored.attempts == [{"phase": "rfl", "ok": True}]
        assert loaded["results"][0].task_id == "t1"
        assert loaded["results"][0].verification_passed is True

    def test_mapping_results_roundtrip(self, tmp_path):
        cp = Checkpoint(str(tmp_path / "ckpt.json"))
        results = {
            "t1": ResolutionResult(
                task_id="t1", success=False, status=ProofStatus.FAILED_ALL
            )
        }
        cp.save(make_tasks(), results, phase="agentic", metrics={})
        loaded = cp.load()
        assert isinstance(loaded["results"], dict)
        assert loaded["results"]["t1"].status is ProofStatus.FAILED_ALL

    def test_empty_results(self, tmp_path):
        cp = Checkpoint(str(tmp_path / "ckpt.json"))
        cp.save([], None, phase="", metrics=None)
        loaded = cp.load()
        assert loaded["tasks"] == []
        assert loaded["results"] == []
        assert loaded["metrics"] == {}

    def test_creates_parent_dirs(self, tmp_path):
        cp = Checkpoint(str(tmp_path / "deep" / "nested" / "ckpt.json"))
        cp.save(make_tasks(), [], phase="p", metrics={})
        assert (tmp_path / "deep" / "nested" / "ckpt.json").exists()


class TestAtomicity:
    def test_tmp_file_replaced(self, tmp_path):
        path = tmp_path / "ckpt.json"
        cp = Checkpoint(str(path))
        cp.save(make_tasks(), [], phase="p", metrics={})
        assert path.exists()
        assert not (tmp_path / "ckpt.json.tmp").exists()

    def test_save_overwrites_cleanly(self, tmp_path):
        path = tmp_path / "ckpt.json"
        cp = Checkpoint(str(path))
        cp.save(make_tasks(), [], phase="p1", metrics={})
        cp.save(make_tasks()[:1], [], phase="p2", metrics={})
        loaded = cp.load()
        assert loaded["phase"] == "p2"
        assert len(loaded["tasks"]) == 1


class TestLoadTolerance:
    def test_missing_file_returns_none(self, tmp_path):
        cp = Checkpoint(str(tmp_path / "nope.json"))
        assert cp.load() is None

    def test_corrupt_json_returns_none(self, tmp_path):
        path = tmp_path / "ckpt.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert Checkpoint(str(path)).load() is None

    def test_truncated_file_returns_none(self, tmp_path):
        path = tmp_path / "ckpt.json"
        cp = Checkpoint(str(path))
        cp.save(make_tasks(), [], phase="p", metrics={})
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw[: len(raw) // 2], encoding="utf-8")  # simulate SIGKILL
        assert cp.load() is None

    def test_non_dict_root_returns_none(self, tmp_path):
        path = tmp_path / "ckpt.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert Checkpoint(str(path)).load() is None

    def test_bad_task_entries_return_none_not_raise(self, tmp_path):
        path = tmp_path / "ckpt.json"
        path.write_text(
            json.dumps({"tasks": ["not-a-dict"], "results": [], "phase": "p"}),
            encoding="utf-8",
        )
        assert Checkpoint(str(path)).load() is None
