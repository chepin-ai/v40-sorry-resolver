"""Resident interactive-REPL session pool with memory guard (roadmap item 1).

Motivation (LOCAL_GUIDE §7, Kimina Lean Server pattern): spawning a fresh
``lake env lean`` Dojo session per candidate costs a fixed spawn+elaborate
overhead (~0.6 s warm on the mini project) every time. A *resident* pool keeps
sessions alive and hands them to consecutive tasks, so the import header and
the elaborated prefix stay in the REPL environment and only the incremental
tactic work is paid per candidate.

Design:
  * ``ReplPool(project_path, size)`` with ``acquire(task) -> PooledSession`` /
    ``release(session)`` coordinated by an ``asyncio.Condition``.
  * Affinity binding: sessions are keyed by ``(project, file, theorem)``;
    consecutive tasks on the same theorem (the common engine loop: many
    candidates per theorem) reuse the same REPL. Eviction prefers sessions
    from *other* files so same-file work keeps its warm session as long as
    possible (LRU within the same file). Note: a lean-dojo session is bound
    to one theorem (the instrumented file differs per theorem), so "reuse"
    means same-theorem reuse; same-file/different-theorem sessions must be
    rebuilt and are still preferred for eviction *last*.
  * Memory guard: Lean 4.20 ignores ``-Dweak.max_memory`` (env_report §已知
    限制), so a REPL has *no* real memory ceiling. A background task
    periodically reads ``/proc/<pid>/status`` VmRSS over the whole process
    tree; a session above ``max_rss_mb`` is poisoned — dropped on release
    (checked-out) or evicted immediately and rebuilt (idle). This is the
    workaround for the "REPL has no memory limit" known limitation.
  * Health: any session error must be reported via ``release(sess, drop=True)``
    (or is detected via process liveness on acquire); broken sessions are
    never handed out again. ``close()`` is idempotent and kills the whole
    child-process tree of every session (plus an ``/proc/*/environ`` sweep
    for reparented orphans, the ``kill_descendants`` race from
    dojo_breakthrough §4.3) so no REPL processes leak.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..config import V40Config
from ..models import SorryTask
from .base import VerificationResult
from .dojo_v2 import (
    DojoV2UnavailableError,
    LeanDojoV2Verifier,
    TacticStepResult,
    _DojoSession,
    _split_tactics,
    _strip_by,
)

logger = logging.getLogger(__name__)

DEFAULT_POOL_SIZE = 2
DEFAULT_MAX_RSS_MB = 1500
DEFAULT_RSS_CHECK_INTERVAL_S = 5.0


class ReplPoolClosedError(RuntimeError):
    """Raised when acquiring from a closed pool."""


# ------------------------------------------------------------- /proc helpers


def _proc_tree_snapshot() -> dict[int, int]:
    """pid -> ppid map from /proc (best-effort; skips transient entries)."""
    table: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                data = fh.read()
            # comm may contain spaces/parens; ppid follows the last ')'.
            rparen = data.rfind(b")")
            parts = data[rparen + 1 :].split()
            # parts[0]=state, parts[1]=ppid
            table[int(entry)] = int(parts[1])
        except (OSError, ValueError, IndexError):
            continue
    return table


def _descendants(root_pid: int, table: Optional[dict[int, int]] = None) -> list[int]:
    """All descendant pids of ``root_pid`` (children first)."""
    if table is None:
        table = _proc_tree_snapshot()
    children: dict[int, list[int]] = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    out: list[int] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            out.append(child)
            stack.append(child)
    return out


def _rss_kb(pid: int) -> int:
    """VmRSS of a single pid in kB; 0 if the process is gone/unreadable."""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def tree_rss_kb(root_pid: int) -> int:
    """Total VmRSS (kB) of ``root_pid`` and all its descendants."""
    table = _proc_tree_snapshot()
    pids = [root_pid] + _descendants(root_pid, table)
    return sum(_rss_kb(p) for p in pids)


def kill_process_tree(root_pid: int) -> None:
    """SIGKILL the whole tree rooted at ``root_pid`` (children first)."""
    pids = _descendants(root_pid) + [root_pid]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            continue


def sweep_orphans_by_environ(token: str, exclude: Optional[set[int]] = None) -> int:
    """Kill own processes whose environment contains ``token``.

    Used to reclaim REPL processes that were reparented away from the
    ``lake env`` wrapper (the ``kill_descendants`` race); the patched dojo
    passes a per-session FIFO path via ``LEAN_DOJO_REQ_FIFO``/``LEAN_DOJO_
    RESP_FIFO`` env vars, which is a unique per-session marker.
    """
    if not token:
        return 0
    killed = 0
    needle = token.encode("utf-8", "replace")
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me or (exclude and pid in exclude):
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as fh:
                if needle in fh.read():
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return killed


# --------------------------------------------------------------- pool types


@dataclass
class PooledSession:
    """A checked-out (or pooled) resident REPL session."""

    key: tuple  # (project_path, file_path, theorem_name)
    session: _DojoSession
    root_pid: int
    fifo_dir: str
    init_state_id: int
    init_goals: str
    uses: int = 0
    tick: int = 0  # LRU clock, bumped on release
    checked_out: bool = True
    poisoned: bool = False  # memory guard or health check tripped
    task: Optional[SorryTask] = field(default=None, repr=False)


class ReplPool:
    """Async pool of resident interactive REPL sessions (see module docstring).

    Parameters:
        project_path: default project root (tasks may override per-task).
        size: max concurrent resident sessions.
        max_rss_mb: per-session process-tree VmRSS ceiling; above it the
            session is poisoned and rebuilt.
        rss_check_interval_s: memory-guard polling period.
        rss_reader: injectable ``(root_pid) -> rss_kb`` for tests (fake pid
            data); defaults to reading /proc.
        opener: optional ``LeanDojoV2Verifier`` used to build theorems and
            apply the lean-dojo patches; a default one is constructed from
            ``cfg`` otherwise.
    """

    def __init__(
        self,
        project_path: str,
        size: int = DEFAULT_POOL_SIZE,
        *,
        max_rss_mb: int = DEFAULT_MAX_RSS_MB,
        lean_timeout_s: float = 30.0,
        rss_check_interval_s: float = DEFAULT_RSS_CHECK_INTERVAL_S,
        rss_reader: Optional[Callable[[int], int]] = None,
        opener: Optional[LeanDojoV2Verifier] = None,
        cfg: Optional[V40Config] = None,
        start_guard: bool = True,
    ) -> None:
        if size < 1:
            raise ValueError(f"pool size must be >= 1, got {size}")
        self.project_path = project_path
        self.size = int(size)
        self.max_rss_mb = int(max_rss_mb)
        self.lean_timeout_s = float(lean_timeout_s)
        self.rss_check_interval_s = float(rss_check_interval_s)
        self._rss_reader = rss_reader or tree_rss_kb
        self._opener = opener
        self._cfg = cfg
        self._start_guard = start_guard

        self._cond: Optional[asyncio.Condition] = None
        self._idle: dict[tuple, list[PooledSession]] = {}
        self._live: set[int] = set()  # id(session) of all live sessions
        self._sessions: dict[int, PooledSession] = {}
        self._opening = 0
        self._closed = False
        self._tick = 0
        self._guard_task: Optional[asyncio.Task] = None
        self.stats: dict[str, int] = {
            "spawned": 0,
            "reused": 0,
            "evicted": 0,
            "dropped": 0,
            "memory_evictions": 0,
        }

    # ------------------------------------------------------------ internals
    def _get_cond(self) -> asyncio.Condition:
        # Loop-aware lazy condition (robust under pytest-asyncio per-test loops).
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def _key_for(self, task: SorryTask) -> tuple:
        project = task.project_path or self.project_path
        return (project, task.file_path, task.theorem_name)

    def _theorem_for(self, task: SorryTask):
        from lean_dojo import LeanGitRepo, Theorem

        project = task.project_path or self.project_path
        commit = "HEAD"
        try:
            import git

            commit = git.Repo(project).head.commit.hexsha
        except Exception:
            try:
                out = subprocess.run(
                    ["git", "-C", project, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if out.returncode == 0:
                    commit = out.stdout.strip()
            except Exception:
                pass
        repo = LeanGitRepo(project, commit)
        return Theorem(repo, Path(task.file_path), task.theorem_name)

    def _spawn_sync(self, task: SorryTask) -> PooledSession:
        """Open a fresh resident session for ``task`` (blocking; ~0.6 s warm)."""
        if self._opener is not None:
            theorem = self._opener._theorem_for(task)
            timeout = self._opener._timeout
        else:
            theorem = self._theorem_for(task)
            timeout = max(int(self.lean_timeout_s), 30)
        sess = _DojoSession(theorem, timeout)
        proc = getattr(sess.dojo, "proc", None)
        root_pid = int(getattr(proc, "pid", 0) or 0)
        fifo_dir = str(getattr(sess.dojo, "_fifo_dir", "") or "")
        return PooledSession(
            key=self._key_for(task),
            session=sess,
            root_pid=root_pid,
            fifo_dir=fifo_dir,
            init_state_id=sess.init_state.id,
            init_goals=sess.init_state.pp,
            task=task,
        )

    def _close_session_sync(self, sess: PooledSession) -> None:
        try:
            sess.session.close()
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.warning("pooled session teardown raised %r", exc)
        if sess.root_pid:
            kill_process_tree(sess.root_pid)
            try:
                # Reap the root if it is our direct child (pexpect spawn);
                # otherwise it lingers as a zombie until GC.
                os.waitpid(sess.root_pid, os.WNOHANG)
            except OSError:
                pass
        # Reclaim reparented orphans carrying this session's FIFO env marker.
        swept = sweep_orphans_by_environ(sess.fifo_dir)
        if swept:
            logger.info("reaped %d orphaned REPL process(es) for %s", swept, sess.fifo_dir)

    async def _close_session(self, sess: PooledSession) -> None:
        self._sessions.pop(id(sess), None)
        try:
            await asyncio.to_thread(self._close_session_sync, sess)
        except Exception as exc:  # noqa: BLE001
            logger.warning("closing pooled session raised %r", exc)

    def _is_unusable(self, sess: PooledSession) -> bool:
        if sess.poisoned or sess.session.closed:
            return True
        if sess.root_pid and not Path(f"/proc/{sess.root_pid}").exists():
            return True  # process vanished: never propagate a broken session
        return False

    # ------------------------------------------------------------------ API
    async def acquire(self, task: SorryTask) -> PooledSession:
        """Return a resident session bound to ``task``'s theorem.

        Reuses a warm session when the exact (project, file, theorem) key is
        idle; otherwise spawns a new one, evicting the LRU idle session
        (preferring other files) when the pool is full. Blocks on the
        condition variable while all sessions are checked out.
        """
        key = self._key_for(task)
        self._ensure_guard()
        cond = self._get_cond()
        while True:
            evict: Optional[PooledSession] = None
            async with cond:
                if self._closed:
                    raise ReplPoolClosedError("ReplPool is closed")
                # 1) exact-key reuse (warm REPL: import head + prefix loaded)
                idle_list = self._idle.get(key)
                while idle_list:
                    cand = idle_list.pop()
                    if not self._is_unusable(cand):
                        cand.checked_out = True
                        cand.uses += 1
                        self.stats["reused"] += 1
                        return cand
                    self._live.discard(id(cand))
                    evict = cand  # broken idle session: reap outside the lock
                    break
                if evict is None:
                    # 2) capacity available -> open a new session
                    if len(self._live) + self._opening < self.size:
                        self._opening += 1
                        break  # spawn outside the lock
                    # 3) evict the LRU idle session, preferring other files
                    victim = self._pick_eviction(key)
                    if victim is not None:
                        self._idle[victim.key].remove(victim)
                        self._live.discard(id(victim))
                        self.stats["evicted"] += 1
                        evict = victim
                    else:
                        # 4) everything is checked out: wait for a release
                        await cond.wait()
                        continue
            if evict is not None:
                await self._close_session(evict)

        try:
            sess = await asyncio.to_thread(self._spawn_sync, task)
        except Exception:
            async with cond:
                self._opening -= 1
                cond.notify_all()
            raise
        async with cond:
            self._opening -= 1
            if self._closed:
                cond.notify_all()
                close_later = True
            else:
                self._live.add(id(sess))
                self._sessions[id(sess)] = sess
                self.stats["spawned"] += 1
                close_later = False
            cond.notify_all()
        if close_later:
            await self._close_session(sess)
            raise ReplPoolClosedError("ReplPool closed while spawning")
        return sess

    async def release(self, sess: PooledSession, *, drop: bool = False) -> None:
        """Return ``sess`` to the pool (or destroy it with ``drop=True``).

        Callers must pass ``drop=True`` after any session error so a poisoned
        REPL is never reused (bad state never propagates).
        """
        cond = self._get_cond()
        to_close = False
        async with cond:
            sess.checked_out = False
            if id(sess) not in self._live:
                return  # unknown/already-reaped session: nothing to do
            if self._closed or drop or sess.poisoned or sess.session.closed:
                self._live.discard(id(sess))
                to_close = True
                if drop or sess.poisoned:
                    self.stats["dropped"] += 1
            else:
                self._tick += 1
                sess.tick = self._tick
                self._idle.setdefault(sess.key, []).append(sess)
            cond.notify_all()
        if to_close:
            await self._close_session(sess)

    async def close(self) -> None:  # idempotent
        cond = self._get_cond()
        async with cond:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values())
            self._idle.clear()
            self._live.clear()
            cond.notify_all()
        if self._guard_task is not None:
            self._guard_task.cancel()
            try:
                await self._guard_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._guard_task = None
        await asyncio.gather(
            *(self._close_session(s) for s in sessions), return_exceptions=True
        )

    # ----------------------------------------------------------- eviction
    def _pick_eviction(self, key: tuple) -> Optional[PooledSession]:
        """LRU idle session, preferring sessions from *other* files.

        Same-(project, file) sessions are kept warm as long as possible so
        consecutive tasks on one file hit a resident REPL (affinity binding).
        """
        target_file = key[:2]
        candidates: list[PooledSession] = []
        for lst in self._idle.values():
            candidates.extend(lst)
        if not candidates:
            return None
        other_file = [c for c in candidates if c.key[:2] != target_file]
        pool = other_file or candidates
        return min(pool, key=lambda c: c.tick)

    # -------------------------------------------------------- memory guard
    def _ensure_guard(self) -> None:
        if not self._start_guard or self._guard_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._guard_task = loop.create_task(self._memory_guard_loop())

    async def _memory_guard_loop(self) -> None:
        while True:
            await asyncio.sleep(self.rss_check_interval_s)
            try:
                await self._check_memory_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - guard must never crash
                logger.warning("memory guard iteration failed: %r", exc)

    async def _check_memory_once(self) -> None:
        """One memory-guard sweep (public for deterministic tests)."""
        cond = self._get_cond()
        async with cond:
            snapshot = list(self._sessions.values())
        for sess in snapshot:
            if id(sess) not in self._live:
                continue
            try:
                rss_kb = await asyncio.to_thread(self._rss_reader, sess.root_pid)
            except Exception:  # noqa: BLE001
                rss_kb = 0
            if rss_kb <= self.max_rss_mb * 1024:
                continue
            self.stats["memory_evictions"] += 1
            logger.warning(
                "session %s exceeded memory guard (%d kB > %d MB); evicting",
                sess.key,
                rss_kb,
                self.max_rss_mb,
            )
            if sess.checked_out:
                # In use: poison so release() destroys it instead of reusing.
                sess.poisoned = True
                continue
            # Idle: evict immediately and rebuild a warm replacement.
            async with cond:
                if id(sess) not in self._live:
                    continue
                lst = self._idle.get(sess.key)
                if lst and sess in lst:
                    lst.remove(sess)
                self._live.discard(id(sess))
                cond.notify_all()
            await self._close_session(sess)
            await self._rebuild_idle(sess, cond)

    async def _rebuild_idle(self, old: PooledSession, cond) -> None:
        """Respawn a warm session for an evicted idle slot (best-effort)."""
        if old.task is None:
            return
        async with cond:
            if self._closed:
                return
            self._opening += 1
        try:
            sess = await asyncio.to_thread(self._spawn_sync, old.task)
        except Exception as exc:  # noqa: BLE001 - rebuild is best-effort
            logger.warning("rebuild after memory eviction failed: %r", exc)
            sess = None
        async with cond:
            self._opening -= 1
            if sess is not None and not self._closed:
                sess.checked_out = False
                self._tick += 1
                sess.tick = self._tick
                self._live.add(id(sess))
                self._sessions[id(sess)] = sess
                self._idle.setdefault(sess.key, []).append(sess)
                self.stats["spawned"] += 1
            elif sess is not None:
                asyncio.get_running_loop().create_task(self._close_session(sess))
            cond.notify_all()


# ------------------------------------------------------------ pool verifier


class ReplPoolVerifier:
    """SPEC 3.6 Verifier backed by a resident ``ReplPool`` (backend ``repl``).

    Whole-proof acceptance still requires a kernel-checked ProofFinished from
    the interactive REPL (same judgement chain as ``LeanDojoV2Verifier``);
    sessions are reused across candidates via the pool.
    """

    def __init__(self, cfg: V40Config) -> None:
        self._cfg = cfg
        self._timeout = max(float(getattr(cfg, "lean_timeout_s", 30.0) or 30.0), 30.0)
        self._pool_size = int(getattr(cfg, "repl_pool_size", DEFAULT_POOL_SIZE) or DEFAULT_POOL_SIZE)
        self._max_rss = int(getattr(cfg, "repl_max_rss_mb", DEFAULT_MAX_RSS_MB) or DEFAULT_MAX_RSS_MB)
        self._ready = False
        self._pools: dict[str, ReplPool] = {}
        self._pools_lock = asyncio.Lock()
        self._opener: Optional[LeanDojoV2Verifier] = None

    async def init(self) -> None:
        opener = LeanDojoV2Verifier(self._cfg)
        await opener.init()  # raises DojoV2UnavailableError when unavailable
        self._opener = opener
        self._ready = True

    async def close(self) -> None:  # re-entrant
        self._ready = False
        async with self._pools_lock:
            pools = list(self._pools.values())
            self._pools = {}
        await asyncio.gather(*(p.close() for p in pools), return_exceptions=True)
        if self._opener is not None:
            await self._opener.close()

    def pool_for(self, project_path: str) -> ReplPool:
        pool = self._pools.get(project_path)
        if pool is None:
            pool = ReplPool(
                project_path,
                size=self._pool_size,
                max_rss_mb=self._max_rss,
                lean_timeout_s=self._timeout,
                opener=self._opener,
            )
            self._pools[project_path] = pool
        return pool

    # ------------------------------------------------- tactic-level probing
    async def open_task(self, task: SorryTask) -> tuple[PooledSession, TacticStepResult]:
        """Acquire a resident session for ``task``; caller MUST release it."""
        if not self._ready or self._opener is None:
            raise DojoV2UnavailableError("ReplPoolVerifier.init() did not complete.")
        pool = self.pool_for(task.project_path)
        sess = await pool.acquire(task)
        return sess, TacticStepResult(
            ok=True, state_id=sess.init_state_id, goals=sess.init_goals
        )

    async def run_tactic(
        self, sess: PooledSession, state_id: int, tactic: str
    ) -> TacticStepResult:
        """Apply one tactic on a checked-out pooled session."""
        if not self._ready or self._opener is None:
            raise DojoV2UnavailableError("ReplPoolVerifier.init() did not complete.")
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(
                    self._opener._run_tac_sync, sess.session, state_id, tactic
                ),
                timeout=self._timeout + 15.0,
            )
        except asyncio.TimeoutError:
            sess.poisoned = True
            return TacticStepResult(
                ok=False, error=f"tactic timed out after {self._timeout}s"
            )
        except Exception as exc:  # noqa: BLE001
            sess.poisoned = True
            return TacticStepResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return res

    # -------------------------------------------------------------- Verifier
    async def verify_proof(self, task: SorryTask, proof: str) -> VerificationResult:
        """Accept iff ``proof`` reaches kernel-checked ProofFinished on a pooled REPL."""
        t0 = time.monotonic()
        pool = self.pool_for(task.project_path)
        sess: Optional[PooledSession] = None
        drop = False
        try:
            sess = await pool.acquire(task)
            ok, diag = await self._drive(sess, proof)
        except Exception as exc:  # noqa: BLE001 - honest rejection, session dropped
            drop = True
            return VerificationResult(
                ok=False,
                error=f"repl pool error: {type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - t0,
            )
        finally:
            if sess is not None:
                await pool.release(sess, drop=drop)
        return VerificationResult(
            ok=ok,
            error=None if ok else "repl pool did not reach ProofFinished",
            duration_s=time.monotonic() - t0,
            diagnostics=diag,
        )

    async def _drive(self, sess: PooledSession, proof: str) -> tuple[bool, str]:
        """Whole-tactic attempt then line-by-line fallback (dojo_v2 algorithm)."""
        diag: list[str] = []
        whole = _strip_by(proof)
        if whole:
            res = await self.run_tactic(sess, sess.init_state_id, whole)
            diag.append(f"<whole> -> ok={res.ok} finished={res.finished}")
            if res.finished:
                return True, "\n".join(diag)
            if res.ok is False and res.error and "timed out" in res.error:
                raise RuntimeError(res.error)
        state_id = sess.init_state_id
        for tac in _split_tactics(proof):
            res = await self.run_tactic(sess, state_id, tac)
            diag.append(f"{tac!r} -> ok={res.ok} finished={res.finished}")
            if res.finished:
                return True, "\n".join(diag)
            if not res.ok:
                return False, "\n".join(diag + [res.error or "?"])
            state_id = res.state_id
        return False, "\n".join(diag + ["goals remained"])
