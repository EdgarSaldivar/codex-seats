# Using codex-seats from an agent

codex-seats gives a Claude or Codex agent one tool to use all of the compute the
pool has: every registered Codex seat, the Claude account, and any local seats.
The agent looks at capacity, spreads work across it with `delegate`, and does
light management when something needs a nudge. `codex-seat help agents` prints
the short version of this page.

## 1. See capacity

```sh
codex-seat capacity --json
```

`capacity` is read-only. It reads cached usage, the live queue and the
`pick-seat` selection, and never calls a provider. The same model drives the
terminal manager's summary strip and seat table, so agents and humans see the
same thing.

`data.pool`:

| key | meaning |
| --- | --- |
| `ready` / `total` | idle entries that fit a small task / enabled entries across Codex, Claude and local; the dashboard header and the manager show the same numbers (`pool_view.summary`) |
| `running`, `queued`, `waiting_capacity` | live job counts |
| `next.small`, `next.big` | the Codex seat `pick-seat --size` would choose (`null` when exhausted); workers before the primary |
| `banked_resets` | fresh banked Codex reset counts summed (`null` when none is fresh) |
| `warnings` | `{provider, seat, code, message}`: stale usage, sign-in needed, queue unavailable, settings |

`data.entries[]`, one per Codex seat, the Claude account, and each local seat:

| key | meaning |
| --- | --- |
| `provider`, `seat`, `label`, `plan`, `role` | identity; `role` is `primary`/`worker` for Codex |
| `used_last` | true for the primary: routing uses it only when no worker fits |
| `lifecycle`, `mode`, `status` | registry lifecycle, effective mode (Claude/local), raw provider state |
| `busy`, `task` | whether it is working, and on what (`label`, `kind`, `job_id`) |
| `windows[]` | `{name, minutes, remaining_pct, resets_at, fresh}` |
| `left` | `{"5h": pct, "7d": pct}` shortcuts (`null` when not reported) |
| `fits` | which task sizes fit, `["small", "big"]`, independent of `busy` |
| `ready` | idle and a small task fits now |
| `reason` | why it does not fit, when it does not |
| `fresh`, `observed_at` | usage freshness and observation time (epoch seconds) |
| `banked_resets` | banked resets available on a Codex seat |

`data.jobs[]` lists live queued and running jobs.

If usage is stale (`fresh: false`), refresh it first:

```sh
codex-seat seats refresh --all      # every active/draining Codex seat and Claude
codex-seat seats refresh seat3      # one seat
```

`pick-seat --size small|big --json` answers just "which Codex seat next":
`data.seat`, or exit 3 with `error.code = "exhausted"` when no idle Codex seat
fits. Exhausted Codex capacity does not mean the pool is full: check Claude and
local entries in `capacity`.

## 2. Use all the compute

Submit one detached job per independent task, then wait for them together:

```sh
codex-seat delegate --detach "implement the parser change"      # prints a job id
codex-seat delegate --detach --kind docs "document the parser"
codex-seat delegate --detach --parallel-safe --scope-hint lib/a "refactor module a"
codex-seat delegate --detach --parallel-safe --scope-hint lib/b "refactor module b"
codex-seat await JOB_A JOB_B JOB_C JOB_D
```

- Routing chooses the provider and seat (`--provider auto`, the default) from the
  same capacity verdict `capacity` shows, and queues work that cannot start yet
  until `--queue-timeout`.
- `--provider codex|claude|local` pins a provider; `--kind` classifies the work
  (`implementation`, `ui_ux`, `mechanical`, `review`, `audit`, `debug`,
  `security`, `test`, `docs`, `planning`, `research`, `deep_research`).
- `--size small|big`, `--tier routine|complex|frontier` and `--effort` that
  differ from the kind's configured default require `--override-reason "..."`.
- `--parallel-safe` with disjoint `--scope-hint PATH` values lets same-repo jobs
  run concurrently. Scope hints are scheduling metadata, not filesystem grants.
- `--read-only` runs a worker with no repository write access.
- `codex-seat job list --json` shows the live queue;
  `codex-seat job status JOB_ID --json` shows one job.

See [dual-head orchestration](dual-head.md) for the full delegate, await and
ChangeSet flow.

## 3. Light management

Heads, controllers and plain shells may do these. Managed workers
(`CODEX_SEATS_MANAGED_WORKER=1` or `CLAUDE_LEAD_WORKER=1`) are refused. Every
command applies without a prompt; `--dry-run` shows the plan first.

```sh
codex-seat seats pause seat4 [--dry-run] [--json]     # idle: deactivate; busy: drain
codex-seat seats resume seat4 [--dry-run] [--json]    # login status + fresh usage, then activate
codex-seat seats refresh seat4 | --all [--json]
codex-seat seats set seat4 --label "Edgar Pro" [--dry-run]
codex-seat seats set-role seat4 worker [--dry-run]
codex-seat seats activate|drain|deactivate seat4 [--dry-run]
codex-seat provider claude set --mode opportunistic [--dry-run] [--json]
codex-seat provider claude set --tier "Max 20x" --small-headroom 10 --big-headroom 30
codex-seat provider claude set --active-usage-refresh on
codex-seat config policy set --set review.mode advisory --apply
```

Pausing a busy seat drains it: no new work is routed to it, the current job
finishes, and a second `seats pause` deactivates it once idle. `--seat SEAT` is
accepted wherever a command takes a seat, as an alias for the positional form.

## 4. JSON envelope

Every agent-facing `--json` output uses one envelope:

```json
{"schema": 1, "ok": true, "data": {}}
{"schema": 1, "ok": false, "error": {"code": "exhausted", "message": "..."}}
```

Failures are printed on stdout and exit nonzero. It applies to `capacity`,
`pick-seat`, `resets`, `provider status`, `provider claude set`,
`config policy show`, `job list` and `job status`. `seats ... --json` predates
the envelope: it keeps its existing top-level keys (`schema` is 1) and adds `ok`
and, on failure, `error` beside them. `status --json` is a forensic dump and is
unchanged.

## 5. Exit codes

| command | codes |
| --- | --- |
| `pick-seat` | 0 seat chosen, 3 exhausted, 2 pool state unreadable |
| `capacity` | 0, 2 pool state unreadable |
| `seats` | 0 ok, 1 doctor errors / blocked plan / refresh failed, 2 refused |
| `delegate` | 0 completed or detached, 1 receipt not completed, 2 error |
| `await` | 0 all completed, 1 some not completed, 2 timeout or error |
| `job status` | 0, 3 unknown job, 2 error |
| `redeem-reset` | 2 bad seat or worker, 3 refused (needs a human), 4 not reset |

## 6. What stays with a human

`seats login`, `seats retire`/`remove` and `redeem-reset` are unchanged and
stay with a person: login and retire/remove need a real terminal outside a head
context, and `redeem-reset` needs a typed terminal confirmation (or a
controller passing `--from-current-user-request` for the user's explicit
request). Login
runs an interactive provider sign-in, retirement takes a seat out of the pool
for good, and a redeemed reset spends a one-time credit. Ask the user to run
them. Everything else on this page is open to agents so they can keep work
moving across the whole pool.
