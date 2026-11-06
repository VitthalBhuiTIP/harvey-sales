"""The MySQL mirror's schema is derived from the live SQLite schema."""

import sqlite3
from datetime import datetime

import pytest

from harvey.mysql_mirror import (
    Column,
    MySQLSettings,
    create_table_sql,
    mysql_type,
    read_sqlite_schema,
    to_mysql_value,
)
from harvey.state import StateManager


@pytest.mark.parametrize("sqlite_type,pk,expected", [
    ("TEXT", True, "VARCHAR(255)"),
    ("INTEGER", True, "BIGINT"),
    ("INTEGER", False, "BIGINT"),
    ("REAL", False, "DOUBLE"),
    ("TIMESTAMP", False, "DATETIME(6)"),
    ("TEXT", False, "LONGTEXT"),
    ("", False, "LONGTEXT"),
])
def test_type_mapping(sqlite_type, pk, expected):
    assert mysql_type(Column("c", sqlite_type, pk)) == expected


def test_timestamps_normalise_to_naive_utc():
    assert to_mysql_value("2026-01-02 03:04:05", "DATETIME(6)") == datetime(2026, 1, 2, 3, 4, 5)
    assert to_mysql_value("2026-01-02T03:04:05+02:00", "DATETIME(6)") == datetime(2026, 1, 2, 1, 4, 5)
    assert to_mysql_value("2026-01-02T03:04:05Z", "DATETIME(6)") == datetime(2026, 1, 2, 3, 4, 5)
    assert to_mysql_value("not a date", "DATETIME(6)") is None
    assert to_mysql_value("", "DATETIME(6)") is None
    assert to_mysql_value("kept", "LONGTEXT") == "kept"


@pytest.mark.asyncio
async def test_every_harvey_table_gets_valid_ddl(tmp_path):
    db = tmp_path / "harvey.db"
    await StateManager(str(db)).init_db()
    conn = sqlite3.connect(db)
    try:
        tables = {t.name: t for t in read_sqlite_schema(conn)}
    finally:
        conn.close()

    assert {"companies", "prospects", "outbox", "observations", "signal_codes"} <= set(tables)
    ddl = create_table_sql(tables["prospects"])
    assert "`id` VARCHAR(255)" in ddl
    assert "PRIMARY KEY (`id`)" in ddl
    # Text columns in an index need a prefix length in MySQL.
    assert "KEY `idx_prospects_email` (`email`(191))" in ddl
    for table in tables.values():
        assert create_table_sql(table).startswith("CREATE TABLE IF NOT EXISTS")


def test_settings_require_host_and_user(monkeypatch):
    for key in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PORT", "MYSQL_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        MySQLSettings.from_env()

    monkeypatch.setenv("MYSQL_HOST", "db.example.test")
    monkeypatch.setenv("MYSQL_USER", "harvey")
    s = MySQLSettings.from_env()
    assert (s.port, s.database) == (3306, "harvey")

    monkeypatch.setenv("MYSQL_DATABASE", "bad`name")
    with pytest.raises(ValueError):
        MySQLSettings.from_env()
