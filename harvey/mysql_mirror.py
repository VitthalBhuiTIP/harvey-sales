"""Mirror Harvey's SQLite database into MySQL.

Harvey itself only reads and writes SQLite (`data/harvey.db`). This copies it
into a MySQL database for reporting, BI tools or ad-hoc SQL: the same tables,
columns and (non-unique) indexes, refreshed in full on every sync.

The MySQL schema is built from the live SQLite schema (PRAGMA table_info),
not a hand-written copy, so columns added by future migrations appear in the
mirror on the next sync without any change here.

Connection settings come from `.env`:
    MYSQL_HOST, MYSQL_PORT (3306), MYSQL_USER, MYSQL_PASSWORD,
    MYSQL_DATABASE (harvey)
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Index prefix length for text columns: MySQL can't index LONGTEXT whole.
INDEX_PREFIX = 191
BATCH_SIZE = 500

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Column:
    name: str
    sqlite_type: str
    pk: bool = False


@dataclass
class Table:
    name: str
    columns: list[Column]
    indexes: list[tuple[str, list[str]]] = field(default_factory=list)


@dataclass
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> "MySQLSettings":
        missing = [k for k in ("MYSQL_HOST", "MYSQL_USER") if not os.getenv(k)]
        if missing:
            raise ValueError(f"Set {', '.join(missing)} in .env")
        database = os.getenv("MYSQL_DATABASE") or "harvey"
        _ident(database)
        return cls(
            host=os.environ["MYSQL_HOST"],
            port=int(os.getenv("MYSQL_PORT") or 3306),
            user=os.environ["MYSQL_USER"],
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=database,
        )


def _ident(name: str) -> str:
    """Backtick-quote an identifier, refusing anything that isn't a plain name."""
    if not _IDENT.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return f"`{name}`"


def mysql_type(col: Column) -> str:
    """Map a SQLite declared type to the MySQL column type used in the mirror."""
    t = col.sqlite_type.upper()
    if col.pk and "INT" not in t:
        return "VARCHAR(255)"
    if "INT" in t:
        return "BIGINT"
    if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return "DOUBLE"
    if "TIMESTAMP" in t or "DATETIME" in t:
        return "DATETIME(6)"
    if "BLOB" in t:
        return "LONGBLOB"
    return "LONGTEXT"


def to_mysql_value(value, mysql_col_type: str):
    """Convert one SQLite value for insertion into a column of the given type.

    SQLite timestamps are text in several shapes ('2026-01-02 03:04:05',
    ISO-8601 with 'T' and an offset). Aware times are stored as UTC; anything
    that doesn't parse becomes NULL rather than failing the whole sync.
    """
    if value is None or not mysql_col_type.startswith("DATETIME"):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def read_sqlite_schema(conn: sqlite3.Connection) -> list[Table]:
    """Every user table in the SQLite database, with columns and indexes."""
    tables = []
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    for name in names:
        cols = [
            Column(name=r[1], sqlite_type=r[2] or "", pk=bool(r[5]))
            for r in conn.execute(f"PRAGMA table_info({_ident(name)})")
        ]
        indexes = []
        for idx in conn.execute(f"PRAGMA index_list({_ident(name)})"):
            idx_name, origin = idx[1], idx[3]
            if origin == "pk" or idx_name.startswith("sqlite_autoindex"):
                continue
            idx_cols = [r[2] for r in conn.execute(f"PRAGMA index_info({_ident(idx_name)})")]
            # Expression indexes report a NULL column name: nothing to mirror.
            if idx_cols and all(idx_cols):
                indexes.append((idx_name, idx_cols))
        tables.append(Table(name=name, columns=cols, indexes=indexes))
    return tables


def create_table_sql(table: Table) -> str:
    """CREATE TABLE for the mirror. No defaults or uniques: rows are copied verbatim,
    and SQLite's partial unique indexes have no MySQL equivalent."""
    lines = [f"  {_ident(c.name)} {mysql_type(c)}" for c in table.columns]
    pks = [c.name for c in table.columns if c.pk]
    if pks:
        lines.append(f"  PRIMARY KEY ({', '.join(_ident(p) for p in pks)})")
    for idx_name, idx_cols in table.indexes:
        lines.append(f"  KEY {_ident(idx_name)} ({_index_cols(table, idx_cols)})")
    return (
        f"CREATE TABLE IF NOT EXISTS {_ident(table.name)} (\n"
        + ",\n".join(lines)
        + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )


def _index_cols(table: Table, idx_cols: list[str]) -> str:
    types = {c.name: mysql_type(c) for c in table.columns}
    parts = []
    for col in idx_cols:
        prefix = f"({INDEX_PREFIX})" if types.get(col) in ("LONGTEXT", "LONGBLOB") else ""
        parts.append(f"{_ident(col)}{prefix}")
    return ", ".join(parts)


def _connect_mysql(settings: MySQLSettings, database: str | None):
    try:
        import pymysql
    except ImportError as e:
        raise RuntimeError(
            "PyMySQL isn't installed. Run: pip install -e \".[mysql]\""
        ) from e
    return pymysql.connect(
        host=settings.host, port=settings.port, user=settings.user,
        password=settings.password, database=database,
        charset="utf8mb4", autocommit=False,
    )


def _ensure_schema(cur, table: Table) -> None:
    """Create the table, or add any columns SQLite has gained since last sync."""
    cur.execute(create_table_sql(table))
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table.name,),
    )
    existing = {r[0] for r in cur.fetchall()}
    for col in table.columns:
        if col.name not in existing:
            cur.execute(
                f"ALTER TABLE {_ident(table.name)} ADD COLUMN {_ident(col.name)} {mysql_type(col)}"
            )


def sync(sqlite_path: Path, settings: MySQLSettings, schema_only: bool = False) -> dict[str, int]:
    """Create the MySQL database and tables, then (unless schema_only) replace
    their contents with the SQLite data. Returns rows copied per table."""
    _ident(settings.database)
    src = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = read_sqlite_schema(src)

        server = _connect_mysql(settings, database=None)
        try:
            with server.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS {_ident(settings.database)} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            server.commit()
        finally:
            server.close()

        dst = _connect_mysql(settings, database=settings.database)
        counts: dict[str, int] = {}
        try:
            # DDL auto-commits in MySQL, so do it all before the data transaction.
            with dst.cursor() as cur:
                for table in tables:
                    _ensure_schema(cur, table)
            if schema_only:
                return {t.name: 0 for t in tables}

            with dst.cursor() as cur:
                for table in tables:
                    counts[table.name] = _copy_table(src, cur, table)
            dst.commit()
        except Exception:
            dst.rollback()
            raise
        finally:
            dst.close()
        return counts
    finally:
        src.close()


def _copy_table(src: sqlite3.Connection, cur, table: Table) -> int:
    names = [c.name for c in table.columns]
    types = [mysql_type(c) for c in table.columns]
    col_list = ", ".join(_ident(n) for n in names)
    insert = (
        f"INSERT INTO {_ident(table.name)} ({col_list}) "
        f"VALUES ({', '.join(['%s'] * len(names))})"
    )
    cur.execute(f"DELETE FROM {_ident(table.name)}")
    total = 0
    # SQLite accepts MySQL-style backtick quoting, so the same list works there.
    rows = src.execute(f"SELECT {col_list} FROM {_ident(table.name)}")
    while batch := rows.fetchmany(BATCH_SIZE):
        cur.executemany(insert, [
            tuple(to_mysql_value(v, t) for v, t in zip(row, types)) for row in batch
        ])
        total += len(batch)
    return total
