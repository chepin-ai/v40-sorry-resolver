"""Shared test fixtures for the v40 sorry resolver.

All contract modules (models/config/cache/checkpoint/metrics/llm/verify/
sorrydb/progress) are REAL — the historical stub-injection machinery was
removed in fix-round1 (stub drift hid the M1<->M3 metrics boundary bugs
and the CLI scanner signature bug; see review N-1/N-2).

Kept here:
- M3 fakes (FakeLLMClient / FakeRouter / FakeVerifier) + engine fixtures;
- M2 fixtures (MINI_PROJECT / mini_project / config / mini_tasks).

All engine tests use fake verifier / fake LLM per the SPEC contract; no
network access anywhere.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from v40_sorry_resolver.llm.router import Role  # noqa: E402
from v40_sorry_resolver.verify.base import VerificationResult  # noqa: E402


# ------------------------------------------------------------------ fakes


class FakeLLMClient:
    """Contract-shaped fake AsyncLLMClient.

    script: callable(prompt, system_prompt, role, call_index) -> str
            or a plain string returned for every call.
    """

    def __init__(self, role, script="VALID proof", delay: float = 0.0):
        self.role = role
        self.script = script
        self.delay = delay
        self.calls: list[dict] = []

    async def generate(
        self,
        prompt,
        system_prompt=None,
        temperature=None,
        max_tokens=2048,
        thinking=False,
        cache_key=None,
    ):
        if self.delay:
            await asyncio.sleep(self.delay)
        idx = len(self.calls) + 1
        self.calls.append(
            {
                "role": self.role,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "thinking": thinking,
                "max_tokens": max_tokens,
            }
        )
        if callable(self.script):
            text = self.script(prompt, system_prompt, self.role, idx)
        else:
            text = self.script

        class _Resp:
            def __init__(self, text, delay):
                self.text = text
                self.model = "fake"
                self.provider = "fake"
                self.prompt_tokens = 10
                self.completion_tokens = 5
                self.latency_s = delay
                self.from_cache = False
                self.error = None

        return _Resp(text, self.delay)

    async def health_check(self):
        return True

    def stats(self):
        return {"calls": len(self.calls)}

    async def close(self):
        return None


class FakeRouter:
    """Contract-shaped fake MultiLLMRouter (with role fallback)."""

    def __init__(self, clients: dict):
        self.clients = dict(clients)

    def client(self, role):
        if role in self.clients:
            return self.clients[role]
        for fallback in (Role.CRITIC, Role.PROVER, Role.EXPLORER, Role.ORCHESTRATOR):
            if fallback in self.clients:
                return self.clients[fallback]
        raise RuntimeError("FakeRouter: no clients")

    async def health_check_all(self):
        return {r.value: True for r in self.clients}

    def available_roles(self):
        return list(self.clients)

    def report(self):
        return "fake router"


class FakeVerifier:
    """Contract-shaped fake verifier: ok iff proof contains marker.

    Tracks in-flight concurrency so tests can assert real parallelism.
    """

    def __init__(self, marker: str = "VALID", delay: float = 0.0, remaining: int = 1):
        self.marker = marker
        self.delay = delay
        self.remaining = remaining
        self.calls: list[tuple[str, str]] = []
        self.inflight = 0
        self.max_inflight = 0

    async def init(self):
        return None

    async def verify_proof(self, task, proof):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            ok = self.marker in (proof or "")
            self.calls.append((task.id, proof))
            return VerificationResult(
                ok=ok,
                error=None if ok else f"{self.marker} marker missing",
                duration_s=self.delay,
                remaining_sorries=0 if ok else self.remaining,
                diagnostics="" if ok else "fake verifier rejected",
            )
        finally:
            self.inflight -= 1

    async def close(self):
        return None


# --------------------------------------------------------------- fixtures


@pytest.fixture
def make_task():
    from v40_sorry_resolver.models import SorryTask

    def _make(i: int, **kwargs) -> SorryTask:
        defaults = dict(
            id=f"task-{i:03d}",
            project_path="/tmp/fake_project",
            file_path="Fake/Basic.lean",
            line_number=10 + i,
            column_number=4,
            theorem_name=f"theorem_{i}",
            goal_state=f"goal_{i}",
            surrounding_context=f"theorem theorem_{i} : True := by\n  sorry",
        )
        defaults.update(kwargs)
        return SorryTask(**defaults)

    return _make


@pytest.fixture
def base_config(tmp_path):
    from v40_sorry_resolver.config import V40Config

    return V40Config(
        work_dir=str(tmp_path / "work"),
        num_workers=4,
        checkpoint_interval_tasks=5,
    )


# ---------------------------------------------------------------------------
# M2 fixtures: real-module based fixtures for verify/sorrydb/progress tests.
# ---------------------------------------------------------------------------

MINI_PROJECT = "/mnt/agents/output/lean_mini_project"


@pytest.fixture()
def mini_project() -> str:
    return MINI_PROJECT


@pytest.fixture()
def config(tmp_path):
    from v40_sorry_resolver.config import V40Config

    return V40Config(work_dir=str(tmp_path / "v40_work"))


@pytest.fixture()
def mini_tasks(mini_project):
    from v40_sorry_resolver.sorrydb import SorryScanner

    return SorryScanner().scan([mini_project])
