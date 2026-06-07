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
LAST_SCAN_PATH = Path(".tmp") / "last-scan.json"
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
    on_item: Callable[[Candidate], None] | None = None,
) -> BatchResult:
    """Skip-and-report executor: per-entity failures are collected and never
    abort; systemic failures abort on first occurrence. on_item fires after
    each attempted candidate (success or per-entity failure) so callers can
    drive a progress display; unattempted items after an abort never fire it."""
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
        if on_item is not None:
            on_item(cand)
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
        # migrated_to is set when the basic group was converted to a supergroup;
        # the old Chat ID is invalid for any leave/kick call — treat as already-left
        # so action_for() degrades to delete_dialog only.
        migrated = getattr(entity, "migrated_to", None) is not None
        left = migrated or bool(getattr(entity, "left", False))
    elif isinstance(entity, t.Channel):
        kind = Kind.MEGAGROUP if entity.megagroup else Kind.BROADCAST
        left = bool(getattr(entity, "left", False))
    else:  # unknown entity type: never a candidate
        kind = Kind.USER
    message = dialog.message
    snippet = ""
    if message is not None:
        if getattr(message, "message", None):
            snippet = message.message[:60].replace("\n", " ")
        else:
            snippet = "(media or service message)"
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
    from telethon.errors import ChatIdInvalidError
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
        try:
            await client(DeleteChatUserRequest(chat_id=info.id, user_id="me"))
        except ChatIdInvalidError:
            # Chat was migrated to a supergroup after classification; the old Chat
            # ID is invalid. Fall through to delete_dialog to at least clear the
            # stale entry from the dialog list.
            pass
        await client.delete_dialog(info.id)


async def _execute(
    candidates: list[Candidate],
    on_item: Callable[[Candidate], None] | None = None,
) -> BatchResult:
    client = await _connect()
    try:
        return await execute_batch(candidates, lambda c: _perform_with(client, c), on_item=on_item)
    finally:
        await client.disconnect()


# Module-level seams: tests monkeypatch these two names; commands call only them.


def gather_candidates(thresholds: Thresholds) -> list[Candidate]:
    return _run_network(_fetch(thresholds))


def run_actions(candidates: list[Candidate]) -> BatchResult:
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeRemainingColumn,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        TextColumn("{task.fields[current]}", style="dim"),
        console=console,
    ) as progress:
        task = progress.add_task("Cleaning", total=len(candidates), current="")
        return _run_network(
            _execute(
                candidates,
                on_item=lambda c: progress.update(task, advance=1, current=c.info.title[:40]),
            )
        )


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


def chat_links(info: DialogInfo) -> list[str]:
    """Best-effort https://t.me/ links that open the chat.

    Returns at most one link. Empty list when no reliable link exists
    (basic private group with no username).
    """
    if info.username:
        return [f"https://t.me/{info.username}"]
    raw = str(info.id)
    if raw.startswith("-100"):
        return [f"https://t.me/c/{raw[4:]}"]
    return []


def _fmt_date(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d") if d else "(no messages)"


def _candidate_table(candidates: list[Candidate], title: str, show_numbers: bool = False) -> Table:
    table = Table(title=title)
    if show_numbers:
        table.add_column("#", justify="right", style="dim")
    table.add_column("Type")
    table.add_column("Title", max_width=40)
    table.add_column("Last message")
    table.add_column("Unread", justify="right")
    table.add_column("Handle")
    for i, c in enumerate(candidates, 1):
        row = [c.category.value, c.info.title, _fmt_date(c.info.last_message_date), str(c.info.unread_count), _handle(c.info)]
        table.add_row(*([str(i)] + row if show_numbers else row))
    return table


def save_last_scan(candidates: list[Candidate]) -> None:
    """Persist numbered candidates from the most recent scan for inspect lookup."""
    LAST_SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = [{"n": i + 1, "id": c.info.id, "title": c.info.title, "category": c.category.value} for i, c in enumerate(candidates)]
    LAST_SCAN_PATH.write_text(json.dumps(data))


def load_last_scan() -> dict[int, int]:
    """Return {scan_number: chat_id} from the last scan. Empty if none."""
    if not LAST_SCAN_PATH.exists():
        return {}
    try:
        return {entry["n"]: entry["id"] for entry in json.loads(LAST_SCAN_PATH.read_text())}
    except (ValueError, KeyError):
        return {}


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
    types: str = typer.Option(
        "",
        help=(
            "Comma-separated types to show in the candidates table "
            "(ghost, forbidden, stale_dm, dead_group, unread_channel). "
            "Omit to show all actionable types."
        ),
    ),
    stale_days: int = typer.Option(730, help="DM silence threshold in days"),
    group_quiet_days: int = typer.Option(365, help="Group silence threshold in days"),
    channel_unread_min: int = typer.Option(50, help="Channel unread-count threshold"),
) -> None:
    """Read-only: classify every dialog and print the results. Always safe."""
    actionable_cats = {c for c in Category if c is not Category.KEEP}
    if types.strip():
        requested = [t.strip() for t in types.split(",") if t.strip()]
        invalid = [t for t in requested if t not in {c.value for c in Category} or t == Category.KEEP.value]
        if invalid:
            console.print(
                f"[red]Unknown type(s): {', '.join(invalid)}. "
                f"Valid: {', '.join(c.value for c in Category if c is not Category.KEEP)}[/red]"
            )
            raise typer.Exit(code=1)
        filter_cats: set[Category] | None = {Category(t) for t in requested}
    else:
        filter_cats = None  # show all actionable

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
    shown_cats = filter_cats if filter_cats is not None else actionable_cats
    actionable = [c for c in candidates if c.category in shown_cats]
    save_last_scan(actionable)
    if actionable:
        console.print(_candidate_table(actionable, "Candidates", show_numbers=True))
        console.print("[dim]Use: uv run cleaner.py inspect <number> [number ...][/dim]")
    else:
        msg = "Nothing to clean. Your chat list is tidy." if filter_cats is None else f"No candidates for: {types}"
        console.print(msg)


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
    approve_all: bool = typer.Option(
        False,
        "--approve-all",
        help=(
            "Skip per-item triage and approve the entire batch at once. "
            "A final confirmation still gates execution."
        ),
    ),
) -> None:
    """Interactive triage: y = approve, n = skip, k = keep forever. One final
    confirmation gates the whole batch. There is no --yes here by design.

    Use --approve-all to skip per-item triage and clean everything the
    scanner found in one shot (final confirmation still required)."""
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

    if approve_all:
        approved = list(candidates)
        console.print(_candidate_table(approved, f"All candidates ({len(approved)}) — approve-all mode"))
    else:
        approved = []
        for i, cand in enumerate(candidates, 1):
            info = cand.info
            links = chat_links(info)
            links_line = "  open: " + "   ".join(f"[link={u}]{u}[/link]" for u in links) + "\n" if links else ""
            console.print(
                f"\n[bold]{i}/{len(candidates)}[/bold]  [{cand.category.value}]  {info.title}"
                f"   (id: {info.id})\n"
                f"  last message: {_fmt_date(info.last_message_date)}"
                + (f" - {info.snippet}" if info.snippet else "")
                + f"   unread: {info.unread_count}\n"
                + links_line
                + f"  {CONFIRM_COPY[cand.category].format(handle=_handle(info))}"
            )
            while True:
                choice = typer.prompt("  approve / skip / keep forever / approve & execute now [y/n/k/q]").strip().lower()
                if choice in ("y", "n", "k", "q"):
                    break
                console.print("  Please answer y, n, k, or q.")
            if choice in ("y", "q"):
                approved.append(cand)
            if choice == "k":
                append_keeplist(info.id)
                console.print(f"  Added to keeplist — {info.title} will never be suggested again.")
            if choice == "q":
                remaining = len(candidates) - i
                if remaining:
                    console.print(f"  Stopping — {remaining} item(s) skipped.")
                break

    if not approved:
        console.print("\nNothing approved. Nothing executed.")
        return
    console.print(_candidate_table(approved, f"Approved actions ({len(approved)})"))
    if not typer.confirm(f"Execute these {len(approved)} actions now?"):
        console.print("Cancelled. Nothing executed.")
        return
    _failures(run_actions(approved))


@app.command()
def inspect(
    targets: list[str] = typer.Argument(
        help="Scan numbers from the last scan (e.g. 1 3 5) or name substrings. Mix freely."
    ),
    messages: int = typer.Option(5, "--messages", "-m", help="Number of recent messages to fetch."),
    stale_days: int = typer.Option(730, help="DM silence threshold in days"),
    group_quiet_days: int = typer.Option(365, help="Group silence threshold in days"),
    channel_unread_min: int = typer.Option(50, help="Channel unread-count threshold"),
) -> None:
    """Show why chats were classified and their recent messages. Read-only.

    Pass scan result numbers (from the # column) or name substrings:

      uv run cleaner.py inspect 1 3 5
      uv run cleaner.py inspect "Mr.Crypto"
      uv run cleaner.py inspect 2 "atlas"
    """
    if not targets:
        console.print("[red]Provide at least one scan number or name substring.[/red]")
        raise typer.Exit(code=1)

    last_scan = load_last_scan()  # {n: chat_id}

    # Resolve each target to either a chat_id (int) or a name needle (str).
    by_id: list[int] = []
    by_name: list[str] = []
    for t in targets:
        if t.isdigit():
            n = int(t)
            if n not in last_scan:
                console.print(f"[red]#{n} not found in last scan. Run 'scan' first.[/red]")
                raise typer.Exit(code=1)
            by_id.append(last_scan[n])
        else:
            by_name.append(t.lower())

    async def _inspect() -> None:
        from telethon.tl.types import Message

        client = await _connect()
        try:
            thresholds = _thresholds_options(stale_days, group_quiet_days, channel_unread_min)
            keeplist = load_keeplist()
            now = datetime.now(timezone.utc)

            found: list[tuple] = []
            async for dialog in client.iter_dialogs():
                info = build_info(dialog)
                matched = info.id in by_id or any(n in (dialog.name or "").lower() for n in by_name)
                if not matched:
                    continue
                category = classify(info, thresholds, keeplist, now)
                found.append((dialog, info, category))

            if not found:
                console.print("No matching chats found.")
                return

            for dialog, info, category in found:
                console.rule(f"[bold]{info.title}[/bold]")
                console.print(f"  id:         {info.id}")
                console.print(f"  kind:       {info.kind.value}")
                console.print(f"  category:   [bold]{category.value}[/bold]")
                console.print(f"  last msg:   {_fmt_date(info.last_message_date)}")
                console.print(f"  unread:     {info.unread_count}")
                console.print(f"  muted:      {info.muted}")
                links = chat_links(info)
                if links:
                    console.print(f"  link:       [link={links[0]}]{links[0]}[/link]")
                else:
                    console.print("  link:       none (private — find it by name in your Telegram app)")

                console.print(f"\n  Last {messages} message(s):")
                async for msg in client.iter_messages(dialog.id, limit=messages):
                    if not isinstance(msg, Message):
                        continue
                    sender = getattr(msg, "sender_id", "?")
                    date = msg.date.strftime("%Y-%m-%d") if msg.date else "?"
                    text = (msg.message or "(media/service)").replace("\n", " ")[:120]
                    console.print(f"    [{date}] {sender}: {text}")
                console.print()
        finally:
            await client.disconnect()

    _run_network(_inspect())


if __name__ == "__main__":
    app()
