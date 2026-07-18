"""Test fixtures for M3 engine tests.

M1 (models/config/cache/checkpoint/metrics/llm) and M2 (verify/sorrydb/
progress) are developed in parallel. To keep these tests runnable, any
contract module that cannot be imported yet is replaced here by a MINIMAL
stub implementing exactly the SPEC contract surface (sections 3.1-3.9).
Once the real modules land, imports succeed and the stubs are bypassed.

All engine tests use fake verifier / fake LLM per the SPEC contract; no
network access anywhere.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import hashlib
import json
import os
import sys
import types
from dataclasses import dataclass, field
from typing import Optional

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_STUBBED: list[str] = []


def _try_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _register(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module
    _STUBBED.append(name)
    # Attach to parent package when it exists in sys.modules.
    parent, _, attr = name.rpartition(".")
    if parent and parent in sys.modules:
        try:
            setattr(sys.modules[parent], attr, module)
        except Exception:
            pass


def _ensure_package(name: str) -> None:
    if name in sys.modules:
        return
    if _try_import(name):
        return
    mod = types.ModuleType(name)
    mod.__path__ = []  # mark as package
    _register(name, mod)


# ---------------------------------------------------------------- models stub


def _stub_models() -> None:
    name = "v40_sorry_resolver.models"
    if _try_import(name):
        return
    mod = types.ModuleType(name)

    class PriorityLevel(enum.Enum):
        P0_CRITICAL = 0
        P1_IMPORTANT = 1
        P2_MEDIUM = 2
        P3_LOW = 3

    class ProofStatus(enum.Enum):
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
            return hashlib.sha256(raw.encode()).hexdigest()[:16]

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
                "attempts": list(self.attempts),
            }

        @classmethod
        def from_dict(cls, d: dict) -> "SorryTask":
            return cls(
                id=d["id"],
                project_path=d["project_path"],
                file_path=d["file_path"],
                line_number=int(d["line_number"]),
                column_number=int(d.get("column_number", 0)),
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
    _register(name, mod)


# ---------------------------------------------------------------- config stub


def _stub_config() -> None:
    name = "v40_sorry_resolver.config"
    if _try_import(name):
        return
    mod = types.ModuleType(name)

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

        @classmethod
        def from_env(cls, env_file: Optional[str] = ".env") -> "V40Config":
            cfg = cls()
            if os.environ.get("V40_VERIFIER"):
                cfg.verifier = os.environ["V40_VERIFIER"]
            if os.environ.get("V40_NUM_WORKERS"):
                cfg.num_workers = int(os.environ["V40_NUM_WORKERS"])
            return cfg

        def validate(self) -> list:
            return []

    mod.LLMProviderConfig = LLMProviderConfig
    mod.V40Config = V40Config
    _register(name, mod)


# ------------------------------------------------------- cache/checkpoint/metrics


def _stub_cache() -> None:
    name = "v40_sorry_resolver.cache"
    if _try_import(name):
        return
    mod = types.ModuleType(name)

    class Cache:
        def __init__(self, *args, **kwargs):
            self._store: dict = {}

        async def get(self, key, namespace: str = "default"):
            return self._store.get(f"{namespace}:{key}")

        async def set(self, key, value, namespace: str = "default"):
            self._store[f"{namespace}:{key}"] = value

        async def close(self):
            return None

    mod.Cache = Cache
    _register(name, mod)


def _stub_checkpoint() -> None:
    name = "v40_sorry_resolver.checkpoint"
    if _try_import(name):
        return
    mod = types.ModuleType(name)

    def _task_to_dict(t):
        if hasattr(t, "to_dict"):
            return t.to_dict()
        return dict(t)

    def _result_to_dict(r):
        d = {}
        for f in dataclasses.fields(r):
            v = getattr(r, f.name)
            if isinstance(v, enum.Enum):
                v = v.name
            d[f.name] = v
        return d

    class Checkpoint:
        """Atomic-write checkpoint: tmp + os.replace; tolerant load."""

        def __init__(self, path: str):
            self.path = str(path)

        def save(self, tasks, results, phase, metrics):
            payload = {
                "tasks": [_task_to_dict(t) for t in tasks],
                "results": [_result_to_dict(r) for r in results],
                "phase": phase,
                "metrics": {},
            }
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.path)

        def load(self):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return None

    mod.Checkpoint = Checkpoint
    _register(name, mod)


def _stub_metrics() -> None:
    name = "v40_sorry_resolver.metrics"
    if _try_import(name):
        return
    mod = types.ModuleType(name)

    class MetricsCollector:
        def __init__(self):
            self.tasks: list = []
            self.llm_calls: list = []

        def record_llm_call(self, **kwargs):
            self.llm_calls.append(kwargs)

        def record_task(self, **kwargs):
            self.tasks.append(kwargs)

        def snapshot(self) -> dict:
            by_solver: dict = {}
            for t in self.tasks:
                s = t.get("solver", "")
                by_solver[s] = by_solver.get(s, 0) + 1
            return {
                "tasks_total": len(self.tasks),
                "llm_calls": len(self.llm_calls),
                "by_solver": by_solver,
                "by_provider": {},
            }

        def render_table(self) -> str:
            return f"tasks={len(self.tasks)} llm_calls={len(self.llm_calls)}"

    mod.MetricsCollector = MetricsCollector
    _register(name, mod)


# ------------------------------------------------------------------ llm stubs


def _stub_llm() -> None:
    _ensure_package("v40_sorry_resolver.llm")

    name = "v40_sorry_resolver.llm.client"
    if not _try_import(name):
        mod = types.ModuleType(name)

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

        class AsyncLLMClient:  # pragma: no cover - placeholder contract
            async def generate(self, *a, **k) -> LLMResponse:
                raise NotImplementedError

            async def health_check(self) -> bool:
                return False

            def stats(self) -> dict:
                return {}

            async def close(self) -> None:
                return None

        mod.LLMResponse = LLMResponse
        mod.AsyncLLMClient = AsyncLLMClient
        _register(name, mod)

    name = "v40_sorry_resolver.llm.router"
    if not _try_import(name):
        mod = types.ModuleType(name)

        class Role(enum.Enum):
            ORCHESTRATOR = "ORCHESTRATOR"
            PROVER = "PROVER"
            CRITIC = "CRITIC"
            EXPLORER = "EXPLORER"

        ROLE_TO_PROVIDER = {
            "ORCHESTRATOR": "deepseek_a",
            "PROVER": "deepseek_b",
            "CRITIC": "kimi",
            "EXPLORER": "longcat",
        }

        class MultiLLMRouter:  # pragma: no cover - tests use FakeRouter
            @classmethod
            def from_config(cls, cfg, cache):
                return cls()

            def client(self, role):
                raise RuntimeError("no providers configured (stub)")

            async def health_check_all(self):
                return {}

            def available_roles(self):
                return []

            def report(self):
                return "stub router"

        mod.Role = Role
        mod.ROLE_TO_PROVIDER = ROLE_TO_PROVIDER
        mod.MultiLLMRouter = MultiLLMRouter
        _register(name, mod)


# --------------------------------------------------------------- verify stubs


def _stub_verify() -> None:
    _ensure_package("v40_sorry_resolver.verify")

    name = "v40_sorry_resolver.verify.base"
    if not _try_import(name):
        mod = types.ModuleType(name)

        @dataclass
        class VerificationResult:
            ok: bool
            error: Optional[str] = None
            duration_s: float = 0.0
            remaining_sorries: int = -1
            diagnostics: str = ""

        class Verifier:  # Protocol-shaped base
            async def init(self) -> None: ...
            async def verify_proof(self, task, proof) -> VerificationResult: ...
            async def close(self) -> None: ...

        def build_verifier(cfg):
            if getattr(cfg, "verifier", "") == "mock":
                mock_mod = sys.modules.get("v40_sorry_resolver.verify.mock")
                if mock_mod is not None:
                    return mock_mod.MockVerifier()
            raise RuntimeError(f"stub build_verifier: {getattr(cfg, 'verifier', '?')}")

        mod.VerificationResult = VerificationResult
        mod.Verifier = Verifier
        mod.build_verifier = build_verifier
        _register(name, mod)

    name = "v40_sorry_resolver.verify.mock"
    if not _try_import(name):
        mod = types.ModuleType(name)
        base = sys.modules["v40_sorry_resolver.verify.base"]

        class MockVerifier:
            """Test-only: ok iff proof contains the 'VALID' marker."""

            async def init(self) -> None:
                return None

            async def verify_proof(self, task, proof):
                ok = "VALID" in (proof or "")
                return base.VerificationResult(
                    ok=ok,
                    error=None if ok else "VALID marker missing",
                    remaining_sorries=0 if ok else 1,
                )

            async def close(self) -> None:
                return None

        mod.MockVerifier = MockVerifier
        _register(name, mod)


# ------------------------------------------------------- sorrydb/progress stubs


def _stub_sorrydb() -> None:
    name = "v40_sorry_resolver.sorrydb"
    if _try_import(name):
        return
    mod = types.ModuleType(name)

    class SorryScanner:
        def __init__(self, *args, **kwargs):
            pass

        def scan(self):
            return []

    class SorryDBClient:
        def __init__(self, *args, **kwargs):
            pass

        async def fetch(self):
            return []

    mod.SorryScanner = SorryScanner
    mod.SorryDBClient = SorryDBClient
    _register(name, mod)


def _stub_progress() -> None:
    name = "v40_sorry_resolver.progress"
    if _try_import(name):
        return
    mod = types.ModuleType(name)

    class LeanProgressV2:
        def __init__(self, *args, **kwargs):
            pass

        def predict(self, tasks):
            for t in tasks:
                if not t.predicted_steps:
                    t.predicted_steps = 2
                if not t.predicted_success:
                    t.predicted_success = 0.5
            return tasks

    mod.LeanProgressV2 = LeanProgressV2
    _register(name, mod)


def _install_stubs() -> None:
    _stub_models()
    _stub_config()
    _stub_cache()
    _stub_checkpoint()
    _stub_metrics()
    _stub_llm()
    _stub_verify()
    _stub_sorrydb()
    _stub_progress()
    if _STUBBED:
        print(f"\n[conftest] stubbed contract modules: {', '.join(_STUBBED)}")


_install_stubs()

# Re-export contract names for test modules.
from v40_sorry_resolver.models import (  # noqa: E402
    PriorityLevel,
    ProofStatus,
    ResolutionResult,
    SOLVED_STATUSES,
    SorryTask,
)
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
# M2 fixtures (merged): real-module based fixtures for verify/sorrydb/progress
# tests. The stub machinery above auto-bypasses now that real modules exist.
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
