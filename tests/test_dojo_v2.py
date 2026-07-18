"""Real LeanDojo interactive (run_tac) verifier tests against the mini project.

These are NOT mocks: they drive real interactive Dojo sessions on
/mnt/agents/output/lean_mini_project (Lean 4.20.0 + lean-dojo 4.20.0 +
patch_lean_dojo.py). See /mnt/agents/output/dojo_breakthrough.md.

Skipped (never failed) when the toolchain is unavailable:
  * lean_dojo not importable,
  * no elan/lean toolchain,
  * mini project missing,
  * repo not traced yet (run trace_noapi.py once; ~5 min) — tracing inside a
    unit test would be too slow and too flaky (GitHub rate limits).

Required assertions (task acceptance):
  * nat_refl: initial goal non-empty, run_tactic("rfl") -> finished=True
  * nat_refl + rfl (verify_proof)          -> ok=True
  * impossible_zero_eq_one + rfl           -> ok=False (LeanError, not success)
  * nat_refl + "sorry"                     -> ok=False (ProofGivenUp)
  * and_comm_simple step-by-step           -> finished=True
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from v40_sorry_resolver.verify import (
    DojoV2UnavailableError,
    LeanDojoV2Verifier,
)
from v40_sorry_resolver.verify.base import Verifier

MINI_PROJECT = Path("/mnt/agents/output/lean_mini_project")


def _by_name(tasks):
    return {t.theorem_name: t for t in tasks}


def _toolchain_available() -> bool:
    if shutil.which("lean") is None and not (Path.home() / ".elan/bin/lean").exists():
        return False
    try:
        import lean_dojo  # noqa: F401
    except Exception:
        return False
    if not MINI_PROJECT.is_dir():
        return False
    return True


def _traced(repo) -> bool:
    try:
        from lean_dojo.data_extraction.trace import is_available_in_cache

        return is_available_in_cache(repo)
    except Exception:
        return False


def _repo():
    from lean_dojo import LeanGitRepo

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=MINI_PROJECT,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return LeanGitRepo(str(MINI_PROJECT), commit)


requires_toolchain = pytest.mark.skipif(
    not _toolchain_available(),
    reason="Lean toolchain / lean_dojo / mini project unavailable",
)

requires_trace = pytest.mark.skipif(
    not (_toolchain_available() and _traced(_repo())),
    reason="mini project not traced yet (run /mnt/agents/output/trace_noapi.py once)",
)

pytestmark = [requires_toolchain, requires_trace, pytest.mark.asyncio]


async def _make_verifier(config) -> LeanDojoV2Verifier:
    v = LeanDojoV2Verifier(config)
    await v.init()
    return v


# --------------------------------------------------------------- protocol


async def test_implements_verifier_protocol(config):
    v = LeanDojoV2Verifier(config)
    assert isinstance(v, Verifier)


async def test_init_close_lifecycle(config):
    v = LeanDojoV2Verifier(config)
    await v.init()
    await v.close()
    await v.close()  # re-entrant
    with pytest.raises(DojoV2UnavailableError):
        await v.verify_proof(_fake_task(), "rfl")


def _fake_task():
    from v40_sorry_resolver.models import SorryTask

    return SorryTask(
        id="x",
        project_path=str(MINI_PROJECT),
        file_path="LeanMiniProject/Trivial.lean",
        line_number=1,
        column_number=1,
        theorem_name="nat_refl",
    )


# ------------------------------------------------------------ verify_proof


async def test_nat_refl_rfl_accepted(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "rfl")
        assert res.ok is True, res.error
        assert res.error is None
    finally:
        await v.close()


async def test_nat_refl_by_rfl_accepted(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "by rfl")
        assert res.ok is True, res.error
    finally:
        await v.close()


async def test_list_map_id_whole_script_accepted(config, mini_tasks):
    """Multi-line with-block combinator proof via the whole-tactic path."""
    v = await _make_verifier(config)
    try:
        proof = "induction xs with\n  | nil => rfl\n  | cons x xs ih => simp [List.map_cons, ih]"
        res = await v.verify_proof(_by_name(mini_tasks)["list_map_id"], proof)
        assert res.ok is True, res.error
    finally:
        await v.close()


async def test_impossible_zero_eq_one_rfl_rejected(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(
            _by_name(mini_tasks)["impossible_zero_eq_one"], "rfl"
        )
        assert res.ok is False
    finally:
        await v.close()


async def test_literal_sorry_rejected(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "sorry")
        assert res.ok is False
    finally:
        await v.close()


# -------------------------------------------------------- tactic-level API


async def test_run_tactic_rfl_finishes(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        task = _by_name(mini_tasks)["nat_refl"]
        init = await v.open_task(task)
        assert init.ok is True
        assert init.state_id == 0
        assert init.goals.strip() != ""
        res = await v.run_tactic(task, init.state_id, "rfl")
        assert res.ok is True
        assert res.finished is True
    finally:
        await v.close()


async def test_run_tactic_impossible_errors(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        task = _by_name(mini_tasks)["impossible_zero_eq_one"]
        init = await v.open_task(task)
        res = await v.run_tactic(task, init.state_id, "rfl")
        assert res.ok is False
        assert res.finished is False
        assert res.error  # real Lean error message
    finally:
        await v.close()


async def test_run_tactic_multistep_and_comm(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        task = _by_name(mini_tasks)["and_comm_simple"]
        s0 = await v.open_task(task)
        r1 = await v.run_tactic(task, s0.state_id, "apply And.intro")
        assert r1.ok and not r1.finished
        assert "case left" in r1.goals and "case right" in r1.goals
        r2 = await v.run_tactic(task, r1.state_id, "exact h.2")
        assert r2.ok and not r2.finished
        r3 = await v.run_tactic(task, r2.state_id, "exact h.1")
        assert r3.ok and r3.finished
        # branch replay from an earlier state with a wrong tactic must error
        r4 = await v.run_tactic(task, r1.state_id, "exact h.1")
        assert r4.ok is False
        assert r4.error
    finally:
        await v.close()


async def test_run_tactic_wrong_tactic_rejected(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        task = _by_name(mini_tasks)["nat_refl"]
        init = await v.open_task(task)
        res = await v.run_tactic(task, init.state_id, "exact Nat.succ n")
        assert res.ok is False
        assert res.finished is False
    finally:
        await v.close()
