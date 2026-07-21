"""Real REPL-pool tests against the mini project (lean-dojo sessions).

These are NOT mocks: sessions are real ``lake env lean`` Dojo processes on
/mnt/agents/output/lean_mini_project (same toolchain requirements as
tests/test_dojo_v2.py). Skipped (never failed) when the toolchain or the
dojo trace cache is unavailable.

Covered (task acceptance):
  * concurrent acquire is mutually exclusive (pool size 1)
  * affinity reuse counting (same theorem -> same REPL, stats["reused"])
  * same-file different-theorem eviction when the pool is full
  * memory guard trigger path with fake pid/RSS data (injected rss_reader):
    idle session evicted + rebuilt; checked-out session poisoned -> dropped
  * process-leak cleanup: close() kills every session's process tree
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import pytest_asyncio

from v40_sorry_resolver.verify import LeanDojoV2Verifier, ReplPool

MINI_PROJECT = Path("/mnt/agents/output/lean_mini_project")


def _toolchain_available() -> bool:
    if shutil.which("lean") is None and not (Path.home() / ".elan/bin/lean").exists():
        return False
    try:
        import lean_dojo  # noqa: F401
    except Exception:
        return False
    return MINI_PROJECT.is_dir()


def _repo():
    from lean_dojo import LeanGitRepo

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=MINI_PROJECT,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return LeanGitRepo(str(MINI_PROJECT), commit)


def _traced(repo) -> bool:
    try:
        from lean_dojo.data_extraction.trace import is_available_in_cache

        return is_available_in_cache(repo)
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _toolchain_available(),
        reason="Lean toolchain / lean_dojo / mini project unavailable",
    ),
    pytest.mark.skipif(
        not (_toolchain_available() and _traced(_repo())),
        reason="mini project not traced yet (run /mnt/agents/output/trace_noapi.py once)",
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def opener(config):
    v = LeanDojoV2Verifier(config)
    await v.init()
    yield v
    await v.close()


def _by_name(tasks):
    return {t.theorem_name: t for t in tasks}


def _proc_alive(pid: int) -> bool:
    """True iff pid exists and is not a zombie (reaped-soon corpse)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        state = data[data.rfind(b")") + 1 :].split()[0].decode()
        return state != "Z"
    except (OSError, IndexError):
        return False


# ---------------------------------------------------------- mutual exclusion


async def test_concurrent_acquire_mutual_exclusion(config, mini_tasks, opener):
    """Pool size 1: at most one checked-out session at any moment."""
    pool = ReplPool(str(MINI_PROJECT), size=1, opener=opener)
    task = _by_name(mini_tasks)["nat_refl"]
    active = 0
    max_active = 0

    async def worker():
        nonlocal active, max_active
        sess = await pool.acquire(task)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        await pool.release(sess)

    try:
        await asyncio.gather(*(worker() for _ in range(3)))
        assert max_active == 1
    finally:
        await pool.close()


# ------------------------------------------------------------ affinity reuse


async def test_affinity_reuse_same_theorem(config, mini_tasks, opener):
    """Consecutive tasks on one theorem reuse the resident REPL session."""
    pool = ReplPool(str(MINI_PROJECT), size=1, opener=opener)
    task = _by_name(mini_tasks)["nat_refl"]
    try:
        first = await pool.acquire(task)
        pid = first.root_pid
        await pool.release(first)
        for _ in range(3):
            sess = await pool.acquire(task)
            assert sess.root_pid == pid  # same resident process
            assert sess.init_goals.strip() != ""
            await pool.release(sess)
        assert pool.stats["spawned"] == 1
        assert pool.stats["reused"] == 3
        assert pool.stats["evicted"] == 0
    finally:
        await pool.close()


async def test_same_file_other_theorem_evicts_when_full(config, mini_tasks, opener):
    """Pool size 1, same file other theorem: LRU eviction, no same-key reuse."""
    pool = ReplPool(str(MINI_PROJECT), size=1, opener=opener)
    tasks = _by_name(mini_tasks)
    try:
        s1 = await pool.acquire(tasks["nat_refl"])
        await pool.release(s1)
        s2 = await pool.acquire(tasks["one_plus_one"])
        assert s2.key != s1.key
        await pool.release(s2)
        assert pool.stats["evicted"] >= 1
        assert pool.stats["spawned"] == 2
    finally:
        await pool.close()


# --------------------------------------------------------- memory guard path


async def test_memory_guard_evicts_idle_and_rebuilds(config, mini_tasks, opener):
    """Fake RSS over the limit: idle session evicted and warm replacement built."""
    pool = ReplPool(
        str(MINI_PROJECT),
        size=1,
        opener=opener,
        max_rss_mb=100,
        rss_reader=lambda pid: 500 * 1024,  # fake pid data: 500 MB > 100 MB
        start_guard=False,  # deterministic: drive the sweep manually
    )
    task = _by_name(mini_tasks)["nat_refl"]
    try:
        sess = await pool.acquire(task)
        old_pid = sess.root_pid
        await pool.release(sess)
        await pool._check_memory_once()
        assert pool.stats["memory_evictions"] == 1
        # a warm replacement was rebuilt for the idle slot
        sess2 = await pool.acquire(task)
        assert sess2.root_pid != old_pid
        assert pool.stats["spawned"] == 2
        assert pool.stats["reused"] == 1  # warm rebuilt session, first checkout
        await pool.release(sess2)
    finally:
        await pool.close()


async def test_memory_guard_poisons_checked_out_session(config, mini_tasks, opener):
    """Checked-out session over the limit is poisoned and dropped on release."""
    pool = ReplPool(
        str(MINI_PROJECT),
        size=1,
        opener=opener,
        max_rss_mb=100,
        rss_reader=lambda pid: 500 * 1024,
        start_guard=False,
    )
    task = _by_name(mini_tasks)["nat_refl"]
    try:
        sess = await pool.acquire(task)
        await pool._check_memory_once()
        assert sess.poisoned is True
        await pool.release(sess)
        assert pool.stats["dropped"] >= 1
        # poisoned session must never be handed out again
        sess2 = await pool.acquire(task)
        assert sess2 is not sess
        await pool.release(sess2)
    finally:
        await pool.close()


# ------------------------------------------------------------ leak cleanup


async def test_close_kills_process_tree(config, mini_tasks, opener):
    """close() tears down every session process (leak backstop)."""
    pool = ReplPool(str(MINI_PROJECT), size=1, opener=opener)
    task = _by_name(mini_tasks)["nat_refl"]
    sess = await pool.acquire(task)
    pid = sess.root_pid
    assert pid > 0 and _proc_alive(pid)
    await pool.close()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _proc_alive(pid):
        await asyncio.sleep(0.05)
    assert not _proc_alive(pid), f"REPL process {pid} leaked"
    # close is idempotent
    await pool.close()


async def test_close_idempotent_and_acquire_after_close_fails(config, opener):
    pool = ReplPool(str(MINI_PROJECT), size=1, opener=opener)
    await pool.close()
    await pool.close()
    from v40_sorry_resolver.verify.repl_pool import ReplPoolClosedError

    from v40_sorry_resolver.models import SorryTask

    task = SorryTask(
        id="x",
        project_path=str(MINI_PROJECT),
        file_path="LeanMiniProject/Trivial.lean",
        line_number=1,
        column_number=1,
        theorem_name="nat_refl",
    )
    with pytest.raises(ReplPoolClosedError):
        await pool.acquire(task)


# ------------------------------------------------- end-to-end via pool proof


async def test_pool_drives_real_proof(config, mini_tasks, opener):
    """A pooled session really proves nat_refl via run_tac (health smoke)."""
    pool = ReplPool(str(MINI_PROJECT), size=1, opener=opener)
    task = _by_name(mini_tasks)["nat_refl"]
    try:
        sess = await pool.acquire(task)
        res = await asyncio.to_thread(
            opener._run_tac_sync, sess.session, sess.init_state_id, "rfl"
        )
        assert res.ok and res.finished
        await pool.release(sess)
    finally:
        await pool.close()
