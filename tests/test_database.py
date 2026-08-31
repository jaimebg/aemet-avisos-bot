"""Regression tests pinning the current behaviour of database.py."""

from __future__ import annotations

import re
import sqlite3

import pytest

import config
import database


def test_add_subscription_true_first_time_false_for_duplicate(temp_db):
    assert database.add_subscription(1, "mad") is True
    assert database.add_subscription(1, "mad") is False


def test_get_user_subscriptions_sorted_and_only_for_that_user(temp_db):
    database.add_subscription(1, "mad")
    database.add_subscription(1, "and")
    database.add_subscription(2, "cat")

    assert database.get_user_subscriptions(1) == ["and", "mad"]
    assert database.get_user_subscriptions(2) == ["cat"]


def test_remove_subscription_true_when_existed_false_otherwise(temp_db):
    database.add_subscription(1, "mad")

    assert database.remove_subscription(1, "mad") is True
    assert database.remove_subscription(1, "mad") is False


def test_remove_all_subscriptions_returns_count_and_empties_list(temp_db):
    database.add_subscription(1, "mad")
    database.add_subscription(1, "and")
    database.add_subscription(2, "cat")

    assert database.remove_all_subscriptions(1) == 2
    assert database.get_user_subscriptions(1) == []
    assert database.get_user_subscriptions(2) == ["cat"]


def test_get_subscribed_regions_returns_distinct_codes_with_a_subscriber(temp_db):
    database.add_subscription(1, "mad")
    database.add_subscription(2, "mad")
    database.add_subscription(3, "and")

    assert sorted(database.get_subscribed_regions()) == ["and", "mad"]


def test_mark_alert_seen_and_get_seen_levels_round_trip(temp_db):
    guid = "AFAZ611402ATTA3119.xml"

    assert database.get_seen_levels([guid]) == {}
    database.mark_alert_seen(guid, "amarillo")
    assert database.get_seen_levels([guid]) == {guid: "amarillo"}

    # INSERT OR IGNORE: marking the same guid again must not raise, and must
    # not overwrite the level stored the first time.
    database.mark_alert_seen(guid, "rojo")
    assert database.get_seen_levels([guid]) == {guid: "amarillo"}


def test_cleanup_old_alerts_deletes_old_row_and_keeps_recent_one(temp_db):
    old_guid = "old-alert"
    recent_guid = "recent-alert"

    # Insert the old row with raw SQL, matching the space-separated format
    # SQLite's CURRENT_TIMESTAMP default (and cleanup_old_alerts's own
    # datetime('now', ...) comparison) use, which is what mark_alert_seen()
    # writes too since Task 4 unified the format (bug D10).
    conn = sqlite3.connect(temp_db)
    try:
        conn.execute(
            "INSERT INTO seen_alerts (guid, first_seen) "
            "VALUES (?, strftime('%Y-%m-%d %H:%M:%S', 'now', '-30 days'))",
            (old_guid,),
        )
        conn.commit()
    finally:
        conn.close()

    database.mark_alert_seen(recent_guid)

    removed = database.cleanup_old_alerts()

    assert removed == 1
    assert database.get_seen_levels([old_guid, recent_guid]) == {recent_guid: None}


def test_init_db_is_idempotent(temp_db):
    database.init_db()
    database.init_db()


def test_get_seen_levels_empty_input_returns_empty_dict(temp_db):
    assert database.get_seen_levels([]) == {}


def test_get_seen_levels_returns_only_existing_guids_with_levels(temp_db):
    database.mark_alert_seen("with-level", "naranja")

    # A legacy row with a NULL level, inserted directly (mark_alert_seen
    # always writes a level, possibly None, but this simulates a row that
    # predates the level column being backfilled).
    conn = sqlite3.connect(temp_db)
    try:
        conn.execute(
            "INSERT INTO seen_alerts (guid, first_seen, level) "
            "VALUES (?, strftime('%Y-%m-%d %H:%M:%S', 'now'), NULL)",
            ("legacy-no-level",),
        )
        conn.commit()
    finally:
        conn.close()

    result = database.get_seen_levels(
        ["with-level", "legacy-no-level", "never-seen-guid"]
    )

    assert result == {"with-level": "naranja", "legacy-no-level": None}


def test_get_seen_levels_chunks_requests_over_900_guids(temp_db):
    guids = [f"guid-{i}" for i in range(1000)]
    conn = sqlite3.connect(temp_db)
    try:
        conn.executemany(
            "INSERT INTO seen_alerts (guid, first_seen, level) "
            "VALUES (?, strftime('%Y-%m-%d %H:%M:%S', 'now'), 'amarillo')",
            [(guid,) for guid in guids],
        )
        conn.commit()
    finally:
        conn.close()

    requested = [f"guid-{i}" for i in range(1200)]
    result = database.get_seen_levels(requested)

    assert len(result) == 1000
    assert set(result) == set(guids)


def test_update_alert_level_changes_level_and_is_noop_for_unknown_guid(temp_db):
    database.mark_alert_seen("guid-1", "amarillo")

    database.update_alert_level("guid-1", "rojo")
    assert database.get_seen_levels(["guid-1"]) == {"guid-1": "rojo"}

    database.update_alert_level("unknown-guid", "rojo")
    assert database.get_seen_levels(["unknown-guid"]) == {}


def test_get_min_level_returns_default_then_stored_value(temp_db):
    assert database.get_min_level(1) == "amarillo"

    database.set_min_level(1, "naranja")

    assert database.get_min_level(1) == "naranja"


def test_set_min_level_rejects_unknown_level(temp_db):
    with pytest.raises(ValueError):
        database.set_min_level(1, "verde")


def test_set_min_level_upserts_instead_of_raising(temp_db):
    database.set_min_level(1, "amarillo")
    database.set_min_level(1, "rojo")

    assert database.get_min_level(1) == "rojo"


def test_get_subscribers_with_min_rank(temp_db):
    database.add_subscription(1, "mad")
    database.add_subscription(2, "mad")
    database.add_subscription(3, "and")

    ranks = dict(database.get_subscribers_with_min_rank("mad"))
    assert ranks == {1: 1, 2: 1}
    assert 3 not in ranks

    database.set_min_level(1, "rojo")

    ranks = dict(database.get_subscribers_with_min_rank("mad"))
    assert ranks == {1: 3, 2: 1}


def test_mark_alert_seen_writes_space_separated_timestamp(temp_db):
    database.mark_alert_seen("guid-1")

    conn = sqlite3.connect(temp_db)
    try:
        first_seen = conn.execute(
            "SELECT first_seen FROM seen_alerts WHERE guid = ?", ("guid-1",)
        ).fetchone()[0]
    finally:
        conn.close()

    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", first_seen)


def test_mark_alert_seen_twice_with_different_levels_keeps_first_timestamp(temp_db):
    database.mark_alert_seen("guid-1", "amarillo")

    conn = sqlite3.connect(temp_db)
    try:
        first_seen_before = conn.execute(
            "SELECT first_seen FROM seen_alerts WHERE guid = ?", ("guid-1",)
        ).fetchone()[0]
    finally:
        conn.close()

    database.mark_alert_seen("guid-1", "rojo")

    conn = sqlite3.connect(temp_db)
    try:
        first_seen_after = conn.execute(
            "SELECT first_seen FROM seen_alerts WHERE guid = ?", ("guid-1",)
        ).fetchone()[0]
    finally:
        conn.close()

    assert first_seen_before == first_seen_after


def test_init_db_migrates_v1_database_in_place(monkeypatch, tmp_path):
    """Build a genuine v1 database with the old CREATE TABLE statements
    (user_version left at 0) and check init_db() upgrades it losslessly.
    """
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    database.reset_connections()

    raw_conn = sqlite3.connect(db_path)
    try:
        raw_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER, region_code TEXT, PRIMARY KEY (user_id, region_code)
            )
            """
        )
        raw_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_alerts (
                guid TEXT PRIMARY KEY,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        raw_conn.execute(
            "INSERT INTO subscriptions (user_id, region_code) VALUES (?, ?)",
            (1, "mad"),
        )
        raw_conn.execute(
            "INSERT INTO seen_alerts (guid, first_seen) VALUES (?, ?)",
            ("legacy-guid", "2026-02-24T10:15:29.123456+00:00"),
        )
        raw_conn.commit()
    finally:
        raw_conn.close()

    database.init_db()

    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2

        subs = conn.execute("SELECT user_id, region_code FROM subscriptions").fetchall()
        assert subs == [(1, "mad")]

        columns = {row[1] for row in conn.execute("PRAGMA table_info(seen_alerts)")}
        assert "level" in columns

        first_seen = conn.execute(
            "SELECT first_seen FROM seen_alerts WHERE guid = ?", ("legacy-guid",)
        ).fetchone()[0]
        assert first_seen == "2026-02-24 10:15:29"
    finally:
        conn.close()

    database.reset_connections()


def test_init_db_on_already_migrated_database_is_noop(temp_db):
    database.add_subscription(1, "mad")
    database.mark_alert_seen("guid-1", "amarillo")

    database.init_db()

    conn = sqlite3.connect(temp_db)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert version == 2
    assert database.get_user_subscriptions(1) == ["mad"]
    assert database.get_seen_levels(["guid-1"]) == {"guid-1": "amarillo"}


def test_add_subscription_false_on_duplicate_after_other_writes(temp_db):
    """D9 regression: the return value must reflect this statement's rowcount,
    not conn.total_changes, which would be polluted by the earlier writes.
    """
    assert database.add_subscription(1, "mad") is True
    assert database.add_subscription(2, "and") is True
    assert database.add_subscription(1, "mad") is False
