"""Default verification path: subprocess ``lake env lean`` (SPEC 3.7).

Algorithm per ``verify_proof``:
  1. Text blacklist first: strip comments from the proof, word-boundary regex
     ``\\b(sorry|admit|stop)\\b`` -> immediate reject (fixes v39 substring
     false-positives such as ``readmit`` or ``-- sorry`` in a comment).
  2. Locate the theorem block by ``theorem_name`` + ``line_number`` and replace
     exactly one ``sorry`` (the one on ``line_number`` when several) with the
     proof. The replacement keeps valid Lean: if the sorry sits in tactic
     position (directly after ``by``) the tactics are spliced raw; otherwise
     they are wrapped in a fresh ``by`` block (unless the proof already starts
     with ``by``). This is written into an *isolated* per-call copy of the
     project so the original project is never polluted (SPEC 3.7.2).
  3. ``asyncio.create_subprocess_exec("lake", "env", "lean", file, cwd=tmp)``
     with ``timeout = lean_timeout_s``; on timeout the whole process group is
     killed. Acceptance = ``rc == 0`` AND the target theorem's
     ``declaration uses 'sorry'`` warning is gone (SPEC 3.7.3). Note: Lean
     emits that warning on **stdout**, positioned at the *declaration* line.
  4. Concurrency bounded by ``asyncio.Semaphore(max_concurrent_lean)``; every
     process is reaped in ``finally`` (no leaks, fixes v39 P0-7 style issues).
  5. Optional ``check_axioms``: append ``#print axioms <name>`` and reject if
     the output mentions ``sorryAx`` (SPEC 3.7.5).
  6. A clean base copy of each project is built once under
     ``work_dir/verify_tmp`` (content-addressed); each call only re-materialises
     the target file into a fresh symlink-forest run dir (SPEC 3.7.6).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ..config import V40Config  # SPEC 3.2 contract (provided by M1)
from ..models import SorryTask  # SPEC 3.1 contract (provided by M1)
from .base import VerificationResult

logger = logging.getLogger(__name__)

# Word-boundary blacklist on the *comment-stripped* proof (SPEC 3.7.1).
_BLACKLIST_RE = re.compile(r"\b(?:sorry|admit|stop)\b")
# Lean "declaration uses 'sorry'" warning (appears on stdout).
_SORRY_WARN_RE = re.compile(r"declaration uses 'sorry'")
# Full warning line parser:  <path>:<line>:<col>: warning: declaration uses 'sorry'
_WARN_LINE_RE = re.compile(
    r"^(?P<path>.*?):(?P<line>\d+):(?P<col>\d+):\s*warning:\s*declaration uses 'sorry'\s*$"
)
# A top-level declaration keyword at column 0 terminates a theorem block.
_DECL_BOUNDARY_RE = re.compile(
    r"^(?:theorem|lemma|def|instance|example|abbrev|inductive|structure|class"
    r"|axiom|section|namespace|end|variable|open|import|attribute|macro|syntax"
    r"|elab|notation|opaque|initialize|set_option|universe)\b"
)


def _strip_comments(text: str) -> str:
    """Return ``text`` with Lean comments blanked out (positions preserved).

    Handles ``--`` line comments and *nested* ``/- ... -/`` block comments, and
    skips over ``"..."`` string literals so comment markers inside strings are
    not treated as comments. Newlines are kept (line numbers stay valid); every
    other comment character becomes a space (columns stay valid).
    """
    out = list(text)
    i, n = 0, len(text)
    depth = 0
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if depth > 0:
            if text.startswith("/-", i):
                depth += 1
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if text.startswith("-/", i):
                depth -= 1
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c != "\n":
                out[i] = " "
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if text.startswith("--", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if text.startswith("/-", i):
            depth = 1
            out[i] = out[i + 1] = " "
            i += 2
            continue
        i += 1
    return "".join(out)


def _leading_ws(line: str) -> str:
    m = re.match(r"[ \t]*", line)
    return m.group(0) if m else ""


class VerificationError(Exception):
    """Raised when the target theorem/sorry cannot be located or spliced."""


class SubprocessLeanVerifier:
    """Verify proofs by compiling a patched copy with ``lake env lean``."""

    # Guards one-time base-copy construction across *all* instances and loops.
    _base_lock = threading.Lock()

    def __init__(self, cfg: V40Config) -> None:
        self._cfg = cfg
        self._timeout = float(getattr(cfg, "lean_timeout_s", 30.0) or 30.0)
        self._max_concurrent = int(getattr(cfg, "max_concurrent_lean", 4) or 4)
        self._check_axioms = bool(getattr(cfg, "check_axioms", False))
        work_dir = getattr(cfg, "work_dir", "./v40_work") or "./v40_work"
        self._verify_tmp = Path(work_dir) / "verify_tmp"
        # Environment with elan on PATH (explicitly passed to subprocess).
        self._env = dict(os.environ)
        elan_bin = str(Path.home() / ".elan" / "bin")
        self._env["PATH"] = elan_bin + os.pathsep + self._env.get("PATH", "")
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._sem_loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    # ------------------------------------------------------------------ API
    async def init(self) -> None:
        self._verify_tmp.mkdir(parents=True, exist_ok=True)
        if shutil.which("lake", path=self._env["PATH"]) is None:
            raise RuntimeError(
                "subprocess verifier requires `lake` on PATH (expected "
                "~/.elan/bin). Run the Lean bootstrap or fix PATH."
            )

    async def close(self) -> None:  # re-entrant
        self._closed = True

    async def self_check(self, project_path: str) -> dict:
        """Built-in self-check (SPEC 3.7.7): sorry rejected, rfl accepted."""
        from ..sorrydb import SorryScanner  # local import to avoid cycle

        tasks = SorryScanner().scan([project_path])
        by_name = {t.theorem_name: t for t in tasks}
        out: dict = {}
        if "nat_refl" in by_name:
            out["nat_refl_rfl"] = (
                await self.verify_proof(by_name["nat_refl"], "rfl")
            ).ok
        sorry_res = await self.verify_proof_raw(project_path, "sorry")
        out["literal_sorry_rejected"] = not sorry_res.ok
        return out

    async def verify_proof_raw(
        self, project_path: str, proof: str
    ) -> VerificationResult:
        """Blacklist-only check (used by self_check for the literal-sorry case)."""
        if self._blacklist_hit(proof):
            return VerificationResult(ok=False, error="blacklisted keyword in proof")
        return VerificationResult(ok=True)

    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        t0 = time.monotonic()
        # 1. Blacklist first (cheap, before any subprocess).
        if self._blacklist_hit(proof):
            return VerificationResult(
                ok=False,
                error="proof contains blacklisted keyword (sorry/admit/stop)",
                duration_s=time.monotonic() - t0,
            )
        try:
            new_content, decl_line1 = self._splice(task, proof)
        except VerificationError as exc:
            return VerificationResult(
                ok=False, error=str(exc), duration_s=time.monotonic() - t0
            )

        if self._check_axioms:
            new_content += f"\n#print axioms {task.theorem_name}\n"

        sem = self._get_semaphore()
        async with sem:
            run_dir = self._verify_tmp / f"run_{uuid.uuid4().hex[:12]}"
            try:
                base = self._ensure_base(task.project_path)
                self._materialize_run(base, task.file_path, new_content, run_dir)
                rc, out, err, timed_out = await self._run_lean(
                    run_dir, task.file_path
                )
            finally:
                shutil.rmtree(run_dir, ignore_errors=True)

        duration = time.monotonic() - t0
        combined = (out or "") + "\n" + (err or "")
        if timed_out:
            return VerificationResult(
                ok=False,
                error=f"lean timed out after {self._timeout:.1f}s",
                duration_s=duration,
                diagnostics=self._tail(combined),
            )

        warn_lines = [l for l in combined.splitlines() if _SORRY_WARN_RE.search(l)]
        remaining = len(warn_lines)
        target_still_sorry = self._target_still_sorry(
            warn_lines, task.file_path, decl_line1
        )

        ok = (rc == 0) and (not target_still_sorry)
        error: Optional[str] = None
        if rc != 0:
            error = f"lean exited rc={rc}"
        elif target_still_sorry:
            error = "target theorem still reports 'declaration uses sorry'"

        if ok and self._check_axioms and "sorryAx" in combined:
            ok, error = False, "#print axioms reveals sorryAx"

        return VerificationResult(
            ok=ok,
            error=error,
            duration_s=duration,
            remaining_sorries=remaining,
            diagnostics=self._tail(combined),
        )

    # ------------------------------------------------------------- helpers
    def _get_semaphore(self) -> asyncio.Semaphore:
        # Loop-aware lazy semaphore (robust under pytest-asyncio per-test loops).
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._sem_loop is not loop:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
            self._sem_loop = loop
        return self._semaphore

    @staticmethod
    def _blacklist_hit(proof: str) -> bool:
        return _BLACKLIST_RE.search(_strip_comments(proof)) is not None

    @staticmethod
    def _tail(text: str, limit: int = 1200) -> str:
        text = text.strip()
        return text[-limit:] if len(text) > limit else text

    @staticmethod
    def _norm(path: str) -> str:
        return os.path.normpath(path).replace(os.sep, "/")

    def _target_still_sorry(
        self, warn_lines: list[str], file_path: str, decl_line1: int
    ) -> bool:
        target = self._norm(file_path)
        for line in warn_lines:
            m = _WARN_LINE_RE.match(line.strip())
            if not m:
                continue
            if self._norm(m.group("path")) == target and int(m.group("line")) == decl_line1:
                return True
        return False

    # ----------------------------------------------------------- splicing
    def _splice(self, task: SorryTask, proof: str) -> tuple[str, int]:
        """Return (new_file_content, target_decl_line1). Raises VerificationError."""
        src_path = Path(task.project_path) / task.file_path
        if not src_path.is_file():
            raise VerificationError(f"source file not found: {src_path}")
        original = src_path.read_text(encoding="utf-8")
        code = _strip_comments(original)
        orig_lines = original.split("\n")
        code_lines = code.split("\n")

        decl_idx = self._find_decl(code_lines, task.theorem_name, task.line_number)
        block_end = self._find_block_end(code_lines, decl_idx)
        sorry_pos = self._find_sorry(code_lines, decl_idx, block_end, task.line_number)
        sline, scol = sorry_pos

        # Indentation context from the (comment-stripped) sorry line.
        line_indent = _leading_ws(code_lines[sline])
        preceded_by_by = self._preceded_by_by(code_lines, sline, scol)
        replacement = self._build_replacement(proof, line_indent, preceded_by_by)

        orig_line = orig_lines[sline]
        if orig_line[scol:scol + 5] != "sorry":
            # Positions should align because comment-stripping preserves them.
            raise VerificationError(
                f"internal misalignment locating sorry for {task.theorem_name}"
            )
        orig_lines[sline] = orig_line[:scol] + replacement + orig_line[scol + 5:]
        return "\n".join(orig_lines), decl_idx + 1  # 1-based decl line

    @staticmethod
    def _find_decl(code_lines: list[str], name: str, line_number: int) -> int:
        decl_re = re.compile(
            r"^\s*(?:theorem|lemma|def|instance|example|abbrev)\s+"
            + re.escape(name)
            + r"\b"
        )
        candidates = [i for i, l in enumerate(code_lines) if decl_re.match(l)]
        if not candidates:
            raise VerificationError(f"theorem/lemma named {name!r} not found")
        sorry_line0 = max(line_number - 1, 0)
        above = [i for i in candidates if i <= sorry_line0]
        # Nearest declaration at/above the sorry line; fallback to the first.
        return max(above) if above else min(candidates)

    @staticmethod
    def _find_block_end(code_lines: list[str], decl_idx: int) -> int:
        for i in range(decl_idx + 1, len(code_lines)):
            if _DECL_BOUNDARY_RE.match(code_lines[i]):
                return i
        return len(code_lines)

    @staticmethod
    def _find_sorry(
        code_lines: list[str], decl_idx: int, block_end: int, line_number: int
    ) -> tuple[int, int]:
        sorry_re = re.compile(r"\bsorry\b")
        found: list[tuple[int, int]] = []
        for i in range(decl_idx, block_end):
            for m in sorry_re.finditer(code_lines[i]):
                found.append((i, m.start()))
        if not found:
            raise VerificationError("no `sorry` found in theorem block")
        if len(found) == 1:
            return found[0]
        # Multiple sorries: take the one on line_number; else the first.
        sorry_line0 = line_number - 1
        for pos in found:
            if pos[0] == sorry_line0:
                return pos
        return found[0]

    @staticmethod
    def _preceded_by_by(code_lines: list[str], sline: int, scol: int) -> bool:
        """True iff the token right before the sorry (skipping whitespace) is `by`."""
        before = "\n".join(code_lines[:sline])
        if before:
            before += "\n"
        before += code_lines[sline][:scol]
        return re.search(r"\bby\s*$", before) is not None

    @staticmethod
    def _reindent(first: str, rest: list[str], cont_indent: str) -> str:
        out = first
        for ln in rest:
            out += "\n" + (cont_indent + ln if ln.strip() else "")
        return out

    def _build_replacement(
        self, proof: str, line_indent: str, preceded_by_by: bool
    ) -> str:
        p = proof.strip("\n")
        if not p.strip():
            raise VerificationError("empty proof")
        lines = p.split("\n")
        first = lines[0].strip()
        rest = lines[1:]
        starts_by = re.match(r"^by\b", first) is not None

        if preceded_by_by:
            # Tactic slot: need a tactic sequence (context already has `by`).
            if starts_by:
                after = re.sub(r"^by\b", "", first, count=1)
                if after.strip():
                    body_first, body_rest = after.strip(), rest
                else:
                    if not rest:
                        raise VerificationError("proof is a bare `by`")
                    body_first, body_rest = rest[0].strip(), rest[1:]
            else:
                body_first, body_rest = first, rest
            return self._reindent(body_first, body_rest, line_indent)
        # Term slot: need a term; wrap tactic proofs in a fresh `by` block.
        if starts_by:
            return self._reindent(first, rest, line_indent)
        cont = line_indent + "  "
        return "by\n" + self._reindent(cont + first, rest, cont)

    # ------------------------------------------------------- copy / cache
    def _base_key(self, project_path: str) -> str:
        ap = os.path.abspath(project_path)
        return hashlib.sha256(ap.encode("utf-8")).hexdigest()[:16]

    def _ensure_base(self, project_path: str) -> Path:
        base = self._verify_tmp / f"base_{self._base_key(project_path)}"
        marker = base / ".v40_base_ready"
        if marker.exists():
            return base
        with self._base_lock:
            if marker.exists():
                return base
            src = Path(project_path)
            if not src.is_dir():
                raise VerificationError(f"project_path not a directory: {src}")
            vt = self._verify_tmp.resolve()

            def _ignore(dirpath: str, names: list[str]) -> list[str]:
                # Always drop .git, and drop the verify_tmp scratch dir if it
                # happens to live *inside* the project (otherwise copytree would
                # recursively copy the build/base dirs into themselves).
                ignored: list[str] = []
                for n in names:
                    if n == ".git":
                        ignored.append(n)
                        continue
                    try:
                        if (Path(dirpath) / n).resolve() == vt:
                            ignored.append(n)
                    except OSError:
                        continue
                return ignored

            tmp = self._verify_tmp / f"_build_{uuid.uuid4().hex[:8]}"
            shutil.copytree(src, tmp, ignore=_ignore, symlinks=False)
            tmp_marker = tmp / ".v40_base_ready"
            tmp_marker.write_text("ok", encoding="utf-8")
            if base.exists():
                shutil.rmtree(base, ignore_errors=True)
            os.replace(tmp, base)  # atomic publish
        return base

    def _materialize_run(
        self, base: Path, rel_target: str, new_content: str, run_dir: Path
    ) -> None:
        """Symlink-forest copy of ``base`` with the target file overwritten."""
        parts = [p for p in Path(rel_target).parts if p not in ("", ".")]
        if not parts:
            raise VerificationError(f"invalid file_path: {rel_target!r}")

        def link(src_dir: Path, dst_dir: Path, sub: list[str]) -> None:
            dst_dir.mkdir(parents=True, exist_ok=True)
            with os.scandir(src_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
            for entry in entries:
                dst = dst_dir / entry.name
                if sub and entry.name == sub[0]:
                    if len(sub) == 1:
                        dst.write_text(new_content, encoding="utf-8")
                    else:
                        link(Path(entry.path), dst, sub[1:])
                else:
                    try:
                        os.symlink(entry.path, dst)
                    except OSError:
                        # Filesystems without symlink support (9p/portal
                        # mounts, e.g. some /mnt sandboxes): fall back to a
                        # real copy so work_dir may live there (BUG-7).
                        if entry.is_dir(follow_symlinks=False):
                            shutil.copytree(entry.path, dst, symlinks=False)
                        else:
                            shutil.copy2(entry.path, dst)

        link(base, run_dir, parts)

    # ------------------------------------------------------------- process
    async def _run_lean(
        self, run_dir: Path, rel_file: str
    ) -> tuple[int, str, str, bool]:
        proc = await asyncio.create_subprocess_exec(
            "lake", "env", "lean", rel_file,
            cwd=str(run_dir),
            env=self._env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group -> clean group kill
        )
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
            return (
                proc.returncode if proc.returncode is not None else -1,
                out_b.decode("utf-8", "replace"),
                err_b.decode("utf-8", "replace"),
                False,
            )
        except asyncio.TimeoutError:
            self._kill_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return (-1, "", "", True)
        finally:
            if proc.returncode is None:
                self._kill_group(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def _kill_group(proc) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
