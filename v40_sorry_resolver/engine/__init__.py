"""Engine layer of v40 sorry resolver.

Shared helpers used by tactic_search / axprover / agents live here to avoid
duplication (v39 P2-13: inconsistent code-fence extraction).
"""

from __future__ import annotations

import inspect
import re

__all__ = ["extract_lean_code", "maybe_await"]

# Level-1 fence: ```lean / ```lean4 / bare ``` (lenient, per SPEC).
_FENCE_LEAN_RE = re.compile(r"```(?:lean4?)?\s*\n(.*?)```", re.DOTALL)
# Level-2 fence: any language tag.
_FENCE_ANY_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
# Level-3: first `by` keyword (tactic block start).
_BY_RE = re.compile(r"\bby\b")


def extract_lean_code(text: str) -> str:
    """Extract Lean proof code from LLM output with a 3-level fallback.

    1. `` ```(?:lean4?)? `` fenced block (longest match wins).
    2. Any fenced code block (any language tag).
    3. Raw text starting at the first ``by`` keyword; otherwise the whole
       stripped text (the verifier downstream is the final judge).

    Returns the stripped code string, ``""`` when input is empty.
    """
    if not text:
        return ""
    blocks = _FENCE_LEAN_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip()
    blocks = _FENCE_ANY_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip()
    m = _BY_RE.search(text)
    if m:
        return text[m.start():].strip()
    return text.strip()


async def maybe_await(value):
    """Await ``value`` when it is awaitable, otherwise return it as-is.

    Used at integration boundaries where the concrete collaborator
    (checkpoint / scanner) may expose sync or async methods.
    """
    if inspect.isawaitable(value):
        return await value
    return value
