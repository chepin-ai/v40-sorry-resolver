"""v40 sorry resolver — public API (M1 core + multi-LLM layer).

Only M1-owned symbols are exported here. engine/* and verify/* belong to
other modules and are intentionally not imported. The llm subpackage
depends on the ``openai`` package, so its symbols are exposed lazily
(PEP 562) to keep the base import light and side-effect free.
"""

from .cache import Cache
from .checkpoint import Checkpoint
from .config import BudgetTier, LLMProviderConfig, V40Config
from .metrics import MetricsCollector, get_global_metrics, reset_global_metrics
from .models import (
    PriorityLevel,
    ProofStatus,
    ResolutionResult,
    SOLVED_STATUSES,
    SorryTask,
)

__version__ = "40.0.0"

_LAZY_LLM = {
    "AsyncLLMClient": ("v40_sorry_resolver.llm.client", "AsyncLLMClient"),
    "LLMResponse": ("v40_sorry_resolver.llm.client", "LLMResponse"),
    "MultiLLMRouter": ("v40_sorry_resolver.llm.router", "MultiLLMRouter"),
    "Role": ("v40_sorry_resolver.llm.router", "Role"),
    "ROLE_TO_PROVIDER": ("v40_sorry_resolver.llm.router", "ROLE_TO_PROVIDER"),
}


def __getattr__(name: str):
    target = _LAZY_LLM.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache for subsequent accesses
    return value


__all__ = [
    "__version__",
    # models
    "PriorityLevel",
    "ProofStatus",
    "SOLVED_STATUSES",
    "SorryTask",
    "ResolutionResult",
    # config
    "BudgetTier",
    "LLMProviderConfig",
    "V40Config",
    # cache / checkpoint / metrics
    "Cache",
    "Checkpoint",
    "MetricsCollector",
    "get_global_metrics",
    "reset_global_metrics",
    # llm (lazy)
    "AsyncLLMClient",
    "LLMResponse",
    "MultiLLMRouter",
    "Role",
    "ROLE_TO_PROVIDER",
]
