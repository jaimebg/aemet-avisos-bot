"""Regression tests pinning the current behaviour of database.py."""

from __future__ import annotations

import sqlite3

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


def test_get_subscribers_for_region_excludes_other_regions(temp_db):
    database.add_subscription(1, "mad")
    database.add_subscription(2, "mad")
    database.add_subscription(3, "and")

    subscribers = database.get_subscribers_for_region("mad")

    assert sorted(subscribers) == [1, 2]
    assert 3 not in subscribers


def test_get_subscribed_regions_returns_distinct_codes_with_a_subscriber(temp_db):
    database.add_subscription(1, "mad")
    database.add_subscription(2, "mad")
    database.add_subscription(3, "and")

    assert sorted(database.get_subscribed_regions()) == ["and", "mad"]


def test_mark_alert_seen_and_is_alert_seen_round_trip(temp_db):
    guid = "AFAZ611402ATTA3119.xml"

    assert database.is_alert_seen(guid) is False
    database.mark_alert_seen(guid)
    assert database.is_alert_seen(guid) is True

    # INSERT OR IGNORE: marking the same guid again must not raise.
    database.mark_alert_seen(guid)
    assert database.is_alert_seen(guid) is True


def test_cleanup_old_alerts_deletes_old_row_and_keeps_recent_one(temp_db):
    old_guid = "old-alert"
    recent_guid = "recent-alert"

    # Insert the old row with raw SQL, matching the space-separated format
    # SQLite's CURRENT_TIMESTAMP default (and cleanup_old_alerts's own
    # datetime('now', ...) comparison) use. mark_alert_seen() itself writes
    # first_seen as datetime.now(timezone.utc).isoformat() ("...T...+00:00"),
    # a different format (this is bug D10). This test only passes today
    # because the "recent" row's date component (today) is lexically greater
    # than the cutoff regardless of separator; Task 4 unifies the format so
    # both styles of comparison agree in every case, not just this one.
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
    assert database.is_alert_seen(old_guid) is False
    assert database.is_alert_seen(recent_guid) is True


def test_init_db_is_idempotent(temp_db):
    database.init_db()
    database.init_db()
