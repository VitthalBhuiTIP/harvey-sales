"""Keep MySQL in step with Harvey's SQLite database.

One function, ``sync_all``, refreshes both MySQL databases:

- ``harvey`` (MYSQL_DATABASE): a full mirror of every SQLite table;
- ``harvey_sales`` (MYSQL_SALES_DATABASE): the sales-team view, upserted so
  the team's own columns survive, with rows Harvey no longer has flagged
  ``in_harvey = 0``.

The dashboard's "Save to MySQL", Lead flow, the list imports and the
heartbeat all call it. A MySQL named lock makes concurrent syncs (the
dashboard and ``harvey run`` are separate processes) take turns instead of
interleaving their delete-and-copy.

``maybe_auto_sync`` is the heartbeat's hook: with MYSQL_HOST set it syncs at
most every MYSQL_SYNC_MINUTES (default 15) unless MYSQL_AUTO_SYNC is 0/false,
and a failure is logged, never raised, so MySQL being down can't stop Harvey.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("harvey.db_sync")

LOCK_NAME = "harvey_mysql_sync"
LOCK_WAIT_SECONDS = 120
SYNCED_AT_KEY = "mysql_synced_at"
DEFAULT_MINUTES = 15


def configured() -> bool:
    return bool((os.getenv("MYSQL_HOST") or "").strip() and (os.getenv("MYSQL_USER") or "").strip())


def _sync_blocking(db_path: Path, model: str, config) -> dict:
    from harvey.mysql_mirror import MySQLSettings, _connect_mysql, sync
    from harvey.sales_db import build_records, database_name, write

    settings = MySQLSettings.from_env()
    lock = _connect_mysql(settings, database=None)
    try:
        with lock.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, %s)", (LOCK_NAME, LOCK_WAIT_SECONDS))
            if (cur.fetchone() or [0])[0] != 1:
                raise RuntimeError("another MySQL sync is still running; try again shortly")
        try:
            mirror = sync(db_path, settings)
            records = asyncio.run(build_records(db_path, config, model))
            sales = write(replace(settings, database=database_name()), records)
        finally:
            with lock.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
    finally:
        lock.close()
    return {"mysql": mirror, "sales_db": sales}


def is_main_db(db_path) -> bool:
    """The mirror is a full copy of Harvey's own database: syncing any other
    SQLite file (a test's, a backup's) would replace it with that file's rows."""
    from harvey.state import DB_PATH
    try:
        return Path(db_path).resolve() == Path(DB_PATH).resolve()
    except OSError:
        return False


async def sync_all(db_path, config, state=None, allow_other_db: bool = False) -> dict:
    """Mirror + sales database. Returns {"mysql": {...}, "sales_db": {...}, "at": ...}.
    Raises on failure (the caller decides whether that's fatal)."""
    if not allow_other_db and not is_main_db(db_path):
        raise RuntimeError(f"refusing to sync {db_path} into MySQL: only Harvey's own "
                           "data/harvey.db is mirrored")
    model = f"{config.brain.provider}:{config.brain.model or 'default'}"
    # build_records is async and opens its own SQLite connection, so the whole
    # sync runs in a worker thread with its own event loop.
    result = await asyncio.to_thread(_sync_blocking, Path(db_path), model, config)
    result["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if state is not None:
        await state.set_setting(SYNCED_AT_KEY, result["at"])
    return result


def _interval() -> timedelta:
    try:
        return timedelta(minutes=max(1, int(os.getenv("MYSQL_SYNC_MINUTES") or DEFAULT_MINUTES)))
    except ValueError:
        return timedelta(minutes=DEFAULT_MINUTES)


def auto_sync_enabled() -> bool:
    return configured() and (os.getenv("MYSQL_AUTO_SYNC") or "1").strip().lower() not in ("0", "false", "no", "off")


async def maybe_auto_sync(state, config) -> dict | None:
    """Sync if it's on and due. Never raises."""
    if not auto_sync_enabled() or not is_main_db(state.db_path):
        return None
    try:
        last = await state.get_setting(SYNCED_AT_KEY) or ""
        if last:
            when = datetime.strptime(last, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - when < _interval():
                return None
    except (ValueError, TypeError):
        pass
    try:
        result = await sync_all(state.db_path, config, state)
        logger.info(f"MySQL synced: {sum(result['mysql'].values())} mirror rows, "
                    f"{sum(result['sales_db'].values())} sales rows.")
        return result
    except Exception as e:
        logger.warning(f"MySQL sync failed (Harvey carries on): {e}")
        return None
