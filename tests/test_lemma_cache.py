"""Shared lemma cache tests (frontier_atp Top-8 #5; BFS-Prover-V2 shared
Subgoal Cache).

Covers: goal normalization / sha256 keys, put/get roundtrip on the real Cache
persistence layer, pipeline-level hit short-circuit (no LLM call at all), and
store-on-success from the direct phase.
"""

from __future__ import annotations

import pytest

from conftest import FakeLLMClient, FakeRouter, FakeVerifier
from v40_sorry_resolver.cache import Cache
from v40_sorry_resolver.checkpoint import Checkpoint
from v40_sorry_resolver.metrics import MetricsCollector
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine.lemma_cache import LemmaCache
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


def _pipeline(cfg, router, verifier, cache):
    return ResolutionPipeline(
        cfg,
        router,
        verifier,
        cache=cache,
        checkpoint=Checkpoint(str(cfg.work_dir) + "/checkpoint.json"),
        metrics=MetricsCollector(),
        strategy=_strategy(),
    )


# ------------------------------------------------------------------ unit


def test_goal_normalization_keys():
    """Whitespace/formatting differences must not fragment the cache."""
    assert LemmaCache.normalize_goal("  a =   b\n c ") == "a = b c"
    assert LemmaCache.key_for("a  =\n b") == LemmaCache.key_for("a = b")
    assert LemmaCache.key_for("a = b") != LemmaCache.key_for("a = c")


@pytest.mark.asyncio
async def test_put_get_roundtrip(tmp_path):
    cache = Cache(str(tmp_path / "cache.db"))
    lc = LemmaCache(cache)
    assert await lc.get("True") is None
    await lc.put("True", "trivial", meta={"solver": "rfl"})
    hit = await lc.get("  True\n")  # normalized lookup
    assert hit is not None
    assert hit["proof"] == "trivial"
    assert hit["meta"]["solver"] == "rfl"
    await cache.close()
    # Persistence: a fresh Cache on the same db sees the entry.
    cache2 = Cache(str(tmp_path / "cache.db"))
    hit2 = await LemmaCache(cache2).get("True")
    assert hit2 is not None and hit2["proof"] == "trivial"
    await cache2.close()


# ------------------------------------------------------------- pipeline


@pytest.mark.asyncio
async def test_pipeline_cache_hit_short_circuits(make_task, base_config, tmp_path):
    """A cached verified proof short-circuits every phase: no LLM call, the
    mandatory re-verification still runs, solver is reported as lemma_cache."""
    cache = Cache(str(tmp_path / "cache.db"))
    lemma_cache = LemmaCache(cache)
    task = make_task(0)
    await lemma_cache.put(task.goal_state, "VALID cached proof", meta={"solver": "x"})

    prover = FakeLLMClient(Role.PROVER, script="junk that would fail")
    router = FakeRouter(
        {Role.PROVER: prover, Role.CRITIC: FakeLLMClient(Role.CRITIC)}
    )
    verifier = FakeVerifier()
    pipeline = _pipeline(base_config, router, verifier, cache)
    assert pipeline.lemma_cache is not None

    report = await pipeline.run([task], resume=False)

    assert report.solved == 1
    assert prover.calls == []  # no LLM was consulted at all
    # Re-verification ran (no self-signed success, v39 P0-3).
    assert ("task-000", "VALID cached proof") in verifier.calls
    result = report.results[0]
    assert result["solver"] == "lemma_cache"
    assert result["verification_passed"] is True
    await cache.close()


@pytest.mark.asyncio
async def test_pipeline_store_on_success(make_task, base_config, tmp_path):
    """Direct-phase success is written into the shared cache for later tasks."""
    cache = Cache(str(tmp_path / "cache.db"))
    prover = FakeLLMClient(Role.PROVER, script="VALID direct proof")
    router = FakeRouter(
        {Role.PROVER: prover, Role.CRITIC: FakeLLMClient(Role.CRITIC)}
    )
    pipeline = _pipeline(base_config, router, FakeVerifier(), cache)
    task = make_task(0)

    report = await pipeline.run([task], resume=False)

    assert report.solved == 1
    hit = await pipeline.lemma_cache.get(task.goal_state)
    assert hit is not None
    assert "VALID direct proof" in hit["proof"]
    await cache.close()


@pytest.mark.asyncio
async def test_pipeline_cache_disabled_by_config(make_task, base_config, tmp_path):
    cache = Cache(str(tmp_path / "cache.db"))
    base_config.lemma_cache_enabled = False
    router = FakeRouter(
        {
            Role.PROVER: FakeLLMClient(Role.PROVER, script="VALID proof"),
            Role.CRITIC: FakeLLMClient(Role.CRITIC),
        }
    )
    pipeline = _pipeline(base_config, router, FakeVerifier(), cache)
    assert pipeline.lemma_cache is None
    report = await pipeline.run([make_task(0)], resume=False)
    assert report.solved == 1  # normal solving still works
    await cache.close()
