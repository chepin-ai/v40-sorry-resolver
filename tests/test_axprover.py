"""AxProverV2 tests (fake verifier / fake LLM, no network)."""

from __future__ import annotations

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
    # One failure lesson was recorded and kept bounded.
    assert 1 <= len(solver.notebook) <= 3
    assert all(len(l) <= 200 for l in solver.notebook)


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
    assert all(len(l) <= 200 for l in solver.notebook)


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
