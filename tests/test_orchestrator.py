"""Orchestrator tests: worker-pool concurrency, budgets, escalation, resume.

All with fake verifier (proof must contain "VALID") + fake LLM; no network.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from conftest import FakeLLMClient, FakeRouter, FakeVerifier
from v40_sorry_resolver.models import ProofStatus
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.checkpoint import Checkpoint
from v40_sorry_resolver.metrics import MetricsCollector
from v40_sorry_resolver.engine.orchestrator import ResolutionPipeline, StrategyConfig


def _strategy() -> StrategyConfig:
    return StrategyConfig(
        tactic_search_depth=2,
        tactic_search_width=1,
        agentic_max_iterations=2,
        thinking_max_tokens=1024,
        enable_thinking=False,
        phase_order=["rfl", "direct", "search", "agentic"],
        explorer_share=0.3,
    )


def _pipeline(cfg, router, verifier, checkpoint=None, metrics=None, strategy=None):
    return ResolutionPipeline(
        cfg,
        router,
        verifier,
        cache=None,
        checkpoint=checkpoint,
        metrics=metrics or MetricsCollector(),
        strategy=strategy or _strategy(),
    )


@pytest.mark.asyncio
async def test_parallel_all_solved_and_faster_than_serial(make_task, base_config, tmp_path):
    """10 tasks, fake verifier (VALID) + fake LLM (VALID proof):
    all SOLVED via 4 workers, wall time < serial(1 worker) / 2, and the
    verifier observes real in-flight concurrency."""
    tasks_a = [make_task(i) for i in range(10)]
    tasks_b = [make_task(i) for i in range(10)]

    async def run_with(workers, tasks, tag):
        cfg = base_config
        cfg.num_workers = workers
        cfg.work_dir = str(tmp_path / tag)
        verifier = FakeVerifier(delay=0.02)
        prover = FakeLLMClient(Role.PROVER, script="VALID direct proof", delay=0.02)
        router = FakeRouter({Role.PROVER: prover, Role.CRITIC: FakeLLMClient(Role.CRITIC)})
        pipeline = _pipeline(cfg, router, verifier)
        t0 = time.monotonic()
        report = await pipeline.run(tasks, resume=False)
        return time.monotonic() - t0, report, verifier

    t_serial, rep1, ver1 = await run_with(1, tasks_a, "w1")
    t_par, rep4, ver4 = await run_with(4, tasks_b, "w4")

    # Correctness: every task solved, verified, accounted.
    assert rep4.solved == 10
    assert rep4.processed == 10
    assert rep4.verify_pass_rate == 1.0
    assert rep4.counts_by_status.get("SOLVED_LLM_DIRECT") == 10
    assert all(r["verification_passed"] for r in rep4.results)
    assert all(t.status == ProofStatus.SOLVED_LLM_DIRECT for t in tasks_b)

    # Concurrency: genuinely parallel verifier traffic.
    assert ver4.max_inflight >= 3, f"max_inflight={ver4.max_inflight}"
    assert ver1.max_inflight == 1

    # Wall time: 4 workers must be at least 2x faster than serial.
    assert t_par < t_serial / 2, f"serial={t_serial:.3f}s parallel={t_par:.3f}s"


@pytest.mark.asyncio
async def test_per_task_time_budget_truncates(make_task, base_config):
    cfg = base_config
    cfg.per_task_time_budget_s = 0.25
    cfg.num_workers = 2
    verifier = FakeVerifier(delay=0.02)
    slow_prover = FakeLLMClient(Role.PROVER, script="VALID proof", delay=2.0)
    router = FakeRouter({Role.PROVER: slow_prover, Role.CRITIC: FakeLLMClient(Role.CRITIC)})
    pipeline = _pipeline(cfg, router, verifier)

    t0 = time.monotonic()
    report = await pipeline.run([make_task(0)], resume=False)
    elapsed = time.monotonic() - t0

    assert report.counts_by_status.get("BUDGET_EXHAUSTED") == 1
    assert report.solved == 0
    assert elapsed < 2.0, f"budget not enforced, took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_per_task_token_budget(make_task, base_config):
    cfg = base_config
    cfg.per_task_token_budget = 1  # direct phase alone uses 15 fake tokens
    cfg.num_workers = 1
    verifier = FakeVerifier()
    prover = FakeLLMClient(Role.PROVER, script="junk")  # never verifies
    router = FakeRouter({Role.PROVER: prover, Role.CRITIC: FakeLLMClient(Role.CRITIC)})
    pipeline = _pipeline(cfg, router, verifier)
    report = await pipeline.run([make_task(0)], resume=False)
    assert report.counts_by_status.get("BUDGET_EXHAUSTED") == 1


@pytest.mark.asyncio
async def test_escalation_accumulates_across_runs(make_task, base_config, tmp_path):
    """escalation_level persists via checkpoint; >= threshold -> MARKED_AXIOM."""
    cfg = base_config
    cfg.escalation_threshold = 2
    cfg.num_workers = 2
    cp_path = str(tmp_path / "cp.json")

    # Run 1: task always fails -> escalation 1, FAILED_ALL.
    verifier1 = FakeVerifier()
    router1 = FakeRouter(
        {Role.PROVER: FakeLLMClient(Role.PROVER, script="junk proof"),
         Role.CRITIC: FakeLLMClient(Role.CRITIC, script="strategy: retry")}
    )
    p1 = _pipeline(cfg, router1, verifier1, checkpoint=Checkpoint(cp_path))
    rep1 = await p1.run([make_task(0)], resume=True)
    assert rep1.counts_by_status.get("FAILED_ALL") == 1

    with open(cp_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["tasks"][0]["escalation_level"] == 1

    # Run 2 (fresh task object): resume merges escalation -> fails again ->
    # escalation 2 >= threshold -> MARKED_AXIOM.
    verifier2 = FakeVerifier()
    router2 = FakeRouter(
        {Role.PROVER: FakeLLMClient(Role.PROVER, script="junk proof"),
         Role.CRITIC: FakeLLMClient(Role.CRITIC, script="strategy: retry")}
    )
    p2 = _pipeline(cfg, router2, verifier2, checkpoint=Checkpoint(cp_path))
    task2 = make_task(0)
    assert task2.escalation_level == 0
    rep2 = await p2.run([task2], resume=True)
    assert task2.escalation_level >= 2
    assert task2.status == ProofStatus.MARKED_AXIOM
    assert rep2.counts_by_status.get("MARKED_AXIOM") == 1


@pytest.mark.asyncio
async def test_axiom_quota_truncates(make_task, base_config, tmp_path):
    """With axiom_quota=0, escalation beyond threshold stays FAILED_ALL."""
    cfg = base_config
    cfg.escalation_threshold = 1
    cfg.axiom_quota = 0
    cfg.num_workers = 1
    router = FakeRouter(
        {Role.PROVER: FakeLLMClient(Role.PROVER, script="junk"),
         Role.CRITIC: FakeLLMClient(Role.CRITIC, script="strategy: retry")}
    )
    pipeline = _pipeline(cfg, router, FakeVerifier(),
                         checkpoint=Checkpoint(str(tmp_path / "cp.json")))
    task = make_task(0)
    report = await pipeline.run([task], resume=False)
    assert task.escalation_level >= 1
    assert task.status == ProofStatus.FAILED_ALL  # quota exhausted, not marked
    assert report.counts_by_status.get("FAILED_ALL") == 1
    assert "MARKED_AXIOM" not in report.counts_by_status


@pytest.mark.asyncio
async def test_resume_skips_solved_and_merges_results(make_task, base_config, tmp_path):
    cfg = base_config
    cfg.escalation_threshold = 2
    cfg.num_workers = 2
    cp_path = str(tmp_path / "cp.json")

    def router_factory():
        def script(prompt, system_prompt, role, idx):
            return "VALID proof" if "theorem_0" in prompt else "junk"

        return FakeRouter(
            {Role.PROVER: FakeLLMClient(Role.PROVER, script=script),
             Role.CRITIC: FakeLLMClient(Role.CRITIC, script="strategy: retry")}
        )

    # Run 1: task-000 solved, task-001 fails.
    p1 = _pipeline(cfg, router_factory(), FakeVerifier(), checkpoint=Checkpoint(cp_path))
    rep1 = await p1.run([make_task(0), make_task(1)], resume=True)
    assert rep1.counts_by_status.get("SOLVED_LLM_DIRECT") == 1
    assert rep1.counts_by_status.get("FAILED_ALL") == 1

    # Run 2: solved task skipped (never re-processed), prev results merged.
    p2 = _pipeline(cfg, router_factory(), FakeVerifier(), checkpoint=Checkpoint(cp_path))
    t0, t1 = make_task(0), make_task(1)
    rep2 = await p2.run([t0, t1], resume=True)

    assert t0.status == ProofStatus.SOLVED_LLM_DIRECT  # restored from checkpoint
    assert rep2.processed == 1  # only the failed task re-entered the chain
    # Prev solved result merged into the new report.
    assert rep2.counts_by_status.get("SOLVED_LLM_DIRECT") == 1
    # Failed task escalated to axiom on second failure (threshold=2).
    assert t1.status == ProofStatus.MARKED_AXIOM


@pytest.mark.asyncio
async def test_shutdown_leaves_tasks_untouched(make_task, base_config):
    cfg = base_config
    pipeline = _pipeline(
        cfg,
        FakeRouter({Role.PROVER: FakeLLMClient(Role.PROVER)}),
        FakeVerifier(),
    )
    pipeline.request_shutdown()
    tasks = [make_task(i) for i in range(5)]
    report = await pipeline.run(tasks, resume=False)
    assert report.processed == 0
    assert report.solved == 0
    assert sorted(report.untouched) == sorted(t.id for t in tasks)


def test_strategy_degraded():
    s = StrategyConfig(
        tactic_search_depth=4,
        tactic_search_width=2,
        agentic_max_iterations=8,
        thinking_max_tokens=2048,
        enable_thinking=True,
        phase_order=["rfl", "direct", "search", "agentic"],
        explorer_share=0.3,
    )
    d = s.degraded()
    assert d.tactic_search_depth == 3
    assert d.tactic_search_width == 1
    assert d.agentic_max_iterations == 4
    assert d.enable_thinking is False
    assert d.explorer_share == 0.3
    assert d.phase_order == s.phase_order


def test_strategy_from_config(base_config):
    s = StrategyConfig.from_config(base_config)
    assert s.tactic_search_depth == base_config.tactic_search_depth
    assert s.agentic_max_iterations == base_config.agentic_max_iterations
    assert s.phase_order == ["rfl", "direct", "search", "agentic"]


@pytest.mark.asyncio
async def test_metrics_recorded_and_report_by_provider(make_task, base_config):
    """N-2 regression: task metrics must actually land in the collector
    (record_task is awaited with duration_s=), and RunReport.counts_by_provider
    is derived from the collector's per-provider llm series."""
    from v40_sorry_resolver.metrics import MetricsCollector

    metrics = MetricsCollector()
    # Seed one provider call (clients record into this same collector in real
    # runs); the report must surface it as counts_by_provider.
    await metrics.record_llm_call(
        provider="deepseek_b", model="deepseek-chat", latency_s=0.1,
        prompt_tokens=3, completion_tokens=2,
    )
    cfg = base_config
    cfg.num_workers = 2
    router = FakeRouter(
        {Role.PROVER: FakeLLMClient(Role.PROVER, script="VALID proof"),
         Role.CRITIC: FakeLLMClient(Role.CRITIC)}
    )
    pipeline = _pipeline(cfg, router, FakeVerifier(), metrics=metrics)
    report = await pipeline.run([make_task(0), make_task(1)], resume=False)

    snap = await metrics.snapshot()
    assert snap["tasks"]["processed"] == 2, snap["tasks"]
    assert snap["tasks"]["succeeded"] == 2
    assert snap["tasks"]["by_status"].get("SOLVED_LLM_DIRECT") == 2
    assert report.counts_by_provider == {"deepseek_b": 1}


@pytest.mark.asyncio
async def test_mock_results_marked_unverified(make_task, base_config):
    """N-5: with the mock verifier every result is labeled unverified and the
    rendered summary carries the [UNVERIFIED] mark (SPEC 0.3/3.8)."""
    from v40_sorry_resolver.verify.mock import MockVerifier

    cfg = base_config
    cfg.num_workers = 2
    verifier = MockVerifier(cfg)
    router = FakeRouter(
        {Role.PROVER: FakeLLMClient(Role.PROVER, script="VALID proof"),
         Role.CRITIC: FakeLLMClient(Role.CRITIC)}
    )
    pipeline = _pipeline(cfg, router, verifier)
    report = await pipeline.run([make_task(0), make_task(1)], resume=False)

    assert report.solved == 2  # mock 'VALID' marker passes
    assert all(r["unverified"] for r in report.results)
    assert "[UNVERIFIED]" in report.render_summary()


@pytest.mark.asyncio
async def test_real_verifier_results_not_unverified(make_task, base_config):
    """Counterpart of N-5: a non-mock verifier must NOT set unverified."""
    cfg = base_config
    cfg.num_workers = 2
    router = FakeRouter(
        {Role.PROVER: FakeLLMClient(Role.PROVER, script="VALID proof"),
         Role.CRITIC: FakeLLMClient(Role.CRITIC)}
    )
    pipeline = _pipeline(cfg, router, FakeVerifier())
    report = await pipeline.run([make_task(0)], resume=False)
    assert report.solved == 1
    assert not any(r["unverified"] for r in report.results)
    assert "[UNVERIFIED]" not in report.render_summary()


@pytest.mark.asyncio
async def test_checkpoint_metrics_is_json_snapshot(make_task, base_config, tmp_path):
    """N-7 regression: the checkpoint's metrics field must be a JSON dict
    snapshot, not a '<MetricsCollector object at 0x...>' repr."""
    cfg = base_config
    cfg.num_workers = 2
    cp_path = str(tmp_path / "cp.json")
    router = FakeRouter(
        {Role.PROVER: FakeLLMClient(Role.PROVER, script="VALID proof"),
         Role.CRITIC: FakeLLMClient(Role.CRITIC)}
    )
    pipeline = _pipeline(cfg, router, FakeVerifier(), checkpoint=Checkpoint(cp_path))
    await pipeline.run([make_task(0)], resume=False)

    with open(cp_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert isinstance(saved["metrics"], dict)
    assert "MetricsCollector object" not in json.dumps(saved["metrics"])
    assert saved["metrics"]["tasks"]["processed"] == 1


# ======================================================================
# Frontier: cost-aware three-tier budgets (frontier_atp Top-8 #8)
# ======================================================================

from v40_sorry_resolver.config import BudgetTier


def test_strategy_config_for_tier_presets():
    light = StrategyConfig.for_tier(BudgetTier.LIGHT)
    assert (light.tactic_search_depth, light.tactic_search_width) == (2, 1)
    assert light.agentic_max_iterations == 3
    assert light.enable_thinking is False

    standard = StrategyConfig.for_tier(BudgetTier.STANDARD)
    # STANDARD = current defaults (SPEC 3.2/from_config).
    assert (standard.tactic_search_depth, standard.tactic_search_width) == (4, 2)
    assert standard.agentic_max_iterations == 8
    assert standard.enable_thinking is True

    deep = StrategyConfig.for_tier(BudgetTier.DEEP)
    assert (deep.tactic_search_depth, deep.tactic_search_width) == (5, 3)
    assert deep.agentic_max_iterations == 10
    assert deep.enable_thinking is True


def _spy_on_agentic(pipeline):
    recorded = []
    orig_solve = pipeline.axprover.solve

    async def spy(task, strategy):
        recorded.append(
            (
                task.id,
                strategy.tactic_search_depth,
                strategy.agentic_max_iterations,
                strategy.enable_thinking,
            )
        )
        return await orig_solve(task, strategy)

    pipeline.axprover.solve = spy
    return recorded


@pytest.mark.asyncio
async def test_pipeline_picks_tier_strategy_per_task(make_task, base_config):
    """LIGHT task (predicted_steps<=3) gets the LIGHT preset; DEEP task
    (predicted_steps>8) gets the DEEP preset."""
    base_config.num_workers = 2
    # All roles produce junk so every phase fails and reaches agentic (the
    # DEEP width-3 beam also consults EXPLORER; pin it to junk explicitly).
    router = FakeRouter(
        {
            Role.PROVER: FakeLLMClient(Role.PROVER, script="junk"),
            Role.EXPLORER: FakeLLMClient(Role.EXPLORER, script="junk"),
            Role.CRITIC: FakeLLMClient(Role.CRITIC),
        }
    )
    pipeline = _pipeline(base_config, router, FakeVerifier())
    recorded = _spy_on_agentic(pipeline)

    tasks = [
        make_task(0, predicted_steps=2),   # LIGHT
        make_task(1, predicted_steps=12),  # DEEP
    ]
    report = await pipeline.run(tasks, resume=False)
    assert report.processed == 2
    assert len(recorded) == 2
    by_id = dict((r[0], r[1:]) for r in recorded)
    # LIGHT preset: depth2 / iter3 / no thinking.
    assert by_id["task-000"] == (2, 3, False)
    # DEEP preset: depth5 / iter10 / thinking on.
    assert by_id["task-001"] == (5, 10, True)


@pytest.mark.asyncio
async def test_dynamic_adjustment_takes_priority_over_tier(make_task, base_config):
    """When OrchestratorLLM really changes the strategy, the adjusted strategy
    wins over the per-task tier preset."""
    base_config.num_workers = 1
    adjust_json = (
        '{"tactic_search_depth": 6, "tactic_search_width": 1, '
        '"agentic_max_iterations": 5, "enable_thinking": true, '
        '"explorer_share": 0.2, "rationale": "go deeper"}'
    )
    router = FakeRouter(
        {
            Role.ORCHESTRATOR: FakeLLMClient(Role.ORCHESTRATOR, script=adjust_json),
            Role.PROVER: FakeLLMClient(Role.PROVER, script="junk"),
            Role.EXPLORER: FakeLLMClient(Role.EXPLORER, script="junk"),
            Role.CRITIC: FakeLLMClient(Role.CRITIC),
        }
    )
    pipeline = _pipeline(base_config, router, FakeVerifier())
    recorded = _spy_on_agentic(pipeline)

    # LIGHT task: tier preset would be depth2/thinking-off, but the dynamic
    # adjustment (valve-limited: depth 2->3, thinking on) must win.
    await pipeline.run([make_task(0, predicted_steps=2)], resume=False)
    assert pipeline._strategy_adjusted is True
    assert len(recorded) == 1
    _, depth, _, thinking = recorded[0]
    assert depth == 3          # adjusted (2 + valve 1), NOT the LIGHT preset 2
    assert thinking is True    # adjusted, NOT the LIGHT preset off
