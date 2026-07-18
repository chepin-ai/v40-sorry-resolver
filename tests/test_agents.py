"""Multi-agent layer tests: CriticAgent / OrchestratorLLM / EmergenceLog.

Fake LLM only; no network.
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import FakeLLMClient, FakeRouter
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine.agents import CriticAgent, EmergenceLog, OrchestratorLLM
from v40_sorry_resolver.engine.orchestrator import StrategyConfig


def _base_strategy() -> StrategyConfig:
    return StrategyConfig(
        tactic_search_depth=4,
        tactic_search_width=2,
        agentic_max_iterations=8,
        thinking_max_tokens=2048,
        enable_thinking=True,
        phase_order=["rfl", "direct", "search", "agentic"],
        explorer_share=0.3,
    )


# --------------------------------------------------------------- CriticAgent


@pytest.mark.asyncio
async def test_summarize_lesson_first_line_and_truncation(make_task):
    client = FakeLLMClient(Role.CRITIC, script="strategy: use omega instead\nextra prose")
    critic = CriticAgent(FakeRouter({Role.CRITIC: client}))
    lesson = await critic.summarize_lesson(make_task(0), "junk", "error: failed")
    assert lesson == "strategy: use omega instead"
    assert len(lesson) <= 200

    long_client = FakeLLMClient(Role.CRITIC, script="y" * 500)
    critic2 = CriticAgent(FakeRouter({Role.CRITIC: long_client}))
    lesson2 = await critic2.summarize_lesson(make_task(0), "junk", "err")
    assert len(lesson2) <= 200


@pytest.mark.asyncio
async def test_summarize_lesson_heuristic_fallback(make_task):
    class BoomClient(FakeLLMClient):
        async def generate(self, *a, **k):
            raise RuntimeError("critic down")

    critic = CriticAgent(FakeRouter({Role.CRITIC: BoomClient(Role.CRITIC)}))
    lesson = await critic.summarize_lesson(
        make_task(0), "proof", "type mismatch: expected Nat"
    )
    assert lesson.startswith("type:")
    assert len(lesson) <= 200


@pytest.mark.asyncio
async def test_review_proof_blacklist_short_circuits(make_task):
    client = FakeLLMClient(Role.CRITIC, script='{"approved": true, "reason": "ok"}')
    critic = CriticAgent(FakeRouter({Role.CRITIC: client}))
    approved, reason = await critic.review_proof(make_task(0), "by sorry")
    assert approved is False
    assert "blacklist" in reason
    assert len(client.calls) == 0  # no LLM call needed


@pytest.mark.asyncio
async def test_review_proof_blacklist_strips_comments(make_task):
    """N-12: the Critic judges like the verifier — 'sorry' inside a comment
    must NOT trip the blacklist (word-boundary + comment-stripped)."""
    client = FakeLLMClient(Role.CRITIC, script='{"approved": true, "reason": "ok"}')
    critic = CriticAgent(FakeRouter({Role.CRITIC: client}))
    approved, reason = await critic.review_proof(
        make_task(0), "rfl -- closes the goal, no sorry here"
    )
    assert approved is True
    assert len(client.calls) == 1  # reached the LLM review (not blocked locally)

    # A real sorry outside comments is still blocked without any LLM call.
    client2 = FakeLLMClient(Role.CRITIC, script='{"approved": true}')
    critic2 = CriticAgent(FakeRouter({Role.CRITIC: client2}))
    approved2, _ = await critic2.review_proof(make_task(0), "rfl\n  sorry")
    assert approved2 is False
    assert len(client2.calls) == 0


@pytest.mark.asyncio
async def test_review_proof_json_and_fallback(make_task):
    ok_client = FakeLLMClient(Role.CRITIC, script='{"approved": true, "reason": "clean"}')
    critic = CriticAgent(FakeRouter({Role.CRITIC: ok_client}))
    approved, reason = await critic.review_proof(make_task(0), "rfl")
    assert approved is True and reason == "clean"

    no_client = FakeLLMClient(Role.CRITIC, script='{"approved": false, "reason": "bad"}')
    critic2 = CriticAgent(FakeRouter({Role.CRITIC: no_client}))
    approved2, _ = await critic2.review_proof(make_task(0), "rfl")
    assert approved2 is False

    garbage = FakeLLMClient(Role.CRITIC, script="I cannot decide")
    critic3 = CriticAgent(FakeRouter({Role.CRITIC: garbage}))
    approved3, reason3 = await critic3.review_proof(make_task(0), "rfl")
    assert approved3 is True  # tolerant default
    assert "unparseable" in reason3


# ---------------------------------------------------------- OrchestratorLLM


@pytest.mark.asyncio
async def test_plan_clamps_and_valve_limits(make_task):
    # LLM asks for out-of-range values; clamp + +/-1 valve must apply.
    script = (
        '{"tactic_search_depth": 99, "tactic_search_width": 4, '
        '"agentic_max_iterations": 12, "enable_thinking": false, '
        '"explorer_share": 0.6, "rationale": "go wide"}'
    )
    router = FakeRouter({Role.ORCHESTRATOR: FakeLLMClient(Role.ORCHESTRATOR, script=script)})
    emergence = EmergenceLog(work_dir=None)
    orch = OrchestratorLLM(router, base_strategy=_base_strategy(), emergence=emergence)

    new = await orch.plan({"total": 10, "by_priority": {"P2_MEDIUM": 10}})

    assert new.tactic_search_depth == 5  # clamp 6 then valve +1 from 4
    assert new.tactic_search_width == 3  # valve +1 from 2
    assert new.agentic_max_iterations == 9  # valve +1 from 8
    assert new.enable_thinking is False
    assert abs(new.explorer_share - 0.4) < 1e-9  # valve +0.1 from 0.3
    adj = [e for e in emergence.events if e["kind"] == "strategy_adjustment"]
    assert adj and adj[0]["rationale"] == "go wide"


@pytest.mark.asyncio
async def test_evaluate_parse_failure_keeps_strategy(make_task):
    router = FakeRouter(
        {Role.ORCHESTRATOR: FakeLLMClient(Role.ORCHESTRATOR, script="not json at all")}
    )
    orch = OrchestratorLLM(router, base_strategy=_base_strategy())
    strategy = _base_strategy()
    result = await orch.evaluate_and_adjust({"tasks_total": 5}, strategy)
    assert result is strategy  # unchanged on parse failure


@pytest.mark.asyncio
async def test_evaluate_valid_json_adjusts_within_valve(make_task):
    script = (
        '{"tactic_search_depth": 2, "tactic_search_width": 1, '
        '"agentic_max_iterations": 3, "enable_thinking": true, '
        '"explorer_share": 0.0, "rationale": "tighten"}'
    )
    router = FakeRouter({Role.ORCHESTRATOR: FakeLLMClient(Role.ORCHESTRATOR, script=script)})
    orch = OrchestratorLLM(router, base_strategy=_base_strategy())
    new = await orch.evaluate_and_adjust({}, _base_strategy())
    assert new.tactic_search_depth == 3  # valve -1 from 4 (clamp floor 2)
    assert new.tactic_search_width == 1
    assert new.agentic_max_iterations == 7  # valve -1 from 8
    assert abs(new.explorer_share - 0.2) < 1e-9  # valve -0.1 from 0.3
    assert new.phase_order == ["rfl", "direct", "search", "agentic"]


@pytest.mark.asyncio
async def test_evaluate_llm_error_keeps_strategy(make_task):
    class BoomClient(FakeLLMClient):
        async def generate(self, *a, **k):
            raise RuntimeError("orchestrator down")

    router = FakeRouter({Role.ORCHESTRATOR: BoomClient(Role.ORCHESTRATOR)})
    orch = OrchestratorLLM(router, base_strategy=_base_strategy())
    strategy = _base_strategy()
    result = await orch.evaluate_and_adjust({}, strategy)
    assert result.to_dict() == strategy.to_dict()


# ------------------------------------------------------------- EmergenceLog


def test_emergence_log_persists_jsonl(tmp_path):
    log = EmergenceLog(work_dir=str(tmp_path))
    log.strategy_adjustment({"depth": 4}, {"depth": 5}, "test adjust")
    log.role_contribution("PROVER", "llm_direct", "t1", True)
    log.role_contribution("PROVER", "llm_direct", "t2", False)
    log.cross_eval("t1", agree=True)
    log.cross_eval("t2", agree=False)

    assert abs(log.agreement_rate() - 0.5) < 1e-9
    summary = log.summary()
    assert summary["strategy_adjustments"] == 1
    assert summary["role_contributions"]["PROVER"] == {"success": 1, "total": 2}

    path = summary["path"]
    assert path and os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    assert len(lines) == 5
    assert {l["kind"] for l in lines} == {
        "strategy_adjustment",
        "role_contribution",
        "cross_eval",
    }
