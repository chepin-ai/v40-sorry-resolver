"""v40 sorry resolver CLI (SPEC 3.13) — every flag is really wired.

Fixes v39 P1-7: CLI arguments that were parsed then silently ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from v40_sorry_resolver.config import V40Config
from v40_sorry_resolver.llm.router import MultiLLMRouter, Role
from v40_sorry_resolver.llm.client import LLMResponse
from v40_sorry_resolver.verify.base import build_verifier
from v40_sorry_resolver.cache import Cache
from v40_sorry_resolver.checkpoint import Checkpoint
from v40_sorry_resolver.metrics import MetricsCollector
from v40_sorry_resolver.sorrydb import SorryScanner, SorryDBClient
from v40_sorry_resolver.progress import LeanProgressV2
from v40_sorry_resolver.engine import maybe_await
from v40_sorry_resolver.engine.orchestrator import ResolutionPipeline, StrategyConfig

logger = logging.getLogger("v40.cli")

KAGGLE_WORK_DIR = "/kaggle/working/v40_work"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="v40_sorry_resolver",
        description="v40 async Lean 4 sorry resolution engine",
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--project-paths",
        "--project",
        dest="project_paths",
        action="append",
        default=None,
        metavar="PATH",
        help="Lean project root(s); repeatable (`--project` is an alias). "
        "Overrides config lean_project_paths.",
    )
    source.add_argument(
        "--sorrydb",
        default=None,
        metavar="PATH_OR_URL",
        help="SorryDB snapshot (local JSON/JSONL file or http(s) URL) as the "
        "task source instead of scanning local project paths. Mutually "
        "exclusive with --project-paths.",
    )
    p.add_argument("--workers", type=int, default=None, help="parallel worker count")
    p.add_argument(
        "--verifier",
        choices=["subprocess", "dojo", "mock"],
        default=None,
        help="verification backend",
    )
    p.add_argument(
        "--wall-clock-budget",
        type=float,
        default=None,
        metavar="SECONDS",
        help="global wall-clock budget",
    )
    p.add_argument(
        "--task-limit", type=int, default=None, help="cap number of tasks solved"
    )
    resume = p.add_mutually_exclusive_group()
    resume.add_argument("--resume", dest="resume", action="store_true", default=True)
    resume.add_argument("--no-resume", dest="resume", action="store_false")
    p.add_argument(
        "--output-dir", default=None, help="work_dir for cache/checkpoint/results"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="scan tasks + health check only; no solving",
    )
    p.add_argument(
        "--mock-llm",
        action="store_true",
        help="use deterministic fake LLM for ALL roles (testing; never mixed "
        "with real provider keys)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


# --------------------------------------------------------------------- mock


class _MockLLMClient:
    """Deterministic in-process fake used by --mock-llm (tests only)."""

    def __init__(self, role: Role):
        self.role = role
        self.calls = 0

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        thinking: bool = False,
        cache_key: Optional[str] = None,
    ) -> LLMResponse:
        self.calls += 1
        blob = f"{system_prompt or ''}\n{prompt}".lower()
        if "lesson" in blob or ("critic" in blob and "diagnostics" in blob):
            text = "strategy: previous attempt failed; try a simpler tactic"
        elif "review" in blob:
            text = '{"approved": true, "reason": "mock review ok"}'
        elif self.role == Role.ORCHESTRATOR or "orchestrator" in blob:
            text = (
                '{"tactic_search_depth": 4, "tactic_search_width": 2, '
                '"agentic_max_iterations": 8, "enable_thinking": false, '
                '"explorer_share": 0.3, "rationale": "mock plan"}'
            )
        else:  # PROVER / EXPLORER propose real Lean code
            text = "```lean\nrfl\n```"
        return LLMResponse(
            text=text,
            model="mock-llm",
            provider="mock",
            prompt_tokens=len(blob.split()),
            completion_tokens=len(text.split()),
            latency_s=0.0,
        )

    async def health_check(self) -> bool:
        return True

    def stats(self) -> dict:
        return {"calls": self.calls, "errors": 0, "provider": "mock"}

    async def close(self) -> None:
        return None


class _MockRouter:
    """Router façade where every role maps to a deterministic fake client."""

    def __init__(self):
        self._clients = {role: _MockLLMClient(role) for role in Role}

    def client(self, role: Role) -> _MockLLMClient:
        return self._clients[role]

    async def health_check_all(self) -> dict:
        return {"mock": True}

    def available_roles(self) -> list:
        return list(self._clients)

    def report(self) -> str:
        return "mock router (--mock-llm): all roles deterministic fakes"


# -------------------------------------------------------------------- helpers


def _construct(cls, *arg_candidates):
    """Try constructor signatures in order; integration-tolerant."""
    for args in arg_candidates:
        try:
            return cls(*args)
        except TypeError:
            continue
    return cls()


def _has_real_keys(cfg) -> bool:
    providers = getattr(cfg, "providers", {}) or {}
    return any(
        bool(getattr(p, "api_key", "")) and getattr(p, "enabled", True)
        for p in providers.values()
    )


# ----------------------------------------------------------------------- main


async def async_main(args: argparse.Namespace) -> int:
    cfg = V40Config.from_env()

    # Real wiring of CLI overrides (v39 P1-7 fix).
    if args.project_paths:
        cfg.lean_project_paths = list(args.project_paths)
    if args.workers is not None:
        cfg.num_workers = max(1, args.workers)
    if args.verifier is not None:
        cfg.verifier = args.verifier
    if args.wall_clock_budget is not None:
        cfg.wall_clock_budget_s = float(args.wall_clock_budget)
    if args.output_dir:
        cfg.work_dir = args.output_dir
    elif os.path.isdir("/kaggle"):
        cfg.work_dir = KAGGLE_WORK_DIR
    os.makedirs(cfg.work_dir, exist_ok=True)

    problems = []
    if hasattr(cfg, "validate"):
        problems = cfg.validate() or []
    for prob in problems:
        logger.warning("config: %s", prob)

    # One MetricsCollector shared by the router's clients and the pipeline
    # (N-2/BUG-4: previously the pipeline used a fresh collector while clients
    # recorded into the global one, so run metrics were always empty).
    metrics = MetricsCollector()

    if args.mock_llm:
        if _has_real_keys(cfg):
            logger.warning(
                "--mock-llm active: real provider keys are IGNORED "
                "(mock and real providers are never mixed)"
            )
        router = _MockRouter()
    else:
        cache_for_router = _construct(
            Cache, (os.path.join(cfg.work_dir, "cache.db"),), (cfg.work_dir,), ()
        )
        router = MultiLLMRouter.from_config(cfg, cache_for_router, metrics=metrics)

    # Task source: a SorryDB snapshot (--sorrydb) or a local-project scan.
    scan_stats: dict = {}
    if args.sorrydb:
        cfg.sorrydb_endpoint = args.sorrydb
        tasks = await SorryDBClient.load(args.sorrydb)
        scan_stats = {"entries": len(tasks)}
        if tasks:
            logger.info("loaded %d sorry tasks from SorryDB %s", len(tasks), args.sorrydb)
    else:
        scanner = _construct(SorryScanner, (cfg,), (cfg.lean_project_paths,), ())
        tasks = await maybe_await(scanner.scan(cfg.lean_project_paths))
        scan_stats = dict(getattr(scanner, "last_stats", {}) or {})
        if tasks:
            logger.info("scanned %d sorry tasks", len(tasks))

    if not tasks:
        # 0 sorries is a *legitimate* outcome (mathlib CI enforces zero
        # sorries), so exit 0 with a friendly explanation + project stats —
        # no health check, no verifier init, no scary warnings.
        print(
            "该项目未发现 sorry（若目标是 mathlib 等 CI 强制无 sorry 的库属正常）"
        )
        if args.sorrydb:
            print(f"项目统计: SorryDB 条目数={scan_stats.get('entries', 0)}")
        else:
            print(
                "项目统计: "
                f"扫描文件数={scan_stats.get('files', 0)} "
                f"定理/声明数={scan_stats.get('declarations', 0)}"
            )
        return 0

    if args.dry_run:
        health = await router.health_check_all()
        print(f"[dry-run] tasks={len(tasks)} verifier={cfg.verifier}")
        for t in tasks[:20]:
            print(
                f"  - {t.id} {t.file_path}:{t.line_number} {t.theorem_name} "
                f"prio={getattr(t.priority, 'name', t.priority)}"
            )
        print(f"[dry-run] llm health: {health}")
        return 0

    if not args.mock_llm:
        # Startup health gate (SPEC 0.2, N-4/BUG-3): probe every provider
        # with a real 1-token chat completion *before* solving. The router
        # disables failing providers; if nothing survives, refuse to run.
        health = await router.health_check_all()
        for name, ok in health.items():
            if not ok:
                logger.warning(
                    "provider '%s' failed the startup health check; disabled", name
                )
        if not health or not any(health.values()):
            logger.error(
                "no LLM provider passed the startup health check; aborting "
                "(check API keys/endpoints in .env, or use --mock-llm for tests)"
            )
            return 2

    # Priority prediction (LeanProgress-v2).
    try:
        progress = _construct(LeanProgressV2, (None,), ())
        tasks = await maybe_await(progress.predict(tasks))
    except Exception as exc:
        logger.warning("priority prediction unavailable (%s); default order", exc)

    if args.task_limit is not None:
        tasks = tasks[: max(0, args.task_limit)]
        logger.info("task-limit -> %d tasks", len(tasks))

    cache = _construct(
        Cache, (os.path.join(cfg.work_dir, "cache.db"),), (cfg.work_dir,), ()
    )
    checkpoint = _construct(
        Checkpoint, (os.path.join(cfg.work_dir, "checkpoint.json"),), (cfg.work_dir,), ()
    )
    verifier = build_verifier(cfg)

    try:
        await maybe_await(verifier.init())
    except Exception as exc:
        logger.error("verifier init failed: %s", exc)
        return 2

    strategy = StrategyConfig.from_config(cfg)
    pipeline = ResolutionPipeline(
        cfg, router, verifier, cache, checkpoint, metrics, strategy
    )
    try:
        report = await pipeline.run(tasks, resume=args.resume)
    finally:
        for closeable in (verifier, cache):
            try:
                await maybe_await(closeable.close())
            except Exception:
                pass
        if hasattr(router, "close"):
            try:
                await maybe_await(router.close())
            except Exception:
                pass

    print(report.render_summary())
    if getattr(cfg, "verifier", "") == "mock":
        print("[UNVERIFIED] mock verifier was used; solved counts are not real")
    return 0


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
