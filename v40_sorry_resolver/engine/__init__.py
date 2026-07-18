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
# Whole-declaration shell: models (DeepSeek in particular) frequently answer
# with the full ``theorem foo ... := by ...`` even when asked for bare tactics.
# Splicing that text back after `by` poisons the file with
# ``unexpected token 'theorem'`` (benchmark BUG-2), so the shell is stripped.
_DECL_START_RE = re.compile(r"^\s*(?:theorem|lemma|example|def|instance)\b")
# First `:=` immediately followed (after whitespace) by a `by` tactic block
# (the `by` is captured so its position can be kept).
_DEF_BY_RE = re.compile(r":=\s*(by\b)")
# First top-level `:=` (term-style proof shell).
_DEF_RE = re.compile(r":=")


def _strip_decl_shell(code: str) -> str:
    """Strip a leading whole-declaration shell, keeping only the proof.

    ``theorem foo (h : P) : Q := by <tactics>`` -> ``by <tactics>``;
    ``theorem foo : Q := <term>`` -> ``exact <term>``. Anything that is not
    a leading declaration is returned unchanged (the downstream verifier is
    the final judge either way).
    """
    if not code or not _DECL_START_RE.match(code):
        return code
    m = _DEF_BY_RE.search(code)
    if m:
        # Keep the `by`: it is valid in both tactic and term splice slots.
        return code[m.start(1):].strip()
    m = _DEF_RE.search(code)
    if m:
        term = code[m.end():].strip()
        if term:
            return "exact " + term
    return code


def extract_lean_code(text: str) -> str:
    """Extract Lean proof code from LLM output with a 3-level fallback.

    1. `` ```(?:lean4?)? `` fenced block (longest match wins).
    2. Any fenced code block (any language tag).
    3. Raw text starting at the first ``by`` keyword; otherwise the whole
       stripped text (the verifier downstream is the final judge).

    Fenced/raw results are de-shelled: a full ``theorem ... := by ...``
    wrapper is reduced to its proof body (benchmark BUG-2).

    Returns the stripped code string, ``""`` when input is empty.
    """
    if not text:
        return ""
    blocks = _FENCE_LEAN_RE.findall(text)
    if blocks:
        return _strip_decl_shell(max(blocks, key=len).strip())
    blocks = _FENCE_ANY_RE.findall(text)
    if blocks:
        return _strip_decl_shell(max(blocks, key=len).strip())
    m = _BY_RE.search(text)
    if m:
        return text[m.start():].strip()
    return _strip_decl_shell(text.strip())


async def maybe_await(value):
    """Await ``value`` when it is awaitable, otherwise return it as-is.

    Used at integration boundaries where the concrete collaborator
    (checkpoint / scanner) may expose sync or async methods.
    """
    if inspect.isawaitable(value):
        return await value
    return value
