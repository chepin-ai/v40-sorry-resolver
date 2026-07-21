"""APOLLO sub-lemma decomposition tests (frontier_atp Top-8 #4).

Full flow with scripted fake LLM + fake verifier, no network / no Lean:
skeleton with 2 sub-lemmas (one proves, one fails then recovers on its
individual budget), isolated per-sub-lemma verification, reassembly, final
end-to-end verification. Also: lemma-cache sub-lemma hit, one recursion
level, and the AxProver integration trigger after >=2 consecutive failures.
"""

from __future__ import annotations

import pytest

from conftest import FakeLLMClient, FakeRouter, FakeVerifier
from v40_sorry_resolver.cache import Cache
from v40_sorry_resolver.models import ProofStatus, SorryTask
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine.decompose import ApolloDecomposer
from v40_sorry_resolver.engine.lemma_cache import LemmaCache
from v40_sorry_resolver.engine.axprover import AxProverV2
from v40_sorry_resolver.engine.orchestrator import StrategyConfig


def _strategy(max_iter=8) -> StrategyConfig:
    return StrategyConfig(
        tactic_search_depth=4,
        tactic_search_width=2,
        agentic_max_iterations=max_iter,
        thinking_max_tokens=1024,
        enable_thinking=False,
        explorer_share=0.3,
    )


@pytest.fixture()
def lean_task(tmp_path):
    """A real (tiny) on-disk project so the isolated synthetic-file path runs."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "lakefile.toml").write_text('name = "apollo"\n')
    (proj / "A.lean").write_text("theorem foo : 4 = 4 := by\n  sorry\n")
    return SorryTask(
        id="apollo-task",
        project_path=str(proj),
        file_path="A.lean",
        line_number=2,
        column_number=3,
        theorem_name="foo",
        goal_state="4 = 4",
        surrounding_context="theorem foo : 4 = 4 := by\n  sorry",
    )


SKELETON = (
    "```lean\n"
    "have h1 : 1 + 1 = 2 := by sorry\n"
    "have h2 : 2 + 2 = 4 := by sorry\n"
    "exact h2\n"
    "```"
)


def _apollo_script(fail_h2_once=True):
    """Prover script: decompose on request, prove h1, stumble once on h2."""
    state = {"h2_failed": False}

    def script(prompt, system_prompt, role, idx):
        if "Decompose the proof" in prompt:
            return SKELETON
        if "Prove ONLY this intermediate sub-lemma" in prompt:
            # Match the actual ask (`have h_i : ...`); the proven-block of an
            # h2 prompt also mentions h1, so check h2 first.
            if "have h2 : 2 + 2 = 4" in prompt:
                if fail_h2_once and not state["h2_failed"]:
                    state["h2_failed"] = True
                    return "junk ring attempt"
                return "VALID ring"
            if "have h1 : 1 + 1 = 2" in prompt:
                return "VALID rfl"
        return "junk whole proof"

    return script


def _critic():
    def critic_script(prompt, system_prompt, role, idx):
        blob = f"{system_prompt or ''} {prompt}".lower()
        if "review" in blob:
            return '{"approved": true, "reason": "ok"}'
        return "strategy: keep going"

    return FakeLLMClient(Role.CRITIC, script=critic_script)


def _router(prover_script):
    return FakeRouter(
        {
            Role.PROVER: FakeLLMClient(Role.PROVER, script=prover_script),
            Role.CRITIC: _critic(),
        }
    )


@pytest.mark.asyncio
async def test_apollo_decompose_isolate_reassemble(lean_task, tmp_path):
    """2 sub-lemmas, one真 one假-then-recovered: isolated verifies happen per
    sub-lemma, the failed one is re-proven on its own budget, and the
    reassembled skeleton passes the final end-to-end verification."""
    cache = Cache(str(tmp_path / "cache.db"))
    lemma_cache = LemmaCache(cache)
    router = _router(_apollo_script())
    verifier = FakeVerifier()
    decomposer = ApolloDecomposer(router, verifier, lemma_cache=lemma_cache, cfg=None)

    full = await decomposer.attempt(lean_task, strategy=_strategy())

    assert full is not None
    assert "sorry" not in full
    assert "have h1 : 1 + 1 = 2 := by" in full and "VALID rfl" in full
    assert "have h2 : 2 + 2 = 4 := by" in full and "VALID ring" in full
    assert "exact h2" in full

    proofs = [p for _tid, p in verifier.calls]
    task_ids = [tid for tid, _p in verifier.calls]
    # 1st: isolated h1 (synthetic task id != main task id).
    assert proofs[0] == "VALID rfl" and task_ids[0] != lean_task.id
    # 2nd: isolated h2 junk attempt fails; 3rd: h2 re-proven on its budget.
    assert proofs[1] == "junk ring attempt" and task_ids[1] != lean_task.id
    assert proofs[2] == "VALID ring" and task_ids[2] != lean_task.id
    # Last: the reassembled full proof against the REAL task.
    assert task_ids[-1] == lean_task.id
    assert proofs[-1] == full
    # The failed h2 attempt got verifier feedback in its retry prompt.
    prover = router.client(Role.PROVER)
    h2_prompts = [
        c["prompt"] for c in prover.calls if "h2 : 2 + 2 = 4" in c["prompt"]
    ]
    assert len(h2_prompts) == 2
    assert "previous attempt failed" in h2_prompts[1].lower()
    # Sub-lemmas + main goal were written to the shared lemma cache.
    assert (await lemma_cache.get("1 + 1 = 2"))["proof"] == "VALID rfl"
    assert (await lemma_cache.get("2 + 2 = 4"))["proof"] == "VALID ring"
    assert (await lemma_cache.get("4 = 4"))["proof"] == full
    # Isolation temp files are cleaned up.
    assert not (tmp_path / "proj" / ".apollo_tmp").exists()
    await cache.close()


@pytest.mark.asyncio
async def test_apollo_sublemma_lemma_cache_hit(lean_task, tmp_path):
    """A cached sub-lemma proof short-circuits its re-proof (Top-8 #5)."""
    cache = Cache(str(tmp_path / "cache.db"))
    lemma_cache = LemmaCache(cache)
    await lemma_cache.put("1 + 1 = 2", "VALID cached_h1", meta={"solver": "test"})
    router = _router(_apollo_script())
    verifier = FakeVerifier()
    decomposer = ApolloDecomposer(router, verifier, lemma_cache=lemma_cache, cfg=None)

    full = await decomposer.attempt(lean_task, strategy=_strategy())

    assert full is not None
    assert "VALID cached_h1" in full
    prover = router.client(Role.PROVER)
    # The prover was never asked to prove h1 (cache short-circuit); the
    # actual ask line is `have h1 : ...` (the h2 prompt's proven-block merely
    # mentions h1).
    assert not any(
        "have h1 : 1 + 1 = 2" in c["prompt"]
        and "Prove ONLY this intermediate sub-lemma" in c["prompt"]
        for c in prover.calls
    )
    await cache.close()


@pytest.mark.asyncio
async def test_apollo_recursive_one_level(lean_task):
    """A stubborn sub-lemma is decomposed one level deeper (APOLLO recursion)."""

    class Cfg:
        apollo_max_sublemmas = 3
        apollo_sublemma_retries = 1  # only one direct shot, then recursion
        apollo_recursive = True

    def script(prompt, system_prompt, role, idx):
        if "Decompose THIS sub-lemma" in prompt:
            return "```lean\nhave k1 : 1 = 1 := by sorry\nexact k1\n```"
        if "Decompose the proof" in prompt:
            return "```lean\nhave h1 : 1 = 1 := by sorry\nexact h1\n```"
        if "k1 : 1 = 1" in prompt:
            return "VALID rfl"
        return "junk"  # direct h1 attempts always fail

    router = _router(script)
    verifier = FakeVerifier()
    decomposer = ApolloDecomposer(router, verifier, lemma_cache=None, cfg=Cfg())

    full = await decomposer.attempt(lean_task, strategy=_strategy())

    assert full is not None
    assert "have k1 : 1 = 1 := by" in full and "VALID rfl" in full


@pytest.mark.asyncio
async def test_apollo_aborts_when_sublemma_unprovable(lean_task):
    """If a sub-lemma cannot be proven (and recursion is off), no proof is
    returned and nothing is booked."""

    class Cfg:
        apollo_max_sublemmas = 3
        apollo_sublemma_retries = 1
        apollo_recursive = False

    router = _router(_apollo_script())  # h2 recovered... force all junk instead

    def all_junk_subs(prompt, system_prompt, role, idx):
        if "Decompose the proof" in prompt:
            return SKELETON
        return "junk"

    router = _router(all_junk_subs)
    verifier = FakeVerifier()
    decomposer = ApolloDecomposer(router, verifier, lemma_cache=None, cfg=Cfg())

    assert await decomposer.attempt(lean_task, strategy=_strategy()) is None
    # The main task was never verified (no fake success booked).
    assert all(tid != lean_task.id for tid, _p in verifier.calls)


@pytest.mark.asyncio
async def test_axprover_triggers_apollo_after_two_failures(make_task):
    """Integration: 2 consecutive agentic failures -> APOLLO decomposition
    solves the task; solver/status/accounting reflect the apollo path."""
    prover = FakeLLMClient(Role.PROVER, script=_apollo_script())
    router = FakeRouter({Role.PROVER: prover, Role.CRITIC: _critic()})
    verifier = FakeVerifier()
    solver = AxProverV2(router, verifier, cfg=None)

    result = await solver.solve(make_task(0), _strategy())

    assert result.success is True
    assert result.solver == "axprover_v2_apollo"
    assert result.status == ProofStatus.SOLVED_AGENTIC
    assert result.verification_passed is True  # grounded in decomposer verify
    assert "have h1" in result.proof and "sorry" not in result.proof
    # APOLLO fired only after >=2 whole-proof failures.
    whole_proof_calls = [
        c for c in prover.calls if "Decompose" not in c["prompt"]
        and "sub-lemma" not in c["prompt"]
    ]
    assert len(whole_proof_calls) >= 2


@pytest.mark.asyncio
async def test_axprover_apollo_disabled_never_decomposes(make_task):
    class Cfg:
        apollo_enabled = False
        replan_max = 0

    prover = FakeLLMClient(Role.PROVER, script="junk proof")
    router = FakeRouter({Role.PROVER: prover, Role.CRITIC: _critic()})
    solver = AxProverV2(router, FakeVerifier(remaining=1), cfg=Cfg())

    result = await solver.solve(make_task(0), _strategy(max_iter=4))

    assert result.success is False
    assert not any("Decompose the proof" in c["prompt"] for c in prover.calls)
