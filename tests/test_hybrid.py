"""Hybrid verifier end-to-end tests on the real mini project.

Dual channel: tactic-level probing via the resident REPL pool (real lean-dojo
sessions) + final judgement via subprocess whole-file compilation. Requires
the full toolchain (lean + lean_dojo + trace cache); skipped otherwise.

Covered (task acceptance):
  * nat_refl: hybrid verify_proof accepts `rfl` (both channels agree)
  * impossible_zero_eq_one: rejected
  * tactic-level probing: open_task returns the real initial goal;
    run_tactic("rfl") finishes; wrong tactic errors
  * disagreement counter integrity: dual_checked == agree + disagree_*
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from v40_sorry_resolver.verify import HybridVerifier
from v40_sorry_resolver.verify.base import Verifier

MINI_PROJECT = Path("/mnt/agents/output/lean_mini_project")


def _toolchain_available() -> bool:
    if shutil.which("lean") is None and not (Path.home() / ".elan/bin/lean").exists():
        return False
    try:
        import lean_dojo  # noqa: F401
    except Exception:
        return False
    return MINI_PROJECT.is_dir()


def _repo():
    from lean_dojo import LeanGitRepo

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=MINI_PROJECT,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return LeanGitRepo(str(MINI_PROJECT), commit)


def _traced(repo) -> bool:
    try:
        from lean_dojo.data_extraction.trace import is_available_in_cache

        return is_available_in_cache(repo)
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _toolchain_available(),
        reason="Lean toolchain / lean_dojo / mini project unavailable",
    ),
    pytest.mark.skipif(
        not (_toolchain_available() and _traced(_repo())),
        reason="mini project not traced yet (run /mnt/agents/output/trace_noapi.py once)",
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def verifier(config):
    v = HybridVerifier(config)
    await v.init()
    yield v
    await v.close()


def _by_name(tasks):
    return {t.theorem_name: t for t in tasks}


async def test_implements_verifier_protocol(config):
    v = HybridVerifier(config)
    assert isinstance(v, Verifier)


async def test_nat_refl_hybrid_accepted(verifier, mini_tasks):
    task = _by_name(mini_tasks)["nat_refl"]
    res = await verifier.verify_proof(task, "rfl")
    assert res.ok is True, res.error
    assert "repl-witness: agree" in res.diagnostics
    s = verifier.stats
    assert s["dual_checked"] >= 1
    assert s["dual_checked"] == (
        s["agree"] + s["disagree_subprocess_only"] + s["disagree_repl_only"]
    )


async def test_impossible_rejected(verifier, mini_tasks):
    task = _by_name(mini_tasks)["impossible_zero_eq_one"]
    res = await verifier.verify_proof(task, "rfl")
    assert res.ok is False
    # subprocess channel is the verdict; REPL witness agrees on rejection
    assert "repl-witness" in (res.diagnostics or "")


async def test_literal_sorry_rejected(verifier, mini_tasks):
    task = _by_name(mini_tasks)["nat_refl"]
    res = await verifier.verify_proof(task, "sorry")
    assert res.ok is False


async def test_tactic_level_probing(verifier, mini_tasks):
    task = _by_name(mini_tasks)["and_comm_simple"]
    sess, init = await verifier.open_task(task)
    try:
        assert init.ok and init.goals.strip() != ""
        r1 = await verifier.run_tactic(sess, init.state_id, "apply And.intro")
        assert r1.ok and not r1.finished
        assert "case left" in r1.goals
        r2 = await verifier.run_tactic(sess, r1.state_id, "exact h.2")
        assert r2.ok and not r2.finished
        r3 = await verifier.run_tactic(sess, r2.state_id, "exact h.1")
        assert r3.ok and r3.finished
        # wrong tactic from an earlier state errors honestly
        r4 = await verifier.run_tactic(sess, r1.state_id, "exact h.1")
        assert r4.ok is False and r4.error
    finally:
        await verifier.release_session(sess)


async def test_probe_goals_one_shot(verifier, mini_tasks):
    init = await verifier.probe_goals(_by_name(mini_tasks)["nat_refl"])
    assert init.ok
    assert "n = n" in init.goals
