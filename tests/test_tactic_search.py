"""TacticSearchEngine tests (fake verifier / fake LLM, no network)."""

from __future__ import annotations

import pytest

from conftest import FakeLLMClient, FakeRouter, FakeVerifier
from v40_sorry_resolver.models import ProofStatus
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine import extract_lean_code
from v40_sorry_resolver.engine.orchestrator import StrategyConfig
from v40_sorry_resolver.engine.tactic_search import TacticSearchEngine


def test_extract_lean_code_three_level_fallback():
    # Level 1: lean/lean4/bare fence (longest block wins).
    assert extract_lean_code("bla\n```lean\n  rfl\n```\ntrail") == "rfl"
    assert extract_lean_code("```lean4\nsimp\n```") == "simp"
    assert extract_lean_code("```\nomega\n```") == "omega"
    assert extract_lean_code("```lean\na\n```\n```lean\nbb\ncc\n```") == "bb\ncc"
    # Level 2: other language tag.
    assert extract_lean_code("```tactics\n  decide\n```") == "decide"
    # Level 3: bare by-block, then raw text.
    assert extract_lean_code("Sure! by\n  exact rfl") == "by\n  exact rfl"
    assert extract_lean_code("rfl") == "rfl"
    assert extract_lean_code("") == ""


def _strategy(depth=4, width=2, share=0.3) -> StrategyConfig:
    return StrategyConfig(
        tactic_search_depth=depth,
        tactic_search_width=width,
        agentic_max_iterations=8,
        thinking_max_tokens=2048,
        enable_thinking=False,
        explorer_share=share,
    )


@pytest.mark.asyncio
async def test_search_solves_at_depth_one(make_task):
    prover = FakeLLMClient(Role.PROVER, script="VALID_tactic")
    router = FakeRouter({Role.PROVER: prover})
    verifier = FakeVerifier()
    engine = TacticSearchEngine(router, verifier)

    result = await engine.search(make_task(0), _strategy())

    assert result.success is True
    assert result.status == ProofStatus.SOLVED_SEARCH
    assert result.solver == "tactic_search"
    assert result.verification_passed is True
    assert "VALID_tactic" in result.proof
    assert result.iterations >= 1
    assert result.tokens_used > 0


@pytest.mark.asyncio
async def test_search_beam_progression_deeper(make_task):
    """VALID tactic only appears at depth 2 (first-level tactic is inert)."""
    calls = {"n": 0}

    def script(prompt, system_prompt, role, idx):
        return "step1" if "empty - propose the first tactic" in prompt else "VALID_step2"

    prover = FakeLLMClient(Role.PROVER, script=script)
    router = FakeRouter({Role.PROVER: prover})
    verifier = FakeVerifier()
    engine = TacticSearchEngine(router, verifier)

    result = await engine.search(make_task(0), _strategy(depth=3, width=1, share=0.0))
    assert result.success is True
    assert "step1" in result.proof and "VALID_step2" in result.proof


@pytest.mark.asyncio
async def test_explorer_share_splits_generation(make_task):
    prover = FakeLLMClient(Role.PROVER, script="junk_p")
    explorer = FakeLLMClient(Role.EXPLORER, script="junk_e")
    router = FakeRouter({Role.PROVER: prover, Role.EXPLORER: explorer})
    verifier = FakeVerifier()
    engine = TacticSearchEngine(router, verifier)

    result = await engine.search(make_task(0), _strategy(depth=1, width=4, share=0.5))

    assert result.success is False
    # width=4, share=0.5 -> 2 prover + 2 explorer proposals on the root node.
    assert len(prover.calls) == 2, len(prover.calls)
    assert len(explorer.calls) == 2, len(explorer.calls)
    # Diversity: prover temperature 0.2, explorer 0.5.
    assert all(c["temperature"] == 0.2 for c in prover.calls)
    assert all(c["temperature"] == 0.5 for c in explorer.calls)


@pytest.mark.asyncio
async def test_heapq_ties_and_dedup_no_typeerror(make_task):
    """Constant tactic => identical fingerprints (dedup) and tied priorities;
    the monotonic counter must prevent heapq TypeError."""
    prover = FakeLLMClient(Role.PROVER, script="same_tactic")
    router = FakeRouter({Role.PROVER: prover})
    verifier = FakeVerifier(remaining=1)  # constant remaining -> tied priorities
    engine = TacticSearchEngine(router, verifier)

    result = await engine.search(make_task(0), _strategy(depth=3, width=2, share=0.0))
    assert result.success is False
    assert result.iterations > 0
    # Dedup: "same_tactic" from the root verified exactly once per unique state.
    states = {proof for _, proof in verifier.calls}
    assert len(states) == len(verifier.calls)


@pytest.mark.asyncio
async def test_search_exhaustion_reports_actual_iterations(make_task):
    prover = FakeLLMClient(Role.PROVER, script="useless")
    router = FakeRouter({Role.PROVER: prover})
    verifier = FakeVerifier()
    engine = TacticSearchEngine(router, verifier)

    result = await engine.search(make_task(0), _strategy(depth=2, width=2, share=0.0))
    assert result.success is False
    assert result.status == ProofStatus.FAILED_ALL
    assert result.iterations == len(verifier.calls)
    assert result.error


# ======================================================================
# Frontier: length-normalized beam tie-break (frontier_atp Top-8 #3,
# BFS-Prover arXiv:2502.03438: score penalty alpha * log L)
# ======================================================================

import math


def _tiebreak_engine(alpha):
    prover = FakeLLMClient(Role.PROVER, script="t")
    router = FakeRouter({Role.PROVER: prover})
    return TacticSearchEngine(
        router, FakeVerifier(), length_norm_alpha=alpha
    )


def test_length_tiebreak_math():
    # alpha = 0 -> always 0 (regression-safe: pure FIFO tie-break).
    assert _tiebreak_engine(0.0)._length_tiebreak("a b c d e") == 0.0
    engine = _tiebreak_engine(0.1)
    assert engine._length_tiebreak("one") == 0.0  # log(1) = 0
    assert engine._length_tiebreak(" ".join(["t"] * 10)) == pytest.approx(
        0.1 * math.log(10)
    )
    # Longer proofs get a strictly larger penalty.
    assert engine._length_tiebreak("t " * 50) > engine._length_tiebreak("t t")


def _two_candidate_setup():
    """width=1, share=0.6 -> prover(long)+explorer(short) both proposed per
    state; both rejected with the same remaining_sorries -> equal priority,
    so the length tie-break alone picks the beam winner."""
    long_tactic = " ".join(["alpha"] * 40)
    prover = FakeLLMClient(Role.PROVER, script=long_tactic)
    explorer = FakeLLMClient(Role.EXPLORER, script="b")
    router = FakeRouter({Role.PROVER: prover, Role.EXPLORER: explorer})
    verifier = FakeVerifier(remaining=1)  # equal priority for all candidates
    return router, verifier, long_tactic


@pytest.mark.asyncio
async def test_length_norm_tiebreak_prefers_shorter_proof(make_task):
    router, verifier, _ = _two_candidate_setup()
    engine = TacticSearchEngine(router, verifier, length_norm_alpha=1.0)
    strategy = _strategy(depth=2, width=1, share=0.6)

    result = await engine.search(make_task(0), strategy)
    assert result.success is False  # no VALID tactic anywhere
    # Depth-2 expansions must build on the SHORT depth-1 winner.
    depth2_states = [proof for _, proof in verifier.calls[2:]]
    assert depth2_states, "expected depth-2 expansions"
    assert all(s.startswith("b\n") for s in depth2_states), depth2_states


@pytest.mark.asyncio
async def test_alpha_zero_matches_fifo_tiebreak(make_task):
    """alpha=0 reproduces the old behavior exactly: equal-priority ties are
    broken by insertion order (prover candidate first)."""
    router, verifier, long_tactic = _two_candidate_setup()
    engine = TacticSearchEngine(router, verifier, length_norm_alpha=0.0)
    strategy = _strategy(depth=2, width=1, share=0.6)

    result = await engine.search(make_task(0), strategy)
    assert result.success is False
    depth2_states = [proof for _, proof in verifier.calls[2:]]
    assert depth2_states
    assert all(s.startswith("alpha") for s in depth2_states), depth2_states
