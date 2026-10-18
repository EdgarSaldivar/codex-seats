# Agents

codex-seats is a pool of Codex seats, a Claude account and optional local seats.
Use all of it:

- `codex-seat capacity --json`: where work can run now (read-only). Seats are
  primary or worker; routing uses the primary only when no worker fits.
- `codex-seat delegate --detach "..."`: one job per independent task, then
  `codex-seat await JOB_ID ...`.
- `codex-seat seats pause|resume|refresh SEAT`, `codex-seat provider claude set`:
  light management from any head or shell (managed workers are refused).
- `--json` output uses `{"schema": 1, "ok": ..., "data" | "error": ...}`.
- `seats login`, `seats retire/remove` and `redeem-reset` stay with a human.

Run `codex-seat help agents` for the summary and read
[docs/agents.md](docs/agents.md) for the full guide.
