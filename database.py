from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence

import config

SCHEMA_VERSION = 3

# SQLite's default compiled-in limit on the number of host parameters in a
# single statement is 999. Stay comfortably under it when chunking `IN (...)`
# queries so a busy region can never trip it.
_SQLITE_MAX_VARIABLES = 900

# Every function in this module is fully synchronous (no `await` inside), and
# PTB dispatches both its handlers and its JobQueue jobs on the same single
# event loop (its AsyncIOExecutor schedules onto that loop). So in practice one
# thread owns one connection and nothing can interleave inside a transaction.
# That safety comes from PTB's threading model, not from anything enforced
# here: a caller that runs these functions from another thread gets its own
# connection, and one that runs them concurrently in the same thread would not
# be protected.
_local = threading.local()


def _connect() -> sqlite3.Connection:
    """Return this thread's cached connection, opening one if needed.

    The cache key includes the resolved DATABASE_PATH so a monkeypatched path
    (as tests do) never returns a connection left open against a stale file.
    """
    db_path = config.DATABASE_PATH
    cached = getattr(_local, "conn", None)
    cached_path = getattr(_local, "path", None)
    if cached is not None and cached_path == db_path:
        return cached

    if cached is not None:
        cached.close()

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")

    _local.conn = conn
    _local.path = db_path
    return conn


def reset_connections() -> None:
    """Close and drop the calling thread's cached connection, if any."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None
    _local.path = None


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Apply the v1 -> v2 schema changes: level column, indices, user_prefs."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seen_alerts)")}
    if "level" not in columns:
        conn.execute("ALTER TABLE seen_alerts ADD COLUMN level TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_region "
        "ON subscriptions(region_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seen_alerts_first_seen "
        "ON seen_alerts(first_seen)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id INTEGER PRIMARY KEY,
            min_level TEXT NOT NULL DEFAULT 'amarillo'
        )
        """
    )

    # Normalise legacy Python-ISO-8601 timestamps ("...T...+00:00" or with
    # microseconds) to the space-separated "YYYY-MM-DD HH:MM:SS" form used
    # everywhere else, so text comparisons against datetime('now', ...) work.
    conn.execute(
        """
        UPDATE seen_alerts
        SET first_seen = replace(substr(first_seen, 1, 19), 'T', ' ')
        WHERE first_seen LIKE '____-__-__T%'
        """
    )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Apply the v2 -> v3 schema change: per-recipient retry queue."""
    # One row per (alert, user) whose delivery failed transiently. The alert
    # itself is recorded in seen_alerts as soon as anyone received it, so this
    # table is what lets the users it missed get it on a later cycle.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS failed_deliveries (
            guid TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            previous_level TEXT,
            failed_at TIMESTAMP NOT NULL,
            PRIMARY KEY (guid, user_id)
        )
        """
    )


def init_db() -> None:
    """Create or upgrade the schema in place. Idempotent, never drops data."""
    conn = _connect()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER,
            region_code TEXT,
            PRIMARY KEY (user_id, region_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_alerts (
            guid TEXT PRIMARY KEY,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version < 2:
        _migrate_v1_to_v2(conn)
    if version < 3:
        _migrate_v2_to_v3(conn)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    conn.commit()


def add_subscription(user_id: int, region_code: str) -> bool:
    """Add a subscription. Returns True if newly added, False if already existed."""
    conn = _connect()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, region_code) VALUES (?, ?)",
        (user_id, region_code),
    )
    conn.commit()
    return cursor.rowcount > 0


def remove_subscription(user_id: int, region_code: str) -> bool:
    """Remove a subscription. Returns True if removed, False if didn't exist."""
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND region_code = ?",
        (user_id, region_code),
    )
    conn.commit()
    return cursor.rowcount > 0


def remove_all_subscriptions(user_id: int) -> int:
    """Remove all subscriptions for a user. Returns count removed."""
    conn = _connect()
    cursor = conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    conn.commit()
    return cursor.rowcount


def forget_user(user_id: int) -> None:
    """Delete everything stored about a user who blocked the bot.

    Unlike remove_all_subscriptions (the user's own "unsubscribe from all"),
    this also drops their level preference and any pending retries.
    """
    conn = _connect()
    conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_prefs WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM failed_deliveries WHERE user_id = ?", (user_id,))
    conn.commit()


def get_user_subscriptions(user_id: int) -> list[str]:
    """Get region codes a user is subscribed to."""
    conn = _connect()
    rows = conn.execute(
        "SELECT region_code FROM subscriptions WHERE user_id = ? ORDER BY region_code",
        (user_id,),
    ).fetchall()
    return [r[0] for r in rows]


def get_subscribed_regions() -> list[str]:
    """Get region codes that have at least one subscriber."""
    conn = _connect()
    rows = conn.execute("SELECT DISTINCT region_code FROM subscriptions").fetchall()
    return [r[0] for r in rows]


def mark_alert_seen(guid: str, level: str | None = None) -> None:
    """Record a guid as seen, storing its severity level.

    INSERT OR IGNORE: calling this twice for the same guid never overwrites
    the original first_seen timestamp (or the level recorded with it).
    """
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO seen_alerts (guid, first_seen, level) "
        "VALUES (?, strftime('%Y-%m-%d %H:%M:%S', 'now'), ?)",
        (guid, level),
    )
    conn.commit()


def cleanup_old_alerts(days: int = 7) -> int:
    """Remove seen alerts older than N days to keep the table small.

    Pending retries go with them: a retry for an alert this old is stale, and
    one whose alert is no longer recorded can never be matched again.
    """
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM seen_alerts WHERE first_seen < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.execute(
        """
        DELETE FROM failed_deliveries
        WHERE failed_at < datetime('now', ?)
           OR guid NOT IN (SELECT guid FROM seen_alerts)
        """,
        (f"-{days} days",),
    )
    conn.commit()
    return cursor.rowcount


def get_seen_levels(guids: Sequence[str]) -> dict[str, str | None]:
    """Map each guid that exists in seen_alerts to its stored level.

    Guids not present in the table are omitted. An empty input returns {}
    without querying. The IN (...) list is chunked to stay under SQLite's
    host-parameter limit.
    """
    if not guids:
        return {}

    conn = _connect()
    result: dict[str, str | None] = {}
    guid_list = list(guids)
    for start in range(0, len(guid_list), _SQLITE_MAX_VARIABLES):
        chunk = guid_list[start : start + _SQLITE_MAX_VARIABLES]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT guid, level FROM seen_alerts WHERE guid IN ({placeholders})",
            chunk,
        ).fetchall()
        result.update(dict(rows))
    return result


def update_alert_level(guid: str, level: str | None) -> None:
    """Set the stored level on an existing seen_alerts row. No-op if absent."""
    conn = _connect()
    conn.execute(
        "UPDATE seen_alerts SET level = ? WHERE guid = ?",
        (level, guid),
    )
    conn.commit()


def get_min_level(user_id: int) -> str:
    """Return the user's minimum alert level, or the default if unset."""
    conn = _connect()
    row = conn.execute(
        "SELECT min_level FROM user_prefs WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return config.DEFAULT_MIN_LEVEL
    return row[0]


def set_min_level(user_id: int, level: str) -> None:
    """Upsert the user's minimum alert level. Raises ValueError for an unknown level."""
    if level not in config.LEVELS:
        raise ValueError(f"Unknown level: {level!r}")

    conn = _connect()
    conn.execute(
        """
        INSERT INTO user_prefs (user_id, min_level) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET min_level = excluded.min_level
        """,
        (user_id, level),
    )
    conn.commit()


def get_subscribers_with_min_rank(region_code: str) -> list[tuple[int, int]]:
    """Return (user_id, min_rank) for every subscriber of region_code.

    min_rank is the integer rank of the user's minimum level, defaulting to
    the rank of config.DEFAULT_MIN_LEVEL for users with no preference row or
    an unrecognised stored value. The mapping lives in config.rank_for, which
    handlers.py uses too, so the rule has exactly one definition.
    """
    conn = _connect()
    rows = conn.execute(
        """
        SELECT s.user_id, p.min_level
        FROM subscriptions s
        LEFT JOIN user_prefs p ON p.user_id = s.user_id
        WHERE s.region_code = ?
        """,
        (region_code,),
    ).fetchall()

    return [(user_id, config.rank_for(min_level)) for user_id, min_level in rows]


def record_failed_delivery(
    guid: str, user_id: int, previous_level: str | None = None
) -> None:
    """Queue a delivery that failed transiently, for retry on a later cycle.

    A later failure for the same (guid, user) replaces the earlier row, so the
    queue always holds the most recent message variant to retry.
    """
    conn = _connect()
    conn.execute(
        """
        INSERT OR REPLACE INTO failed_deliveries
            (guid, user_id, previous_level, failed_at)
        VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', 'now'))
        """,
        (guid, user_id, previous_level),
    )
    conn.commit()


def get_failed_deliveries(
    guids: Sequence[str],
) -> dict[str, list[tuple[int, str | None]]]:
    """Map each guid with queued retries to its (user_id, previous_level) rows.

    Guids with nothing queued are omitted; an empty input returns {} without
    querying. Chunked like get_seen_levels.
    """
    if not guids:
        return {}

    conn = _connect()
    result: dict[str, list[tuple[int, str | None]]] = {}
    guid_list = list(guids)
    for start in range(0, len(guid_list), _SQLITE_MAX_VARIABLES):
        chunk = guid_list[start : start + _SQLITE_MAX_VARIABLES]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT guid, user_id, previous_level FROM failed_deliveries "
            f"WHERE guid IN ({placeholders}) ORDER BY user_id",
            chunk,
        ).fetchall()
        for guid, user_id, previous_level in rows:
            result.setdefault(guid, []).append((user_id, previous_level))
    return result


def clear_failed_delivery(guid: str, user_id: int) -> None:
    """Drop one queued retry (delivered, or no longer wanted)."""
    conn = _connect()
    conn.execute(
        "DELETE FROM failed_deliveries WHERE guid = ? AND user_id = ?",
        (guid, user_id),
    )
    conn.commit()


def clear_failed_deliveries(guid: str) -> None:
    """Drop every queued retry of guid, e.g. when an escalation supersedes it."""
    conn = _connect()
    conn.execute("DELETE FROM failed_deliveries WHERE guid = ?", (guid,))
    conn.commit()
