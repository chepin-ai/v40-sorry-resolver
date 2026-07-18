"""Multi-LLM role router (M1).

Contract: SPEC.md section 3.4.

Role semantics:
- DeepSeek key1 (deepseek_a) = ORCHESTRATOR: planning/scheduling/coordination,
  periodic metrics evaluation emitting strategy-adjustment JSON.
- DeepSeek key2 (deepseek_b) = PROVER: main proof generation.
- Kimi = CRITIC: proof review / cross-evaluation / lesson summarization.
- LongCat = EXPLORER: tactic diversity sampling / alternative routes.

Roles without a key fall back automatically (CRITIC->PROVER->EXPLORER->
ORCHESTRATOR) with a WARNING, and the report marks the fallback.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Optional

from ..config import LEGACY_DEEPSEEK_ALIASES, V40Config
from .client import AsyncLLMClient

__all__ = ["Role", "ROLE_TO_PROVIDER", "MultiLLMRouter"]

logger = logging.getLogger(__name__)


class Role(Enum):
    ORCHESTRATOR = "ORCHESTRATOR"
    PROVER = "PROVER"
    CRITIC = "CRITIC"
    EXPLORER = "EXPLORER"


#: Role name -> provider key in V40Config.providers (SPEC 3.4).
ROLE_TO_PROVIDER = {
    "ORCHESTRATOR": "deepseek_a",
    "PROVER": "deepseek_b",
    "CRITIC": "kimi",
    "EXPLORER": "longcat",
}

#: Fallback order when a role's provider is not enabled (SPEC 3.4).
_FALLBACK_CHAIN = (Role.CRITIC, Role.PROVER, Role.EXPLORER, Role.ORCHESTRATOR)


class MultiLLMRouter:
    """Routes LLM roles to provider clients with automatic fallback."""

    def __init__(self, clients: dict, provider_cfgs: dict) -> None:
        # Active (enabled) clients, keyed by provider name.
        self._clients: dict = dict(clients)
        # Every client created (including later-disabled), for report/close.
        self._all_clients: dict = dict(clients)
        self._provider_cfgs = provider_cfgs
        self._warned_fallbacks: set = set()

    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: V40Config, cache=None, metrics=None) -> "MultiLLMRouter":
        """Build clients for all enabled providers in the config.

        ``metrics`` is the collector shared with the pipeline (N-2); when
        None each client falls back to the process-wide global collector.
        DeepSeek providers get the configured reasoner model wired for
        thinking=True calls (SPEC 3.3 reasoning-model routing, N-8).
        """
        clients = {}
        for name, provider_cfg in cfg.providers.items():
            if not provider_cfg.enabled:
                continue
            client = AsyncLLMClient(provider_cfg, cache=cache, metrics=metrics)
            client.default_temperature = cfg.llm_temperature
            client.thinking_max_tokens = cfg.thinking_max_tokens
            if name.startswith("deepseek"):
                reasoner = getattr(cfg, "deepseek_reasoner_model", "") or ""
                client.reasoner_model = reasoner.strip() or None
                # DeepSeek V4 migration (frontier_resources section 6): wire
                # the retired legacy alias as a one-shot health-check fallback
                # (two-stage probe) during the transition window.
                client.fallback_model = LEGACY_DEEPSEEK_ALIASES.get(
                    provider_cfg.model
                )
                if client.reasoner_model:
                    client.reasoner_fallback_model = LEGACY_DEEPSEEK_ALIASES.get(
                        client.reasoner_model
                    )
            clients[name] = client
        return cls(clients, cfg.providers)

    # ------------------------------------------------------------------
    def client(self, role: Role) -> AsyncLLMClient:
        """Return the client for a role, following the fallback chain.

        The same role always resolves to the same client instance.
        Raises RuntimeError if no provider is enabled at all.
        """
        provider_name, client = self._resolve(role)
        if client is None:
            raise RuntimeError(
                f"no enabled LLM provider available for role {role.name}; "
                "configure API keys in .env (see .env.example)"
            )
        primary = ROLE_TO_PROVIDER[role.name]
        if provider_name != primary:
            self._warn_fallback_once(role, provider_name)
        return client

    # ------------------------------------------------------------------
    async def health_check_all(self) -> dict:
        """Concurrently health-check all providers; failures get disabled."""
        names = list(self._all_clients.keys())
        checks = [self._all_clients[name].health_check() for name in names]
        results = await asyncio.gather(*checks, return_exceptions=True)
        outcome: dict = {}
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                logger.warning("health check for '%s' raised: %s", name, result)
                ok = False
            else:
                ok = bool(result)
            outcome[name] = ok
            if not ok:
                logger.warning("provider '%s' failed health check; disabling", name)
                cfg = self._provider_cfgs.get(name)
                if cfg is not None:
                    cfg.enabled = False
                self._clients.pop(name, None)
        return outcome

    # ------------------------------------------------------------------
    def available_roles(self) -> list:
        """Roles that currently resolve to an enabled provider (incl. fallback)."""
        return [role for role in Role if self._resolve(role)[1] is not None]

    # ------------------------------------------------------------------
    def report(self) -> str:
        """Aligned per-provider status/usage/cost table."""
        header = (
            f"{'provider':<14} {'role':<13} {'enabled':>7} {'model':<22} "
            f"{'calls':>6} {'errors':>6} {'tokens':>10} {'breaker':>7} {'note':<18}"
        )
        lines = ["=== multi-LLM router report ===", header, "-" * len(header)]
        provider_to_role = {v: k for k, v in ROLE_TO_PROVIDER.items()}
        for name in sorted(self._all_clients):
            stats = self._all_clients[name].stats()
            cfg = self._provider_cfgs.get(name)
            enabled = bool(cfg.enabled) if cfg is not None else False
            fallback_users = sorted(
                role.name
                for role in Role
                if ROLE_TO_PROVIDER[role.name] != name
                and self._resolve(role)[0] == name
            )
            note = f"fallback:{','.join(fallback_users)}" if fallback_users else ""
            lines.append(
                f"{name:<14} {provider_to_role.get(name, '-'):<13} "
                f"{str(enabled):>7} {stats['model']:<22} "
                f"{stats['calls']:>6} {stats['errors']:>6} "
                f"{stats['total_tokens']:>10} {stats['breaker_state']:>7} {note:<18}"
            )
        disabled = [
            name
            for name, cfg in self._provider_cfgs.items()
            if not cfg.enabled and name not in self._all_clients
        ]
        for name in sorted(disabled):
            lines.append(
                f"{name:<14} {provider_to_role.get(name, '-'):<13} {'False':>7} "
                f"{'(no api key)':<22} {'0':>6} {'0':>6} {'0':>10} {'-':>7} {'disabled':<18}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Close all underlying clients (re-entrant per client)."""
        await asyncio.gather(
            *(client.close() for client in self._all_clients.values()),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _resolve(self, role: Role) -> tuple:
        primary = ROLE_TO_PROVIDER[role.name]
        client = self._clients.get(primary)
        if client is not None:
            return primary, client
        for fallback_role in _FALLBACK_CHAIN:
            provider_name = ROLE_TO_PROVIDER[fallback_role.name]
            client = self._clients.get(provider_name)
            if client is not None:
                return provider_name, client
        return None, None

    def _warn_fallback_once(self, role: Role, provider_name: str) -> None:
        key = (role.name, provider_name)
        if key in self._warned_fallbacks:
            return
        self._warned_fallbacks.add(key)
        logger.warning(
            "role %s provider '%s' not enabled; falling back to provider '%s'",
            role.name,
            ROLE_TO_PROVIDER[role.name],
            provider_name,
        )
