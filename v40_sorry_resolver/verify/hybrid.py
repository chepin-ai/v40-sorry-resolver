"""Hybrid dual-channel verifier (roadmap item 1, backend ``hybrid``).

Two complementary verification channels cross-check each other:

  * **Tactic-level probing** (goal states, step-by-step search) goes through
    the resident ``ReplPool`` — cheap interactive ``run_tac`` calls with the
    import head already elaborated in the REPL environment.
  * **Final judgement** (``verify_proof``) goes through
    ``SubprocessLeanVerifier`` whole-file compilation — the production path,
    immune to REPL-protocol drift (patched FIFO protocol, ProofFinished
    semantics). The REPL pool's verdict is also computed as a *witness*:
    agreement between the two channels is evidence against protocol drift;
    disagreement never changes the subprocess verdict (it is appended to
    diagnostics and logged as a WARNING) because whole-file compilation is
    the ground truth.

The subprocess channel is authoritative: a proof counts as solved only when
``lake env lean`` accepts it. This matches the SPEC invariant that every
"success" must pass the unified verifier before it is booked.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ..config import V40Config
from ..models import SorryTask
from .base import VerificationResult
from .dojo_v2 import DojoV2UnavailableError, TacticStepResult
from .repl_pool import PooledSession, ReplPoolVerifier
from .subprocess_lean import SubprocessLeanVerifier

logger = logging.getLogger(__name__)


class HybridVerifier:
    """REPL-pool probing + subprocess final judgement (SPEC 3.6 Verifier)."""

    def __init__(self, cfg: V40Config) -> None:
        self._cfg = cfg
        self._sub = SubprocessLeanVerifier(cfg)
        self._repl = ReplPoolVerifier(cfg)
        self._ready = False
        # cross-channel agreement stats (drift detection evidence)
        self.stats: dict[str, int] = {
            "dual_checked": 0,
            "agree": 0,
            "disagree_subprocess_only": 0,
            "disagree_repl_only": 0,
        }

    async def init(self) -> None:
        await self._sub.init()
        await self._repl.init()  # raises DojoV2UnavailableError when unavailable
        self._ready = True

    async def close(self) -> None:  # re-entrant
        self._ready = False
        await asyncio.gather(
            self._sub.close(), self._repl.close(), return_exceptions=True
        )

    # ------------------------------------------------- tactic-level probing
    async def open_task(self, task: SorryTask) -> tuple[PooledSession, TacticStepResult]:
        """Acquire a pooled REPL session for stepwise probing (release it after)."""
        if not self._ready:
            raise DojoV2UnavailableError("HybridVerifier.init() did not complete.")
        return await self._repl.open_task(task)

    async def run_tactic(
        self, sess: PooledSession, state_id: int, tactic: str
    ) -> TacticStepResult:
        """One interactive tactic step on the REPL-pool channel."""
        if not self._ready:
            raise DojoV2UnavailableError("HybridVerifier.init() did not complete.")
        return await self._repl.run_tactic(sess, state_id, tactic)

    async def release_session(self, sess: PooledSession, *, drop: bool = False) -> None:
        """Return a session acquired via :meth:`open_task` to the pool."""
        pool = self._repl.pool_for(sess.key[0])
        await pool.release(sess, drop=drop)

    async def probe_goals(self, task: SorryTask) -> TacticStepResult:
        """Convenience one-shot: initial goal state of ``task`` via the pool."""
        sess, init = await self.open_task(task)
        await self.release_session(sess)
        return init

    # -------------------------------------------------------------- Verifier
    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        """Final judgement: subprocess compilation; REPL pool cross-witness.

        Both channels run concurrently; the subprocess verdict is returned.
        The REPL verdict is appended to ``diagnostics`` and disagreement is
        logged (protocol-drift tripwire) without changing the verdict.
        """
        if not self._ready:
            raise DojoV2UnavailableError("HybridVerifier.init() did not complete.")
        sub_res, repl_res = await asyncio.gather(
            self._sub.verify_proof(task, proof),
            self._repl.verify_proof(task, proof),
        )
        self.stats["dual_checked"] += 1
        if sub_res.ok == repl_res.ok:
            self.stats["agree"] += 1
            witness = f"repl-witness: agree(ok={repl_res.ok})"
        else:
            if sub_res.ok:
                self.stats["disagree_subprocess_only"] += 1
            else:
                self.stats["disagree_repl_only"] += 1
            witness = (
                f"repl-witness: DISAGREE subprocess ok={sub_res.ok} "
                f"repl ok={repl_res.ok} err={repl_res.error}"
            )
            logger.warning(
                "hybrid channel disagreement on %s: subprocess=%s repl=%s (%s)",
                task.theorem_name,
                sub_res.ok,
                repl_res.ok,
                repl_res.error,
            )
        diag = sub_res.diagnostics or ""
        sub_res.diagnostics = (diag + "\n" + witness).strip() if diag else witness
        return sub_res
