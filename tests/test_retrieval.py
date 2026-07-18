"""Premise retrieval tests (frontier_atp Top-8 #6; engine/retrieval.py).

All HTTP is mocked via injected transport callables — no real network access.
Covered: endpoint payload parsing (leansearch.net POST + premise-search.com
GET), merge/dedupe/top_k, 8s-timeout and failure degradation ([] + WARNING,
never blocking), mathlib-constant gating, and Critic/AxProver prompt wiring.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import FakeLLMClient, FakeRouter, FakeVerifier
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine.agents import CriticAgent
from v40_sorry_resolver.engine.axprover import AxProverV2
from v40_sorry_resolver.engine.retrieval import (
    PremiseRetriever,
    has_mathlib_constant,
    search_premises,
    _extract_names,
)


# ------------------------------------------------------------------ helpers

def _retriever(post_payload=None, get_payload=None, post_exc=None, get_exc=None,
               post_delay=0.0, get_delay=0.0, timeout_s=8.0, calls=None):
    async def fake_post(url, payload, timeout):
        if calls is not None:
            calls.append(("POST", url, payload))
        if post_delay:
            await asyncio.sleep(post_delay)
        if post_exc is not None:
            raise post_exc
        return post_payload

    async def fake_get(url, params, timeout):
        if calls is not None:
            calls.append(("GET", url, params))
        if get_delay:
            await asyncio.sleep(get_delay)
        if get_exc is not None:
            raise get_exc
        return get_payload

    return PremiseRetriever(
        timeout_s=timeout_s, http_post=fake_post, http_get=fake_get
    )


# ------------------------------------------------------------------ regex

def test_has_mathlib_constant():
    assert has_mathlib_constant("⊢ Nat.add_comm n m = Nat.add_comm m n")
    assert has_mathlib_constant("List.Perm (a :: l) l'")
    assert not has_mathlib_constant("⊢ n + 0 = n")
    assert not has_mathlib_constant("")
    assert not has_mathlib_constant(None)


# ------------------------------------------------------------------ parsing

def test_extract_names_variants():
    # Bare string list.
    assert _extract_names(["Nat.add_comm", "Nat.add_assoc"]) == [
        "Nat.add_comm",
        "Nat.add_assoc",
    ]
    # leansearch.net style: name as list of components.
    assert _extract_names([{"name": ["Nat", "add_comm"]}]) == ["Nat.add_comm"]
    # premise-search.com style: name as plain string + dedupe.
    assert _extract_names(
        [{"name": "Nat.add_comm"}, {"name": "Nat.add_comm"}, {"name": " "}]
    ) == ["Nat.add_comm"]
    # Dict wrapper + junk tolerated.
    assert _extract_names({"results": [{"full_name": "List.map"}]}) == ["List.map"]
    assert _extract_names({"unexpected": 1}) == []
    assert _extract_names(None) == []
    assert _extract_names([42, {}, {"name": ["Nat", "zero"]}]) == ["Nat.zero"]


# ------------------------------------------------------------------ merge

@pytest.mark.asyncio
async def test_search_premises_merges_both_sources():
    calls = []
    r = _retriever(
        post_payload=[{"name": ["Nat", "add_comm"]}, {"name": ["Nat", "add_assoc"]}],
        get_payload=[{"name": "Nat.add_comm"}, {"name": "Nat.mul_comm"}],
        calls=calls,
    )
    out = await r.search_premises("n + m = m + n", top_k=5)
    # leansearch.net first, then premise-search.com; deduped.
    assert out == ["Nat.add_comm", "Nat.add_assoc", "Nat.mul_comm"]
    # Correct endpoints/methods were used.
    methods = {(m, u.split("/")[2]) for m, u, _ in calls}
    assert ("POST", "leansearch.net") in methods
    assert ("GET", "premise-search.com") in methods


@pytest.mark.asyncio
async def test_search_premises_top_k_cap():
    r = _retriever(
        post_payload=[{"name": f"Lemma.a{i}"} for i in range(10)],
        get_payload=[{"name": f"Lemma.b{i}"} for i in range(10)],
    )
    out = await r.search_premises("q", top_k=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_search_premises_empty_query():
    r = _retriever(post_payload=[{"name": "X.y"}], get_payload=[{"name": "X.y"}])
    assert await r.search_premises("") == []
    assert await r.search_premises("   ") == []


# ------------------------------------------------------------------ degrade

@pytest.mark.asyncio
async def test_search_premises_timeout_degrades_to_empty(caplog):
    r = _retriever(post_delay=30.0, get_delay=30.0, timeout_s=0.05)
    with caplog.at_level("WARNING"):
        out = await r.search_premises("Nat.add_comm applies here", top_k=5)
    assert out == []
    assert "retrieval failed" in caplog.text


@pytest.mark.asyncio
async def test_search_premises_one_source_down_other_survives(caplog):
    r = _retriever(
        post_exc=ConnectionError("boom"),
        get_payload=[{"name": "Nat.mul_comm"}],
    )
    with caplog.at_level("WARNING"):
        out = await r.search_premises("comm lemma", top_k=5)
    assert out == ["Nat.mul_comm"]
    assert "leansearch.net retrieval failed" in caplog.text


@pytest.mark.asyncio
async def test_search_premises_never_raises_on_garbage():
    r = _retriever(post_payload=42, get_payload={"weird": object()})
    assert await r.search_premises("q") == []


@pytest.mark.asyncio
async def test_module_level_search_premises_signature():
    # The unified module-level entry exists and is awaitable; with the
    # default (real) endpoints unreachable in tests we only assert it
    # degrades instead of raising by swapping the default retriever.
    import v40_sorry_resolver.engine.retrieval as retrieval_mod

    original = retrieval_mod._default_retriever
    retrieval_mod._default_retriever = _retriever(
        post_payload=[{"name": ["Nat", "add_comm"]}], get_payload=[]
    )
    try:
        assert await search_premises("n + m = m + n", top_k=5) == ["Nat.add_comm"]
    finally:
        retrieval_mod._default_retriever = original


# ------------------------------------------------------------------ wiring

def _critic_router(captured: list):
    critic = FakeLLMClient(Role.CRITIC, script="strategy: use Nat.add_comm")
    router = FakeRouter({Role.CRITIC: critic})
    return router, critic


@pytest.mark.asyncio
async def test_critic_injects_premises_when_mathlib_goal(make_task):
    captured = []
    r = _retriever(
        post_payload=[{"name": ["Nat", "add_comm"]}], get_payload=[], calls=captured
    )
    router, critic = _critic_router(captured)
    agent = CriticAgent(router, retriever=r)
    task = make_task(0, goal_state="n m : Nat ⊢ Nat.add_comm n m")

    lesson = await agent.summarize_lesson(task, "exact Nat.foo", "type mismatch")
    assert lesson == "strategy: use Nat.add_comm"
    assert captured and captured[0][0] == "POST"  # retriever was called
    prompt = critic.calls[0]["prompt"]
    assert "Related Mathlib lemmas" in prompt
    assert "Nat.add_comm" in prompt


@pytest.mark.asyncio
async def test_critic_skips_retrieval_without_mathlib_goal(make_task):
    captured = []
    r = _retriever(post_payload=[{"name": "X.y"}], get_payload=[], calls=captured)
    router, critic = _critic_router(captured)
    agent = CriticAgent(router, retriever=r)
    task = make_task(0, goal_state="⊢ n + 0 = n")

    await agent.summarize_lesson(task, "simp", "failed")
    assert captured == []  # no mathlib constant -> retriever never called
    assert "Related Mathlib lemmas" not in critic.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_critic_retrieval_failure_does_not_block(make_task):
    async def boom(url, payload, timeout):
        raise ConnectionError("down")

    r = PremiseRetriever(http_post=boom, http_get=boom, timeout_s=0.05)
    router, critic = _critic_router([])
    agent = CriticAgent(router, retriever=r)
    task = make_task(0, goal_state="⊢ Nat.add_comm n m")

    lesson = await agent.summarize_lesson(task, "simp", "failed")
    assert lesson == "strategy: use Nat.add_comm"  # flow unaffected
    assert "Related Mathlib lemmas" not in critic.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_critic_no_retriever_unchanged(make_task):
    router, critic = _critic_router([])
    agent = CriticAgent(router)  # default: retrieval disabled
    task = make_task(0, goal_state="⊢ Nat.add_comm n m")
    await agent.summarize_lesson(task, "simp", "failed")
    assert "Related Mathlib lemmas" not in critic.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_axprover_propose_includes_premises(make_task):
    from test_axprover import _strategy

    r = _retriever(
        post_payload=[{"name": ["Nat", "add_comm"]}], get_payload=[]
    )
    prover = FakeLLMClient(Role.PROVER, script="VALID proof")
    router = FakeRouter(
        {Role.PROVER: prover, Role.CRITIC: FakeLLMClient(Role.CRITIC)}
    )
    solver = AxProverV2(router, FakeVerifier(), cfg=None, retriever=r)
    task = make_task(0, goal_state="⊢ Nat.add_comm n m = Nat.add_comm m n")

    result = await solver.solve(task, _strategy())
    assert result.success is True
    prompt = prover.calls[0]["prompt"]
    assert "Related Mathlib lemmas" in prompt
    assert "Nat.add_comm" in prompt
