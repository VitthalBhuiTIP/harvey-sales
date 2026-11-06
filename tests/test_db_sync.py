"""Keeping MySQL in step: auto-sync throttle, never fatal, stale rows flagged."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from harvey import db_sync, sales_db
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_USER", "harvey")
    monkeypatch.delenv("MYSQL_AUTO_SYNC", raising=False)
    monkeypatch.delenv("MYSQL_SYNC_MINUTES", raising=False)
    monkeypatch.setattr(db_sync, "is_main_db", lambda p: True)
    s = StateManager(str(tmp_path / "harvey.db"))
    await s.init_db()
    return s


@pytest.mark.asyncio
async def test_only_harveys_own_database_is_ever_synced(tmp_path, monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_USER", "harvey")
    monkeypatch.delenv("MYSQL_AUTO_SYNC", raising=False)

    def must_not_connect(*a, **k):
        raise AssertionError("tried to sync a test database into MySQL")
    monkeypatch.setattr(db_sync, "_sync_blocking", must_not_connect)
    with pytest.raises(RuntimeError, match="only Harvey's own"):
        await db_sync.sync_all(tmp_path / "other.db", None)
    other = StateManager(str(tmp_path / "other.db"))
    await other.init_db()
    assert await db_sync.maybe_auto_sync(other, None) is None


@pytest.mark.asyncio
async def test_auto_sync_is_throttled_and_records_the_time(state, monkeypatch):
    calls = []

    async def fake(db_path, config, st=None):
        calls.append(db_path)
        await st.set_setting(db_sync.SYNCED_AT_KEY, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        return {"mysql": {"companies": 1}, "sales_db": {"companies": 1}, "at": "now"}
    monkeypatch.setattr(db_sync, "sync_all", fake)

    assert await db_sync.maybe_auto_sync(state, None) is not None
    assert await db_sync.maybe_auto_sync(state, None) is None        # within 15 minutes
    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M UTC")
    await state.set_setting(db_sync.SYNCED_AT_KEY, old)
    assert await db_sync.maybe_auto_sync(state, None) is not None
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_auto_sync_off_or_unconfigured(state, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("should not sync")
    monkeypatch.setattr(db_sync, "sync_all", boom)
    monkeypatch.setenv("MYSQL_AUTO_SYNC", "0")
    assert await db_sync.maybe_auto_sync(state, None) is None
    monkeypatch.delenv("MYSQL_AUTO_SYNC")
    monkeypatch.delenv("MYSQL_HOST")
    assert await db_sync.maybe_auto_sync(state, None) is None


@pytest.mark.asyncio
async def test_a_failed_sync_never_raises(state, monkeypatch):
    async def down(*a, **k):
        raise ConnectionError("MySQL is down")
    monkeypatch.setattr(db_sync, "sync_all", down)
    assert await db_sync.maybe_auto_sync(state, None) is None


def test_every_sales_table_tracks_in_harvey():
    for name, columns, _, _ in sales_db.TABLES:
        cols = [c for c, *_ in columns]
        assert "in_harvey" in cols and "synced_at" in cols, name
        sql, upsert_cols = sales_db.upsert_sql(name, columns)
        assert "in_harvey" in upsert_cols      # a re-appearing row is marked current again


class _Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def executemany(self, sql, rows):
        self.log.append((sql, rows))

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cursor(self.log)

    def commit(self):
        self.log.append(("COMMIT", None))

    def rollback(self):
        pass

    def close(self):
        pass


def test_write_flags_rows_harvey_no_longer_has(monkeypatch):
    log = []
    monkeypatch.setattr(sales_db, "_connect_mysql", lambda settings, database: _Conn(log))
    settings = sales_db.MySQLSettings("h", 3306, "u", "", "harvey_sales")
    sales_db.write(settings, {"contacts": [{"id": "p1", "email": "pat@acme.example"}]})

    upsert = next(rows for sql, rows in log if sql.startswith("INSERT INTO `contacts`"))
    cols = sales_db.upsert_sql("contacts", dict((t[0], t[1]) for t in sales_db.TABLES)["contacts"])[1]
    row = dict(zip(cols, upsert[0]))
    assert row["in_harvey"] == 1 and row["synced_at"].microsecond == 0
    sweeps = [(sql, p) for sql, p in log if sql.startswith("UPDATE `contacts` SET in_harvey = 0")]
    assert sweeps and sweeps[0][1] == (row["synced_at"],)
    # Every table is swept, including ones with no rows this time.
    assert len([s for s, _ in log if "SET in_harvey = 0" in s]) == len(sales_db.TABLES)
