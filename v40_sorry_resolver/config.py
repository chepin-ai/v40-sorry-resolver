"""Configuration for the v40 sorry resolver (M1).

Contract: SPEC.md section 3.2. Zero hard-coded secrets: every API key comes
from the process environment or a ``.env`` file (parsed by a small built-in
parser; real environment variables take precedence over the file).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

__all__ = ["LLMProviderConfig", "V40Config", "BudgetTier"]

logger = logging.getLogger(__name__)

VALID_VERIFIERS = ("subprocess", "dojo", "mock")

# Public, real default model/base-URL values (SPEC 3.2 env contract).
#
# DeepSeek V4 migration (frontier_resources.md section 6, verified 2026-07):
# the platform entered the V4 era on 2026-04-24; the old aliases
# ``deepseek-chat`` / ``deepseek-reasoner`` retire on **2026-07-24**. New
# defaults are the real V4 model names. ``LEGACY_DEEPSEEK_ALIASES`` maps each
# new name back to its legacy alias so the health check can do a two-stage
# probe (new name first, legacy alias as automatic fallback) during the
# transition window.
_DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_DEFAULT_DEEPSEEK_REASONER_MODEL = "deepseek-v4-pro"
#: new V4 model name -> retired legacy alias (removal date 2026-07-24).
LEGACY_DEEPSEEK_ALIASES = {
    "deepseek-v4-flash": "deepseek-chat",
    "deepseek-v4-pro": "deepseek-reasoner",
}
_DEFAULT_KIMI_BASE_URL = "https://api.moonshot.cn/v1"
_DEFAULT_KIMI_MODEL = "moonshot-v1-8k"
# T1-verified working values (benchmark BUG-6): the old `openapi/v1` endpoint
# 404s and `LongCat-Flash-Chat` is retired; only `LongCat-2.0` is served.
_DEFAULT_LONGCAT_BASE_URL = "https://api.longcat.chat/openai/v1"
_DEFAULT_LONGCAT_MODEL = "LongCat-2.0"

#: Thinking calls get a dedicated timeout floor (SPEC: >= 240s).
MIN_THINKING_TIMEOUT_S = 240.0
#: Normal (non-thinking) LLM timeout ceiling (SPEC: <= 60s).
MAX_NORMAL_TIMEOUT_S = 60.0


class BudgetTier(Enum):
    """Cost-aware per-task budget tier (frontier_atp Top-8 #8).

    Assigned from ``LeanProgressV2.predicted_steps``; the orchestrator picks a
    matching ``StrategyConfig`` preset so cheap tasks never burn deep-search
    budget (pass>64 marginal returns collapse, EconProver arXiv:2509.12603).
    """

    LIGHT = "LIGHT"
    STANDARD = "STANDARD"
    DEEP = "DEEP"


@dataclass
class LLMProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    max_concurrent: int = 4
    timeout_s: float = 60.0
    thinking_timeout_s: float = 300.0
    enabled: bool = True


@dataclass
class V40Config:
    # Task sources
    lean_project_paths: list = field(
        default_factory=lambda: ["/mnt/agents/output/lean_mini_project"]
    )
    sorrydb_endpoint: Optional[str] = None  # None = do not fetch remotely
    # Verification
    verifier: str = "subprocess"  # subprocess|dojo|mock (mock only for tests)
    lean_timeout_s: float = 30.0
    max_concurrent_lean: int = 4
    check_axioms: bool = False  # append `#print axioms`, reject sorryAx
    # SorryDB anti-cheat protocol (frontier_atp 5.1 / Top-8 #7): when True the
    # verifier additionally asserts (1) the target theorem's sorry count drops
    # by exactly 1, (2) the theorem statement text is unchanged by the splice,
    # (3) with check_axioms=True, `#print axioms` shows no sorryAx.
    sorrydb_mode: bool = False
    # Concurrency & budgets
    num_workers: int = 8
    wall_clock_budget_s: float = 36000.0  # global budget, default 10h
    per_task_time_budget_s: float = 600.0
    per_task_token_budget: int = 200_000
    soft_deadline_s: float = 32400.0
    # Solving parameters (may be adjusted dynamically by OrchestratorLLM)
    tactic_search_depth: int = 4
    tactic_search_width: int = 2
    agentic_max_iterations: int = 8
    agentic_stall_patience: int = 3
    thinking_max_tokens: int = 2048
    escalation_threshold: int = 3
    axiom_quota: int = 45
    # Length-normalized beam tie-break (BFS-Prover, arXiv:2502.03438;
    # frontier_atp Top-8 #3): same-priority candidates are ordered by
    # alpha * log(L) where L is the accumulated proof length in tokens.
    # alpha = 0.0 reproduces the previous FIFO tie-break exactly.
    search_length_norm_alpha: float = 0.1
    # Premise retrieval tool (frontier_atp Top-8 #6): leansearch.net +
    # premise-search.com async clients feeding top-k Mathlib lemma names into
    # Critic/AxProver prompts. Default OFF; failures degrade to [] + WARNING
    # and never block the solving flow.
    retrieval_enabled: bool = False
    # LLM
    providers: dict = field(default_factory=dict)  # filled by from_env
    llm_temperature: float = 0.3
    # Reasoning model used for thinking calls on DeepSeek providers
    # (from DEEPSEEK_REASONER_MODEL; routing decision belongs to llm/router).
    deepseek_reasoner_model: str = _DEFAULT_DEEPSEEK_REASONER_MODEL
    # Storage
    work_dir: str = "./v40_work"
    checkpoint_interval_tasks: int = 10

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env_file: Optional[str] = ".env") -> "V40Config":
        """Build config from ``.env`` (if present) + process environment.

        Real environment variables take precedence over the .env file.
        Providers whose API key is missing are created with
        ``enabled=False`` and a WARNING is logged.
        """
        file_vars = _parse_env_file(env_file) if env_file else {}

        def get(name: str, default: str = "") -> str:
            if name in os.environ:
                return os.environ[name]
            return file_vars.get(name, default)

        cfg = cls()
        cfg.providers = {
            "deepseek_a": _provider_from_env(
                get,
                name="deepseek_a",
                key_env="DEEPSEEK_API_KEY",
                url_env="DEEPSEEK_BASE_URL",
                url_default=_DEFAULT_DEEPSEEK_BASE_URL,
                model_env="DEEPSEEK_MODEL",
                model_default=_DEFAULT_DEEPSEEK_MODEL,
            ),
            "deepseek_b": _provider_from_env(
                get,
                name="deepseek_b",
                key_env="DEEPSEEK_API_KEY_2",
                url_env="DEEPSEEK_BASE_URL",
                url_default=_DEFAULT_DEEPSEEK_BASE_URL,
                model_env="DEEPSEEK_MODEL",
                model_default=_DEFAULT_DEEPSEEK_MODEL,
            ),
            "kimi": _provider_from_env(
                get,
                name="kimi",
                key_env="KIMI_API_KEY",
                url_env="KIMI_BASE_URL",
                url_default=_DEFAULT_KIMI_BASE_URL,
                model_env="KIMI_MODEL",
                model_default=_DEFAULT_KIMI_MODEL,
            ),
            "longcat": _provider_from_env(
                get,
                name="longcat",
                key_env="LONGCAT_API_KEY",
                url_env="LONGCAT_BASE_URL",
                url_default=_DEFAULT_LONGCAT_BASE_URL,
                model_env="LONGCAT_MODEL",
                model_default=_DEFAULT_LONGCAT_MODEL,
            ),
        }
        cfg.deepseek_reasoner_model = (
            get("DEEPSEEK_REASONER_MODEL", _DEFAULT_DEEPSEEK_REASONER_MODEL).strip()
            or _DEFAULT_DEEPSEEK_REASONER_MODEL
        )
        verifier = get("V40_VERIFIER", "subprocess").strip() or "subprocess"
        cfg.verifier = verifier
        cfg.num_workers = _as_int(get("V40_NUM_WORKERS", ""), cfg.num_workers)
        return cfg

    # ------------------------------------------------------------------
    def validate(self) -> list:
        """Return a human-readable list of configuration problems."""
        problems: list = []
        if self.verifier not in VALID_VERIFIERS:
            problems.append(
                f"verifier '{self.verifier}' is invalid; "
                f"expected one of {VALID_VERIFIERS}"
            )
        if self.verifier == "mock":
            problems.append(
                "verifier 'mock' is enabled: results will be [UNVERIFIED]; "
                "use only for tests"
            )
        if self.num_workers < 1:
            problems.append(f"num_workers must be >= 1, got {self.num_workers}")
        if self.max_concurrent_lean < 1:
            problems.append(
                f"max_concurrent_lean must be >= 1, got {self.max_concurrent_lean}"
            )
        if self.lean_timeout_s <= 0:
            problems.append(f"lean_timeout_s must be > 0, got {self.lean_timeout_s}")
        if self.wall_clock_budget_s <= 0:
            problems.append(
                f"wall_clock_budget_s must be > 0, got {self.wall_clock_budget_s}"
            )
        if self.per_task_time_budget_s <= 0:
            problems.append(
                f"per_task_time_budget_s must be > 0, "
                f"got {self.per_task_time_budget_s}"
            )
        if self.per_task_token_budget <= 0:
            problems.append(
                f"per_task_token_budget must be > 0, got {self.per_task_token_budget}"
            )
        if self.soft_deadline_s >= self.wall_clock_budget_s:
            problems.append(
                f"soft_deadline_s ({self.soft_deadline_s}) should be smaller "
                f"than wall_clock_budget_s ({self.wall_clock_budget_s})"
            )
        if self.tactic_search_depth < 1:
            problems.append(
                f"tactic_search_depth must be >= 1, got {self.tactic_search_depth}"
            )
        if self.tactic_search_width < 1:
            problems.append(
                f"tactic_search_width must be >= 1, got {self.tactic_search_width}"
            )
        if self.agentic_max_iterations < 1:
            problems.append(
                f"agentic_max_iterations must be >= 1, "
                f"got {self.agentic_max_iterations}"
            )
        if self.agentic_stall_patience < 1:
            problems.append(
                f"agentic_stall_patience must be >= 1, "
                f"got {self.agentic_stall_patience}"
            )
        if self.thinking_max_tokens <= 0:
            problems.append(
                f"thinking_max_tokens must be > 0, got {self.thinking_max_tokens}"
            )
        if self.escalation_threshold < 1:
            problems.append(
                f"escalation_threshold must be >= 1, got {self.escalation_threshold}"
            )
        if self.axiom_quota < 0:
            problems.append(f"axiom_quota must be >= 0, got {self.axiom_quota}")
        try:
            alpha = float(self.search_length_norm_alpha)
        except (TypeError, ValueError):
            alpha = -1.0
        if alpha < 0:
            problems.append(
                f"search_length_norm_alpha must be >= 0, "
                f"got {self.search_length_norm_alpha}"
            )
        if self.checkpoint_interval_tasks < 1:
            problems.append(
                f"checkpoint_interval_tasks must be >= 1, "
                f"got {self.checkpoint_interval_tasks}"
            )
        if not self.lean_project_paths:
            problems.append("lean_project_paths is empty: no task source")
        else:
            for path in self.lean_project_paths:
                if not Path(path).exists():
                    problems.append(f"lean_project_path does not exist: {path}")

        enabled = [p for p in self.providers.values() if p.enabled]
        for provider in self.providers.values():
            if not provider.enabled:
                problems.append(
                    f"provider '{provider.name}' is disabled "
                    f"(missing/empty API key or health check failure)"
                )
                continue
            if not provider.api_key:
                problems.append(f"provider '{provider.name}' has an empty api_key")
            if not provider.base_url:
                problems.append(f"provider '{provider.name}' has an empty base_url")
            if not provider.model:
                problems.append(f"provider '{provider.name}' has an empty model")
            if provider.thinking_timeout_s < MIN_THINKING_TIMEOUT_S:
                problems.append(
                    f"provider '{provider.name}' thinking_timeout_s="
                    f"{provider.thinking_timeout_s} < {MIN_THINKING_TIMEOUT_S}s "
                    f"floor for thinking calls"
                )
            if provider.timeout_s > MAX_NORMAL_TIMEOUT_S:
                problems.append(
                    f"provider '{provider.name}' timeout_s={provider.timeout_s} "
                    f"exceeds the {MAX_NORMAL_TIMEOUT_S}s normal-call budget"
                )
        if self.providers and not enabled:
            problems.append(
                "no LLM provider is enabled; set API keys in .env "
                "(see .env.example) or use --mock-llm for tests"
            )
        return problems


def _provider_from_env(
    get,
    name: str,
    key_env: str,
    url_env: str,
    url_default: str,
    model_env: str,
    model_default: str,
) -> LLMProviderConfig:
    api_key = get(key_env).strip()
    base_url = get(url_env, url_default).strip() or url_default
    model = get(model_env, model_default).strip() or model_default
    enabled = bool(api_key)
    if not enabled:
        logger.warning(
            "LLM provider '%s' disabled: environment variable %s is not set",
            name,
            key_env,
        )
    return LLMProviderConfig(
        name=name,
        base_url=base_url,
        api_key=api_key,
        model=model,
        enabled=enabled,
    )


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_env_file(path: Optional[str]) -> dict:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes.

    Real environment variables always take precedence (handled by caller).
    """
    result: dict = {}
    if not path:
        return result
    env_path = Path(path)
    if not env_path.is_file():
        return result
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read env file %s: %s", env_path, exc)
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            # Quoted value: extract up to the closing quote (allows a
            # trailing comment after it).
            closing = value.find(value[0], 1)
            if closing != -1:
                value = value[1:closing]
        else:
            hash_idx = value.find(" #")
            if hash_idx != -1:
                value = value[:hash_idx].rstrip()
        if key:
            result[key] = value
    return result
