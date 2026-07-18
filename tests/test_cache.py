"""Tests for v40_sorry_resolver.cache (SPEC 3.5)."""

import pytest
import pytest_asyncio

from v40_sorry_resolver.cache import Cache

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def cache(tmp_path):
    c = Cache(str(tmp_path / "cache.db"))
    yield c
    await c.close()


class TestGetSet:
    async def test_set_get_roundtrip(self, cache):
        await cache.set("k1", "v1")
        assert await cache.get("k1") == "v1"

    async def test_get_miss_returns_none(self, cache):
        assert await cache.get("missing") is None

    async def test_namespaces_isolated(self, cache):
        await cache.set("k", "a", namespace="ns1")
        await cache.set("k", "b", namespace="ns2")
        assert await cache.get("k", namespace="ns1") == "a"
        assert await cache.get("k", namespace="ns2") == "b"
        assert await cache.get("k", namespace="ns3") is None

    async def test_overwrite(self, cache):
        await cache.set("k", "old")
        await cache.set("k", "new")
        assert await cache.get("k") == "new"

    async def test_non_string_value_serialized(self, cache):
        await cache.set("k", {"a": 1})
        assert await cache.get("k") == '{"a": 1}'

    async def test_pending_write_visible_via_lru_before_flush(self, cache):
        # set() returns after enqueue; the value must already be readable
        # from the LRU even before the writer persists it.
        await cache.set("pending", "x")
        assert await cache.get("pending") == "x"


class TestSingleWriterPersistence:
    async def test_flush_persists_to_db(self, tmp_path):
        db_path = str(tmp_path / "cache.db")
        c = Cache(db_path)
        await c.set("k", "v")
        await c.flush()
        await c.close()
        # Fresh instance: LRU empty, must hit the DB layer.
        c2 = Cache(db_path)
        assert await c2.get("k") == "v"
        await c2.close()

    async def test_close_flushes(self, tmp_path):
        db_path = str(tmp_path / "cache.db")
        c = Cache(db_path)
        for i in range(50):
            await c.set(f"k{i}", f"v{i}")
        await c.close()  # close() must flush
        c2 = Cache(db_path)
        for i in range(50):
            assert await c2.get(f"k{i}") == f"v{i}"
        await c2.close()

    async def test_many_writes_no_loss(self, tmp_path):
        db_path = str(tmp_path / "cache.db")
        c = Cache(db_path, write_batch_size=16)
        n = 500
        for i in range(n):
            await c.set(f"key-{i}", f"value-{i}", namespace="bulk")
        await c.close()
        c2 = Cache(db_path)
        for i in range(n):
            assert await c2.get(f"key-{i}", namespace="bulk") == f"value-{i}"
        await c2.close()


class TestBoundedLRU:
    async def test_lru_eviction_keeps_db_copy(self, tmp_path):
        db_path = str(tmp_path / "cache.db")
        c = Cache(db_path, lru_capacity=4)
        for i in range(10):
            await c.set(f"k{i}", f"v{i}")
        assert len(c._lru) == 4
        await c.close()
        # Everything still retrievable (DB fallback).
        c2 = Cache(db_path, lru_capacity=4)
        for i in range(10):
            assert await c2.get(f"k{i}") == f"v{i}"
        await c2.close()

    async def test_lru_recency_order(self, tmp_path):
        c = Cache(str(tmp_path / "cache.db"), lru_capacity=3)
        await c.set("a", "1")
        await c.set("b", "2")
        await c.set("c", "3")
        assert await c.get("a") == "1"  # touch 'a' -> most recent
        await c.set("d", "4")  # evicts 'b'
        assert list(c._lru.keys()) == [
            c._lru_key("default", "c"),
            c._lru_key("default", "a"),
            c._lru_key("default", "d"),
        ]
        await c.close()


class TestClose:
    async def test_close_reentrant(self, cache):
        await cache.set("k", "v")
        await cache.close()
        await cache.close()  # must not raise

    async def test_set_after_close_dropped_not_raised(self, cache, caplog):
        await cache.close()
        await cache.set("late", "x")  # warning, no exception
        assert any("closed" in r.getMessage() for r in caplog.records)

    async def test_get_after_close_safe(self, cache):
        await cache.set("k", "v")
        await cache.close()
        # LRU may still serve; DB is closed. Either way, no exception.
        await cache.get("k")

    async def test_close_without_use(self, tmp_path):
        c = Cache(str(tmp_path / "cache.db"))
        await c.close()
        await c.close()
