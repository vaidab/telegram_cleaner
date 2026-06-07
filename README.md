# telegram_cleaner

Personal Telegram janitor. Sweeps the dead weight out of your chat list.

Every dialog is classified into exactly one type. The type decides both what
happens to it and **which command handles it**:

| Type | What it is | Detected by | Action | Command |
|------|-----------|-------------|--------|---------|
| `ghost` | DM with a "Deleted Account" user | account's deleted flag | delete conversation | `purge-ghosts` |
| `forbidden` | Tombstone of a group that kicked you or was deleted by its owner | entity type | delete dead entry | `purge-ghosts --include-forbidden` |
| `stale_dm` | DM silent longer than `--stale-days` (default 730 days) | last message age | delete conversation | `review` |
| `dead_group` | Group with no messages for `--group-quiet-days` (default 365 days) | last message age | leave + delete history | `review` |
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
- Muted channels never classify as `unread_channel`: mute is read as "deliberately kept".
- `ghost` and `forbidden` are purge-only; passing them to `review --types` is rejected.

The full clean is two commands:

```bash
uv run cleaner.py purge-ghosts --yes --include-forbidden   # the no-questions sweep
uv run cleaner.py review                                   # the judgment calls
```

Runs locally against your own account via Telethon (official MTProto API). No bot,
no third-party service ever sees your account.

## Setup

1. Get API credentials at https://my.telegram.org → API development tools.
2. Copy `.env.example` to `.env` and fill in your values:

```
TG_API_ID=12345678
TG_API_HASH=0123456789abcdef...
TG_PHONE=+40700000000
```

3. Install dependencies:

```bash
uv sync
```

First run will ask for the login code Telegram sends to your app (and your 2FA
password if set). A `cleaner.session` file is created — treat it like a password,
it is gitignored. See `runbook/auth.md` for details.

## Usage

### scan

Read-only. Classifies every dialog and prints a summary table (counts per
category) plus a detail table of all actionable candidates. Always safe to run.

```bash
uv run cleaner.py scan

# Show only specific types in the candidates table (summary still shows all counts)
uv run cleaner.py scan --types ghost
uv run cleaner.py scan --types stale_dm,dead_group

# Adjust thresholds
uv run cleaner.py scan --stale-days 365 --group-quiet-days 180 --channel-unread-min 100
```

### purge-ghosts

Deletes deleted-account DMs (and optionally forbidden tombstones) in one shot.
Dry run by default — prints what would be deleted. Pass `--yes` to execute.

```bash
uv run cleaner.py purge-ghosts            # dry run: shows candidates, does nothing
uv run cleaner.py purge-ghosts --yes      # deletes all ghost DMs
uv run cleaner.py purge-ghosts --yes --include-forbidden   # also clears tombstones
```

### review

Interactive triage for `stale_dm`, `dead_group`, and `unread_channel`. Each
candidate is shown with its Telegram ID, last message date and preview, unread
count, and a clickable `https://t.me/` link (where available) so you can open
the chat before deciding.

Per-item choices:
- `y` — approve (add to the batch)
- `n` — skip (chat stays, reappears on the next scan)
- `k` — keep forever (chat ID added to `keeplist.json`, never suggested again)
- `q` — approve this item and stop (skips all remaining, then confirms the batch)

After triage, the full list of approved actions is shown and a single final
confirmation gates execution. Cancelling at that point executes nothing.

```bash
uv run cleaner.py review                           # all three reviewable types
uv run cleaner.py review --types stale_dm          # only stale DMs
uv run cleaner.py review --types dead_group,unread_channel

# Skip per-item triage: approve the entire batch at once (final confirmation still required)
uv run cleaner.py review --approve-all
uv run cleaner.py review --approve-all --types dead_group

# Adjust thresholds
uv run cleaner.py review --stale-days 365 --group-quiet-days 180 --channel-unread-min 100
```

## Thresholds

All three threshold flags work on both `scan` and `review`:

| Flag | Default | Applies to |
|------|---------|-----------|
| `--stale-days` | 730 | `stale_dm` |
| `--group-quiet-days` | 365 | `dead_group` |
| `--channel-unread-min` | 50 | `unread_channel` |

## keeplist.json

Chat IDs marked `k` in review are written to `keeplist.json` (gitignored,
hand-editable — it is a plain JSON list of integers). Any chat ID in this file
classifies as `keep` unconditionally and never appears as a candidate again.

## Safety model

- Deletion on Telegram is irreversible. Every destructive command prints the full
  candidate list before acting.
- `purge-ghosts` without `--yes` is a dry run.
- `review` requires per-item approval AND a final batch confirmation; `--approve-all`
  skips the per-item loop but the final confirmation still fires.
- Per-chat failures are skipped and reported at the end; auth/network failures
  abort the batch immediately.
- Rate limits are handled by Telethon (`flood_sleep_threshold=300`); any wait
  above 5 minutes aborts cleanly with a re-run-later message.
- A 1-second pause between consecutive destructive calls keeps batches polite.

## Tests

```bash
uv run pytest
uv run ruff check .
```

Classification, action mapping, batch executor (including the error-class circuit
breaker), and all CLI safety gates are covered without touching the network.

## Runbook

- `runbook/auth.md` — first-login flow, session files, credential rotation
- `runbook/flood-wait.md` — rate-limit behavior, observed timings, known unknowns
