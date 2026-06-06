# FloodWait / rate limit runbook

## How the tool handles rate limits

- The Telethon client is constructed with `flood_sleep_threshold=300`: any
  FloodWait up to 300 seconds is slept through automatically by the library,
  on both the read pass (scan) and destructive calls. You see a pause, not an
  error.
- A FloodWait above 300 seconds aborts the current command cleanly with the
  requested wait time. Re-run after that long; `scan` is idempotent and a
  re-run of `purge-ghosts`/`review` picks up the survivors.
- A fixed 1-second pause runs between consecutive destructive calls to keep
  batches polite.

## Error classes in batches

- Per-chat errors (weird entity, partially unavailable ghost): skipped,
  collected, printed in the failures table at the end. They never abort.
- Systemic errors (ConnectionError, auth key / unauthorized): abort the batch
  immediately on first occurrence.

## Observed timings

(fill in after first real runs)

- Dialog count: ___
- scan wall time: ___
- purge-ghosts batch of N=___: ___ (FloodWaits hit: ___)

## Known unknowns

- `delete_dialog` behavior on deleted-account entities was verified manually
  during the initial spike (see design doc Assignment). Record the result here:
  ___
