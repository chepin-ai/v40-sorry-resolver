"""AxProverV2 tests (fake verifier / fake LLM, no network)."""

from __future__ import annotations

import asyncio

import pytest

from conftest import FakeLLMClient, FakeRouter, FakeVerifier
from v40_sorry_resolver.models import ProofStatus
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine.orchestrator import StrategyConfig
from v40_sorry_resolver.engine.axprover import AxProverV2


def _strategy(max_iter=8, thinking=False) -> StrategyConfig:
    return StrategyConfig(
        tactic_search_depth=4,
        tactic_search_width=2,
        agentic_max_iterations=max_iter,
        thinking_max_tokens=1024,
        enable_thinking=thinking,
        explorer_share=0.3,
    )


def _critic_script(prompt, system_prompt, role, idx):
    blob = f"{system_prompt or ''} {prompt}".lower()
    if "review" in blob:
        return '{"approved": true, "reason": "looks valid"}'
    return "strategy: try a simpler tactic"


def _router(prover_script, critic_script=_critic_script):
    return FakeRouter(
        {
            Role.PROVER: FakeLLMClient(Role.PROVER, script=prover_script),
            Role.CRITIC: FakeLLMClient(Role.CRITIC, script=critic_script),
        }
    )


@pytest.mark.asyncio
async def test_agentic_solves_on_second_iteration(make_task):
    def prover_script(prompt, system_prompt, role, idx):
        return "junk proof" if idx == 1 else "VALID proof v2"

    router = _router(prover_script)
    verifier = FakeVerifier()
    solver = AxProverV2(router, verifier, cfg=None)

    result = await solver.solve(make_task(0), _strategy())

    assert result.success is True
    assert result.status == ProofStatus.SOLVED_AGENTIC
    assert result.solver == "axprover_v2"
    assert result.iterations == 2  # actual iterations, not max
    assert result.verification_passed is True
    assert result.tokens_used > 0
    # One failure lesson was recorded and kept bounded; entries are
    # (lesson, raw_diagnostics) pairs (frontier_atp Top-8 #2).
    assert 1 <= len(solver.notebook) <= 3
    assert all(len(pair) == 2 and len(pair[0]) <= 200 for pair in solver.notebook)


@pytest.mark.asyncio
async def test_stall_breaks_at_patience(make_task):
    class Cfg:
        agentic_stall_patience = 3

    router = _router("junk proof")
    # remaining_sorries constant -> never improves.
    verifier = FakeVerifier(remaining=1)
    solver = AxProverV2(router, verifier, cfg=Cfg())

    result = await solver.solve(make_task(0), _strategy(max_iter=8))

    assert result.success is False
    assert result.verification_passed is False  # never self-signed
    # iters 0..3: stall reaches patience(3) at iter 3 -> break -> 4 iterations.
    assert result.iterations == 4, result.iterations
    assert len(solver.notebook) <= 3  # bounded notebook


@pytest.mark.asyncio
async def test_lessons_truncated_to_200_chars(make_task):
    long_lesson = "x" * 500
    router = _router("junk", critic_script=lambda *a: long_lesson)
    verifier = FakeVerifier(remaining=1)
    solver = AxProverV2(router, verifier, cfg=None)

    result = await solver.solve(make_task(0), _strategy(max_iter=4))
    assert result.success is False
    assert solver.notebook
    assert all(len(pair[0]) <= 200 for pair in solver.notebook)


@pytest.mark.asyncio
async def test_critic_rejection_continues_loop(make_task):
    def critic_script(prompt, system_prompt, role, idx):
        blob = f"{system_prompt or ''} {prompt}".lower()
        if "review" in blob:
            return '{"approved": false, "reason": "circular argument"}'
        return "direction: fix circularity"

    router = _router("VALID proof", critic_script=critic_script)
    verifier = FakeVerifier()
    solver = AxProverV2(router, verifier, cfg=None)

    result = await solver.solve(make_task(0), _strategy(max_iter=3))
    # Proof verifies but critic keeps rejecting -> not solved, not self-signed.
    assert result.success is False
    assert result.verification_passed is False
    assert result.iterations == 3


@pytest.mark.asyncio
async def test_thinking_budget_used_when_enabled(make_task):
    router = _router("VALID proof")
    verifier = FakeVerifier()
    solver = AxProverV2(router, verifier, cfg=None)
    strategy = _strategy(thinking=True)

    result = await solver.solve(make_task(0), strategy)
    assert result.success is True
    prover = router.client(Role.PROVER)
    assert prover.calls[0]["thinking"] is True
    assert prover.calls[0]["max_tokens"] == strategy.thinking_max_tokens


@pytest.mark.asyncio
async def test_notebook_isolated_across_concurrent_tasks(make_task):
    """N-3 regression: ONE shared AxProverV2, two CONCURRENT solves — a lesson
    learned on task A must never leak into task B's propose prompts."""
    seen_prompts: dict[str, list[str]] = {"theorem_0": [], "theorem_1": []}
    attempts: dict[str, int] = {"theorem_0": 0, "theorem_1": 0}

    def prover_script(prompt, system_prompt, role, idx):
        name = "theorem_0" if "Theorem theorem_0" in prompt else "theorem_1"
        seen_prompts[name].append(prompt)
        attempts[name] += 1
        # First attempt per task fails verification, the second succeeds.
        return "junk proof" if attempts[name] == 1 else "VALID proof"

    def critic_script(prompt, system_prompt, role, idx):
        blob = f"{system_prompt or ''} {prompt}"
        if "review" in blob.lower():
            return '{"approved": true, "reason": "ok"}'
        # Lesson names the failed task (unique cross-contamination marker).
        name = "theorem_0" if "Theorem theorem_0" in blob else "theorem_1"
        return f"direction: avoid the {name} mistake"

    router = FakeRouter(
        {
            Role.PROVER: FakeLLMClient(Role.PROVER, script=prover_script, delay=0.01),
            Role.CRITIC: FakeLLMClient(Role.CRITIC, script=critic_script, delay=0.01),
        }
    )
    solver = AxProverV2(router, FakeVerifier(), cfg=None)  # shared instance
    r0, r1 = await asyncio.gather(
        solver.solve(make_task(0), _strategy(max_iter=3)),
        solver.solve(make_task(1), _strategy(max_iter=3)),
    )
    assert r0.success and r1.success

    # Each task's second prompt carries its OWN lesson...
    assert any(
        "avoid the theorem_0 mistake" in p for p in seen_prompts["theorem_0"][1:]
    )
    assert any(
        "avoid the theorem_1 mistake" in p for p in seen_prompts["theorem_1"][1:]
    )
    # ...and NEVER the other task's lesson (the pre-fix pollution).
    assert "theorem_1 mistake" not in "\n".join(seen_prompts["theorem_0"])
    assert "theorem_0 mistake" not in "\n".join(seen_prompts["theorem_1"])


# ======================================================================
# Frontier: verifier-guided repair loop (frontier_atp Top-8 #2)
# ======================================================================

class _DiagnosticsVerifier(FakeVerifier):
    """Fake verifier emitting a configurable raw diagnostics string."""

    def __init__(self, diagnostics: str, **kwargs):
        super().__init__(**kwargs)
        self._diagnostics = diagnostics

    async def verify_proof(self, task, proof):
        vr = await super().verify_proof(task, proof)
        if not vr.ok:
            vr.diagnostics = self._diagnostics
        return vr


@pytest.mark.asyncio
async def test_raw_diagnostics_injected_into_next_prompt(make_task):
    """On verify failure the NEXT propose prompt must carry the CRITIC lesson
    AND the verifier's raw Lean diagnostics (frontier_atp Top-8 #2)."""
    raw_diag = "Main.lean:2:4: error: type mismatch\n  rfl\nhas type\n  ?m = ?m"
    prompts: list[str] = []

    def prover_script(prompt, system_prompt, role, idx):
        prompts.append(prompt)
        return "junk proof" if idx == 1 else "VALID proof v2"

    router = _router(prover_script)
    verifier = _DiagnosticsVerifier(raw_diag)
    solver = AxProverV2(router, verifier, cfg=None)

    result = await solver.solve(make_task(0), _strategy())
    assert result.success is True
    assert len(prompts) == 2
    # Round-2 prompt: raw diagnostics + critic lesson both present.
    assert "Raw verifier diagnostics" in prompts[1]
    assert "type mismatch" in prompts[1]
    assert "strategy: try a simpler tactic" in prompts[1]
    # Round-1 prompt (no failures yet) must NOT contain the block.
    assert "Raw verifier diagnostics" not in prompts[0]
    # Notebook holds (lesson, raw_diagnostics) pairs, <=3 entries.
    assert 1 <= len(solver.notebook) <= 3
    lesson, diag = solver.notebook[-1]
    assert lesson == "strategy: try a simpler tactic"
    assert "type mismatch" in diag


@pytest.mark.asyncio
async def test_raw_diagnostics_truncated_to_500_chars(make_task):
    """Raw diagnostics are truncated to ~500 chars per notebook entry."""
    raw_diag = "E" * 1200
    router = _router("junk proof")
    verifier = _DiagnosticsVerifier(raw_diag)
    solver = AxProverV2(router, verifier, cfg=None)

    result = await solver.solve(make_task(0), _strategy(max_iter=2))
    assert result.success is False
    assert solver.notebook
    for lesson, diag in solver.notebook:
        assert len(lesson) <= 200
        assert len(diag) <= 500
    assert solver.notebook[-1][1] == "E" * 500
