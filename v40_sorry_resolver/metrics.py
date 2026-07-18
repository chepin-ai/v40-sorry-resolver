"""Metrics collection for the v40 sorry resolver (M1).

Contract: SPEC.md section 3.5. Coroutine-safe counters guarded by an
``asyncio.Lock`` plus bounded per-series latency samples (10k). The global
collector is created lazily (no import-time side effects).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Optional

__all__ = ["MetricsCollector", "get_global_metrics", "reset_global_metrics"]

#: Max latency samples kept per series (SPEC 3.5).
MAX_SAMPLES_PER_SERIES = 10_000


def _percentile(sorted_samples, q: float) -> float:
    """Nearest-rank percentile over an already-sorted sequence."""
    if not sorted_samples:
        return 0.0
    n = len(sorted_samples)
    if n == 1:
        return float(sorted_samples[0])
    rank = q * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return float(sorted_samples[lo] * (1.0 - frac) + sorted_samples[hi] * frac)


class MetricsCollector:
    """Coroutine-safe counters + bounded latency samples.

    Per provider / per phase: throughput, latency p50/p95, tokens, success
    rate (SPEC 3.5).
    """

    def __init__(self, max_samples: int = MAX_SAMPLES_PER_SERIES) -> None:
        self._lock = asyncio.Lock()
        self._max_samples = max_samples
        self._started_at = time.time()
        # LLM series, keyed by provider name.
        self._llm: dict[str, dict[str, Any]] = {}
        # Task series, keyed by dimension value.
        self._task_by_status: dict[str, int] = {}
        self._task_by_solver: dict[str, int] = {}
        self._task_by_phase: dict[str, int] = {}
        self._tasks_processed = 0
        self._tasks_succeeded = 0
        self._task_tokens = 0
        self._task_latencies: deque = deque(maxlen=max_samples)

    def _llm_entry(self, provider: str) -> dict[str, Any]:
        entry = self._llm.get(provider)
        if entry is None:
            entry = {
                "calls": 0,
                "errors": 0,
                "cache_hits": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latencies": deque(maxlen=self._max_samples),
            }
            self._llm[provider] = entry
        return entry

    async def record_llm_call(
        self,
        provider: str,
        model: str,
        latency_s: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        success: bool = True,
        from_cache: bool = False,
        error: Optional[str] = None,
        role: str = "",
    ) -> None:
        """Record one LLM call (provider/model/latency/tokens/outcome)."""
        del model, role  # model/role are accepted for forward compatibility
        async with self._lock:
            entry = self._llm_entry(provider)
            entry["calls"] += 1
            entry["latencies"].append(float(latency_s))
            entry["prompt_tokens"] += int(prompt_tokens)
            entry["completion_tokens"] += int(completion_tokens)
            if from_cache:
                entry["cache_hits"] += 1
            if not success or error:
                entry["errors"] += 1

    async def record_task(
        self,
        task_id: str,
        status: str,
        solver: str = "",
        duration_s: float = 0.0,
        tokens_used: int = 0,
        success: bool = False,
        phase: str = "",
    ) -> None:
        """Record one finished task (per status/solver/phase throughput)."""
        del task_id
        async with self._lock:
            self._tasks_processed += 1
            if success:
                self._tasks_succeeded += 1
            self._task_tokens += int(tokens_used)
            self._task_latencies.append(float(duration_s))
            if status:
                self._task_by_status[status] = self._task_by_status.get(status, 0) + 1
            if solver:
                self._task_by_solver[solver] = self._task_by_solver.get(solver, 0) + 1
            if phase:
                self._task_by_phase[phase] = self._task_by_phase.get(phase, 0) + 1

    def _snapshot_unlocked(self) -> dict:
        llm_out: dict[str, dict[str, Any]] = {}
        for provider, entry in self._llm.items():
            lat = sorted(entry["latencies"])
            calls = entry["calls"]
            llm_out[provider] = {
                "calls": calls,
                "errors": entry["errors"],
                "error_rate": (entry["errors"] / calls) if calls else 0.0,
                "cache_hits": entry["cache_hits"],
                "prompt_tokens": entry["prompt_tokens"],
                "completion_tokens": entry["completion_tokens"],
                "total_tokens": entry["prompt_tokens"] + entry["completion_tokens"],
                "latency_p50_s": _percentile(lat, 0.50),
                "latency_p95_s": _percentile(lat, 0.95),
            }
        task_lat = sorted(self._task_latencies)
        processed = self._tasks_processed
        return {
            "uptime_s": time.time() - self._started_at,
            "llm": llm_out,
            "tasks": {
                "processed": processed,
                "succeeded": self._tasks_succeeded,
                "success_rate": (self._tasks_succeeded / processed) if processed else 0.0,
                "total_tokens": self._task_tokens,
                "by_status": dict(self._task_by_status),
                "by_solver": dict(self._task_by_solver),
                "by_phase": dict(self._task_by_phase),
                "latency_p50_s": _percentile(task_lat, 0.50),
                "latency_p95_s": _percentile(task_lat, 0.95),
            },
        }

    async def snapshot(self) -> dict:
        """Return a point-in-time dict snapshot (includes p50/p95)."""
        async with self._lock:
            return self._snapshot_unlocked()

    def render_table(self, snapshot: Optional[dict] = None) -> str:
        """Render an aligned plain-text table.

        If ``snapshot`` is None, one is computed from current internal state
        (best-effort, lock-free; call :meth:`snapshot` for a consistent view).
        """
        if snapshot is None:
            snapshot = self._snapshot_unlocked()
        lines: list[str] = []
        lines.append(f"=== v40 metrics (uptime {snapshot['uptime_s']:.1f}s) ===")
        header = f"{'provider':<16} {'calls':>6} {'errors':>6} {'p50(s)':>8} {'p95(s)':>8} {'tokens':>10} {'cache':>6}"
        lines.append(header)
        lines.append("-" * len(header))
        for provider in sorted(snapshot["llm"]):
            row = snapshot["llm"][provider]
            lines.append(
                f"{provider:<16} {row['calls']:>6} {row['errors']:>6} "
                f"{row['latency_p50_s']:>8.2f} {row['latency_p95_s']:>8.2f} "
                f"{row['total_tokens']:>10} {row['cache_hits']:>6}"
            )
        tasks = snapshot["tasks"]
        lines.append("-" * len(header))
        lines.append(
            f"tasks processed={tasks['processed']} succeeded={tasks['succeeded']} "
            f"success_rate={tasks['success_rate']:.1%} "
            f"p50={tasks['latency_p50_s']:.2f}s p95={tasks['latency_p95_s']:.2f}s "
            f"tokens={tasks['total_tokens']}"
        )
        if tasks["by_status"]:
            by_status = ", ".join(
                f"{k}={v}" for k, v in sorted(tasks["by_status"].items())
            )
            lines.append(f"by_status: {by_status}")
        if tasks["by_solver"]:
            by_solver = ", ".join(
                f"{k}={v}" for k, v in sorted(tasks["by_solver"].items())
            )
            lines.append(f"by_solver: {by_solver}")
        if tasks["by_phase"]:
            by_phase = ", ".join(
                f"{k}={v}" for k, v in sorted(tasks["by_phase"].items())
            )
            lines.append(f"by_phase: {by_phase}")
        return "\n".join(lines)


_global_metrics: Optional[MetricsCollector] = None


def get_global_metrics() -> MetricsCollector:
    """Lazily create the process-wide collector (no import-time side effects)."""
    global _global_metrics
    if _global_metrics is None:
        _global_metrics = MetricsCollector()
    return _global_metrics


def reset_global_metrics() -> MetricsCollector:
    """Replace the global collector (test isolation helper)."""
    global _global_metrics
    _global_metrics = MetricsCollector()
    return _global_metrics
