from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from config import DATABASE_PATH


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DATABASE_PATH)


def init_db() -> None:
    conn = _connect()
    try:
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
        conn.commit()
    finally:
        conn.close()


def add_subscription(user_id: int, region_code: str) -> bool:
    """Add a subscription. Returns True if newly added, False if already existed."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, region_code) VALUES (?, ?)",
            (user_id, region_code),
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def remove_subscription(user_id: int, region_code: str) -> bool:
    """Remove a subscription. Returns True if removed, False if didn't exist."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND region_code = ?",
            (user_id, region_code),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def remove_all_subscriptions(user_id: int) -> int:
    """Remove all subscriptions for a user. Returns count removed."""
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_user_subscriptions(user_id: int) -> list[str]:
    """Get region codes a user is subscribed to."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT region_code FROM subscriptions "
            "WHERE user_id = ? ORDER BY region_code",
            (user_id,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_subscribers_for_region(region_code: str) -> list[int]:
    """Get user IDs subscribed to a region."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id FROM subscriptions WHERE region_code = ?",
            (region_code,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_subscribed_regions() -> list[str]:
    """Get region codes that have at least one subscriber."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT DISTINCT region_code FROM subscriptions").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def is_alert_seen(guid: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM seen_alerts WHERE guid = ?", (guid,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_alert_seen(guid: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen_alerts (guid, first_seen) VALUES (?, ?)",
            (guid, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_old_alerts(days: int = 7) -> int:
    """Remove seen alerts older than N days to keep the table small."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM seen_alerts WHERE first_seen < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
