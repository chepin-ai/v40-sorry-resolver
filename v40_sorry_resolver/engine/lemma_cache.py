"""Shared lemma cache (frontier_atp Top-8 #5; BFS-Prover-V2 Planner-Prover
shared Subgoal Cache, arXiv:2509.06493).

A thin goal-indexed layer over the persistent :class:`Cache` (SQLite WAL +
bounded LRU, single-writer queue), so every worker in the pipeline shares the
same store:

- key   = sha256 of the *normalized* goal text (whitespace-collapsed), so
  formatting differences across tasks do not fragment the cache;
- value = JSON ``{"proof": str, "meta": {...}, "ts": float}``.

Written whenever a sub-lemma / direct / search / agentic proof verifies;
consulted *before* any proving attempt — a hit short-circuits the whole
solving flow (subject to the pipeline's mandatory re-verification, v39 P0-3).
All failures degrade to a miss / dropped write with a WARNING: the cache must
never block the solving flow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("v40.lemma_cache")

_WS_RE = re.compile(r"\s+")


class LemmaCache:
    """Goal -> verified-proof store shared by all workers/phases."""

    NAMESPACE = "lemma_cache"

    def __init__(self, cache, namespace: str = NAMESPACE) -> None:
        self._cache = cache
        self._ns = str(namespace or self.NAMESPACE)

    # ------------------------------------------------------------- keys
    @staticmethod
    def normalize_goal(goal: str) -> str:
        """Whitespace-collapsed, stripped goal text (cache canonical form)."""
        return _WS_RE.sub(" ", goal or "").strip()

    @classmethod
    def key_for(cls, goal: str) -> str:
        """sha256 of the normalized goal (full hex; SQLite keys are cheap)."""
        return hashlib.sha256(cls.normalize_goal(goal).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------- API
    async def get(self, goal: str) -> Optional[dict]:
        """Return ``{"proof": ..., "meta": ...}`` on hit, else None."""
        norm = self.normalize_goal(goal)
        if not norm:
            return None
        try:
            raw = await self._cache.get(self.key_for(norm), namespace=self._ns)
        except Exception as exc:
            logger.warning("lemma cache get failed (%r); treating as miss", exc)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            # Tolerate a bare-proof payload written by older versions.
            return {"proof": raw, "meta": {}}
        if isinstance(data, dict) and isinstance(data.get("proof"), str) and data["proof"]:
            return data
        return None

    async def put(self, goal: str, proof: str, meta: Optional[dict] = None) -> None:
        """Record a verified proof for ``goal`` (fire-and-forget write)."""
        norm = self.normalize_goal(goal)
        if not norm or not (proof or "").strip():
            return
        payload = json.dumps(
            {"proof": proof, "meta": dict(meta or {}), "ts": time.time()},
            ensure_ascii=False,
        )
        try:
            await self._cache.set(self.key_for(norm), payload, namespace=self._ns)
        except Exception as exc:
            logger.warning("lemma cache put failed (%r); dropped", exc)
