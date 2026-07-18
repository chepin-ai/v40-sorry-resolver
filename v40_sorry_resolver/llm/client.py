"""OpenAI-compatible async LLM client (M1).

Contract: SPEC.md section 3.3. Uses the openai SDK ``AsyncOpenAI`` with
``max_retries=0`` (retries are implemented here): retry only on 429/5xx/
connection errors with 1s/2s/4s backoff (at most 3 retries); 4xx returns an
error immediately; 3 consecutive 4xx responses open the circuit breaker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ..config import LLMProviderConfig, MIN_THINKING_TIMEOUT_S
from ..metrics import get_global_metrics

__all__ = ["LLMResponse", "AsyncLLMClient", "DEFAULT_TEMPERATURE"]

logger = logging.getLogger(__name__)

#: Default sampling temperature when ``temperature=None`` is passed
#: (matches V40Config.llm_temperature; the router overrides this attribute).
DEFAULT_TEMPERATURE = 0.3
#: Retry backoff schedule for 429/5xx/connection errors (at most 3 retries).
RETRY_BACKOFF_S = (1.0, 2.0, 4.0)
#: Consecutive 4xx responses that trip the circuit breaker.
BREAKER_4XX_THRESHOLD = 3
#: Cache namespace used for LLM responses.
LLM_CACHE_NAMESPACE = "llm"


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    from_cache: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_s": self.latency_s,
        }

    @classmethod
    def from_dict(cls, d: dict, from_cache: bool = False) -> "LLMResponse":
        return cls(
            text=str(d.get("text", "")),
            model=str(d.get("model", "")),
            provider=str(d.get("provider", "")),
            prompt_tokens=int(d.get("prompt_tokens", 0) or 0),
            completion_tokens=int(d.get("completion_tokens", 0) or 0),
            latency_s=float(d.get("latency_s", 0.0) or 0.0),
            from_cache=from_cache,
            error=None,
        )


class AsyncLLMClient:
    """Async client for one OpenAI-compatible provider."""

    def __init__(self, cfg: LLMProviderConfig, cache=None) -> None:
        self.cfg = cfg
        self.cache = cache
        # Router overridables (kept in sync with V40Config by from_config).
        self.default_temperature = DEFAULT_TEMPERATURE
        self.thinking_max_tokens = 2048
        self._client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "unset",
            max_retries=0,  # retries are implemented in this class
            timeout=cfg.timeout_s,
        )
        self._semaphore = asyncio.Semaphore(max(1, cfg.max_concurrent))
        self._closed = False
        self._breaker_open = False
        self._consecutive_4xx = 0
        self._calls = 0
        self._errors = 0
        self._cache_hits = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._latencies: list = []

    # ------------------------------------------------------------------
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        thinking: bool = False,
        cache_key: Optional[str] = None,
    ) -> LLMResponse:
        """Generate a completion. Never raises; errors land in ``.error``."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        # temperature=None falls back to the default; 0.0 must survive
        # (v39 bug: `or` swallowed 0.0).
        temp = temperature if temperature is not None else self.default_temperature
        model = self.cfg.model

        if self._breaker_open:
            return await self._error_response(
                "breaker_open: provider disabled after "
                f"{BREAKER_4XX_THRESHOLD} consecutive 4xx responses "
                "(call health_check to reset)",
                start,
            )
        if not self.cfg.enabled:
            return await self._error_response(
                "provider_disabled: provider is marked disabled in config", start
            )

        full_cache_key = self._build_cache_key(cache_key, model, system_prompt, prompt, temp)
        if full_cache_key is not None:
            cached = await self._cache_get(full_cache_key)
            if cached is not None:
                self._calls += 1
                self._cache_hits += 1
                await self._record_metrics(
                    latency_s=loop.time() - start,
                    prompt_tokens=cached.prompt_tokens,
                    completion_tokens=cached.completion_tokens,
                    success=True,
                    from_cache=True,
                    error=None,
                )
                return cached

        if thinking:
            timeout_s = max(self.cfg.thinking_timeout_s, MIN_THINKING_TIMEOUT_S)
            max_tokens = min(max_tokens, self.thinking_max_tokens)
        else:
            timeout_s = self.cfg.timeout_s

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        last_error = "unknown error"
        async with self._semaphore:
            for attempt in range(len(RETRY_BACKOFF_S) + 1):
                try:
                    completion = await self._client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temp,
                        max_tokens=max_tokens,
                        timeout=timeout_s,
                    )
                except Exception as exc:  # classified below
                    kind, status = self._classify_error(exc)
                    if kind == "client_error":
                        self._consecutive_4xx += 1
                        if self._consecutive_4xx >= BREAKER_4XX_THRESHOLD:
                            self._breaker_open = True
                            logger.error(
                                "provider '%s' circuit breaker OPEN after %d "
                                "consecutive 4xx responses (last=%s)",
                                self.cfg.name,
                                self._consecutive_4xx,
                                status,
                            )
                        last_error = f"http_{status}: {exc}"
                        break  # 4xx: fail fast, never retried
                    if kind == "retryable":
                        last_error = f"retryable(status={status}): {exc}"
                        if attempt < len(RETRY_BACKOFF_S):
                            delay = RETRY_BACKOFF_S[attempt]
                            logger.warning(
                                "provider '%s' retryable error (attempt %d/%d), "
                                "retrying in %.0fs: %s",
                                self.cfg.name,
                                attempt + 1,
                                len(RETRY_BACKOFF_S),
                                delay,
                                exc,
                            )
                            await asyncio.sleep(delay)
                            continue
                        break  # retries exhausted
                    last_error = f"unexpected: {type(exc).__name__}: {exc}"
                    break
                # success path
                self._consecutive_4xx = 0
                response = self._parse_completion(completion, model, start)
                await self._record_success(response)
                if full_cache_key is not None and response.error is None:
                    await self._cache_set(full_cache_key, response)
                return response

        return await self._error_response(last_error, start)

    # ------------------------------------------------------------------
    async def health_check(self) -> bool:
        """Return True if the provider responds; 4xx -> False.

        A successful health check resets the circuit breaker.
        """
        try:
            await self._client.models.list()
        except Exception as exc:
            kind, status = self._classify_error(exc)
            if kind == "client_error" and status == 404:
                # Some providers do not expose /models; fall back to a
                # 1-token chat probe (SPEC 3.3).
                try:
                    await self._client.chat.completions.create(
                        model=self.cfg.model,
                        messages=[{"role": "user", "content": "ping"}],
                        max_tokens=1,
                        timeout=self.cfg.timeout_s,
                    )
                except Exception as exc2:
                    logger.warning(
                        "health_check '%s' chat probe failed: %s", self.cfg.name, exc2
                    )
                    return False
            else:
                logger.warning("health_check '%s' failed: %s", self.cfg.name, exc)
                return False
        self._breaker_open = False
        self._consecutive_4xx = 0
        return True

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """calls, errors, tokens, latency p50/p95, breaker_state."""
        lat = sorted(self._latencies)
        n = len(lat)

        def pct(q: float) -> float:
            if n == 0:
                return 0.0
            idx = min(int(q * (n - 1)), n - 1)
            return float(lat[idx])

        return {
            "provider": self.cfg.name,
            "model": self.cfg.model,
            "enabled": self.cfg.enabled,
            "calls": self._calls,
            "errors": self._errors,
            "cache_hits": self._cache_hits,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._prompt_tokens + self._completion_tokens,
            "latency_p50_s": pct(0.50),
            "latency_p95_s": pct(0.95),
            "breaker_state": "open" if self._breaker_open else "closed",
            "consecutive_4xx": self._consecutive_4xx,
        }

    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Close the underlying SDK client. Re-entrant."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        except Exception:
            logger.warning("error closing client '%s'", self.cfg.name, exc_info=True)

    async def __aenter__(self) -> "AsyncLLMClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _classify_error(exc: Exception) -> tuple:
        """Classify into ('client_error'|'retryable'|'unexpected', status)."""
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            if status == 429 or status >= 500:
                return "retryable", status
            return "client_error", status
        if isinstance(exc, (APITimeoutError, APIConnectionError)):
            return "retryable", None
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return "retryable", None
        if isinstance(exc, APIStatusError):  # defensive: no status_code attr
            return "unexpected", None
        return "unexpected", None

    def _build_cache_key(
        self,
        caller_key: Optional[str],
        model: str,
        system_prompt: Optional[str],
        prompt: str,
        temperature: float,
    ) -> Optional[str]:
        if self.cache is None or caller_key is None:
            return None
        raw = "|".join(
            [
                self.cfg.name,
                caller_key,
                model,
                system_prompt or "",
                prompt,
                repr(temperature),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _cache_get(self, full_key: str) -> Optional[LLMResponse]:
        try:
            raw = await self.cache.get(full_key, namespace=LLM_CACHE_NAMESPACE)
        except Exception as exc:
            logger.warning("cache get failed (provider '%s'): %s", self.cfg.name, exc)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return LLMResponse.from_dict(data, from_cache=True)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("corrupt cache entry ignored: %s", exc)
            return None

    async def _cache_set(self, full_key: str, response: LLMResponse) -> None:
        try:
            payload = json.dumps(response.to_dict(), ensure_ascii=False)
            await self.cache.set(full_key, payload, namespace=LLM_CACHE_NAMESPACE)
        except Exception as exc:
            logger.warning("cache set failed (provider '%s'): %s", self.cfg.name, exc)

    def _parse_completion(self, completion, model: str, start: float) -> LLMResponse:
        loop = asyncio.get_running_loop()
        text = ""
        try:
            text = completion.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            text = ""
        usage = getattr(completion, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return LLMResponse(
            text=text,
            model=model,
            provider=self.cfg.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_s=loop.time() - start,
        )

    async def _record_success(self, response: LLMResponse) -> None:
        self._calls += 1
        self._latencies.append(response.latency_s)
        if len(self._latencies) > 10_000:
            del self._latencies[: len(self._latencies) - 10_000]
        self._prompt_tokens += response.prompt_tokens
        self._completion_tokens += response.completion_tokens
        await self._record_metrics(
            latency_s=response.latency_s,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            success=True,
            from_cache=False,
            error=None,
        )

    async def _error_response(self, message: str, start: float) -> LLMResponse:
        loop = asyncio.get_running_loop()
        latency = loop.time() - start
        self._calls += 1
        self._errors += 1
        self._latencies.append(latency)
        await self._record_metrics(
            latency_s=latency,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            from_cache=False,
            error=message,
        )
        return LLMResponse(
            text="",
            model=self.cfg.model,
            provider=self.cfg.name,
            prompt_tokens=0,
            completion_tokens=0,
            latency_s=latency,
            error=message,
        )

    async def _record_metrics(self, **kwargs) -> None:
        try:
            await get_global_metrics().record_llm_call(
                provider=self.cfg.name, model=self.cfg.model, **kwargs
            )
        except Exception:
            logger.warning("metrics recording failed", exc_info=True)
