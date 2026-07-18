"""LeanDojo interactive verification path v2 (tactic-level, state-level).

Status: **working** on Lean 4.20.0 + lean-dojo 4.20.0 with the local patch set
in ``/mnt/agents/output/patch_lean_dojo.py`` (bidirectional FIFO IPC +
``unlockAsync`` kernel check). Root-cause chain and e2e evidence:
``/mnt/agents/output/dojo_breakthrough.md``.

This is the second verification channel beyond whole-file subprocess
compilation (``subprocess_lean.py``):

* ``verify_proof(task, proof)`` — SPEC 3.6 ``Verifier`` protocol: split the
  candidate proof into tactics, drive an interactive Dojo session, accept iff
  a kernel-checked ``ProofFinished`` is reached (the REPL re-checks the proof
  term with the Lean kernel before reporting "no goals").
* ``open_task(task)`` / ``run_tactic(task, state_id, tactic)`` — tactic-level
  interface for search/agents: one tactic at a time against a tactic-state id,
  returning the new state (goals), a finished flag, or a structured error.

Sessions are OS processes (``lake env lean`` on a REPL-instrumented copy of
the theorem file, requests/responses over FIFOs). They are opened lazily per
task, reused across ``run_tactic`` calls, and torn down by ``close()``.

Coexists with ``dojo.py`` (the old, upstream-blocked path kept for reference).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import V40Config
from ..models import SorryTask
from .base import VerificationResult

logger = logging.getLogger(__name__)

PATCH_SCRIPT = Path("/mnt/agents/output/patch_lean_dojo.py")


class DojoV2UnavailableError(RuntimeError):
    """Raised when the interactive dojo path cannot honour verification."""


@dataclass
class TacticStepResult:
    """Result of one tactic-level interaction step."""

    ok: bool  # tactic applied without error
    finished: bool = False  # kernel-checked ProofFinished reached
    given_up: bool = False  # proof contained `sorry` (ProofGivenUp)
    state_id: int = -1  # new tactic-state id (valid when ok and not finished)
    goals: str = ""  # pretty-printed goals of the new state
    error: Optional[str] = None
    duration_s: float = 0.0


class _DojoSession:
    """One open interactive Dojo session for a single theorem (sync, 1 process)."""

    def __init__(self, theorem, timeout: int):
        from lean_dojo import Dojo  # lazy: heavy optional dependency

        self._cm = Dojo(theorem, timeout=timeout, build_deps=False)
        self.dojo, self.init_state = self._cm.__enter__()
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._cm.__exit__(None, None, None)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.warning("dojo session teardown raised %r", exc)


class LeanDojoV2Verifier:
    """Interactive lean-dojo verifier (SPEC 3.6) + tactic-level interface."""

    def __init__(self, cfg: V40Config) -> None:
        self._cfg = cfg
        # Dojo's own timeout bounds a single interaction; keep it generous
        # because session start elaborates the instrumented file (~1s warm).
        self._timeout = max(int(getattr(cfg, "lean_timeout_s", 30) or 30), 30)
        self._patch = bool(getattr(cfg, "dojo_apply_patch", True))
        self._ready = False
        self._commits: dict[str, str] = {}
        self._sessions: dict[str, _DojoSession] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- Verifier
    async def init(self) -> None:
        """Apply the lean-dojo patches and check the toolchain (SPEC 3.8).

        Raises ``DojoV2UnavailableError`` with a clear message when the path
        is unavailable; never silently degrades.
        """
        if self._patch:
            await asyncio.to_thread(self._run_patch_script)
        try:
            import lean_dojo  # noqa: F401
        except Exception as exc:  # ImportError or worse
            raise DojoV2UnavailableError(
                f"lean_dojo is not importable ({exc!r}). Run "
                f"/mnt/agents/output/bootstrap_lean_env.sh first."
            ) from exc
        if not (Path.home() / ".elan" / "bin" / "lean").exists() and not _which(
            "lean"
        ):
            raise DojoV2UnavailableError(
                "no `lean` binary on PATH or ~/.elan/bin; run "
                "/mnt/agents/output/bootstrap_lean_env.sh first."
            )
        self._ready = True

    async def close(self) -> None:  # re-entrant
        self._ready = False
        sessions, self._sessions = self._sessions, {}
        for key, sess in sessions.items():
            try:
                await asyncio.to_thread(sess.close)
            except Exception as exc:  # noqa: BLE001
                logger.warning("closing dojo session %s raised %r", key, exc)

    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        """Accept iff ``proof`` drives the theorem to kernel-checked ProofFinished."""
        if not self._ready:
            raise DojoV2UnavailableError(
                "LeanDojoV2Verifier.init() did not complete successfully."
            )
        t0 = time.monotonic()
        try:
            ok, diag = await asyncio.wait_for(
                asyncio.to_thread(self._verify_sync, task, proof),
                timeout=self._timeout * 3 + 60.0,
            )
        except asyncio.TimeoutError:
            await self._drop_session(task)
            return VerificationResult(
                ok=False,
                error=f"dojo_v2 verification timed out ({self._timeout}s/interaction)",
                duration_s=time.monotonic() - t0,
            )
        except Exception as exc:  # DojoCrashError etc. -> honest rejection
            await self._drop_session(task)
            return VerificationResult(
                ok=False,
                error=f"dojo_v2 error: {type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - t0,
            )
        return VerificationResult(
            ok=ok,
            error=None if ok else "dojo_v2 did not reach ProofFinished",
            duration_s=time.monotonic() - t0,
            diagnostics=diag,
        )

    # ------------------------------------------------- tactic-level interface
    async def open_task(self, task: SorryTask) -> TacticStepResult:
        """Open (or return) the interactive session for ``task``; initial goals."""
        if not self._ready:
            raise DojoV2UnavailableError(
                "LeanDojoV2Verifier.init() did not complete successfully."
            )
        t0 = time.monotonic()
        async with self._lock:
            sess = await asyncio.to_thread(self._session_for, task)
        st = sess.init_state
        return TacticStepResult(
            ok=True,
            state_id=st.id,
            goals=st.pp,
            duration_s=time.monotonic() - t0,
        )

    async def run_tactic(
        self, task: SorryTask, state_id: int, tactic: str
    ) -> TacticStepResult:
        """Apply one ``tactic`` to tactic-state ``state_id`` of ``task``'s session."""
        if not self._ready:
            raise DojoV2UnavailableError(
                "LeanDojoV2Verifier.init() did not complete successfully."
            )
        t0 = time.monotonic()
        try:
            async with self._lock:
                sess = await asyncio.to_thread(self._session_for, task)
                res = await asyncio.wait_for(
                    asyncio.to_thread(self._run_tac_sync, sess, state_id, tactic),
                    timeout=self._timeout + 15.0,
                )
        except asyncio.TimeoutError:
            await self._drop_session(task)
            return TacticStepResult(
                ok=False,
                error=f"tactic timed out after {self._timeout}s",
                duration_s=time.monotonic() - t0,
            )
        except Exception as exc:  # crash -> honest error, poisoned session dropped
            await self._drop_session(task)
            return TacticStepResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - t0,
            )
        res.duration_s = time.monotonic() - t0
        return res

    # ------------------------------------------------------------- helpers
    def _run_patch_script(self) -> None:
        if not PATCH_SCRIPT.is_file():
            logger.warning("patch script not found at %s; skipping", PATCH_SCRIPT)
            return
        try:
            subprocess.run(
                [sys.executable, str(PATCH_SCRIPT)],
                check=False,
                capture_output=True,
                text=True,
                timeout=900,  # may rebuild Lean4Repl in trace caches
            )
        except Exception as exc:  # patch is best-effort; idempotent
            logger.warning("patch_lean_dojo.py failed (continuing): %r", exc)

    def _commit_for(self, project_path: str) -> str:
        if project_path in self._commits:
            return self._commits[project_path]
        commit = "HEAD"
        try:
            import git

            commit = git.Repo(project_path).head.commit.hexsha
        except Exception:
            try:
                out = subprocess.run(
                    ["git", "-C", project_path, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if out.returncode == 0:
                    commit = out.stdout.strip()
            except Exception:
                pass
        self._commits[project_path] = commit
        return commit

    def _theorem_for(self, task: SorryTask):
        from lean_dojo import LeanGitRepo, Theorem

        commit = self._commit_for(task.project_path)
        # Local path -> GitPython transport (no GitHub API), per env_report.
        repo = LeanGitRepo(task.project_path, commit)
        return Theorem(repo, Path(task.file_path), task.theorem_name)

    def _session_for(self, task: SorryTask) -> _DojoSession:
        key = task.cache_key()
        sess = self._sessions.get(key)
        if sess is None or sess.closed:
            sess = _DojoSession(self._theorem_for(task), self._timeout)
            self._sessions[key] = sess
        return sess

    async def _drop_session(self, task: SorryTask) -> None:
        sess = self._sessions.pop(task.cache_key(), None)
        if sess is not None:
            try:
                await asyncio.to_thread(sess.close)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _run_tac_sync(sess: _DojoSession, state_id: int, tactic: str) -> TacticStepResult:
        from lean_dojo.interaction.dojo import (
            LeanError,
            ProofFinished,
            ProofGivenUp,
            TacticState,
        )

        # run_tac requires a TacticState carrying the sid; the REPL only uses
        # the id, so rehydrate a minimal state object for non-initial states.
        state = _state_with_id(sess, state_id)
        res = sess.dojo.run_tac(state, tactic)
        if isinstance(res, ProofFinished):
            return TacticStepResult(ok=True, finished=True, state_id=res.tactic_state_id)
        if isinstance(res, ProofGivenUp):
            return TacticStepResult(
                ok=False, given_up=True, error="proof contains `sorry`"
            )
        if isinstance(res, LeanError):
            return TacticStepResult(ok=False, error=res.error)
        if isinstance(res, TacticState):
            return TacticStepResult(ok=True, state_id=res.id, goals=res.pp)
        return TacticStepResult(ok=False, error=f"unexpected result: {res!r}")

    def _verify_sync(self, task: SorryTask, proof: str) -> tuple[bool, str]:
        """Interactive whole-proof verification (one session per call).

        Two attempts, each from the initial state:
          1. the whole proof as a single tactic (handles `with`-block
             combinators such as ``induction xs with | nil => ...``);
          2. line-by-line sequential tactics.
        Tactic application is atomic on failure (the REPL commits only on
        success), so retrying from the initial state is sound.
        """
        diag: list[str] = []
        with _fresh_session(self._theorem_for(task), self._timeout) as sess:
            whole = _strip_by(proof)
            if whole:
                res = self._run_tac_sync(sess, sess.init_state.id, whole)
                diag.append(f"<whole> -> ok={res.ok} finished={res.finished}")
                if res.finished:
                    return True, "\n".join(diag)
            state_id = sess.init_state.id
            for tac in _split_tactics(proof):
                res = self._run_tac_sync(sess, state_id, tac)
                diag.append(f"{tac!r} -> ok={res.ok} finished={res.finished}")
                if res.finished:
                    return True, "\n".join(diag)
                if not res.ok:
                    return False, "\n".join(diag + [res.error or "?"])
                state_id = res.state_id
        return False, "\n".join(diag + ["goals remained"])


def _which(name: str) -> Optional[str]:
    from shutil import which

    return which(name)


def _state_with_id(sess: _DojoSession, state_id: int):
    """Rehydrate a lean-dojo TacticState shell for ``state_id``.

    ``Dojo.run_tac`` type-checks ``isinstance(state, TacticState)`` and then
    only reads ``state.id``, so a shell with the right id is sufficient. The
    pretty text is irrelevant to the REPL protocol.
    """
    from lean_dojo.interaction.dojo import TacticState

    if state_id == sess.init_state.id:
        return sess.init_state
    # TacticState(pp, id); pp "?" parses to zero goals which is fine because
    # only `.id` is consumed by run_tac.
    return TacticState("⊢ unknown", state_id)


class _fresh_session:
    """Context manager: a short-lived session for one verify_proof call."""

    def __init__(self, theorem, timeout: int):
        self._theorem = theorem
        self._timeout = timeout

    def __enter__(self) -> _DojoSession:
        self._sess = _DojoSession(self._theorem, self._timeout)
        return self._sess

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._sess.close()


def _strip_by(proof: str) -> str:
    """Strip a single leading ``by`` keyword from a proof script."""
    text = proof.strip()
    if text == "by":
        return ""
    if text.startswith("by\n"):
        return text[3:]
    if text.startswith("by "):
        return text[3:]
    return text


def _split_tactics(proof: str) -> list[str]:
    """Split a candidate proof into per-line tactics for sequential run_tac.

    Strips a single leading ``by``. Multi-line tactic combinators (e.g.
    ``induction ... with | nil => ...``) are NOT handled here; the caller
    tries the whole script as one tactic first and only falls back to this
    line-by-line split.
    """
    return [ln.strip() for ln in _strip_by(proof).split("\n") if ln.strip()]
