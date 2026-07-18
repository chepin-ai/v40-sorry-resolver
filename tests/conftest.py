"""Shared test fixtures for the M2 verification/task-source/predictor layer.

``models.py`` and ``config.py`` are owned by M1 and developed in parallel, so
they may be absent from this worktree. Per the task contract, the M2 modules
import strictly against the SPEC 3.1/3.2 contracts
(``from ..models import SorryTask`` / ``from ..config import V40Config``); these
tests therefore *try the real modules first* and, only if they are missing,
inject minimal SPEC-faithful stubs into ``sys.modules`` before any ``v40_*``
import. After the M1 merge the real modules are used automatically.
"""
from __future__ import annotations

import hashlib
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MINI_PROJECT = "/mnt/agents/output/lean_mini_project"


def _build_models_stub() -> types.ModuleType:
    mod = types.ModuleType("v40_sorry_resolver.models")
    mod.__package__ = "v40_sorry_resolver"

    class PriorityLevel(Enum):
        P0_CRITICAL = 0
        P1_IMPORTANT = 1
        P2_MEDIUM = 2
        P3_LOW = 3

    class ProofStatus(Enum):
        OPEN = "OPEN"
        IN_PROGRESS = "IN_PROGRESS"
        SOLVED_RFL = "SOLVED_RFL"
        SOLVED_LLM_DIRECT = "SOLVED_LLM_DIRECT"
        SOLVED_SEARCH = "SOLVED_SEARCH"
        SOLVED_AGENTIC = "SOLVED_AGENTIC"
        FAILED_ALL = "FAILED_ALL"
        MARKED_AXIOM = "MARKED_AXIOM"
        OPEN_PROBLEM = "OPEN_PROBLEM"
        BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
        UNVERIFIED_MOCK = "UNVERIFIED_MOCK"

    SOLVED_STATUSES = frozenset(
        {
            ProofStatus.SOLVED_RFL,
            ProofStatus.SOLVED_LLM_DIRECT,
            ProofStatus.SOLVED_SEARCH,
            ProofStatus.SOLVED_AGENTIC,
        }
    )

    @dataclass
    class SorryTask:
        id: str
        project_path: str
        file_path: str
        line_number: int
        column_number: int
        theorem_name: str
        goal_state: str = ""
        surrounding_context: str = ""
        priority: PriorityLevel = PriorityLevel.P2_MEDIUM
        status: ProofStatus = ProofStatus.OPEN
        proof: Optional[str] = None
        predicted_steps: int = 0
        predicted_success: float = 0.0
        escalation_level: int = 0
        attempts: list = field(default_factory=list)

        def cache_key(self) -> str:
            raw = f"{self.project_path}:{self.file_path}:{self.line_number}:{self.column_number}"
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

        def to_dict(self) -> dict:
            return {
                "id": self.id,
                "project_path": self.project_path,
                "file_path": self.file_path,
                "line_number": self.line_number,
                "column_number": self.column_number,
                "theorem_name": self.theorem_name,
                "goal_state": self.goal_state,
                "surrounding_context": self.surrounding_context,
                "priority": self.priority.name,
                "status": self.status.name,
                "proof": self.proof,
                "predicted_steps": self.predicted_steps,
                "predicted_success": self.predicted_success,
                "escalation_level": self.escalation_level,
                "attempts": list(self.attempts)[-20:],
            }

        @classmethod
        def from_dict(cls, d: dict) -> "SorryTask":
            return cls(
                id=d["id"],
                project_path=d["project_path"],
                file_path=d["file_path"],
                line_number=int(d["line_number"]),
                column_number=int(d.get("column_number", 1)),
                theorem_name=d["theorem_name"],
                goal_state=d.get("goal_state", ""),
                surrounding_context=d.get("surrounding_context", ""),
                priority=PriorityLevel[d.get("priority", "P2_MEDIUM")],
                status=ProofStatus[d.get("status", "OPEN")],
                proof=d.get("proof"),
                predicted_steps=int(d.get("predicted_steps", 0)),
                predicted_success=float(d.get("predicted_success", 0.0)),
                escalation_level=int(d.get("escalation_level", 0)),
                attempts=list(d.get("attempts", [])),
            )

    @dataclass
    class ResolutionResult:
        task_id: str
        success: bool
        status: ProofStatus
        proof: Optional[str] = None
        solver: str = ""
        iterations: int = 0
        tokens_used: int = 0
        time_elapsed: float = 0.0
        remaining_goals: int = -1
        verification_passed: bool = False
        unverified: bool = False
        error: Optional[str] = None

    mod.PriorityLevel = PriorityLevel
    mod.ProofStatus = ProofStatus
    mod.SOLVED_STATUSES = SOLVED_STATUSES
    mod.SorryTask = SorryTask
    mod.ResolutionResult = ResolutionResult
    return mod


def _build_config_stub() -> types.ModuleType:
    mod = types.ModuleType("v40_sorry_resolver.config")
    mod.__package__ = "v40_sorry_resolver"

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
        lean_project_paths: list = field(
            default_factory=lambda: ["/mnt/agents/output/lean_mini_project"]
        )
        sorrydb_endpoint: Optional[str] = None
        verifier: str = "subprocess"
        lean_timeout_s: float = 30.0
        max_concurrent_lean: int = 4
        num_workers: int = 8
        wall_clock_budget_s: float = 36000.0
        per_task_time_budget_s: float = 600.0
        per_task_token_budget: int = 200_000
        soft_deadline_s: float = 32400.0
        tactic_search_depth: int = 4
        tactic_search_width: int = 2
        agentic_max_iterations: int = 8
        agentic_stall_patience: int = 3
        thinking_max_tokens: int = 2048
        escalation_threshold: int = 3
        axiom_quota: int = 45
        providers: dict = field(default_factory=dict)
        llm_temperature: float = 0.3
        work_dir: str = "./v40_work"
        checkpoint_interval_tasks: int = 10
        # Optional knobs consumed by the M2 verifiers (getattr-defaulted anyway).
        check_axioms: bool = False
        dojo_experimental: bool = False
        dojo_apply_patch: bool = True

        @classmethod
        def from_env(cls, env_file: Optional[str] = ".env") -> "V40Config":
            return cls()

        def validate(self) -> list:
            return []

    mod.LLMProviderConfig = LLMProviderConfig
    mod.V40Config = V40Config
    return mod


def _ensure_contract_modules() -> None:
    """Import real M1 modules if present, else inject SPEC-faithful stubs."""
    try:
        import v40_sorry_resolver.models  # noqa: F401
    except Exception:
        sys.modules["v40_sorry_resolver.models"] = _build_models_stub()
    try:
        import v40_sorry_resolver.config  # noqa: F401
    except Exception:
        sys.modules["v40_sorry_resolver.config"] = _build_config_stub()


_ensure_contract_modules()

# Re-export the (real or stub) contract classes for use in tests/fixtures.
from v40_sorry_resolver.models import PriorityLevel, ProofStatus, SorryTask  # noqa: E402
from v40_sorry_resolver.config import V40Config  # noqa: E402


@pytest.fixture()
def mini_project() -> str:
    return MINI_PROJECT


@pytest.fixture()
def config(tmp_path) -> V40Config:
    return V40Config(work_dir=str(tmp_path / "v40_work"))


@pytest.fixture()
def mini_tasks(mini_project):
    from v40_sorry_resolver.sorrydb import SorryScanner

    return SorryScanner().scan([mini_project])


__all__ = [
    "PriorityLevel",
    "ProofStatus",
    "SorryTask",
    "V40Config",
    "MINI_PROJECT",
]
