"""Action mapping and batch executor tests. No network: fake action functions."""

import asyncio
from datetime import datetime, timezone

import pytest

from cleaner import (
    Action,
    Candidate,
    Category,
    DialogInfo,
    Kind,
    action_for,
    execute_batch,
    is_systemic,
)

NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)


def cand(category: Category, kind: Kind = Kind.USER, left: bool = False, id: int = 1) -> Candidate:
    return Candidate(
        DialogInfo(id=id, title=f"chat-{id}", kind=kind, last_message_date=NOW, dialog_date=NOW, left=left),
        category,
    )


# --- action_for mapping ---------------------------------------------------------


def test_ghost_maps_to_delete_dialog():
    assert action_for(cand(Category.GHOST)) is Action.DELETE_DIALOG


def test_forbidden_maps_to_delete_dialog():
    assert action_for(cand(Category.FORBIDDEN, kind=Kind.FORBIDDEN)) is Action.DELETE_DIALOG


def test_stale_dm_maps_to_delete_dialog():
    assert action_for(cand(Category.STALE_DM)) is Action.DELETE_DIALOG


def test_dead_basic_group_maps_to_delete_chat_user():
    assert action_for(cand(Category.DEAD_GROUP, kind=Kind.GROUP)) is Action.DELETE_CHAT_USER_THEN_DELETE


def test_dead_megagroup_maps_to_leave_channel():
    assert action_for(cand(Category.DEAD_GROUP, kind=Kind.MEGAGROUP)) is Action.LEAVE_CHANNEL_THEN_DELETE


def test_already_left_group_degrades_to_delete_only():
    assert action_for(cand(Category.DEAD_GROUP, kind=Kind.MEGAGROUP, left=True)) is Action.DELETE_DIALOG


def test_migrated_basic_group_degrades_to_delete_only():
    # migrated_to set in build_info -> left=True -> DELETE_DIALOG (no DeleteChatUserRequest)
    assert action_for(cand(Category.DEAD_GROUP, kind=Kind.GROUP, left=True)) is Action.DELETE_DIALOG


def test_unread_channel_maps_to_leave_only():
    assert action_for(cand(Category.UNREAD_CHANNEL, kind=Kind.BROADCAST)) is Action.LEAVE_CHANNEL


def test_keep_is_not_actionable():
    with pytest.raises(ValueError):
        action_for(cand(Category.KEEP))


# --- is_systemic ------------------------------------------------------------------


def test_connection_error_is_systemic():
    assert is_systemic(ConnectionError("dead network"))


def test_value_error_is_per_entity():
    assert not is_systemic(ValueError("weird entity"))


def test_auth_key_error_is_systemic():
    from telethon import errors as te

    assert is_systemic(te.AuthKeyError(request=None, message="auth key not found"))


def test_unauthorized_rpc_error_is_systemic():
    from telethon import errors as te

    assert is_systemic(te.rpcbaseerrors.UnauthorizedError(request=None, message="401"))


# --- execute_batch ----------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def test_all_succeed():
    performed = []

    async def perform(c):
        performed.append(c.info.id)

    items = [cand(Category.GHOST, id=i) for i in (1, 2, 3)]
    result = run(execute_batch(items, perform, pause=0))
    assert performed == [1, 2, 3]
    assert len(result.done) == 3
    assert result.failed == []
    assert not result.aborted


def test_per_entity_failure_is_skipped_and_reported():
    async def perform(c):
        if c.info.id == 2:
            raise ValueError("half-dead ghost entity")

    items = [cand(Category.GHOST, id=i) for i in (1, 2, 3)]
    result = run(execute_batch(items, perform, pause=0))
    assert [c.info.id for c in result.done] == [1, 3]
    assert len(result.failed) == 1
    assert result.failed[0][0].info.id == 2
    assert "half-dead" in result.failed[0][1]
    assert not result.aborted


def test_consecutive_per_entity_failures_never_abort():
    # D9 decision: a run of quirky entities must not kill a healthy batch.
    async def perform(c):
        if c.info.id in (1, 2, 3):
            raise ValueError("quirky")

    items = [cand(Category.GHOST, id=i) for i in (1, 2, 3, 4)]
    result = run(execute_batch(items, perform, pause=0))
    assert [c.info.id for c in result.done] == [4]
    assert len(result.failed) == 3
    assert not result.aborted


def test_on_item_fires_for_every_attempted_candidate():
    seen = []

    async def perform(c):
        if c.info.id == 2:
            raise ValueError("quirky")  # per-entity failure still counts as attempted

    items = [cand(Category.GHOST, id=i) for i in (1, 2, 3)]
    result = run(execute_batch(items, perform, pause=0, on_item=lambda c: seen.append(c.info.id)))
    assert seen == [1, 2, 3]
    assert len(result.done) == 2 and len(result.failed) == 1


def test_on_item_does_not_fire_after_systemic_abort():
    seen = []

    async def perform(c):
        if c.info.id == 2:
            raise ConnectionError("socket died")

    items = [cand(Category.GHOST, id=i) for i in (1, 2, 3, 4)]
    result = run(execute_batch(items, perform, pause=0, on_item=lambda c: seen.append(c.info.id)))
    assert seen == [1]  # the aborting item and everything after never fire progress
    assert result.aborted


def test_systemic_error_aborts_immediately():
    attempted = []

    async def perform(c):
        attempted.append(c.info.id)
        if c.info.id == 2:
            raise ConnectionError("socket died")

    items = [cand(Category.GHOST, id=i) for i in (1, 2, 3, 4)]
    result = run(execute_batch(items, perform, pause=0))
    assert attempted == [1, 2]  # 3 and 4 never attempted
    assert [c.info.id for c in result.done] == [1]
    assert result.aborted
    assert "socket died" in result.abort_reason
    assert result.failed == []  # systemic abort is not a per-entity failure
