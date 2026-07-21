"""LeanInteract verification backend (roadmap item 2, backend ``lean_interact``).

`LeanInteract <https://github.com/augustepoiroux/LeanInteract>`_ (the same
stack the official SorryDB verifier is built on, frontier_resources §3b)
wraps the ``repl`` JSON protocol with async-friendly Python bindings,
incremental+parallel elaboration and per-project environment management.

Design:
  * One ``AutoLeanServer`` per project (lazy; the underlying REPL is built
    once per toolchain into lean_interact's cache).
  * ``verify_proof`` reuses the SPEC 3.7 splice machinery from
    ``SubprocessLeanVerifier`` (blacklist + single-sorry replacement) and
    submits the patched file as one REPL ``Command``. Each command runs in a
    fresh REPL environment (``env=None``), so repeated candidates never hit
    redeclaration errors; incremental elaboration still caches the shared
    prefix, which is where the speedup over cold subprocess compilation
    comes from (measured: ~0.02 s warm vs ~0.26 s cold per candidate on the
    mini project).
  * Acceptance mirrors the subprocess path: no ``error``-severity message
    AND no ``sorry`` reported inside the target theorem block.

Per SPEC 3.8, a missing package or failed initialisation raises
``LeanInteractUnavailableError`` with install instructions — never a silent
fallback.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from ..config import V40Config
from ..models import SorryTask
from .base import VerificationResult
from .subprocess_lean import SubprocessLeanVerifier, VerificationError

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "pip install -i https://pypi.tuna.tsinghua.edu.cn/simple lean-interact "
    "(see frontier_resources.md section 3b; Lean toolchain via elan required)"
)


class LeanInteractUnavailableError(RuntimeError):
    """Raised when the lean-interact backend cannot honour verification."""


class LeanInteractVerifier:
    """SPEC 3.6 Verifier backed by LeanInteract's REPL server."""

    def __init__(self, cfg: V40Config) -> None:
        self._cfg = cfg
        self._timeout = max(float(getattr(cfg, "lean_timeout_s", 30.0) or 30.0), 30.0)
        # Reuse the SPEC 3.7 splice/blacklist machinery (no subprocesses run).
        self._splicer = SubprocessLeanVerifier(cfg)
        self._ready = False
        self._servers: dict[str, object] = {}  # project_path -> AutoLeanServer
        self._server_locks: dict[str, asyncio.Lock] = {}
        self._build_lock = asyncio.Lock()

    # ------------------------------------------------------------------ API
    async def init(self) -> None:
        """Check the optional dependency and the Lean toolchain.

        Raises ``LeanInteractUnavailableError`` with install guidance; never
        silently degrades (SPEC 3.8).
        """
        try:
            import lean_interact  # noqa: F401
        except ImportError as exc:
            raise LeanInteractUnavailableError(
                f"lean-interact is not installed ({exc!r}). Install it with: "
                f"{_INSTALL_HINT}"
            ) from exc
        elan_lake = Path.home() / ".elan" / "bin" / "lake"
        import shutil

        if shutil.which("lake") is None and not elan_lake.exists():
            raise LeanInteractUnavailableError(
                "no `lake` binary on PATH or ~/.elan/bin; run "
                "/mnt/agents/output/bootstrap_lean_env.sh first."
            )
        self._ready = True

    async def close(self) -> None:  # re-entrant
        self._ready = False
        servers, self._servers = list(self._servers.values()), {}
        for server in servers:
            try:
                await asyncio.to_thread(server.kill)
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                logger.warning("lean-interact server kill raised %r", exc)

    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        t0 = time.monotonic()
        if not self._ready:
            raise LeanInteractUnavailableError(
                "LeanInteractVerifier.init() did not complete successfully."
            )
        # 1. Text blacklist first (same semantics as SPEC 3.7.1).
        if SubprocessLeanVerifier._blacklist_hit(proof):
            return VerificationResult(
                ok=False,
                error="proof contains blacklisted keyword (sorry/admit/stop)",
                duration_s=time.monotonic() - t0,
            )
        # 2. Splice the candidate into the theorem block (SPEC 3.7.2).
        try:
            new_content, decl_line1 = self._splicer._splice(task, proof)
            block_end1 = self._block_end1(new_content, decl_line1)
        except VerificationError as exc:
            return VerificationResult(
                ok=False, error=str(exc), duration_s=time.monotonic() - t0
            )
        # 3. Run the patched file through the REPL (fresh env per candidate).
        try:
            server = await self._server_for(task.project_path)
            lock = self._server_locks.setdefault(task.project_path, asyncio.Lock())
            async with lock:
                resp = await asyncio.wait_for(
                    server.async_run(self._command(new_content), timeout=self._timeout),
                    timeout=self._timeout + 15.0,
                )
        except asyncio.TimeoutError:
            return VerificationResult(
                ok=False,
                error=f"lean-interact timed out after {self._timeout:.1f}s",
                duration_s=time.monotonic() - t0,
            )
        except Exception as exc:  # noqa: BLE001 - honest rejection
            return VerificationResult(
                ok=False,
                error=f"lean-interact error: {type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - t0,
            )
        duration = time.monotonic() - t0

        from lean_interact.interface import LeanError

        if isinstance(resp, LeanError):
            return VerificationResult(
                ok=False,
                error=f"lean-interact protocol error: {resp.message}",
                duration_s=duration,
            )

        # 4. Judgement: no error message, no sorry inside the target block.
        errors = [m for m in resp.messages if m.severity == "error"]
        target_sorry = any(
            s.start_pos is not None
            and decl_line1 <= s.start_pos.line <= block_end1
            for s in (resp.sorries or [])
        )
        ok = not errors and not target_sorry
        error: Optional[str] = None
        if errors:
            error = "; ".join(
                f"line {m.start_pos.line if m.start_pos else '?'}: {m.data[:200]}"
                for m in errors[:3]
            )
        elif target_sorry:
            error = "target theorem still contains sorry"
        diagnostics = "\n".join(
            f"[{m.severity}] {m.data[:200]}" for m in resp.messages[:20]
        )
        return VerificationResult(
            ok=ok,
            error=error,
            duration_s=duration,
            remaining_sorries=len(resp.sorries or []),
            diagnostics=diagnostics[-1200:],
        )

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _command(content: str):
        from lean_interact.interface import Command

        return Command(cmd=content)  # env=None -> fresh session per candidate

    @staticmethod
    def _block_end1(content: str, decl_line1: int) -> int:
        """1-based inclusive end line of the target theorem block."""
        from .subprocess_lean import _strip_comments

        code_lines = _strip_comments(content).split("\n")
        end0 = SubprocessLeanVerifier._find_block_end(code_lines, decl_line1 - 1)
        # end0 is a 0-based exclusive boundary; inclusive 1-based end line:
        return max(end0, decl_line1)

    async def _server_for(self, project_path: str):
        """Lazily build (or return) the per-project REPL server."""
        server = self._servers.get(project_path)
        if server is not None:
            return server
        async with self._build_lock:
            server = self._servers.get(project_path)
            if server is not None:
                return server
            server = await asyncio.wait_for(
                asyncio.to_thread(self._build_server, project_path),
                timeout=1800.0,  # first-ever REPL build can take minutes
            )
            self._servers[project_path] = server
            return server

    def _build_server(self, project_path: str):
        from lean_interact import AutoLeanServer, LeanREPLConfig, LocalProject

        if not Path(project_path).is_dir():
            raise LeanInteractUnavailableError(
                f"project_path not a directory: {project_path}"
            )
        # Optional REPL git mirror override (GitHub intermittent in some
        # sandboxes; e.g. https://ghfast.top/https://github.com/augustepoiroux/repl).
        # Only consulted on first clone; a warm lean-interact cache reuses the
        # on-disk checkout regardless.
        repl_git = os.environ.get("V40_LEAN_INTERACT_REPL_GIT", "").strip() or None
        try:
            # auto_build=False: the target project is expected to be built
            # already (engine precondition); avoids a FileLock on the project
            # directory, which deadlocks on shared (9p) mounts.
            kwargs = {"repl_git": repl_git} if repl_git else {}
            config = LeanREPLConfig(
                project=LocalProject(directory=project_path, auto_build=False),
                memory_hard_limit_mb=int(
                    getattr(self._cfg, "repl_max_rss_mb", 1500) or 1500
                ),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            raise LeanInteractUnavailableError(
                f"failed to configure lean-interact REPL for {project_path}: "
                f"{type(exc).__name__}: {exc}. Try: {_INSTALL_HINT}; the REPL "
                f"is cloned from github.com/augustepoiroux/repl (use the "
                f"ghfast.top proxy when GitHub is unreachable)."
            ) from exc
        try:
            return AutoLeanServer(config)
        except Exception as exc:  # noqa: BLE001
            raise LeanInteractUnavailableError(
                f"failed to start lean-interact server for {project_path}: "
                f"{type(exc).__name__}: {exc}. Check that `lake build` passes "
                f"in the project and the toolchain matches lean-toolchain."
            ) from exc
