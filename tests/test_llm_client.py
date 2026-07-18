"""Tests for v40_sorry_resolver.llm.client + llm.router (SPEC 3.3/3.4).

All HTTP behavior is faked via unittest.mock / monkeypatch; no real network
calls are made.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from v40_sorry_resolver.config import LLMProviderConfig, V40Config
from v40_sorry_resolver.llm import client as client_mod
from v40_sorry_resolver.llm.client import (
    BREAKER_4XX_THRESHOLD,
    AsyncLLMClient,
)
from v40_sorry_resolver.llm.router import (
    ROLE_TO_PROVIDER,
    MultiLLMRouter,
    Role,
)
from v40_sorry_resolver.metrics import get_global_metrics, reset_global_metrics

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------
def make_completion(text="proof", prompt_tokens=11, completion_tokens=7):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


def make_status_error(status: int):
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    if status == 429:
        return openai.RateLimitError("rate limited", response=resp, body=None)
    if status >= 500:
        return openai.InternalServerError("server error", response=resp, body=None)
    if status == 404:
        return openai.NotFoundError("model not found", response=resp, body=None)
    if status == 401:
        return openai.AuthenticationError("bad key", response=resp, body=None)
    return openai.BadRequestError("bad request", response=resp, body=None)


def make_timeout_error():
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    return openai.APITimeoutError(request=req)


class FakeAsyncOpenAI:
    """Stands in for openai.AsyncOpenAI (constructor kwargs recorded)."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = MagicMock()
        self.chat.completions.create = AsyncMock(return_value=make_completion())
        self.models = MagicMock()
        if "fail" in str(kwargs.get("base_url", "")):
            # Health checks probe chat completions first (BUG-3), so a dead
            # provider must fail the generation probe, not just /models.
            self.chat.completions.create = AsyncMock(
                side_effect=make_status_error(401)
            )
            self.models.list = AsyncMock(side_effect=make_status_error(401))
        else:
            self.models.list = AsyncMock(return_value=SimpleNamespace(data=[]))
        self.close = AsyncMock()
        FakeAsyncOpenAI.instances.append(self)


@pytest.fixture()
def fake_openai(monkeypatch):
    FakeAsyncOpenAI.instances = []
    monkeypatch.setattr(client_mod, "AsyncOpenAI", FakeAsyncOpenAI)
    # Zero-out retry backoff so tests stay fast (still exercises the logic).
    monkeypatch.setattr(
        client_mod, "RETRY_BACKOFF_S", (0.0, 0.0, 0.0)
    )
    return FakeAsyncOpenAI


@pytest.fixture(autouse=True)
def fresh_metrics():
    reset_global_metrics()
    yield
    reset_global_metrics()


def make_cfg(**overrides):
    kwargs = dict(
        name="testprov",
        base_url="https://api.test/v1",
        api_key="test-key",
        model="test-model",
        timeout_s=10.0,
        thinking_timeout_s=300.0,
    )
    kwargs.update(overrides)
    return LLMProviderConfig(**kwargs)


def make_client(**cfg_overrides):
    return AsyncLLMClient(make_cfg(**cfg_overrides))


# ----------------------------------------------------------------------
# client construction / success path
# ----------------------------------------------------------------------
class TestConstruction:
    async def test_sdk_configured_per_spec(self, fake_openai):
        make_client()
        kwargs = FakeAsyncOpenAI.instances[0].kwargs
        assert kwargs["base_url"] == "https://api.test/v1"
        assert kwargs["api_key"] == "test-key"
        assert kwargs["max_retries"] == 0
        assert kwargs["timeout"] == 10.0

    async def test_success_response_fields(self, fake_openai):
        client = make_client()
        resp = await client.generate("prove it", system_prompt="sys")
        assert resp.error is None
        assert resp.text == "proof"
        assert resp.model == "test-model"
        assert resp.provider == "testprov"
        assert resp.prompt_tokens == 11
        assert resp.completion_tokens == 7
        assert resp.from_cache is False
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        call = create.call_args.kwargs
        assert call["model"] == "test-model"
        assert call["messages"][0] == {"role": "system", "content": "sys"}
        assert call["messages"][1] == {"role": "user", "content": "prove it"}
        await client.close()

    async def test_disabled_provider_short_circuits(self, fake_openai):
        client = make_client(enabled=False)
        resp = await client.generate("x")
        assert resp.error is not None
        assert "provider_disabled" in resp.error
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        assert create.call_count == 0

    async def test_metrics_recorded(self, fake_openai):
        client = make_client()
        await client.generate("a")
        await client.generate("b")
        snap = await get_global_metrics().snapshot()
        entry = snap["llm"]["testprov"]
        assert entry["calls"] == 2
        assert entry["errors"] == 0
        assert entry["prompt_tokens"] == 22
        assert entry["completion_tokens"] == 14


class TestTemperature:
    async def test_none_falls_back_to_default(self, fake_openai):
        client = make_client()
        await client.generate("x", temperature=None)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["temperature"] == 0.3

    async def test_zero_temperature_not_swallowed(self, fake_openai):
        # v39 bug: `temperature or default` turned 0.0 into the default.
        client = make_client()
        await client.generate("x", temperature=0.0)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["temperature"] == 0.0

    async def test_explicit_temperature_passed(self, fake_openai):
        client = make_client()
        await client.generate("x", temperature=0.9)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["temperature"] == 0.9


class TestThinking:
    async def test_thinking_uses_thinking_timeout(self, fake_openai):
        client = make_client()
        await client.generate("x", thinking=True)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["timeout"] == 300.0

    async def test_thinking_timeout_floor_240(self, fake_openai):
        client = make_client(thinking_timeout_s=100.0)
        await client.generate("x", thinking=True)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["timeout"] == 240.0

    async def test_thinking_clamps_max_tokens(self, fake_openai):
        client = make_client()
        client.thinking_max_tokens = 2048
        await client.generate("x", thinking=True, max_tokens=8192)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["max_tokens"] == 2048

    async def test_non_thinking_normal_timeout(self, fake_openai):
        client = make_client()
        await client.generate("x", thinking=False)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["timeout"] == 10.0


class TestRetryAndBreaker:
    async def test_429_retried_then_success(self, fake_openai):
        create = FakeAsyncOpenAI.instances
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = [make_status_error(429), make_completion()]
        resp = await client.generate("x")
        assert resp.error is None
        assert resp.text == "proof"
        assert create.call_count == 2

    async def test_5xx_retries_exhausted(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(500)
        resp = await client.generate("x")
        assert resp.error is not None
        assert "retryable" in resp.error
        # 1 initial + 3 retries (SPEC: at most 3 retries).
        assert create.call_count == 4

    async def test_timeout_classified_retryable(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = [make_timeout_error(), make_completion()]
        resp = await client.generate("x")
        assert resp.error is None
        assert create.call_count == 2

    async def test_4xx_fails_fast_no_retry(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(404)
        resp = await client.generate("x")
        assert resp.error is not None
        assert "http_404" in resp.error
        assert create.call_count == 1

    async def test_breaker_opens_after_3_consecutive_4xx(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(404)
        for _ in range(BREAKER_4XX_THRESHOLD):
            await client.generate("x")
        assert client.stats()["breaker_state"] == "open"
        calls_before = create.call_count
        resp = await client.generate("x")
        assert "breaker_open" in resp.error
        assert create.call_count == calls_before  # no new API call

    async def test_health_check_success_resets_breaker(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(404)
        for _ in range(BREAKER_4XX_THRESHOLD):
            await client.generate("x")
        assert client.stats()["breaker_state"] == "open"
        create.side_effect = None  # provider recovers
        create.return_value = make_completion()
        assert await client.health_check() is True
        assert client.stats()["breaker_state"] == "closed"
        resp = await client.generate("x")
        assert resp.error is None

    async def test_success_resets_consecutive_4xx(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = [
            make_status_error(400),
            make_status_error(400),
            make_completion(),
            make_status_error(400),
        ]
        await client.generate("a")
        await client.generate("b")
        await client.generate("c")  # success resets the counter
        await client.generate("d")
        assert client.stats()["breaker_state"] == "closed"
        assert client.stats()["consecutive_4xx"] == 1

    async def test_health_check_4xx_returns_false(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(401)
        assert await client.health_check() is False

    async def test_health_check_models_ok_but_chat_401_is_unhealthy(
        self, fake_openai
    ):
        """BUG-3 regression (LongCat): /models answers 200 while every chat
        completion 401s — the provider must be judged UNHEALTHY."""
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(401)
        # /models succeeds (the false-positive the old logic fell for).
        assert await client.health_check() is False

    async def test_health_check_chat_probe_success_no_models_needed(
        self, fake_openai
    ):
        client = make_client()
        models_list = FakeAsyncOpenAI.instances[0].models.list
        models_list.side_effect = AssertionError("/models must not be called")
        assert await client.health_check() is True

    async def test_health_check_chat_404_falls_back_to_models(self, fake_openai):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(404)
        # /models succeeds -> provider without a chat route is still alive.
        assert await client.health_check() is True

    async def test_health_check_chat_404_and_models_down_is_unhealthy(
        self, fake_openai
    ):
        client = make_client()
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(404)
        FakeAsyncOpenAI.instances[0].models.list.side_effect = make_status_error(
            401
        )
        assert await client.health_check() is False


class TestCacheIntegration:
    async def test_cache_hit_avoids_api_call(self, fake_openai, tmp_path):
        from v40_sorry_resolver.cache import Cache

        cache = Cache(str(tmp_path / "c.db"))
        client = AsyncLLMClient(make_cfg(), cache=cache)
        r1 = await client.generate("p", cache_key="lemma1")
        assert r1.from_cache is False
        r2 = await client.generate("p", cache_key="lemma1")
        assert r2.from_cache is True
        assert r2.text == "proof"
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        assert create.call_count == 1
        await client.close()
        await cache.close()

    async def test_cache_key_depends_on_temperature(self, fake_openai, tmp_path):
        from v40_sorry_resolver.cache import Cache

        cache = Cache(str(tmp_path / "c.db"))
        client = AsyncLLMClient(make_cfg(), cache=cache)
        await client.generate("p", temperature=0.1, cache_key="k")
        await client.generate("p", temperature=0.2, cache_key="k")
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        assert create.call_count == 2
        await client.close()
        await cache.close()

    async def test_no_cache_key_no_caching(self, fake_openai, tmp_path):
        from v40_sorry_resolver.cache import Cache

        cache = Cache(str(tmp_path / "c.db"))
        client = AsyncLLMClient(make_cfg(), cache=cache)
        await client.generate("p")
        await client.generate("p")
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        assert create.call_count == 2
        await client.close()
        await cache.close()


class TestClose:
    async def test_close_reentrant(self, fake_openai):
        client = make_client()
        await client.close()
        await client.close()
        assert FakeAsyncOpenAI.instances[0].close.call_count == 1


class TestReasonerRouting:
    async def test_thinking_uses_reasoner_model_when_wired(self, fake_openai):
        client = make_client()
        client.reasoner_model = "deepseek-reasoner"
        await client.generate("x", thinking=True)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["model"] == "deepseek-reasoner"
        assert call.kwargs["timeout"] == 300.0

    async def test_non_thinking_keeps_chat_model(self, fake_openai):
        client = make_client()
        client.reasoner_model = "deepseek-reasoner"
        await client.generate("x", thinking=False)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["model"] == "test-model"

    async def test_no_reasoner_wired_keeps_chat_model(self, fake_openai):
        client = make_client()
        await client.generate("x", thinking=True)
        call = FakeAsyncOpenAI.instances[0].chat.completions.create.call_args
        assert call.kwargs["model"] == "test-model"

    async def test_router_wires_reasoner_for_deepseek_only(
        self, fake_openai, tmp_path
    ):
        cfg = router_config(tmp_path)
        cfg.deepseek_reasoner_model = "deepseek-reasoner"
        router = MultiLLMRouter.from_config(cfg)
        assert (
            router.client(Role.PROVER).reasoner_model == "deepseek-reasoner"
        )
        assert (
            router.client(Role.ORCHESTRATOR).reasoner_model
            == "deepseek-reasoner"
        )
        assert router.client(Role.CRITIC).reasoner_model is None
        assert router.client(Role.EXPLORER).reasoner_model is None
        await router.close()


class TestMetricsInjection:
    async def test_injected_collector_receives_calls(self, fake_openai):
        from v40_sorry_resolver.metrics import MetricsCollector

        collector = MetricsCollector()
        client = AsyncLLMClient(make_cfg(), metrics=collector)
        await client.generate("a")
        await client.generate("b")
        snap = await collector.snapshot()
        assert snap["llm"]["testprov"]["calls"] == 2
        assert snap["by_provider"] == {"testprov": 2}
        # The global collector stays untouched (no split accounting).
        global_snap = await get_global_metrics().snapshot()
        assert global_snap["llm"] == {}

    async def test_router_from_config_shares_collector(self, fake_openai, tmp_path):
        from v40_sorry_resolver.metrics import MetricsCollector

        collector = MetricsCollector()
        router = MultiLLMRouter.from_config(router_config(tmp_path), metrics=collector)
        await router.client(Role.PROVER).generate("prove")
        await router.client(Role.CRITIC).generate("review")
        snap = await collector.snapshot()
        assert snap["llm"]["deepseek_b"]["calls"] == 1
        assert snap["llm"]["kimi"]["calls"] == 1
        assert snap["by_provider"] == {"deepseek_b": 1, "kimi": 1}
        await router.close()

    async def test_default_falls_back_to_global_collector(self, fake_openai):
        client = make_client()
        await client.generate("a")
        snap = await get_global_metrics().snapshot()
        assert snap["llm"]["testprov"]["calls"] == 1


# ----------------------------------------------------------------------
# router
# ----------------------------------------------------------------------
def router_config(tmp_path, enabled=("deepseek_a", "deepseek_b", "kimi", "longcat")):
    cfg = V40Config()
    cfg.lean_project_paths = [str(tmp_path)]
    cfg.providers = {}
    for name in ("deepseek_a", "deepseek_b", "kimi", "longcat"):
        cfg.providers[name] = LLMProviderConfig(
            name=name,
            base_url=f"https://api.test/{name}/v1",
            api_key="k" if name in enabled else "",
            model=f"model-{name}",
            enabled=name in enabled,
        )
    return cfg


class TestRouter:
    async def test_role_to_provider_contract(self):
        assert ROLE_TO_PROVIDER == {
            "ORCHESTRATOR": "deepseek_a",
            "PROVER": "deepseek_b",
            "CRITIC": "kimi",
            "EXPLORER": "longcat",
        }
        assert {r.name for r in Role} == set(ROLE_TO_PROVIDER)

    async def test_client_per_role(self, fake_openai, tmp_path):
        router = MultiLLMRouter.from_config(router_config(tmp_path))
        assert router.client(Role.ORCHESTRATOR).cfg.name == "deepseek_a"
        assert router.client(Role.PROVER).cfg.name == "deepseek_b"
        assert router.client(Role.CRITIC).cfg.name == "kimi"
        assert router.client(Role.EXPLORER).cfg.name == "longcat"
        await router.close()

    async def test_disabled_roles_not_constructed(self, fake_openai, tmp_path):
        router = MultiLLMRouter.from_config(
            router_config(tmp_path, enabled=("kimi",))
        )
        assert len(FakeAsyncOpenAI.instances) == 1
        await router.close()

    async def test_fallback_chain_order(self, fake_openai, tmp_path, caplog):
        # Only longcat enabled: every role must fall back to EXPLORER's
        # provider (chain CRITIC -> PROVER -> EXPLORER -> ORCHESTRATOR).
        import logging

        router = MultiLLMRouter.from_config(
            router_config(tmp_path, enabled=("longcat",))
        )
        with caplog.at_level(logging.WARNING, logger="v40_sorry_resolver.llm.router"):
            client = router.client(Role.ORCHESTRATOR)
        assert client.cfg.name == "longcat"
        assert any("falling back" in r.getMessage() for r in caplog.records)
        await router.close()

    async def test_fallback_prefers_critic_provider(self, fake_openai, tmp_path):
        router = MultiLLMRouter.from_config(
            router_config(tmp_path, enabled=("kimi", "longcat"))
        )
        assert router.client(Role.PROVER).cfg.name == "kimi"
        await router.close()

    async def test_same_role_returns_same_instance(self, fake_openai, tmp_path):
        router = MultiLLMRouter.from_config(
            router_config(tmp_path, enabled=("longcat",))
        )
        assert router.client(Role.PROVER) is router.client(Role.PROVER)
        await router.close()

    async def test_no_providers_raises(self, fake_openai, tmp_path):
        router = MultiLLMRouter.from_config(router_config(tmp_path, enabled=()))
        with pytest.raises(RuntimeError):
            router.client(Role.PROVER)

    async def test_available_roles(self, fake_openai, tmp_path):
        router = MultiLLMRouter.from_config(
            router_config(tmp_path, enabled=("longcat",))
        )
        roles = router.available_roles()
        # Fallback makes every role usable as long as one provider is up.
        assert set(roles) == set(Role)
        await router.close()

    async def test_health_check_all_disables_failures(self, fake_openai, tmp_path):
        cfg = router_config(tmp_path, enabled=("deepseek_a", "kimi"))
        cfg.providers["kimi"].base_url = "https://api.test/fail/v1"
        router = MultiLLMRouter.from_config(cfg)
        result = await router.health_check_all()
        assert result == {"deepseek_a": True, "kimi": False}
        assert cfg.providers["kimi"].enabled is False
        # kimi disabled -> CRITIC falls back to deepseek_a.
        assert router.client(Role.CRITIC).cfg.name == "deepseek_a"
        await router.close()

    async def test_report_lists_providers(self, fake_openai, tmp_path):
        router = MultiLLMRouter.from_config(
            router_config(tmp_path, enabled=("deepseek_a",))
        )
        text = router.report()
        assert "deepseek_a" in text
        assert "breaker" in text
        await router.close()


# ======================================================================
# Frontier: DeepSeek V4 migration — two-stage health probe
# (frontier_resources section 6: legacy aliases retire 2026-07-24)
# ======================================================================


class TestTwoStageHealthProbe:
    async def test_fallback_alias_adopted_when_primary_fails(self, fake_openai):
        client = make_client(model="deepseek-v4-flash")
        client.fallback_model = "deepseek-chat"
        client.reasoner_model = "deepseek-v4-pro"
        client.reasoner_fallback_model = "deepseek-reasoner"

        async def fail_only_v4(**kwargs):
            if kwargs.get("model") == "deepseek-v4-flash":
                raise make_status_error(400)
            return make_completion()

        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = fail_only_v4

        assert await client.health_check() is True
        # Legacy alias adopted for both chat and reasoner models.
        assert client.cfg.model == "deepseek-chat"
        assert client.reasoner_model == "deepseek-reasoner"
        models_probed = [c.kwargs["model"] for c in create.call_args_list]
        assert models_probed == ["deepseek-v4-flash", "deepseek-chat"]

    async def test_no_fallback_when_primary_healthy(self, fake_openai):
        client = make_client(model="deepseek-v4-flash")
        client.fallback_model = "deepseek-chat"
        assert await client.health_check() is True
        assert client.cfg.model == "deepseek-v4-flash"
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        assert create.call_count == 1  # single-stage probe only

    async def test_fallback_absent_keeps_single_stage_behavior(self, fake_openai):
        client = make_client()
        assert client.fallback_model is None
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(401)
        assert await client.health_check() is False
        assert create.call_count == 1  # no second stage attempted

    async def test_both_stages_fail_returns_false(self, fake_openai):
        client = make_client(model="deepseek-v4-flash")
        client.fallback_model = "deepseek-chat"
        create = FakeAsyncOpenAI.instances[0].chat.completions.create
        create.side_effect = make_status_error(401)
        assert await client.health_check() is False
        assert client.cfg.model == "deepseek-v4-flash"  # unchanged
        assert create.call_count == 2  # both stages probed

    async def test_router_wires_legacy_alias_fallbacks(self, fake_openai, tmp_path):
        cfg = router_config(tmp_path, enabled=("deepseek_a", "deepseek_b"))
        cfg.providers["deepseek_a"].model = "deepseek-v4-flash"
        cfg.providers["deepseek_b"].model = "deepseek-v4-flash"
        cfg.deepseek_reasoner_model = "deepseek-v4-pro"
        router = MultiLLMRouter.from_config(cfg)
        for role in (Role.ORCHESTRATOR, Role.PROVER):
            client = router.client(role)
            assert client.fallback_model == "deepseek-chat"
            assert client.reasoner_fallback_model == "deepseek-reasoner"
        # Non-deepseek providers get no alias wiring.
        cfg2 = router_config(tmp_path, enabled=("kimi",))
        router2 = MultiLLMRouter.from_config(cfg2)
        assert router2.client(Role.CRITIC).fallback_model is None
        await router.close()
        await router2.close()

    async def test_router_no_alias_for_already_legacy_model(
        self, fake_openai, tmp_path
    ):
        cfg = router_config(tmp_path, enabled=("deepseek_a",))
        cfg.providers["deepseek_a"].model = "some-other-model"
        router = MultiLLMRouter.from_config(cfg)
        assert router.client(Role.ORCHESTRATOR).fallback_model is None
        await router.close()
