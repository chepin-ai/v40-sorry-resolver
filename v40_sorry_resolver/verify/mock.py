"""Test-only mock verifier (SPEC 3.8).

Rules:
  * ``verify_proof`` returns ``ok=True`` only when the proof contains the
    literal marker ``"VALID"``; every other proof is rejected. The v39 ``apply``
    -heuristic false-positive is explicitly forbidden.
  * The ``unverified=True`` annotation on the final ``ResolutionResult`` is the
    *caller's* responsibility (the caller knows it built a ``MockVerifier`` via
    ``cfg.verifier == "mock"`` and must mark results ``UNVERIFIED`` accordingly).
"""
from __future__ import annotations

import time
from typing import Optional

from ..config import V40Config  # SPEC 3.2 contract (provided by M1)
from ..models import SorryTask  # SPEC 3.1 contract (provided by M1)
from .base import VerificationResult

VALID_MARKER = "VALID"


class MockVerifier:
    """Deterministic, dependency-free verifier for tests only."""

    # Lets a caller detect the mock and mark results UNVERIFIED (SPEC 3.8).
    is_mock = True

    def __init__(self, cfg: Optional[V40Config] = None) -> None:
        self._cfg = cfg
        self.calls: list[tuple[str, str]] = []  # (task_id, proof) audit trail

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        t0 = time.monotonic()
        self.calls.append((getattr(task, "id", ""), proof))
        ok = isinstance(proof, str) and VALID_MARKER in proof
        return VerificationResult(
            ok=ok,
            error=None if ok else "mock: proof lacks VALID marker",
            duration_s=time.monotonic() - t0,
        )
