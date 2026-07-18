"""Verification layer base types and factory (SPEC 3.6).

A ``Verifier`` decides whether a candidate proof for a ``SorryTask`` is
accepted by the real Lean toolchain. The default implementation is the
subprocess ``lake env lean`` path (see ``subprocess_lean.py``); ``dojo`` is an
optional flag-gated path and ``mock`` is for tests only.

Comments/identifiers are English per SPEC 0.9; user-facing logs may be Chinese.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from ..config import V40Config  # SPEC 3.2 contract (provided by M1)
from ..models import SorryTask  # SPEC 3.1 contract (provided by M1)


@dataclass
class VerificationResult:
    """Outcome of a single proof-verification attempt (SPEC 3.6)."""

    ok: bool
    error: Optional[str] = None
    duration_s: float = 0.0
    # Number of `sorry` warnings still attributed to the target theorem after
    # compilation; -1 means "unknown / not measured".
    remaining_sorries: int = -1
    # Free-form compiler output (tail) useful for debugging / critic lessons.
    diagnostics: str = ""


@runtime_checkable
class Verifier(Protocol):
    """Protocol every concrete verifier implements (SPEC 3.6)."""

    async def init(self) -> None:
        """Prepare the verifier (build caches, run patch scripts, health check).

        Implementations must raise a clear error when their backing toolchain
        is unavailable; silent degradation is forbidden (SPEC 3.8).
        """
        ...

    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        """Return whether ``proof`` discharges the sorry in ``task``."""
        ...

    async def close(self) -> None:
        """Release resources. Must be re-entrant."""
        ...


def build_verifier(cfg: V40Config) -> Verifier:
    """Factory: pick a verifier implementation from ``cfg.verifier`` (SPEC 3.6).

    - ``subprocess`` (default): ``SubprocessLeanVerifier``.
    - ``dojo``: ``LeanDojoVerifier`` (flag-gated, optional).
    - ``mock``: ``MockVerifier`` (tests only; must be explicitly requested).

    Imports are lazy so importing this module never pulls in heavy optional
    dependencies (lean_dojo) unless that path is actually selected.
    """
    name = (getattr(cfg, "verifier", "subprocess") or "subprocess").strip().lower()
    if name == "subprocess":
        from .subprocess_lean import SubprocessLeanVerifier

        return SubprocessLeanVerifier(cfg)
    if name == "dojo":
        from .dojo import LeanDojoVerifier

        return LeanDojoVerifier(cfg)
    if name == "mock":
        from .mock import MockVerifier

        return MockVerifier(cfg)
    raise ValueError(
        f"unknown verifier {name!r}; expected one of: subprocess|dojo|mock"
    )
