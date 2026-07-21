"""APOLLO-style sub-lemma decomposition (frontier_atp Top-8 #4;
arXiv:2505.05758 — isolate failing sub-lemmas, re-prove them with a small
budget, reassemble; sampling budget /100 vs whole-proof resampling).

Flow (:meth:`ApolloDecomposer.attempt`):

1. The PROVER decomposes the goal into a proof *skeleton*: at most
   ``apollo_max_sublemmas`` ``have h_i : P_i := by sorry`` sub-lemmas plus the
   main proof that closes the goal from the ``h_i``.
2. Each sub-lemma is proven and **verified in isolation**: a synthetic
   ``theorem <parent>_apollo_<h_i> <binders> : P_i := by sorry`` is inserted
   into a throwaway copy of the source file (original file never touched) and
   checked through the normal verifier, so a broken sub-lemma cannot hide
   behind the others.
3. A failed sub-lemma is re-proven individually with the remaining budget;
   when ``apollo_recursive`` is on it may itself be decomposed once more
   (one recursive level).
4. Once every sub-lemma verifies, the skeleton is reassembled with the
   verified sub-proofs and the complete proof is verified end-to-end.

Proven sub-lemmas are written to the shared :class:`LemmaCache` and consulted
before re-proving (frontier_atp Top-8 #5).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from v40_sorry_resolver.models import SorryTask
from v40_sorry_resolver.llm.router import Role
from v40_sorry_resolver.engine import extract_lean_code

logger = logging.getLogger("v40.apollo")

# `have h_i : <prop> := by sorry` inside a skeleton (prop may span lines).
_HAVE_RE = re.compile(
    r"have[ \t]+(?P<name>[A-Za-z_][\w']*)\s*:\s*(?P<goal>.+?)\s*:=\s*by\s+sorry\b",
    re.DOTALL,
)
_DECL_OF_NAME_TMPL = r"^[ \t]*(?:theorem|lemma|def|instance|example|abbrev)[ \t]+%s\b"
_BANNED_NAME_RE = re.compile(r"[^\w']")


@dataclass
class SubLemma:
    name: str
    goal: str
    proof: Optional[str] = None


@dataclass
class _Skeleton:
    text: str
    # (name, goal, match_start, match_end) in `text` order.
    slots: list = field(default_factory=list)


class ApolloDecomposer:
    def __init__(self, router, verifier, lemma_cache=None, cfg=None):
        self.router = router
        self.verifier = verifier
        self.lemma_cache = lemma_cache
        self.cfg = cfg

    # ------------------------------------------------------------ config
    def _max_sublemmas(self) -> int:
        return max(1, int(getattr(self.cfg, "apollo_max_sublemmas", 3) or 3))

    def _retries(self) -> int:
        return max(0, int(getattr(self.cfg, "apollo_sublemma_retries", 2) or 0))

    def _recursive(self) -> bool:
        return bool(getattr(self.cfg, "apollo_recursive", True))

    # ------------------------------------------------------------ main
    async def attempt(
        self,
        task: SorryTask,
        strategy=None,
        notebook: Optional[list] = None,
        depth: int = 0,
    ) -> Optional[str]:
        """Try a decomposed proof of ``task``; return the full verified proof
        text or None (caller falls back to the normal agentic loop)."""
        skeleton = await self._request_skeleton(task, strategy, notebook)
        if skeleton is None or not skeleton.slots:
            logger.info("apollo %s: decomposition produced no sub-lemmas", task.id)
            return None
        proven: list[SubLemma] = []
        for name, goal, _s, _e in skeleton.slots:
            sub = await self._prove_sublemma(
                task, SubLemma(name=name, goal=goal), proven, strategy, depth
            )
            if sub is None or not sub.proof:
                logger.info(
                    "apollo %s: sub-lemma %s could not be proven; aborting",
                    task.id, name,
                )
                return None
            proven.append(sub)
        full = self._reassemble(skeleton, proven)
        if not full:
            return None
        try:
            vr = await self.verifier.verify_proof(task, full)
        except Exception as exc:
            logger.info("apollo %s: reassembled proof verify error: %r", task.id, exc)
            return None
        if not getattr(vr, "ok", False):
            logger.info(
                "apollo %s: reassembled proof rejected (%s)",
                task.id, getattr(vr, "error", "") or "verify failed",
            )
            return None
        await self._cache_put(task.goal_state or "", full, meta={
            "source": "apollo", "task_id": task.id,
        })
        return full

    # ------------------------------------------------- sub-lemma proving
    async def _prove_sublemma(
        self,
        task: SorryTask,
        sub: SubLemma,
        proven: list[SubLemma],
        strategy,
        depth: int,
    ) -> Optional[SubLemma]:
        """Prove one sub-lemma in isolation (cache -> direct -> recurse)."""
        # 1. Shared lemma cache first (frontier_atp Top-8 #5).
        hit = await self._cache_get(sub.goal)
        if hit:
            vr = await self._verify_isolated(task, proven, sub, hit)
            if getattr(vr, "ok", False):
                sub.proof = hit
                return sub
            logger.info(
                "apollo %s: cached proof for %s failed isolation; re-proving",
                task.id, sub.name,
            )
        # 2. Direct attempts with the remaining budget (verifier feedback is
        #    fed back into the next prompt, Top-8 #2 style).
        prev_error = ""
        for _ in range(max(1, self._retries())):
            proof = await self._request_subproof(
                task, sub, proven, strategy, prev_error
            )
            if not proof:
                prev_error = "empty proof from prover"
                continue
            vr = await self._verify_isolated(task, proven, sub, proof)
            if getattr(vr, "ok", False):
                sub.proof = proof
                await self._cache_put(sub.goal, proof, meta={
                    "source": "apollo_sublemma", "task_id": task.id,
                })
                return sub
            prev_error = (
                getattr(vr, "diagnostics", "") or getattr(vr, "error", "") or ""
            )[:500]
        # 3. One recursive level: decompose the sub-lemma itself.
        if self._recursive() and depth < 1:
            proof = await self._recursive_decompose(task, sub, proven, strategy, depth)
            if proof:
                vr = await self._verify_isolated(task, proven, sub, proof)
                if getattr(vr, "ok", False):
                    sub.proof = proof
                    await self._cache_put(sub.goal, proof, meta={
                        "source": "apollo_sublemma_recursive", "task_id": task.id,
                    })
                    return sub
        return None

    async def _recursive_decompose(
        self,
        task: SorryTask,
        sub: SubLemma,
        proven: list[SubLemma],
        strategy,
        depth: int,
    ) -> Optional[str]:
        """Decompose a stubborn sub-lemma one level deeper (APOLLO recursion)."""
        prover = self.router.client(Role.PROVER)
        prompt = (
            f"While proving theorem {task.theorem_name}, the sub-lemma "
            f"`{sub.name} : {sub.goal}` resisted direct proof.\n"
            f"Decompose THIS sub-lemma into at most {self._max_sublemmas()} "
            "smaller `have k_i : <proposition> := by sorry` steps plus a "
            "closing tactic. Respond with Lean code only, inside a ```lean "
            "fenced block."
        )
        try:
            resp = await prover.generate(
                prompt,
                system_prompt=(
                    "You are an expert Lean 4 prover. Output only Lean code."
                ),
                temperature=0.3,
                max_tokens=2048,
                cache_key=None,
            )
        except Exception as exc:
            logger.debug("apollo recursive skeleton llm error: %r", exc)
            return None
        skeleton = self._parse_skeleton(extract_lean_code(getattr(resp, "text", "") or ""))
        if skeleton is None or not skeleton.slots:
            return None
        sub_proven: list[SubLemma] = list(proven)
        for name, goal, _s, _e in skeleton.slots:
            child = await self._prove_sublemma(
                task, SubLemma(name=name, goal=goal), sub_proven, strategy, depth + 1
            )
            if child is None or not child.proof:
                return None
            sub_proven.append(child)
        # Reassemble the sub-lemma proof from the deeper skeleton; only the
        # slots introduced at THIS level are substituted (parents stay).
        own = sub_proven[len(proven):]
        return self._reassemble(skeleton, own)

    # --------------------------------------------------------- LLM calls
    async def _request_skeleton(
        self, task: SorryTask, strategy, notebook: Optional[list]
    ) -> Optional[_Skeleton]:
        prover = self.router.client(Role.PROVER)
        lessons = ""
        if notebook:
            recent = [
                str(e[0] if isinstance(e, tuple) else e) for e in notebook[-3:]
            ]
            if recent:
                lessons = (
                    "Lessons from failed whole-proof attempts:\n"
                    + "\n".join(f"- {l}" for l in recent)
                    + "\n"
                )
        prompt = (
            f"Theorem {task.theorem_name} (file {task.file_path}, "
            f"line {task.line_number}).\n"
            f"Goal: {task.goal_state or '(infer from context)'}\n"
            f"Context:\n{(task.surrounding_context or '')[:1500]}\n"
            f"{lessons}"
            "Direct whole-proof attempts have failed. Decompose the proof "
            f"into at most {self._max_sublemmas()} sub-lemmas: output a Lean 4 "
            "tactic block that states each sub-lemma as "
            "`have h_i : <proposition> := by sorry` and then closes the goal "
            "using the h_i. Respond with Lean code only, inside a ```lean "
            "fenced block."
        )
        try:
            resp = await prover.generate(
                prompt,
                system_prompt=(
                    "You are an expert Lean 4 prover. Output only Lean code. "
                    "Use sorry ONLY as the body of the have-sub-lemmas."
                ),
                temperature=0.3,
                max_tokens=2048,
                cache_key=None,
            )
        except Exception as exc:
            logger.info("apollo %s: skeleton llm error: %r", task.id, exc)
            return None
        if getattr(resp, "error", None):
            return None
        return self._parse_skeleton(extract_lean_code(getattr(resp, "text", "") or ""))

    async def _request_subproof(
        self,
        task: SorryTask,
        sub: SubLemma,
        proven: list[SubLemma],
        strategy,
        prev_error: str,
    ) -> str:
        prover = self.router.client(Role.PROVER)
        proven_block = "".join(
            f"- {p.name} : {p.goal} (already proven)\n" for p in proven
        ) or "(none yet)\n"
        error_block = (
            f"The previous attempt failed with Lean diagnostics:\n{prev_error}\n"
            if prev_error
            else ""
        )
        prompt = (
            f"Theorem {task.theorem_name}: main goal "
            f"{task.goal_state or '(infer from context)'}.\n"
            f"Prove ONLY this intermediate sub-lemma:\n"
            f"have {sub.name} : {sub.goal}\n"
            f"Already-proven sub-lemmas you may use:\n{proven_block}"
            f"{error_block}"
            "Output a Lean 4 tactic block proving the sub-lemma. Respond with "
            "Lean code only, inside a ```lean fenced block. Never use "
            "sorry/admit."
        )
        try:
            resp = await prover.generate(
                prompt,
                system_prompt=(
                    "You are an expert Lean 4 prover. Output only Lean code. "
                    "Never use sorry/admit."
                ),
                temperature=0.3,
                max_tokens=2048,
                cache_key=None,
            )
        except Exception as exc:
            logger.debug("apollo %s: sub-proof llm error: %r", task.id, exc)
            return ""
        if getattr(resp, "error", None):
            return ""
        return extract_lean_code(getattr(resp, "text", "") or "")

    # ------------------------------------------------------ parsing
    def _parse_skeleton(self, code: str) -> Optional[_Skeleton]:
        """Extract up to ``apollo_max_sublemmas`` have-slots from a skeleton."""
        if not code or "sorry" not in code:
            return None
        slots = []
        for m in _HAVE_RE.finditer(code):
            name = m.group("name")
            goal = re.sub(r"\s+", " ", m.group("goal")).strip()
            if not goal:
                continue
            slots.append((name, goal, m.start(), m.end()))
            if len(slots) >= self._max_sublemmas():
                break
        if not slots:
            return None
        return _Skeleton(text=code, slots=slots)

    @staticmethod
    def _reassemble(skeleton: _Skeleton, proven: list[SubLemma]) -> str:
        """Substitute verified sub-proofs into the skeleton (last-to-first so
        spans stay valid)."""
        by_name = {p.name: p for p in proven}
        text = skeleton.text
        for name, goal, start, end in reversed(skeleton.slots):
            sub = by_name.get(name)
            if sub is None or not sub.proof:
                return ""
            line_start = text.rfind("\n", 0, start) + 1
            base_indent = text[line_start:start]
            indent = base_indent + "  "
            body = "\n".join(
                indent + line if line.strip() else line
                for line in sub.proof.strip().splitlines()
            )
            replacement = f"have {name} : {goal} := by\n{body}"
            text = text[:start] + replacement + text[end:]
        return text.strip()

    # ------------------------------------------- isolated verification
    async def _verify_isolated(
        self,
        task: SorryTask,
        proven: list[SubLemma],
        sub: SubLemma,
        proof: str,
    ):
        """Verify ``proof`` against ``sub.goal`` alone (APOLLO isolation).

        Builds a synthetic task pointing at a throwaway copy of the source
        file with (a) the already-proven sub-lemmas as auxiliary theorems and
        (b) a synthetic ``<parent>_apollo_<name>`` theorem whose sorry is the
        sub-lemma goal. The original file is never modified; the temp file is
        removed afterwards. When the source file is unavailable (e.g. pure
        in-memory tasks) we fall back to verifying through the parent task.
        """
        built = self._build_synthetic_task(task, proven, sub)
        if built is None:
            return await self.verifier.verify_proof(task, proof)
        syn_task, tmp_file = built
        try:
            return await self.verifier.verify_proof(syn_task, proof)
        finally:
            try:
                Path(tmp_file).unlink(missing_ok=True)
                tmp_dir = Path(tmp_file).parent
                if tmp_dir.name == ".apollo_tmp" and not any(tmp_dir.iterdir()):
                    tmp_dir.rmdir()
            except OSError as exc:
                logger.debug("apollo temp cleanup failed: %r", exc)

    def _build_synthetic_task(
        self, task: SorryTask, proven: list[SubLemma], sub: SubLemma
    ):
        """Materialize the isolation file; return (SorryTask, tmp_path)."""
        try:
            src = Path(task.project_path) / task.file_path
            if not src.is_file():
                return None
            original = src.read_text(encoding="utf-8")
        except OSError:
            return None
        parent = _BANNED_NAME_RE.sub("_", task.theorem_name or "target")
        decl_re = re.compile(_DECL_OF_NAME_TMPL % re.escape(task.theorem_name or ""))
        lines = original.split("\n")
        decl_idx = None
        if task.theorem_name:
            for i, line in enumerate(lines):
                if decl_re.match(line):
                    decl_idx = i
                    break
        if decl_idx is None:
            return None
        binders = self._extract_binders(lines, decl_idx, task.theorem_name)
        block: list[str] = []
        for p in proven:
            safe = _BANNED_NAME_RE.sub("_", p.name)
            block.append(f"theorem {safe} {binders} : {p.goal} := by".rstrip())
            block.extend("  " + l for l in p.proof.strip().splitlines())
        synth_name = f"{parent}_apollo_{_BANNED_NAME_RE.sub('_', sub.name)}"
        block.append(f"theorem {synth_name} {binders} : {sub.goal} := by".rstrip())
        block.append("  sorry")
        new_lines = lines[:decl_idx] + block + lines[decl_idx:]
        sorry_line0 = decl_idx + len(block) - 1  # 0-based index of `  sorry`
        col1 = new_lines[sorry_line0].index("sorry") + 1

        tmp_dir = Path(task.project_path) / ".apollo_tmp"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = tmp_dir / f"apollo_{uuid.uuid4().hex[:12]}.lean"
            tmp_file.write_text("\n".join(new_lines), encoding="utf-8")
            rel = str(tmp_file.relative_to(Path(task.project_path)))
        except OSError as exc:
            logger.debug("apollo synthetic file unavailable: %r", exc)
            return None
        syn_id = hashlib.sha1(
            f"{rel}:{sorry_line0 + 1}:{col1}".encode("utf-8")
        ).hexdigest()[:12]
        return (
            SorryTask(
                id=syn_id,
                project_path=task.project_path,
                file_path=rel,
                line_number=sorry_line0 + 1,
                column_number=col1,
                theorem_name=synth_name,
                goal_state=sub.goal,
                surrounding_context="\n".join(block),
            ),
            tmp_file,
        )

    @staticmethod
    def _extract_binders(lines: list[str], decl_idx: int, theorem_name: str) -> str:
        """Best-effort binder block (``(x : Nat) {h : P}`` ...) of the parent
        declaration, so the synthetic sub-lemma theorem states the goal in the
        same context. Returns '' on any ambiguity."""
        text = "\n".join(lines[decl_idx:decl_idx + 20])
        m = re.match(
            r"\s*(?:theorem|lemma|def|instance|example|abbrev)\s+"
            + re.escape(theorem_name)
            + r"\b",
            text,
        )
        if not m:
            return ""
        i, depth = m.end(), 0
        start = i
        limit = min(len(text), i + 2000)
        while i < limit:
            c = text[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth = max(0, depth - 1)
            elif depth == 0 and c == ":" and not text.startswith(":=", i):
                binders = text[start:i]
                return re.sub(r"\s+", " ", binders).strip()
            elif depth == 0 and text.startswith(":=", i):
                return ""
            i += 1
        return ""

    # --------------------------------------------------------- cache I/O
    async def _cache_get(self, goal: str) -> Optional[str]:
        if self.lemma_cache is None:
            return None
        try:
            hit = await self.lemma_cache.get(goal)
        except Exception as exc:
            logger.debug("lemma cache get failed: %r", exc)
            return None
        if hit and isinstance(hit, dict):
            return hit.get("proof") or None
        return None

    async def _cache_put(self, goal: str, proof: str, meta: Optional[dict] = None) -> None:
        if self.lemma_cache is None or not goal.strip():
            return
        try:
            await self.lemma_cache.put(goal, proof, meta=meta)
        except Exception as exc:
            logger.debug("lemma cache put failed: %r", exc)
