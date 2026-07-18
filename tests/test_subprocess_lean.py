"""Real subprocess-lean verifier tests against the mini project (SPEC 3.7).

These are NOT mocks: they run the real ``lake env lean`` toolchain on
/mnt/agents/output/lean_mini_project. Required assertions (task acceptance):
  * nat_refl + rfl            -> ok=True
  * one_plus_one + rfl/decide -> ok=True
  * proof = "sorry"           -> ok=False   (built-in self-check, SPEC 3.7.7)
  * impossible_zero_eq_one+rfl-> ok=False   (false statement rejected)
  * blacklist "admit"         -> ok=False
"""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from v40_sorry_resolver.verify import (
    MockVerifier,
    SubprocessLeanVerifier,
    build_verifier,
)

def _by_name(tasks):
    return {t.theorem_name: t for t in tasks}


async def _make_verifier(config) -> SubprocessLeanVerifier:
    v = build_verifier(config)
    assert isinstance(v, SubprocessLeanVerifier)
    await v.init()
    return v


# --------------------------------------------------------------- acceptance
@pytest.mark.asyncio
async def test_nat_refl_rfl_accepted(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "rfl")
        assert res.ok is True, res.error
        assert res.error is None
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_one_plus_one_rfl_and_decide_accepted(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        task = _by_name(mini_tasks)["one_plus_one"]
        for proof in ("rfl", "decide"):
            res = await v.verify_proof(task, proof)
            assert res.ok is True, f"{proof}: {res.error}"
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_literal_sorry_rejected(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "sorry")
        assert res.ok is False
        assert "blacklist" in (res.error or "").lower()
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_impossible_zero_eq_one_rfl_rejected(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["impossible_zero_eq_one"], "rfl")
        assert res.ok is False
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_blacklist_admit_rejected(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "admit")
        assert res.ok is False
        assert "blacklist" in (res.error or "").lower()
    finally:
        await v.close()


# ----------------------------------------------------------------- robustness
@pytest.mark.asyncio
async def test_false_statements_rejected_for_all_tactics(config, mini_tasks):
    """Every candidate for the intentionally-unprovable theorems must fail."""
    v = await _make_verifier(config)
    try:
        by = _by_name(mini_tasks)
        for name in ("impossible_zero_eq_one", "unprovable_all_even"):
            for proof in ("rfl", "decide", "omega", "simp"):
                res = await v.verify_proof(by[name], proof)
                assert res.ok is False, f"{name} wrongly accepted {proof!r}"
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_word_boundary_blacklist_not_substring(config, mini_tasks):
    """`sorry` only inside a comment or as a substring must NOT be blacklisted.

    The underlying proof may still fail to *compile*, but it must not be
    rejected by the text blacklist (fixes v39 substring false-positives).
    """
    v = await _make_verifier(config)
    try:
        task = _by_name(mini_tasks)["nat_refl"]
        # "sorry" appears only in a trailing comment -> not blacklisted.
        res = await v.verify_proof(task, "rfl -- closes the goal, no sorry here")
        assert res.ok is True, res.error
        # `readmit`/`stoppable` contain admit/stop as substrings -> not blocked.
        res2 = await v.verify_proof(task, "rfl -- readmit stoppable")
        assert res2.ok is True, res2.error
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_multiline_tactic_proof(config, mini_tasks):
    v = await _make_verifier(config)
    try:
        task = _by_name(mini_tasks)["list_map_id"]
        proof = "induction xs with\n  | nil => rfl\n  | cons x xs ih => simp [List.map_cons, ih]"
        res = await v.verify_proof(task, proof)
        assert res.ok is True, res.error
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_original_project_not_polluted(config, mini_tasks, mini_project):
    target = Path(mini_project) / "LeanMiniProject" / "Trivial.lean"
    before = target.read_text()
    v = await _make_verifier(config)
    try:
        await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "rfl")
        await v.verify_proof(_by_name(mini_tasks)["one_plus_one"], "decide")
    finally:
        await v.close()
    assert target.read_text() == before, "original project file was mutated!"


@pytest.mark.asyncio
async def test_concurrent_same_file_verifies(config, mini_tasks):
    """Concurrent verifies (incl. same file) stay isolated + correct."""
    v = await _make_verifier(config)
    try:
        by = _by_name(mini_tasks)
        jobs = [
            (by["nat_refl"], "rfl", True),
            (by["one_plus_one"], "decide", True),          # same file as nat_refl
            (by["and_comm_simple"], "exact ⟨h.2, h.1⟩", True),  # same file too
            (by["impossible_zero_eq_one"], "rfl", False),
            (by["add_comm_custom"], "omega", True),
        ]
        results = await asyncio.gather(*(v.verify_proof(t, p) for t, p, _ in jobs))
        for (_, _, expect), res in zip(jobs, results):
            assert res.ok is expect, f"expected {expect}, got {res.ok} ({res.error})"
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_timeout_kills_process(config, mini_tasks):
    fast = dataclasses.replace(config, lean_timeout_s=0.05)
    v = await _make_verifier(fast)
    try:
        res = await v.verify_proof(_by_name(mini_tasks)["nat_refl"], "rfl")
        assert res.ok is False
        assert "timed out" in (res.error or "").lower()
    finally:
        await v.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_term_position_and_inline_sorry_splice(config, tmp_path):
    """Sorries in term position (``:= sorry``) and inline (``:= by sorry``).

    The mini project only has tactic-position sorries; real SorryDB projects
    also have term/inline ones. The splice must produce valid Lean for both
    (wrap term-position tactic proofs in a fresh ``by`` block, don't double-wrap
    proofs that already start with ``by``).
    """
    proj = tmp_path / "proj"  # keep the project separate from config.work_dir
    proj.mkdir()
    (proj / "lakefile.toml").write_text('name = "tmptest"\n')
    (proj / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
    (proj / "Term.lean").write_text(
        "theorem foo_term : 1 + 1 = 2 := sorry\n\n"
        "theorem bar_term (n : Nat) : n = n := sorry\n\n"
        "theorem baz_tac (n : Nat) : n = n := by sorry\n"
    )
    from v40_sorry_resolver.sorrydb import SorryScanner

    tasks = {t.theorem_name: t for t in SorryScanner().scan([str(proj)])}
    v = await _make_verifier(config)
    try:
        for name, proof in [
            ("foo_term", "rfl"), ("foo_term", "decide"),
            ("bar_term", "rfl"), ("bar_term", "by rfl"),
            ("baz_tac", "rfl"),
        ]:
            res = await v.verify_proof(tasks[name], proof)
            assert res.ok is True, f"{name}/{proof}: {res.error}"
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_workdir_inside_project_no_recursion(tmp_path):
    """Base copy must exclude verify_tmp when work_dir lives inside the project."""
    from v40_sorry_resolver.sorrydb import SorryScanner
    from v40_sorry_resolver.config import V40Config

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "lakefile.toml").write_text('name = "x"\n')
    (proj / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
    (proj / "A.lean").write_text("theorem a : True := by\n  sorry\n")
    cfg = V40Config(work_dir=str(proj / ".v40_scratch"))  # inside the project
    tasks = SorryScanner().scan([str(proj)])
    assert [t.theorem_name for t in tasks] == ["a"]
    v = build_verifier(cfg)
    await v.init()
    try:
        res = await v.verify_proof(tasks[0], "trivial")
        assert res.ok is True, res.error
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_self_check(config, mini_project):
    v = await _make_verifier(config)
    try:
        report = await v.self_check(mini_project)
        assert report["nat_refl_rfl"] is True
        assert report["literal_sorry_rejected"] is True
    finally:
        await v.close()


# --------------------------------------------------------------- mock sanity
@pytest.mark.asyncio
async def test_mock_verifier_valid_marker_only(config, mini_tasks):
    v = MockVerifier(config)
    task = _by_name(mini_tasks)["nat_refl"]
    ok = await v.verify_proof(task, "VALID rfl")
    bad = await v.verify_proof(task, "rfl")
    apply_heuristic = await v.verify_proof(task, "apply some_lemma")  # no false+
    assert ok.ok is True
    assert bad.ok is False
    assert apply_heuristic.ok is False, "mock must not have the v39 apply false-positive"
    # caller can detect the mock to annotate results UNVERIFIED (SPEC 3.8)
    assert getattr(v, "is_mock", False) is True
    assert getattr(build_verifier(config), "is_mock", False) is False


# ------------------------------------------------------------- factory gating
def test_build_verifier_factory(config):
    assert isinstance(build_verifier(config), SubprocessLeanVerifier)
    mock_cfg = dataclasses.replace(config, verifier="mock")
    assert isinstance(build_verifier(mock_cfg), MockVerifier)
    bad_cfg = dataclasses.replace(config, verifier="nope")
    with pytest.raises(ValueError):
        build_verifier(bad_cfg)


@pytest.mark.asyncio
async def test_dojo_verifier_fails_loudly(config):
    """Dojo path is upstream-blocked: init must raise, never silently degrade."""
    from v40_sorry_resolver.verify import DojoUnavailableError, LeanDojoVerifier

    dojo_cfg = dataclasses.replace(config, verifier="dojo")
    v = build_verifier(dojo_cfg)
    assert isinstance(v, LeanDojoVerifier)
    with pytest.raises(DojoUnavailableError):
        await v.init()
