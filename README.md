# telegram_cleaner

Personal Telegram janitor. Sweeps the dead weight out of your chat list.

Every dialog is classified into exactly one type. The type decides both what
happens to it and **which command handles it**:

| Type | What it is | Detected by | Action | Command |
|------|-----------|-------------|--------|---------|
| `ghost` | DM with a "Deleted Account" user | account's deleted flag | delete conversation | `purge-ghosts` |
| `forbidden` | Tombstone of a group that kicked you or was deleted by its owner | entity type | delete dead entry | `purge-ghosts --include-forbidden` |
| `stale_dm` | DM silent longer than `--stale-days` (default 730) | last message age | delete conversation | `review` |
| `dead_group` | Group with no messages for `--group-quiet-days` (default 365) | last message age | leave + delete history | `review` |
| `unread_channel` | Broadcast channel with `--channel-unread-min`+ unread (default 50), not muted | unread count | unsubscribe | `review` |
| `keep` | Everything else, plus anything in `keeplist.json` | — | never touched | — |

Why two commands: `ghost` and `forbidden` are unambiguous dead weight — nothing
of value can be lost, so `purge-ghosts` sweeps them in one shot. The other three
are heuristics that can be wrong about chats you still want (a quiet group, a
muted-adjacent channel), so `review` walks you through them one at a time and
nothing executes until you approve each item AND confirm the final batch.

Notes on detection:
- An empty dialog (zero messages) counts as stale/dead only if the dialog
  itself is older than the threshold — a contact added yesterday never shows up.
- Muted channels never classify as `unread_channel`: mute is read as
  "deliberately kept".
- `ghost` and `forbidden` are purge-only; `review --types ghost` is rejected.

The full clean is two commands:

```bash
uv run cleaner.py purge-ghosts --yes --include-forbidden   # the no-questions sweep
uv run cleaner.py review                                   # the judgment calls
```

Runs locally against your own account via Telethon (official MTProto API). No bot,
no third-party service ever sees your account.

## Setup

1. Get API credentials at https://my.telegram.org (API development tools).
2. Copy `.env.example` to `.env` and fill in `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`.
3. Install dependencies:

```bash
uv sync
```

First run will ask for the login code Telegram sends you (and your 2FA password if
set). See `runbook/auth.md`.

## Usage

```bash
# Read-only: classify every dialog, print counts and tables. Always safe.
uv run cleaner.py scan

# Delete all deleted-account DMs. Dry-run by default; --yes to execute.
uv run cleaner.py purge-ghosts
uv run cleaner.py purge-ghosts --yes
uv run cleaner.py purge-ghosts --yes --include-forbidden

# Interactive triage of stale DMs / dead groups / unread channels.
# y = approve, n = skip, k = keep forever (never suggested again).
# One final confirmation gates the whole batch.
uv run cleaner.py review
uv run cleaner.py review --types stale_dm
uv run cleaner.py review --stale-days 365
```

Thresholds: `--stale-days` (default 730), `--group-quiet-days` (default 365),
`--channel-unread-min` (default 50) on both `scan` and `review`.

`keeplist.json` (gitignored, hand-editable) holds chat IDs marked "keep forever"
via `k` in review; they never appear as candidates again.

## Safety model

- Deletion on Telegram is irreversible. Every destructive command prints the full
  candidate list first.
- `purge-ghosts` without `--yes` is a dry run; `review` requires per-item approval
  AND a final batch confirmation.
- Per-chat failures are skipped and reported at the end; auth/network failures
  abort the batch immediately.
- Rate limits are handled by Telethon (`flood_sleep_threshold=300`); a wait above
  5 minutes aborts cleanly with a re-run-later message.

## Tests

```bash
uv run pytest
uv run ruff check .
```

Classification, action mapping, batch executor, and the CLI safety gates are all
covered without touching the network.

## Runbook

- `runbook/auth.md` — first-login flow, session files, credential rotation
- `runbook/flood-wait.md` — rate-limit behavior, observed timings, known unknowns
