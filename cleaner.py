"""telegram_cleaner — personal Telegram janitor.

Architecture (this diagram is the design; keep it current with any change):

                       +-------------------------------------------------+
                       | cleaner.py                                      |
.env --validate--> client (Telethon, flood_sleep_threshold=300)          |
                       |      |                                          |
                       | iter_dialogs()  -- one read pass, no extra calls|
                       |      |                                          |
                       |      v                                          |
                       | classify(info)          PURE -- tests need no net
                       |  +- chat ID in keeplist.json?       -> keep     |
                       |  +- ChatForbidden/ChannelForbidden? -> forbidden|
                       |  +- entity.deleted?                 -> ghost    |
                       |  +- DM old / empty+old?             -> stale_dm |
                       |  +- group old / empty+old?          -> dead_group
                       |  +- channel unread>=min AND unmuted?-> unread_ch|
                       |  +- else                            -> keep     |
                       |      |                                          |
            +----------+------+----------+                               |
            v                            v                               |
   scan: rich table              candidates by type                      |
   (read-only, exits)                    |                               |
                          +--------------+--------------+                |
                          v                             v                |
                 purge-ghosts                     review (interactive)   |
                 ghosts [+forbidden]              y/n/k marks per item   |
                 dry-run unless --yes             final y/n gates batch  |
                          +--------------+--------------+                |
                                         v                               |
                       batch executor (skip-and-report)                  |
                        +- per-entity error -> skip, collect, continue   |
                        +- is_systemic(exc)? -> abort immediately        |
                        +- action per type via action_for(candidate)     |
                                         v                               |
                       failures table + summary (re-run scan = survivors)|
                       +-------------------------------------------------+
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

FLOOD_SLEEP_THRESHOLD = 300
PAUSE_BETWEEN_ACTIONS = 1.0
KEEPLIST_PATH = Path("keeplist.json")
SESSION_NAME = "cleaner"

app = typer.Typer(add_completion=False, help="Personal Telegram janitor.")
console = Console()


# --------------------------------------------------------------------------
# Pure data model and classification (no telethon imports needed for tests)
# --------------------------------------------------------------------------


class Kind(str, Enum):
    USER = "user"
    GROUP = "group"  # basic Chat
    MEGAGROUP = "megagroup"  # Channel with megagroup flag
    BROADCAST = "broadcast"  # Channel without megagroup flag
    FORBIDDEN = "forbidden"  # ChatForbidden / ChannelForbidden tombstone


class Category(str, Enum):
    GHOST = "ghost"
    FORBIDDEN = "forbidden"
    STALE_DM = "stale_dm"
    DEAD_GROUP = "dead_group"
    UNREAD_CHANNEL = "unread_channel"
    KEEP = "keep"


REVIEW_TYPES = {Category.STALE_DM, Category.DEAD_GROUP, Category.UNREAD_CHANNEL}


@dataclass(frozen=True)
class DialogInfo:
    id: int
    title: str
    kind: Kind
    last_message_date: datetime | None  # None = dialog has no messages
    dialog_date: datetime | None  # creation-time fallback, set even when empty
    unread_count: int = 0
    muted: bool = False
    deleted: bool = False  # User.deleted flag (ghost signal)
    left: bool = False  # already left this group/channel
    username: str | None = None  # public handle, for rejoin hints
    snippet: str = ""  # last message preview, display only


@dataclass(frozen=True)
class Thresholds:
    stale_days: int = 730
    group_quiet_days: int = 365
    channel_unread_min: int = 50


@dataclass(frozen=True)
class Candidate:
    info: DialogInfo
    category: Category


def _older_than(date: datetime | None, days: int, now: datetime) -> bool:
    if date is None:
        return False
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date < now - timedelta(days=days)


def _inactive(info: DialogInfo, days: int, now: datetime) -> bool:
    """Old last message, or no messages at all in a dialog that is itself old.

    The dialog-age guard means a contact added yesterday (no messages yet)
    never classifies as a candidate.
    """
    if info.last_message_date is not None:
        return _older_than(info.last_message_date, days, now)
    return _older_than(info.dialog_date, days, now)


def classify(
    info: DialogInfo,
    thresholds: Thresholds,
    keeplist: frozenset[int] | set[int] = frozenset(),
    now: datetime | None = None,
) -> Category:
    """Decision tree mirrors the header diagram; each branch has a test."""
    now = now or datetime.now(timezone.utc)
    if info.id in keeplist:
        return Category.KEEP
    if info.kind is Kind.FORBIDDEN:
        return Category.FORBIDDEN
    if info.kind is Kind.USER:
        if info.deleted:
            return Category.GHOST
        if _inactive(info, thresholds.stale_days, now):
            return Category.STALE_DM
        return Category.KEEP
    if info.kind in (Kind.GROUP, Kind.MEGAGROUP):
        if _inactive(info, thresholds.group_quiet_days, now):
            return Category.DEAD_GROUP
        return Category.KEEP
    if info.kind is Kind.BROADCAST:
        if info.unread_count >= thresholds.channel_unread_min and not info.muted:
            return Category.UNREAD_CHANNEL
        return Category.KEEP
    return Category.KEEP


# --------------------------------------------------------------------------
# Action mapping (pure) and batch execution
# --------------------------------------------------------------------------


class Action(str, Enum):
    DELETE_DIALOG = "delete_dialog"
    LEAVE_CHANNEL_THEN_DELETE = "leave_channel_then_delete"
    DELETE_CHAT_USER_THEN_DELETE = "delete_chat_user_then_delete"
    LEAVE_CHANNEL = "leave_channel"


def action_for(candidate: Candidate) -> Action:
    """Entity-type to Telethon-call mapping. Wrong mapping is the likeliest
    v1 bug, so this stays a pure function with its own tests."""
    cat, info = candidate.category, candidate.info
    if cat in (Category.GHOST, Category.FORBIDDEN, Category.STALE_DM):
        return Action.DELETE_DIALOG
    if cat is Category.DEAD_GROUP:
        if info.left:
            return Action.DELETE_DIALOG  # lingering dialog of a group we left
        if info.kind is Kind.GROUP:
            return Action.DELETE_CHAT_USER_THEN_DELETE
        return Action.LEAVE_CHANNEL_THEN_DELETE  # megagroup
    if cat is Category.UNREAD_CHANNEL:
        return Action.LEAVE_CHANNEL
    raise ValueError(f"category {cat} is not actionable")


def is_systemic(exc: BaseException) -> bool:
    """Systemic errors abort the batch immediately; per-entity errors only
    skip. Auth and connection failures are systemic."""
    if isinstance(exc, ConnectionError):
        return True
    try:
        from telethon import errors as te
    except ImportError:  # pragma: no cover - telethon always present in prod
        return False
    if isinstance(exc, te.AuthKeyError):
        return True
    unauthorized = getattr(te.rpcbaseerrors, "UnauthorizedError", None)
    if unauthorized is not None and isinstance(exc, unauthorized):
        return True
    return False


@dataclass
class BatchResult:
    done: list[Candidate]
    failed: list[tuple[Candidate, str]]
    aborted: bool = False
    abort_reason: str = ""


async def execute_batch(
    candidates: list[Candidate],
    perform: Callable[[Candidate], Awaitable[None]],
    pause: float = PAUSE_BETWEEN_ACTIONS,
) -> BatchResult:
    """Skip-and-report executor: per-entity failures are collected and never
    abort; systemic failures abort on first occurrence."""
    result = BatchResult(done=[], failed=[])
    for i, cand in enumerate(candidates):
        try:
            await perform(cand)
            result.done.append(cand)
        except Exception as exc:
            if is_systemic(exc):
                result.aborted = True
                result.abort_reason = f"{type(exc).__name__}: {exc}"
                break
            result.failed.append((cand, f"{type(exc).__name__}: {exc}"))
        if pause and i < len(candidates) - 1:
            await asyncio.sleep(pause)
    return result


# --------------------------------------------------------------------------
# Keeplist
# --------------------------------------------------------------------------


def load_keeplist() -> set[int]:
    if not KEEPLIST_PATH.exists():
        return set()
    try:
        return {int(x) for x in json.loads(KEEPLIST_PATH.read_text())}
    except (ValueError, TypeError):
        console.print(f"[yellow]Warning: {KEEPLIST_PATH} is unreadable, ignoring it.[/yellow]")
        return set()


def append_keeplist(chat_id: int) -> None:
    ids = load_keeplist()
    ids.add(chat_id)
    KEEPLIST_PATH.write_text(json.dumps(sorted(ids), indent=0) + "\n")


# --------------------------------------------------------------------------
# Telethon adapter (the only network-aware section)
# --------------------------------------------------------------------------


def validate_env() -> tuple[int, str, str]:
    load_dotenv()
    missing = [k for k in ("TG_API_ID", "TG_API_HASH", "TG_PHONE") if not os.environ.get(k)]
    if missing:
        console.print(
            f"[red]Missing environment variables: {', '.join(missing)}.[/red]\n"
            "Copy .env.example to .env and fill in your credentials from "
            "https://my.telegram.org — see runbook/auth.md."
        )
        raise typer.Exit(code=1)
    return int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"], os.environ["TG_PHONE"]


def _is_muted(dialog, now: datetime) -> bool:
    settings = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None)
    if mute_until is None:
        return False
    if isinstance(mute_until, datetime):
        if mute_until.tzinfo is None:
            mute_until = mute_until.replace(tzinfo=timezone.utc)
        return mute_until > now
    return bool(mute_until)


def build_info(dialog) -> DialogInfo:
    from telethon.tl import types as t

    entity = dialog.entity
    deleted = False
    left = False
    username = getattr(entity, "username", None)
    if isinstance(entity, (t.ChatForbidden, t.ChannelForbidden)):
        kind = Kind.FORBIDDEN
    elif isinstance(entity, t.User):
        kind = Kind.USER
        deleted = bool(entity.deleted)
    elif isinstance(entity, t.Chat):
        kind = Kind.GROUP
        left = bool(getattr(entity, "left", False))
    elif isinstance(entity, t.Channel):
        kind = Kind.MEGAGROUP if entity.megagroup else Kind.BROADCAST
        left = bool(getattr(entity, "left", False))
    else:  # unknown entity type: never a candidate
        kind = Kind.USER
    message = dialog.message
    snippet = ""
    if message is not None and getattr(message, "message", None):
        snippet = message.message[:60].replace("\n", " ")
    return DialogInfo(
        id=dialog.id,
        title=dialog.name or "(no title)",
        kind=kind,
        last_message_date=getattr(message, "date", None),
        dialog_date=dialog.date,
        unread_count=dialog.unread_count or 0,
        muted=_is_muted(dialog, datetime.now(timezone.utc)),
        deleted=deleted,
        left=left,
        username=username,
        snippet=snippet,
    )


async def _connect():
    from telethon import TelegramClient

    api_id, api_hash, phone = validate_env()
    client = TelegramClient(SESSION_NAME, api_id, api_hash, flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD)
    await client.start(phone=phone)
    return client


async def _fetch(thresholds: Thresholds) -> list[Candidate]:
    keeplist = load_keeplist()
    now = datetime.now(timezone.utc)
    client = await _connect()
    try:
        infos = [build_info(d) async for d in client.iter_dialogs()]
    finally:
        await client.disconnect()
    return [Candidate(i, classify(i, thresholds, keeplist, now)) for i in infos]


async def _perform_with(client, candidate: Candidate) -> None:
    from telethon.tl.functions.channels import LeaveChannelRequest
    from telethon.tl.functions.messages import DeleteChatUserRequest

    action = action_for(candidate)
    info = candidate.info
    if action is Action.DELETE_DIALOG:
        await client.delete_dialog(info.id)
    elif action is Action.LEAVE_CHANNEL:
        await client(LeaveChannelRequest(await client.get_input_entity(info.id)))
    elif action is Action.LEAVE_CHANNEL_THEN_DELETE:
        await client(LeaveChannelRequest(await client.get_input_entity(info.id)))
        await client.delete_dialog(info.id)
    elif action is Action.DELETE_CHAT_USER_THEN_DELETE:
        await client(DeleteChatUserRequest(chat_id=info.id, user_id="me"))
        await client.delete_dialog(info.id)


async def _execute(candidates: list[Candidate]) -> BatchResult:
    client = await _connect()
    try:
        return await execute_batch(candidates, lambda c: _perform_with(client, c))
    finally:
        await client.disconnect()


# Module-level seams: tests monkeypatch these two names; commands call only them.


def gather_candidates(thresholds: Thresholds) -> list[Candidate]:
    return _run_network(_fetch(thresholds))


def run_actions(candidates: list[Candidate]) -> BatchResult:
    return _run_network(_execute(candidates))


def _run_network(coro):
    from telethon import errors as te

    try:
        return asyncio.run(coro)
    except te.FloodWaitError as exc:
        console.print(
            f"[red]Telegram asked for a {exc.seconds}s wait (above the {FLOOD_SLEEP_THRESHOLD}s "
            "auto-sleep threshold). Nothing more was changed — re-run later; "
            "scan is idempotent and will pick up survivors.[/red]"
        )
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

CONFIRM_COPY = {
    Category.GHOST: "PERMANENT: this deletes the conversation for you. There is no undo.",
    Category.STALE_DM: "PERMANENT: this deletes the conversation for you. There is no undo.",
    Category.FORBIDDEN: (
        "Removes this dead entry from your chat list (the group already removed "
        "you or was deleted). Nothing else to lose."
    ),
    Category.DEAD_GROUP: (
        "PERMANENT: your copy of this group's history will be deleted. "
        "The group itself can be rejoined via {handle}."
    ),
    Category.UNREAD_CHANNEL: "Leaves the channel. Rejoinable via {handle}.",
}


def _handle(info: DialogInfo) -> str:
    return f"@{info.username}" if info.username else "no public link"


def _fmt_date(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d") if d else "(no messages)"


def _candidate_table(candidates: list[Candidate], title: str) -> Table:
    table = Table(title=title)
    table.add_column("Type")
    table.add_column("Title", max_width=40)
    table.add_column("Last message")
    table.add_column("Unread", justify="right")
    table.add_column("Handle")
    for c in candidates:
        table.add_row(
            c.category.value,
            c.info.title,
            _fmt_date(c.info.last_message_date),
            str(c.info.unread_count),
            _handle(c.info),
        )
    return table


def _failures(result: BatchResult) -> None:
    if result.failed:
        table = Table(title="Failed (skipped, will reappear on next scan)")
        table.add_column("Title")
        table.add_column("Error")
        for cand, err in result.failed:
            table.add_row(cand.info.title, err)
        console.print(table)
    if result.aborted:
        console.print(
            f"[red]Batch aborted on systemic error: {result.abort_reason}. "
            "Fix the connection/auth and re-run.[/red]"
        )
    console.print(
        f"Done: {len(result.done)}  Failed: {len(result.failed)}"
        + ("  ABORTED" if result.aborted else "")
    )


def _thresholds_options(stale_days: int, group_quiet_days: int, channel_unread_min: int) -> Thresholds:
    return Thresholds(stale_days, group_quiet_days, channel_unread_min)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


@app.command()
def scan(
    stale_days: int = typer.Option(730, help="DM silence threshold in days"),
    group_quiet_days: int = typer.Option(365, help="Group silence threshold in days"),
    channel_unread_min: int = typer.Option(50, help="Channel unread-count threshold"),
) -> None:
    """Read-only: classify every dialog and print the results. Always safe."""
    candidates = gather_candidates(_thresholds_options(stale_days, group_quiet_days, channel_unread_min))
    counts: dict[Category, int] = {}
    for c in candidates:
        counts[c.category] = counts.get(c.category, 0) + 1
    summary = Table(title="Scan summary")
    summary.add_column("Category")
    summary.add_column("Count", justify="right")
    for cat in Category:
        summary.add_row(cat.value, str(counts.get(cat, 0)))
    console.print(summary)
    actionable = [c for c in candidates if c.category is not Category.KEEP]
    if actionable:
        console.print(_candidate_table(actionable, "Candidates"))
    else:
        console.print("Nothing to clean. Your chat list is tidy.")


@app.command("purge-ghosts")
def purge_ghosts(
    yes: bool = typer.Option(False, "--yes", help="Actually delete. Without it: dry run."),
    include_forbidden: bool = typer.Option(
        False, "--include-forbidden", help="Also sweep ChatForbidden/ChannelForbidden tombstones."
    ),
) -> None:
    """Delete all deleted-account DMs (and optionally forbidden tombstones)."""
    wanted = {Category.GHOST} | ({Category.FORBIDDEN} if include_forbidden else set())
    candidates = [c for c in gather_candidates(Thresholds()) if c.category in wanted]
    if not candidates:
        console.print("No ghosts found. Nothing to do.")
        return
    console.print(_candidate_table(candidates, f"Ghost purge targets ({len(candidates)})"))
    console.print(CONFIRM_COPY[Category.GHOST])
    if not yes:
        console.print(
            f"[yellow]DRY RUN: nothing deleted. Re-run with --yes to delete "
            f"{len(candidates)} chats.[/yellow]"
        )
        return
    _failures(run_actions(candidates))


@app.command()
def review(
    types: str = typer.Option(
        "stale_dm,dead_group,unread_channel",
        help="Comma-separated candidate types to review.",
    ),
    stale_days: int = typer.Option(730, help="DM silence threshold in days"),
    group_quiet_days: int = typer.Option(365, help="Group silence threshold in days"),
    channel_unread_min: int = typer.Option(50, help="Channel unread-count threshold"),
) -> None:
    """Interactive triage: y = approve, n = skip, k = keep forever. One final
    confirmation gates the whole batch. There is no --yes here by design."""
    allowed = {c.value for c in REVIEW_TYPES}
    requested = [t.strip() for t in types.split(",") if t.strip()]
    invalid = [t for t in requested if t not in allowed]
    if invalid:
        console.print(
            f"[red]Invalid --types value(s): {', '.join(invalid)}. "
            f"Reviewable types: {', '.join(sorted(allowed))}. "
            "(ghost/forbidden are purge-only via purge-ghosts.)[/red]"
        )
        raise typer.Exit(code=1)
    wanted = {Category(t) for t in requested}
    thresholds = _thresholds_options(stale_days, group_quiet_days, channel_unread_min)
    candidates = [c for c in gather_candidates(thresholds) if c.category in wanted]
    if not candidates:
        console.print("No candidates for the requested types. Nothing to review.")
        return

    approved: list[Candidate] = []
    for i, cand in enumerate(candidates, 1):
        info = cand.info
        console.print(
            f"\n[bold]{i}/{len(candidates)}[/bold]  [{cand.category.value}]  {info.title}\n"
            f"  last message: {_fmt_date(info.last_message_date)}   unread: {info.unread_count}\n"
            + (f"  preview: {info.snippet}\n" if info.snippet else "")
            + f"  {CONFIRM_COPY[cand.category].format(handle=_handle(info))}"
        )
        while True:
            choice = typer.prompt("  approve / skip / keep forever [y/n/k]").strip().lower()
            if choice in ("y", "n", "k"):
                break
            console.print("  Please answer y, n, or k.")
        if choice == "y":
            approved.append(cand)
        elif choice == "k":
            append_keeplist(info.id)
            console.print(f"  Added to keeplist — {info.title} will never be suggested again.")

    if not approved:
        console.print("\nNothing approved. Nothing executed.")
        return
    console.print(_candidate_table(approved, f"Approved actions ({len(approved)})"))
    if not typer.confirm(f"Execute these {len(approved)} actions now?"):
        console.print("Cancelled. Nothing executed.")
        return
    _failures(run_actions(approved))


if __name__ == "__main__":
    app()
