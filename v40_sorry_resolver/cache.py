"""Persistent cache for the v40 sorry resolver (M1).

Contract: SPEC.md section 3.5. SQLite (WAL) persistence plus a bounded
in-memory LRU. All writes go through a single writer coroutine fed by an
``asyncio.Queue`` (fixes the v39 multi-writer SQLITE_BUSY silent batch loss).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

__all__ = ["Cache", "DEFAULT_LRU_CAPACITY"]

logger = logging.getLogger(__name__)

#: Bounded in-memory LRU capacity (SPEC: 5000).
DEFAULT_LRU_CAPACITY = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    namespace TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT,
    updated_at REAL,
    PRIMARY KEY (namespace, key)
)
"""


class Cache:
    """Async cache: bounded LRU in front of a single-writer SQLite store.

    - ``get``: checks the bounded LRU first, then the DB.
    - ``set``: updates the LRU and enqueues the write; returns immediately.
    - ``close``: flushes the queue, waits for the writer task to exit, and
      is re-entrant.
    """

    def __init__(
        self,
        db_path: str,
        lru_capacity: int = DEFAULT_LRU_CAPACITY,
        write_batch_size: int = 200,
    ) -> None:
        self._db_path = str(db_path)
        self._lru_capacity = max(1, int(lru_capacity))
        self._write_batch_size = max(1, int(write_batch_size))
        self._lru: OrderedDict = OrderedDict()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self._db: Optional[sqlite3.Connection] = None
        self._closed = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def get(self, key: str, namespace: str = "default") -> Optional[str]:
        """Return the cached value or None (LRU first, then DB)."""
        lru_key = self._lru_key(namespace, key)
        if lru_key in self._lru:
            self._lru.move_to_end(lru_key)
            return self._lru[lru_key]
        if self._closed:
            return None  # DB already closed; LRU-only mode
        db = self._ensure_db()
        if db is None:
            return None
        try:
            row = db.execute(
                "SELECT value FROM cache WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.error("cache get failed for key %s: %s", key, exc)
            return None
        if row is None:
            return None
        value = row[0]
        self._lru_put(lru_key, value)
        return value

    async def set(self, key: str, value, namespace: str = "default") -> None:
        """Enqueue a write and return immediately (single-writer mode)."""
        if self._closed:
            logger.warning("cache.set on closed cache dropped (key=%s)", key)
            return
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, default=str)
        self._lru_put(self._lru_key(namespace, key), value)
        self._ensure_writer()
        self._queue.put_nowait(("set", (namespace, key, value)))

    async def flush(self) -> None:
        """Wait until all queued writes have been persisted."""
        if self._closed or self._writer_task is None:
            return
        event = asyncio.Event()
        self._queue.put_nowait(("flush", event))
        await event.wait()

    async def close(self) -> None:
        """Flush pending writes, stop the writer task, close the DB.

        Re-entrant: subsequent calls return immediately.
        """
        if self._closed:
            return
        self._closed = True
        if self._writer_task is not None:
            event = asyncio.Event()
            self._queue.put_nowait(("close", event))
            await event.wait()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("cache writer task failed during close")
            self._writer_task = None
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                logger.exception("error closing cache db")
            self._db = None

    async def __aenter__(self) -> "Cache":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _lru_key(namespace: str, key: str) -> str:
        return f"{namespace}\x00{key}"

    def _lru_put(self, lru_key: str, value: str) -> None:
        self._lru[lru_key] = value
        self._lru.move_to_end(lru_key)
        while len(self._lru) > self._lru_capacity:
            self._lru.popitem(last=False)

    def _ensure_db(self) -> Optional[sqlite3.Connection]:
        # Note: no _closed check here — the writer's final flush runs while
        # close() is in progress and must still be able to open/use the DB.
        if self._db is not None:
            return self._db
        try:
            parent = Path(self._db_path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self._db_path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(_SCHEMA)
            db.commit()
        except (sqlite3.Error, OSError) as exc:
            logger.error("could not open cache db %s: %s", self._db_path, exc)
            return None
        self._db = db
        return db

    def _ensure_writer(self) -> None:
        if self._writer_task is None or self._writer_task.done():
            loop = asyncio.get_running_loop()
            self._writer_task = loop.create_task(
                self._writer_loop(), name="v40-cache-writer"
            )

    def _write_batch(self, items: list) -> None:
        db = self._ensure_db()
        if db is None:
            logger.error("cache db unavailable; dropping %d writes", len(items))
            return
        now = time.time()
        try:
            with db:
                db.executemany(
                    "INSERT OR REPLACE INTO cache (namespace, key, value, "
                    "updated_at) VALUES (?, ?, ?, ?)",
                    [(ns, key, value, now) for ns, key, value in items],
                )
        except sqlite3.Error as exc:
            logger.error("cache write batch of %d failed: %s", len(items), exc)

    async def _writer_loop(self) -> None:
        pending: list = []
        while True:
            op, payload = await self._queue.get()
            if op == "set":
                pending.append(payload)
                # Drain whatever else is already queued without blocking.
                while len(pending) < self._write_batch_size:
                    try:
                        next_op, next_payload = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if next_op == "set":
                        pending.append(next_payload)
                        continue
                    # flush/close barrier: persist what we have first.
                    if pending:
                        self._write_batch(pending)
                        pending = []
                    next_payload.set()
                    if next_op == "close":
                        return
                if len(pending) >= self._write_batch_size:
                    self._write_batch(pending)
                    pending = []
            else:  # flush / close barrier
                if pending:
                    self._write_batch(pending)
                    pending = []
                payload.set()
                if op == "close":
                    return
