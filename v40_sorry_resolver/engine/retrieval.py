"""Premise retrieval tool (frontier_atp Top-8 #6; frontier_resources section 4).

Async clients for two free HTTP premise-search services (both verified
reachable 2026-07-19, endpoints from the LeanSearchClient source):

- **LeanSearch** — ``POST https://leansearch.net/search`` with a JSON body
  (natural-language / formal statement query -> Mathlib theorem names);
- **LeanStateSearch** — ``GET https://premise-search.com/api/search`` with
  ``query``/``results`` params (proof-state -> premise lemmas).

Unified interface::

    async def search_premises(query: str, top_k: int = 5) -> list[str]

Both sources are queried concurrently and merged (deduped, order preserved).
Every source call has an 8s timeout; any failure (network, timeout, malformed
payload) logs a WARNING and degrades to ``[]`` for that source — retrieval
**never blocks or fails the solving flow**. Callers are expected to gate usage
behind ``V40Config.retrieval_enabled`` (default False) and to skip queries
without mathlib-style constants (``has_mathlib_constant``).

The HTTP layer is injectable (``http_post``/``http_get``) so tests can mock
all network behavior; this module itself performs no real network access in
tests.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("v40.retrieval")

#: Verified free endpoints (frontier_resources.md section 4, 2026-07-19).
LEANSEARCH_URL = "https://leansearch.net/search"
PREMISE_SEARCH_URL = "https://premise-search.com/api/search"

#: Per-source timeout; retrieval must never stall the main flow.
DEFAULT_TIMEOUT_S = 8.0

# Mathlib-style constant: dotted name whose first segment is capitalized,
# e.g. ``Nat.add_comm``, ``List.Perm``, ``Finset.sum_range_succ``.
_MATHLIB_CONST_RE = re.compile(r"\b[A-Z][A-Za-z0-9_']*(?:\.[A-Za-z0-9_']+)+\b")

#: Type of the injectable async HTTP helpers.
HttpPost = Callable[[str, dict, float], Awaitable[object]]
HttpGet = Callable[[str, dict, float], Awaitable[object]]


def has_mathlib_constant(text: str) -> bool:
    """True iff ``text`` mentions a mathlib-style dotted constant."""
    return bool(_MATHLIB_CONST_RE.search(text or ""))


def _extract_names(payload) -> list[str]:
    """Tolerantly pull lemma names out of either service's JSON payload.

    Accepts a list (leansearch.net / premise-search.com) or a dict wrapping a
    list under common keys. Items may be bare strings or objects carrying a
    ``name`` (string or list of name components), ``full_name``, or
    ``declaration``/``type`` field.
    """
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "sorries"):
            inner = payload.get(key)
            if isinstance(inner, list):
                payload = inner
                break
        else:
            return []
    if not isinstance(payload, list):
        return []
    names: list[str] = []
    for item in payload:
        name: Optional[str] = None
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            raw = item.get("name")
            if isinstance(raw, list):
                raw = ".".join(str(part) for part in raw)
            if not raw:
                raw = item.get("full_name") or item.get("declaration")
            if not raw and isinstance(item.get("type"), str):
                raw = item["type"]
            name = str(raw) if raw else None
        if name:
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names


async def _httpx_post(url: str, payload: dict, timeout_s: float):
    import httpx

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def _httpx_get(url: str, params: dict, timeout_s: float):
    import httpx

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


class PremiseRetriever:
    """Unified async premise-search client (leansearch.net + premise-search.com)."""

    def __init__(
        self,
        leansearch_url: str = LEANSEARCH_URL,
        premise_search_url: str = PREMISE_SEARCH_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        http_post: Optional[HttpPost] = None,
        http_get: Optional[HttpGet] = None,
    ) -> None:
        self._leansearch_url = leansearch_url
        self._premise_search_url = premise_search_url
        self._timeout_s = float(timeout_s)
        self._http_post = http_post or _httpx_post
        self._http_get = http_get or _httpx_get

    async def search_premises(self, query: str, top_k: int = 5) -> list[str]:
        """Return up to ``top_k`` lemma names for ``query`` (never raises).

        Both sources are queried concurrently; each source degrades to []
        on timeout/error (WARNING logged). Results are deduped preserving
        order (leansearch.net hits first).
        """
        query = (query or "").strip()
        if not query:
            return []
        top_k = max(1, int(top_k))
        results = await asyncio.gather(
            self._query_leansearch(query, top_k),
            self._query_premise_search(query, top_k),
            return_exceptions=True,
        )
        names: list[str] = []
        for res in results:
            if isinstance(res, BaseException):  # defensive; sources self-guard
                logger.warning("premise retrieval source crashed: %r", res)
                continue
            for name in res:
                if name not in names:
                    names.append(name)
        return names[:top_k]

    # ------------------------------------------------------------ sources
    async def _query_leansearch(self, query: str, top_k: int) -> list[str]:
        try:
            payload = {"query": query, "num_results": top_k}
            data = await asyncio.wait_for(
                self._http_post(self._leansearch_url, payload, self._timeout_s),
                timeout=self._timeout_s,
            )
            return _extract_names(data)[:top_k]
        except Exception as exc:
            logger.warning("leansearch.net retrieval failed (%r); using []", exc)
            return []

    async def _query_premise_search(self, query: str, top_k: int) -> list[str]:
        try:
            params = {"query": query, "results": top_k}
            data = await asyncio.wait_for(
                self._http_get(self._premise_search_url, params, self._timeout_s),
                timeout=self._timeout_s,
            )
            return _extract_names(data)[:top_k]
        except Exception as exc:
            logger.warning(
                "premise-search.com retrieval failed (%r); using []", exc
            )
            return []


#: Shared default retriever (stateless; safe to reuse across tasks).
_default_retriever = PremiseRetriever()


async def search_premises(query: str, top_k: int = 5) -> list[str]:
    """Module-level convenience wrapper around the shared default retriever."""
    return await _default_retriever.search_premises(query, top_k=top_k)
