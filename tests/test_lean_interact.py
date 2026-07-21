"""LeanInteract backend tests (backend ``lean_interact``).

Two layers:
  * Always-on: missing-package error path raises
    ``LeanInteractUnavailableError`` with install guidance (no silent
    degradation, SPEC 3.8).
  * Protocol conformance against the real mini project (skipped when the
    lean-interact package or the Lean toolchain is unavailable): the backend
    implements the SPEC 3.6 Verifier protocol; accepts a real proof of
    ``nat_refl``; rejects a wrong proof, a literal ``sorry``, and a proof of
    the impossible theorem.
"""
from __future__ import annotations

import builtins
import shutil
from pathlib import Path

import pytest

from v40_sorry_resolver.verify.base import Verifier
from v40_sorry_resolver.verify.lean_interact import (
    LeanInteractUnavailableError,
    LeanInteractVerifier,
)

MINI_PROJECT = Path("/mnt/agents/output/lean_mini_project")


def _lean_interact_available() -> bool:
    try:
        import lean_interact  # noqa: F401
    except Exception:
        return False
    if shutil.which("lake") is None and not (Path.home() / ".elan/bin/lake").exists():
        return False
    return MINI_PROJECT.is_dir()


requires_lean_interact = pytest.mark.skipif(
    not _lean_interact_available(),
    reason="lean-interact package / Lean toolchain / mini project unavailable",
)

pytestmark = pytest.mark.asyncio


def _by_name(tasks):
    return {t.theorem_name: t for t in tasks}


# --------------------------------------------------- missing package path


async def test_missing_package_raises_with_install_guidance(config, monkeypatch):
    """No lean-interact installed -> explicit error with pip instructions."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "lean_interact" or name.startswith("lean_interact."):
            raise ImportError("No module named 'lean_interact'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    v = LeanInteractVerifier(config)
    with pytest.raises(LeanInteractUnavailableError) as excinfo:
        await v.init()
    msg = str(excinfo.value)
    assert "pip install" in msg and "lean-interact" in msg


async def test_verify_before_init_raises(config):
    v = LeanInteractVerifier(config)
    with pytest.raises(LeanInteractUnavailableError):
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


# ------------------------------------------------------- protocol conformance


@requires_lean_interact
async def test_implements_verifier_protocol(config):
    v = LeanInteractVerifier(config)
    assert isinstance(v, Verifier)


@requires_lean_interact
async def test_nat_refl_rfl_accepted(config, mini_tasks):
    v = LeanInteractVerifier(config)
    await v.init()
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "rfl")
        assert res.ok is True, res.error
        assert res.error is None
    finally:
        await v.close()


@requires_lean_interact
async def test_wrong_proof_rejected(config, mini_tasks):
    v = LeanInteractVerifier(config)
    await v.init()
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "exact 0")
        assert res.ok is False
        assert res.error  # real Lean error diagnostics
    finally:
        await v.close()


@requires_lean_interact
async def test_literal_sorry_rejected(config, mini_tasks):
    v = LeanInteractVerifier(config)
    await v.init()
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "sorry")
        assert res.ok is False
    finally:
        await v.close()


@requires_lean_interact
async def test_impossible_theorem_rejected(config, mini_tasks):
    v = LeanInteractVerifier(config)
    await v.init()
    try:
        res = await v.verify_proof(
            _by_name(mini_tasks)["impossible_zero_eq_one"], "rfl"
        )
        assert res.ok is False
    finally:
        await v.close()


@requires_lean_interact
async def test_medium_theorem_and_reuse(config, mini_tasks):
    """A multi-candidate run on one file reuses the warm REPL server."""
    v = LeanInteractVerifier(config)
    await v.init()
    try:
        task = _by_name(mini_tasks)["and_comm_simple"]
        bad = await v.verify_proof(task, "exact ⟨h.1, h.2⟩")
        assert bad.ok is False
        good = await v.verify_proof(task, "exact ⟨h.2, h.1⟩")
        assert good.ok is True, good.error
        await v.close()
        await v.close()  # re-entrant
    finally:
        await v.close()
