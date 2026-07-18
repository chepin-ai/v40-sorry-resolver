"""Optional LeanDojo verification path, flag-gated (SPEC 3.8).

Status (see ``env_report.md`` §Dojo 阻塞): the interactive ``run_tac`` path is
**blocked upstream** on Lean 4.20 / lean-dojo 4.20.0:
  * lean-dojo/LeanDojo#250 — the ``lean file.lean`` driver redirects fd 0 to
    /dev/null during elaboration, so the REPL tactic gets instant EOF
    (partially mitigated by the FIFO patch in ``patch_lean_dojo.py``);
  * REPL success-path responses written to stdout do not reach the process's
    real fd 1 during elaboration (swallowed);
  * the kernel rejects the anonymous ProofFinished declaration ("restricted to
    the prefix <theorem>") under 4.20's environment-prefix restriction.

Per SPEC 3.8 this verifier therefore **raises a clear error at ``init()``**
advising the subprocess fallback rather than silently degrading. Set
``cfg.dojo_experimental = True`` to force the experimental path (a real, correct
lean-dojo 4.20 API implementation, for when the upstream issues are fixed).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

from ..config import V40Config  # SPEC 3.2 contract (provided by M1)
from ..models import SorryTask  # SPEC 3.1 contract (provided by M1)
from .base import VerificationResult

logger = logging.getLogger(__name__)

PATCH_SCRIPT = Path("/mnt/agents/output/patch_lean_dojo.py")

_UPSTREAM_GUIDANCE = (
    "LeanDojo interactive (run_tac) path is blocked upstream on Lean 4.20 "
    "(lean-dojo/LeanDojo#250 stdin->/dev/null during elaboration; REPL "
    "responses swallowed; rfl收官 kernel error 'restricted to the prefix'). "
    "See env_report.md §Dojo 阻塞. Use V40_VERIFIER=subprocess (the default, "
    "real path). Set dojo_experimental=True to force the experimental Dojo path."
)


class DojoUnavailableError(RuntimeError):
    """Raised when the LeanDojo path cannot honour verification (no silent fallback)."""


class LeanDojoVerifier:
    """lean-dojo based verifier (optional, flag-gated, currently upstream-blocked)."""

    def __init__(self, cfg: V40Config) -> None:
        self._cfg = cfg
        self._timeout = int(getattr(cfg, "lean_timeout_s", 30) or 30)
        self._experimental = bool(getattr(cfg, "dojo_experimental", False))
        self._patch = bool(getattr(cfg, "dojo_apply_patch", True))
        self._ready = False
        self._dojo_mod = None
        self._commits: dict[str, str] = {}  # project_path -> commit cache

    # ------------------------------------------------------------------ API
    async def init(self) -> None:
        if self._patch:
            self._run_patch_script()
        try:
            import lean_dojo  # noqa: F401
        except Exception as exc:  # ImportError or worse
            raise DojoUnavailableError(
                f"lean_dojo is not importable ({exc!r}). {_UPSTREAM_GUIDANCE}"
            ) from exc
        self._dojo_mod = lean_dojo
        if not self._experimental:
            # Honest, explicit failure: the upstream block is documented, so we
            # refuse rather than silently degrade to a non-verifying stub.
            raise DojoUnavailableError(_UPSTREAM_GUIDANCE)
        self._ready = True
        logger.warning(
            "LeanDojoVerifier running in EXPERIMENTAL mode; the run_tac path is "
            "known-broken upstream (see env_report.md). Prefer subprocess."
        )

    async def close(self) -> None:  # re-entrant
        self._ready = False

    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        if not self._ready:
            raise DojoUnavailableError(
                "LeanDojoVerifier.init() did not complete successfully; "
                "the dojo path is unavailable. " + _UPSTREAM_GUIDANCE
            )
        t0 = time.monotonic()
        try:
            ok, diag = await asyncio.wait_for(
                asyncio.to_thread(self._verify_sync, task, proof),
                timeout=self._timeout + 30.0,  # dojo has its own internal timeout
            )
        except asyncio.TimeoutError:
            return VerificationResult(
                ok=False,
                error=f"dojo verification timed out after {self._timeout}s",
                duration_s=time.monotonic() - t0,
            )
        except Exception as exc:  # DojoCrashError etc. -> honest rejection
            return VerificationResult(
                ok=False,
                error=f"dojo error: {type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - t0,
            )
        return VerificationResult(
            ok=ok,
            error=None if ok else "dojo did not reach ProofFinished",
            duration_s=time.monotonic() - t0,
            diagnostics=diag,
        )

    # ------------------------------------------------------------- helpers
    def _run_patch_script(self) -> None:
        if not PATCH_SCRIPT.is_file():
            logger.warning("patch script not found at %s; skipping", PATCH_SCRIPT)
            return
        try:
            subprocess.run(
                [sys.executable, str(PATCH_SCRIPT)],
                check=False, capture_output=True, text=True, timeout=120,
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
                    capture_output=True, text=True, timeout=15,
                )
                if out.returncode == 0:
                    commit = out.stdout.strip()
            except Exception:
                pass
        self._commits[project_path] = commit
        return commit

    def _verify_sync(self, task: SorryTask, proof: str) -> tuple[bool, str]:
        """Real lean-dojo 4.20 API interaction (experimental path only)."""
        from lean_dojo import Dojo, LeanGitRepo, ProofFinished, Theorem

        commit = self._commit_for(task.project_path)
        # Local path -> GitPython transport (no GitHub API), per env_report.
        repo = LeanGitRepo(task.project_path, commit)
        theorem = Theorem(repo, Path(task.file_path), task.theorem_name)
        tactics = [t.strip() for t in proof.split("\n") if t.strip()]
        # Strip a single leading `by` (the theorem already opens a tactic block).
        if tactics and tactics[0] == "by":
            tactics = tactics[1:]
        elif tactics and tactics[0].startswith("by "):
            tactics[0] = tactics[0][3:].strip()

        diag: list[str] = []
        # build_deps=False per env_report (True would trace the whole toolchain).
        with Dojo(theorem, timeout=self._timeout, build_deps=False) as (dojo, state):
            for tac in tactics:
                res = dojo.run_tac(state, tac)
                diag.append(f"{tac!r} -> {type(res).__name__}")
                if isinstance(res, ProofFinished):
                    return True, "\n".join(diag)
                if not hasattr(res, "pp"):  # LeanError / TimeoutError etc.
                    return False, "\n".join(diag + [repr(res)])
                state = res
        return False, "\n".join(diag + ["goals remained"])
