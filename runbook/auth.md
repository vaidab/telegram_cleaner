# Auth runbook

## First login

1. Credentials come from https://my.telegram.org -> API development tools.
   Put them in `.env` (`TG_API_ID`, `TG_API_HASH`, `TG_PHONE`). All three are
   required; the tool exits with a pointer to this file if any is missing.
2. On first run, Telethon sends a login code to your Telegram app (not SMS by
   default). Enter it at the prompt.
3. If your account has 2FA (cloud password), you will be prompted for it next.
4. A `cleaner.session` file is created in the project root. It contains your
   auth key: treat it like a password. It is gitignored along with `.env`.

## Session file notes

- Delete `cleaner.session` to force a fresh login.
- A `database is locked` error means another process holds the session file;
  close the other run.
- If you revoke the session from Telegram (Settings -> Devices), the next run
  raises an auth error and the tool aborts immediately (systemic error class).

## Credential rotation

Regenerate the api_hash at my.telegram.org, update `.env`, delete the session
file, and log in again.

## Timing expectations

- First login: under a minute, interactive.
- Subsequent runs: connect takes 1-3 seconds with a valid session file.
