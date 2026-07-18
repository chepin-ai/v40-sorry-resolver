"""Task source: local-project sorry scanner + optional SorryDB client (SPEC 3.9).

``SorryScanner`` walks ``lean_project_paths`` (files or directories, recursing
into subdirectories), strips comments, and for every ``sorry`` token records the
enclosing theorem (the *nearest* ``theorem|lemma`` declaration *above* the sorry
line — fixing v39's "first declaration in context" bug, P2-19), the 1-based
line/column, the owning file, and the goal (extracted by paren-balancing the
declaration header). It emits ``list[SorryTask]`` and **never injects fake
tasks** (v39 P1-9): an empty scan logs a WARNING and returns ``[]``.

``SorryDBClient`` pulls real SorryDB snapshots (frontier_resources.md
section 1) from a remote URL or a local JSON/JSONL file and maps the SorryDB
pydantic schema (repo/location/debug_info/metadata) onto ``SorryTask``. It is
disabled when ``endpoint`` is None; any failure logs a WARNING and returns
``[]`` — never a fake task.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from .models import PriorityLevel, SorryTask  # SPEC 3.1 contract (provided by M1)

logger = logging.getLogger(__name__)

# Enclosing declaration for a sorry (nearest match above wins).
_DECL_RE = re.compile(
    r"^[ \t]*(theorem|lemma|def|instance|example|abbrev)[ \t]+([^\s(\[{:=]+)"
)
_SORRY_RE = re.compile(r"\bsorry\b")
_LAKEFILE_NAMES = ("lakefile.toml", "lakefile.lean")


def _strip_comments(text: str) -> str:
    """Blank out Lean comments, preserving positions (shared logic, see verify)."""
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


def _find_project_root(start: Path) -> Path:
    """Nearest ancestor (or self) containing a lakefile; else ``start``'s dir."""
    cur = start if start.is_dir() else start.parent
    cur = cur.resolve()
    for cand in [cur, *cur.parents]:
        if any((cand / name).exists() for name in _LAKEFILE_NAMES):
            return cand
    return cur


class SorryScanner:
    """Scan Lean projects for ``sorry`` placeholders -> ``list[SorryTask]``."""

    def scan(self, paths: list[str]) -> list[SorryTask]:
        tasks: list[SorryTask] = []
        for raw in paths or []:
            root = Path(raw)
            if not root.exists():
                logger.warning("SorryScanner: path does not exist, skipped: %s", raw)
                continue
            for lean_file in self._iter_lean_files(root):
                try:
                    tasks.extend(self._scan_file(lean_file))
                except OSError as exc:
                    logger.warning("SorryScanner: cannot read %s: %r", lean_file, exc)
        if not tasks:
            logger.warning("SorryScanner: no sorries found in %s (returning [])", list(paths or []))
        return tasks

    # ------------------------------------------------------------------ walk
    @staticmethod
    def _iter_lean_files(root: Path) -> list[Path]:
        def keep(p: Path) -> bool:
            parts = set(p.parts)
            return ".lake" not in parts and not any(
                seg.startswith(".") for seg in p.parts
            )

        if root.is_file():
            return [root] if root.suffix == ".lean" and keep(root) else []
        files = [
            p for p in root.rglob("*.lean")
            if p.is_file() and keep(p.relative_to(root) if root.is_dir() else p)
        ]
        # Deterministic order.
        return sorted(files, key=lambda p: str(p))

    # ------------------------------------------------------------------ file
    def _scan_file(self, lean_file: Path) -> list[SorryTask]:
        project_root = _find_project_root(lean_file)
        try:
            rel_file = str(lean_file.resolve().relative_to(project_root))
        except ValueError:
            rel_file = lean_file.name
        original = lean_file.read_text(encoding="utf-8")
        code = _strip_comments(original)
        orig_lines = original.split("\n")
        code_lines = code.split("\n")

        priority = self._priority_for(rel_file, original)
        tasks: list[SorryTask] = []
        for idx, cline in enumerate(code_lines):
            for m in _SORRY_RE.finditer(cline):
                line1 = idx + 1
                col1 = m.start() + 1
                decl = self._enclosing_decl(code_lines, idx)
                if decl is None:
                    logger.warning(
                        "SorryScanner: sorry at %s:%d has no enclosing "
                        "theorem/lemma above; skipped", rel_file, line1,
                    )
                    continue
                decl_idx, _kw, name = decl
                goal = self._extract_goal(code, code_lines, decl_idx)
                context = "\n".join(orig_lines[decl_idx:line1]).strip("\n")
                task_id = hashlib.sha1(
                    f"{rel_file}:{line1}:{col1}".encode("utf-8")
                ).hexdigest()[:12]
                tasks.append(
                    SorryTask(
                        id=task_id,
                        project_path=str(project_root),
                        file_path=rel_file,
                        line_number=line1,
                        column_number=col1,
                        theorem_name=name,
                        goal_state=goal,
                        surrounding_context=context,
                        priority=priority,
                    )
                )
        return tasks

    # ------------------------------------------------------------- analysis
    @staticmethod
    def _enclosing_decl(code_lines: list[str], sorry_idx: int) -> Optional[tuple[int, str, str]]:
        """Nearest declaration at/above the sorry line (fixes v39 P2-19)."""
        for i in range(sorry_idx, -1, -1):
            m = _DECL_RE.match(code_lines[i])
            if m:
                return i, m.group(1), m.group(2)
        return None

    @staticmethod
    def _priority_for(rel_file: str, content: str) -> PriorityLevel:
        """Deterministic heuristic: impossible|hard -> P0, trivial -> P2, else P1."""
        text = (rel_file + "\n" + content).lower()
        if "impossible" in text or "hard" in text:
            return PriorityLevel.P0_CRITICAL
        if "trivial" in text:
            return PriorityLevel.P2_MEDIUM
        return PriorityLevel.P1_IMPORTANT

    @staticmethod
    def _extract_goal(code: str, code_lines: list[str], decl_idx: int) -> str:
        """Goal type via paren-balanced scan of the declaration header."""
        # Offset of the declaration start within the full comment-stripped text.
        decl_start = sum(len(l) + 1 for l in code_lines[:decl_idx])
        n = len(code)
        m = re.match(
            r"\s*(?:theorem|lemma|def|instance|example|abbrev)\s+\S+",
            code[decl_start:],
        )
        if not m:
            return ""
        i = decl_start + m.end()
        limit = min(n, i + 4000)  # safety bound

        def scan(start: int, stop_at_colon: bool) -> int:
            depth = 0
            in_str = False
            j = start
            while j < limit:
                c = code[j]
                if in_str:
                    if c == "\\":
                        j += 2
                        continue
                    if c == '"':
                        in_str = False
                    j += 1
                    continue
                if c == '"':
                    in_str = True
                elif c in "([{":
                    depth += 1
                elif c in ")]}":
                    depth = max(0, depth - 1)
                elif depth == 0:
                    if stop_at_colon and c == ":" and not code.startswith(":=", j):
                        return j
                    if not stop_at_colon and code.startswith(":=", j):
                        return j
                j += 1
            return -1

        colon = scan(i, stop_at_colon=True)
        if colon < 0:
            return ""
        assign = scan(colon + 1, stop_at_colon=False)
        if assign < 0:
            return ""
        goal = code[colon + 1:assign]
        return re.sub(r"\s+", " ", goal).strip()


class SorryDBClient:
    """Optional SorryDB snapshot source (frontier_resources.md section 1).

    ``endpoint`` may be:
    - ``None`` -> disabled (``enabled`` is False, ``fetch_tasks`` -> []);
    - an ``http(s)://`` URL of a published SorryDB snapshot (e.g. the data
      repo's ``deduplicated_sorries.json`` or the 125 KB smoke set
      ``static_100_varied_recent_deduplicated_sorries.json``);
    - a local filesystem path (or ``file://`` URI) to a snapshot file.

    Snapshot formats accepted (real SorryDB layout, doc/DATABASE.md):
    - **JSON**: a single object ``{"repos": [...], "sorries": [...]}`` (or a
      bare list of sorry entries);
    - **JSONL**: one sorry entry per line.

    Each entry follows the SorryDB pydantic model: ``id``,
    ``repo{remote, branch, commit, lean_version}``,
    ``location{path, start_line, start_column, end_line, end_column}``,
    ``debug_info{goal, url}``, ``metadata{...}``. Missing fields are tolerated
    (entry skipped only when file/line are unusable). **No fake tasks are ever
    injected** (v39 P1-9): empty payloads or any failure log a WARNING and
    return ``[]``.
    """

    def __init__(self, endpoint: Optional[str] = None, timeout_s: float = 10.0) -> None:
        self._endpoint = endpoint
        self._timeout = float(timeout_s)

    @property
    def enabled(self) -> bool:
        return bool(self._endpoint)

    async def fetch_tasks(self, project_path: str = "") -> list[SorryTask]:
        """Fetch snapshot sorries; on any failure log a WARNING and return []."""
        if not self.enabled:
            return []
        try:
            text = await self._load_text()
        except Exception as exc:
            # Never inject fake tasks; just report emptiness.
            logger.warning("SorryDBClient: fetch failed (%r); returning []", exc)
            return []
        entries = self._parse_payload(text)
        if not entries:
            logger.warning(
                "SorryDBClient: no sorry entries parsed from %s", self._endpoint
            )
            return []
        tasks = [t for t in (self._parse_entry(e, project_path) for e in entries) if t]
        if not tasks:
            logger.warning("SorryDBClient: endpoint returned no usable tasks")
        return tasks

    # ------------------------------------------------------------ loading
    async def _load_text(self) -> str:
        """Return the raw snapshot text from a URL or a local file."""
        endpoint = str(self._endpoint)
        if endpoint.startswith(("http://", "https://")):
            try:
                import httpx
            except Exception as exc:
                raise RuntimeError(f"httpx unavailable: {exc!r}") from exc
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(endpoint)
                resp.raise_for_status()
                return resp.text
        path = endpoint[7:] if endpoint.startswith("file://") else endpoint
        # Local snapshot file; IO is tiny vs network so a thread offload is
        # enough to keep the event loop responsive for large (65 MB) files.
        return await asyncio.to_thread(Path(path).read_text, encoding="utf-8")

    # ------------------------------------------------------------ parsing
    @staticmethod
    def _parse_payload(text: str) -> list:
        """Parse snapshot text into a list of raw entry dicts (JSON or JSONL)."""
        import json

        text = (text or "").strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            # Real SorryDB layout: {"repos": [...], "sorries": [...]}.
            for key in ("sorries", "items", "tasks"):
                entries = payload.get(key)
                if isinstance(entries, list):
                    return entries
            return []
        if isinstance(payload, list):
            return payload
        # JSONL fallback: one entry per non-blank line.
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                logger.warning("SorryDBClient: skipping unparseable JSONL line")
        return entries

    @classmethod
    def _parse_entry(cls, entry, project_path: str) -> Optional[SorryTask]:
        """Map one entry (SorryDB pydantic shape or legacy flat shape)."""
        if not isinstance(entry, dict):
            logger.warning("SorryDBClient: skipping non-dict entry")
            return None
        if isinstance(entry.get("location"), dict) or isinstance(entry.get("repo"), dict):
            return cls._parse_sorrydb_entry(entry, project_path)
        return cls._parse_flat_entry(entry, project_path)

    @classmethod
    def _parse_sorrydb_entry(cls, entry: dict, project_path: str) -> Optional[SorryTask]:
        """SorryDB snapshot model -> SorryTask (missing fields tolerated)."""
        try:
            location = entry.get("location") or {}
            repo = entry.get("repo") or {}
            debug_info = entry.get("debug_info") or {}
            file_path = str(location.get("path") or entry.get("file_path") or "")
            line = int(location.get("start_line") or entry.get("line_number") or 0)
            if not file_path or line <= 0:
                raise ValueError("missing location.path/start_line")
            col = int(location.get("start_column") or entry.get("column_number") or 1)
            remote = str(repo.get("remote") or "")
            commit = str(repo.get("commit") or "")
            # Theorem name is not part of the SorryDB schema; keep whatever a
            # local/derived snapshot may add, else stay empty (tolerant).
            name = str(entry.get("theorem_name") or entry.get("name") or "")
            goal = str(debug_info.get("goal") or entry.get("goal_state") or "")
            context_bits = []
            if remote:
                context_bits.append(
                    f"repo: {remote}" + (f" @ {commit}" if commit else "")
                )
            if repo.get("lean_version"):
                context_bits.append(f"lean: {repo['lean_version']}")
            if debug_info.get("url"):
                context_bits.append(f"url: {debug_info['url']}")
            task_id = str(entry.get("id") or "") or hashlib.sha1(
                f"{file_path}:{line}:{col}".encode()
            ).hexdigest()[:12]
            return SorryTask(
                id=task_id,
                project_path=project_path or str(entry.get("project_path") or remote),
                file_path=file_path,
                line_number=line,
                column_number=col,
                theorem_name=name,
                goal_state=goal,
                surrounding_context=str(
                    entry.get("surrounding_context") or "\n".join(context_bits)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("SorryDBClient: skipping malformed entry (%r)", exc)
            return None

    @staticmethod
    def _parse_flat_entry(entry: dict, project_path: str) -> Optional[SorryTask]:
        """Legacy flat v40 shape (file_path/line_number/theorem_name)."""
        try:
            file_path = str(entry["file_path"])
            line = int(entry["line_number"])
            col = int(entry.get("column_number", 1))
            name = str(entry["theorem_name"])
            return SorryTask(
                id=hashlib.sha1(f"{file_path}:{line}:{col}".encode()).hexdigest()[:12],
                project_path=project_path or str(entry.get("project_path", "")),
                file_path=file_path,
                line_number=line,
                column_number=col,
                theorem_name=name,
                goal_state=str(entry.get("goal_state", "")),
                surrounding_context=str(entry.get("surrounding_context", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("SorryDBClient: skipping malformed entry (%r)", exc)
            return None
