"""Text printed by ``codex-seat help agents``; docs/agents.md expands on it."""

AGENT_GUIDE = """\
codex-seat for agents: use all the compute the pool has

1. See capacity (read-only, never calls a provider)
   codex-seat capacity --json        every Codex seat, Claude and local seat:
                                     status, 5h/7d left, resets, fits (small/big),
                                     busy task, banked resets, running/queued jobs,
                                     and the next seat per size
   codex-seat pick-seat --size small|big --json
                                     the Codex seat routing would choose now;
                                     workers first, the primary only when no
                                     worker fits
   codex-seat seats refresh --all    refresh stale usage first if `fresh` is false

2. Use it: one delegate per independent task, then await them together
   codex-seat delegate --detach "task A"     prints a job id; repeat per task
   codex-seat delegate --detach --parallel-safe --scope-hint src/a "task B"
   codex-seat await JOB_ID [JOB_ID ...]      one blocking wait for every receipt
   codex-seat job list --json                live queue; job status JOB_ID --json
   Routing picks the provider/seat (--provider auto). Steer with
   --provider codex|claude|local, --kind (implementation, review, docs, ...),
   --read-only. --size, --tier and --effort that differ from the kind default
   need --override-reason "...".

3. Light management (heads and plain shells; managed workers are refused)
   codex-seat seats pause SEAT [--dry-run]    idle: deactivate; busy: drain
   codex-seat seats resume SEAT [--dry-run]   re-checks login and usage first
   codex-seat seats refresh SEAT | --all
   codex-seat seats set|set-role|activate|drain|deactivate SEAT ... [--dry-run]
                                     roles are primary or worker; make another
                                     seat primary before pausing the primary
   codex-seat provider claude set --mode M --tier T --small-headroom N
                                  --big-headroom N --active-usage-refresh on|off
   codex-seat config policy set --set KEY VALUE --apply
   --seat SEAT is accepted wherever a command takes a seat.

4. JSON envelope (--json)
   success  {"schema": 1, "ok": true, "data": ...}
   failure  {"schema": 1, "ok": false, "error": {"code": ..., "message": ...}}
            on stdout, with a nonzero exit. `seats ... --json` keeps its
            existing top-level keys and adds "ok"/"error" beside them.

5. Exit codes
   pick-seat     0 seat, 3 exhausted (error.code "exhausted"), 2 unreadable
   capacity      0, 2 pool state unreadable
   seats         0 ok, 1 doctor errors / blocked plan / refresh failed,
                 2 refused (for example a managed worker)
   delegate      0 completed or detached, 1 not completed, 2 error
   await         0 all completed, 1 some not completed, 2 timeout or error
   job status    0, 3 unknown job, 2 error
   redeem-reset  2 bad seat or worker, 3 refused (needs a human), 4 not reset

6. Human terminal only (unchanged)
   seats login, seats retire/remove, redeem-reset: they sign in, discard a
   seat, or spend a one-time reset credit, so a person confirms them.

Full guide: docs/agents.md
"""
