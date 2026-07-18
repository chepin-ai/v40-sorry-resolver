"""Core data models for the v40 sorry resolver (M1).

Contract: SPEC.md section 3.1. Field-by-field serialization only
(no ``dataclasses.asdict``) to avoid deep-copying foreign objects
(v39 bug P1-5).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

__all__ = [
    "PriorityLevel",
    "ProofStatus",
    "SOLVED_STATUSES",
    "SorryTask",
    "ResolutionResult",
    "MAX_ATTEMPTS_KEPT",
]

# Bound for SorryTask.attempts: only the most recent N attempt summaries
# are kept (SPEC 3.1).
MAX_ATTEMPTS_KEPT = 20


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


#: The four SOLVED_* statuses (SPEC 3.1).
SOLVED_STATUSES: frozenset = frozenset(
    {
        ProofStatus.SOLVED_RFL,
        ProofStatus.SOLVED_LLM_DIRECT,
        ProofStatus.SOLVED_SEARCH,
        ProofStatus.SOLVED_AGENTIC,
    }
)


def _parse_enum(enum_cls, value: Any, default):
    """Tolerant enum parsing: accept enum member, name, or value."""
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        member = enum_cls.__members__.get(value)
        if member is not None:
            return member
        try:  # value-of-enum given as string
            return enum_cls(value)
        except ValueError:
            return default
    if isinstance(value, int):
        for member in enum_cls:
            if member.value == value:
                return member
    return default


@dataclass
class SorryTask:
    """A single `sorry` occurrence to resolve (SPEC 3.1)."""

    id: str  # sha1[:12] of "{file}:{line}:{col}" or explicit id
    project_path: str  # Lean project root (contains lakefile)
    file_path: str  # .lean path relative to project_path
    line_number: int
    column_number: int
    theorem_name: str  # required: theorem containing the sorry
    goal_state: str = ""
    surrounding_context: str = ""
    priority: PriorityLevel = PriorityLevel.P2_MEDIUM
    status: ProofStatus = ProofStatus.OPEN
    proof: Optional[str] = None
    predicted_steps: int = 0
    predicted_success: float = 0.0
    escalation_level: int = 0
    attempts: list = field(default_factory=list)  # bounded: last 20 summaries

    @staticmethod
    def make_id(file_path: str, line_number: int, column_number: int) -> str:
        """Stable task id: sha1[:12] of "{file}:{line}:{col}"."""
        raw = f"{file_path}:{line_number}:{column_number}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def cache_key(self) -> str:
        """sha256(project:file:line:col)[:16] (SPEC 3.1)."""
        raw = (
            f"{self.project_path}:{self.file_path}:"
            f"{self.line_number}:{self.column_number}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def add_attempt(self, summary: dict) -> None:
        """Append an attempt summary, keeping only the most recent 20."""
        self.attempts.append(summary)
        if len(self.attempts) > MAX_ATTEMPTS_KEPT:
            del self.attempts[: len(self.attempts) - MAX_ATTEMPTS_KEPT]

    def to_dict(self) -> dict:
        """Field-by-field serialization (asdict is forbidden: it would
        deep-copy foreign objects, v39 bug P1-5)."""
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
            # Shallow-copy the bounded list of plain dicts.
            "attempts": list(self.attempts[-MAX_ATTEMPTS_KEPT:]),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SorryTask":
        """Tolerant deserialization: missing fields fall back to defaults."""
        if not isinstance(d, dict):
            raise TypeError(f"SorryTask.from_dict expects dict, got {type(d)!r}")
        file_path = str(d.get("file_path", ""))
        line_number = int(d.get("line_number", 0) or 0)
        column_number = int(d.get("column_number", 0) or 0)
        task_id = d.get("id") or cls.make_id(file_path, line_number, column_number)
        attempts = d.get("attempts") or []
        if not isinstance(attempts, list):
            attempts = []
        task = cls(
            id=str(task_id),
            project_path=str(d.get("project_path", "")),
            file_path=file_path,
            line_number=line_number,
            column_number=column_number,
            theorem_name=str(d.get("theorem_name", "")),
            goal_state=str(d.get("goal_state", "") or ""),
            surrounding_context=str(d.get("surrounding_context", "") or ""),
            priority=_parse_enum(
                PriorityLevel, d.get("priority"), PriorityLevel.P2_MEDIUM
            ),
            status=_parse_enum(ProofStatus, d.get("status"), ProofStatus.OPEN),
            proof=d.get("proof"),
            predicted_steps=int(d.get("predicted_steps", 0) or 0),
            predicted_success=float(d.get("predicted_success", 0.0) or 0.0),
            escalation_level=int(d.get("escalation_level", 0) or 0),
            attempts=list(attempts)[-MAX_ATTEMPTS_KEPT:],
        )
        return task


@dataclass
class ResolutionResult:
    """Outcome of attempting to resolve one SorryTask (SPEC 3.1)."""

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
    unverified: bool = False  # True when produced via the mock path
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """Field-by-field serialization."""
        return {
            "task_id": self.task_id,
            "success": self.success,
            "status": self.status.name,
            "proof": self.proof,
            "solver": self.solver,
            "iterations": self.iterations,
            "tokens_used": self.tokens_used,
            "time_elapsed": self.time_elapsed,
            "remaining_goals": self.remaining_goals,
            "verification_passed": self.verification_passed,
            "unverified": self.unverified,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResolutionResult":
        """Tolerant deserialization."""
        if not isinstance(d, dict):
            raise TypeError(
                f"ResolutionResult.from_dict expects dict, got {type(d)!r}"
            )
        return cls(
            task_id=str(d.get("task_id", "")),
            success=bool(d.get("success", False)),
            status=_parse_enum(ProofStatus, d.get("status"), ProofStatus.OPEN),
            proof=d.get("proof"),
            solver=str(d.get("solver", "") or ""),
            iterations=int(d.get("iterations", 0) or 0),
            tokens_used=int(d.get("tokens_used", 0) or 0),
            time_elapsed=float(d.get("time_elapsed", 0.0) or 0.0),
            remaining_goals=int(d.get("remaining_goals", -1)),
            verification_passed=bool(d.get("verification_passed", False)),
            unverified=bool(d.get("unverified", False)),
            error=d.get("error"),
        )
