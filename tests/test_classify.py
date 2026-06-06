"""Pure classification tests: every branch of the classify() decision tree.

The matrix mirrors the header diagram in cleaner.py:
keeplist -> forbidden -> ghost -> stale_dm -> dead_group -> unread_channel -> keep
"""

from datetime import datetime, timedelta, timezone

from cleaner import Category, DialogInfo, Kind, Thresholds, classify

NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
TH = Thresholds()  # stale_days=730, group_quiet_days=365, channel_unread_min=50


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def info(**overrides) -> DialogInfo:
    defaults = dict(
        id=1,
        title="x",
        kind=Kind.USER,
        last_message_date=days_ago(1),
        dialog_date=days_ago(1000),
    )
    defaults.update(overrides)
    return DialogInfo(**defaults)


# --- keeplist pre-filter -----------------------------------------------------


def test_keeplisted_id_is_always_keep():
    ghost = info(id=42, deleted=True)
    assert classify(ghost, TH, keeplist={42}, now=NOW) is Category.KEEP


def test_keeplist_does_not_affect_other_ids():
    ghost = info(id=43, deleted=True)
    assert classify(ghost, TH, keeplist={42}, now=NOW) is Category.GHOST


# --- forbidden tombstones (isinstance-first: fields may be missing) ----------


def test_forbidden_tombstone():
    tomb = info(kind=Kind.FORBIDDEN, last_message_date=None, dialog_date=None)
    assert classify(tomb, TH, now=NOW) is Category.FORBIDDEN


def test_forbidden_wins_over_any_date_logic():
    tomb = info(kind=Kind.FORBIDDEN, last_message_date=days_ago(1))
    assert classify(tomb, TH, now=NOW) is Category.FORBIDDEN


# --- ghosts -------------------------------------------------------------------


def test_ghost_deleted_account():
    assert classify(info(deleted=True), TH, now=NOW) is Category.GHOST


def test_ghost_wins_even_if_recently_active():
    assert classify(info(deleted=True, last_message_date=days_ago(0)), TH, now=NOW) is Category.GHOST


# --- stale DMs ----------------------------------------------------------------


def test_dm_older_than_threshold_is_stale():
    assert classify(info(last_message_date=days_ago(731)), TH, now=NOW) is Category.STALE_DM


def test_dm_exactly_at_threshold_is_keep():
    # Boundary: "older than" is strict. 730 days on the nose stays.
    assert classify(info(last_message_date=days_ago(730)), TH, now=NOW) is Category.KEEP


def test_dm_recent_is_keep():
    assert classify(info(last_message_date=days_ago(10)), TH, now=NOW) is Category.KEEP


def test_empty_dm_with_old_dialog_is_stale():
    empty_old = info(last_message_date=None, dialog_date=days_ago(800))
    assert classify(empty_old, TH, now=NOW) is Category.STALE_DM


def test_empty_dm_added_yesterday_is_keep():
    # The D7 refinement: a fresh contact with no messages never appears.
    fresh = info(last_message_date=None, dialog_date=days_ago(1))
    assert classify(fresh, TH, now=NOW) is Category.KEEP


def test_empty_dm_with_no_dates_at_all_is_keep():
    # Cannot establish age: be conservative, keep.
    unknown = info(last_message_date=None, dialog_date=None)
    assert classify(unknown, TH, now=NOW) is Category.KEEP


def test_naive_datetime_is_treated_as_utc():
    naive = info(last_message_date=(NOW - timedelta(days=900)).replace(tzinfo=None))
    assert classify(naive, TH, now=NOW) is Category.STALE_DM


# --- dead groups ---------------------------------------------------------------


def test_quiet_basic_group_is_dead():
    g = info(kind=Kind.GROUP, last_message_date=days_ago(366))
    assert classify(g, TH, now=NOW) is Category.DEAD_GROUP


def test_quiet_megagroup_is_dead():
    g = info(kind=Kind.MEGAGROUP, last_message_date=days_ago(366))
    assert classify(g, TH, now=NOW) is Category.DEAD_GROUP


def test_group_exactly_at_threshold_is_keep():
    g = info(kind=Kind.GROUP, last_message_date=days_ago(365))
    assert classify(g, TH, now=NOW) is Category.KEEP


def test_active_group_is_keep():
    g = info(kind=Kind.GROUP, last_message_date=days_ago(5))
    assert classify(g, TH, now=NOW) is Category.KEEP


def test_empty_old_group_is_dead():
    g = info(kind=Kind.GROUP, last_message_date=None, dialog_date=days_ago(400))
    assert classify(g, TH, now=NOW) is Category.DEAD_GROUP


def test_empty_fresh_group_is_keep():
    g = info(kind=Kind.GROUP, last_message_date=None, dialog_date=days_ago(2))
    assert classify(g, TH, now=NOW) is Category.KEEP


# --- unread channels ------------------------------------------------------------


def test_unread_unmuted_broadcast_is_candidate():
    c = info(kind=Kind.BROADCAST, unread_count=50, muted=False)
    assert classify(c, TH, now=NOW) is Category.UNREAD_CHANNEL


def test_unread_below_threshold_is_keep():
    c = info(kind=Kind.BROADCAST, unread_count=49, muted=False)
    assert classify(c, TH, now=NOW) is Category.KEEP


def test_muted_channel_is_never_candidate():
    # D10 decision: mute means deliberately kept.
    c = info(kind=Kind.BROADCAST, unread_count=5000, muted=True)
    assert classify(c, TH, now=NOW) is Category.KEEP


def test_broadcast_ignores_message_age():
    c = info(kind=Kind.BROADCAST, unread_count=0, last_message_date=days_ago(2000))
    assert classify(c, TH, now=NOW) is Category.KEEP


# --- custom thresholds -----------------------------------------------------------


def test_custom_thresholds_apply():
    th = Thresholds(stale_days=30, group_quiet_days=10, channel_unread_min=5)
    assert classify(info(last_message_date=days_ago(31)), th, now=NOW) is Category.STALE_DM
    g = info(kind=Kind.GROUP, last_message_date=days_ago(11))
    assert classify(g, th, now=NOW) is Category.DEAD_GROUP
    c = info(kind=Kind.BROADCAST, unread_count=5)
    assert classify(c, th, now=NOW) is Category.UNREAD_CHANNEL
