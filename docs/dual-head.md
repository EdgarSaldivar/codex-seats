# Dual-head orchestration

Both **Claude** and **Codex** can act as an interactive *head* that plans work, spawns
workers from the dynamic local seat registry, and reviews their diffs — from a terminal or, for Codex, from
your phone via ChatGPT Remote Control. Workers use the controller-selected restricted or
trusted-development mode; regular Codex terminal and Remote heads independently share the
official full-access permission default.

## Launch

```sh
codex-seat config --set-primary-full-access # one-time primary/Remote config update
codex-seat seats list                  # current non-retired registry and capacity
codex-seat seats doctor --all          # retired tombstones and owner follow-ups
codex-seat head codex --cd REPO       # Codex :danger-full-access head (default)
codex-seat head codex --seat 2 --cd REPO  # full-access head on seat2
codex-seat head codex --safe --cd REPO  # explicitly contained head
codex-seat head claude --cd REPO      # leaves healthy Codex identities available
```

Canonical form:
`codex-seat head {codex,claude} [--seat N] [--cd repo] [--safe]`.
`--seat` is valid only for a Codex head and defaults to the configured primary.
`N` is any currently registered, active strict positive local seat identity;
authentication, current capacity, and idleness are separate launch-time/operator checks.
It is not a workspace paid-seat number.
Local shell shortcuts such as `cxh`, `cch`/`ccx`, and `cxw` are external aliases, not
installed by this repository. When present, they should invoke the canonical,
release-managed head commands above and `codex-seat dash --watch`, respectively. They are
not proprietary Codex or Claude UI integrations.

Settings have exactly one format, the schema-3 seat registry. An older or invalid
`settings.json` is never migrated: readers fall back to an empty registry with a visible
warning, dispatch admits no new work, and every settings mutation refuses to overwrite it.
Replace it from a trusted terminal with a schema-3 document modeled on
`templates/settings.example.json`.

Inside a head, delegate instead of spawning workers yourself:

```sh
codex-seat delegate "implement X"          # submit one worker task, block for its receipt
codex-seat delegate --read-only "audit X"  # worker cannot write the checkout
codex-seat delegate --mcp visual "inspect X" # named, committed MCP grant
codex-seat delegate --kind ui_ux --effort max --override-reason "high-stakes interaction"
codex-seat delegate --detach "…"           # print a job id, don't wait
codex-seat await <job-id> [<job-id>…]       # one blocking wait for detached jobs
codex-seat job list [--json]                # one sanitized queue/runner snapshot
codex-seat job list --watch                 # event-driven terminal redraw on transitions
```

`--mcp` is an optional catalog grant or override. Ordinary trusted-development
jobs discover existing skills and MCP connections without that flag; see the
[current worker guide](trusted-development-workers.md).

The command path is identical in both interactive head kinds. In the canonical commands
(and optional `cxh` or `cch`/`ccx` shortcuts), a blocking delegate or await reports concise submitted, capacity-wait,
running (provider/seat when known), and terminal transitions in that head's command tool;
the final receipt remains the command's stdout result. Keep `cxw` or
`codex-seat job list --watch` visible in another pane for the shared BACKGROUND WORK view. This is
terminal/command observability supplied by codex-seats; it does not modify the proprietary
Codex or Claude user interface.

### Dashboard signals

`codex-seat dash --watch` distinguishes watcher state from provider state. `CACHED`
is a passive snapshot, `REFRESHING` is an active provider probe, and `LIVE` is the
period between watcher probes; provider telemetry can still be stale in any row and is
reported separately. Row activity is limited to `disabled`, `probing`, the current
work kind, `idle`, `capped`, or `error`. The independent `FIT` value remains visible
while idle or busy: `S+B` means both task sizes fit, `S` or `B` means only that size,
`none` means a healthy enabled provider fits neither, and `—` means disabled or
unhealthy/auth state makes fit inapplicable. Codex FIT comes from its routing capacity
report; Claude FIT uses the same mode, provider health, model availability, and quota
verdict as `status --json`.

In a normal terminal, watch resamples columns and rows each tick, enters the alternate
screen once, and replaces each bounded frame atomically without scrolling. History is
truncated before forensic/secondary detail and excess warnings/jobs, with an explicit
omitted-count or resize row; one-shot `dash` remains vertically unbounded. Non-TTY
captures and Codex/Claude controller command panes receive no ANSI or alternate-screen
sequences: the initial snapshot is printed once, then only meaningful transitions are
appended. Volatile clock, age, elapsed, and next-countdown changes alone do not append a
new snapshot; quota, activity, FIT, job, attention, and refresh-result changes do.

Outside a head/controller, press `m` in this same watch for preview-first seat
management (`?` help, `q` quit), or run `codex-seat dash --manage`. Management is
intentionally refused inside either head and inside managed workers even when a PTY
is present. See the [terminal seat manager](terminal-seat-manager.md) for actions,
drain semantics, login handoff, and settings forms.

Every terminal worker artifact with a nonempty diff or changed-file set is also published
as a durable ChangeSet keyed by that validated job id. The runner atomically stores a
canonical manifest and `git diff --binary` patch under owner-only
`dispatch/changesets/<job-id>/`; the manifest binds the request/hash, release/protocol,
registered repo id, base commit, terminal status, exact changed files, patch length, and
SHA-256. The receipt's compatible `diff_path` points at that durable patch and normal
delegate output includes `changeset=<job-id>`.

Review and disposition are explicit:

```sh
codex-seat changeset list [--json]
codex-seat changeset inspect <job-id> [--patch] [--json]
codex-seat changeset apply <job-id> [--json]
codex-seat changeset drop <job-id> [--json]
```

List and concise inspect are read-only and verify owner, file type, no-symlink, canonical
manifest, and patch hashes. Apply is controller-only: it re-resolves the repo from the
current trusted project registry, strips Git redirect/config environment, takes a bounded
per-repository lock, refuses any staged/unstaged/untracked work or a missing/non-ancestor
base, performs a mutation-free check, then applies to worktree and index. It verifies the
staged filename set exactly and records an owner-only applied event. Preflight/apply
failures leave the checkout clean without destructive reset/clean rollback; a later event
write failure reports that the verified changes are already staged. It supports binary patches but never runs acceptance, commits,
merges, pushes, or deploys. Drop verifies first and atomically moves the whole artifact to
owner-only tombstone storage; repeated drop is stable and applied repository changes are
untouched.

The durable filesystem inbox is the sole queue owner. Provider capacity responses are
retried there with bounded backoff until the existing `expires_at` queue deadline, without
an early terminal receipt or any secondary queue. Timeout names are
deliberately distinct: `--queue-timeout` sets `expires_at` (`--ttl` is compatible), while
`--wait-timeout` limits only the client wait (`--timeout` is compatible). The provider run
timeout comes from task policy and `--estimated-minutes`; the pinned child gets a small
additional cleanup allowance.

### Worker access grants

A restricted worker gets read access to system/tool paths, write access to its entire disposable
checkout, and one private temporary directory. Credential families, Git metadata, the pool
control plane, and arbitrary external writes remain denied. `--read-only` removes checkout
write access too. `--scope-hint PATH` is scheduling metadata used only with
`--parallel-safe`; it does not narrow or widen filesystem access.

Git metadata protection includes the shared common directory and initialized
submodules; `allow_git_write` cannot override it. Provider launch settings and
schemas live under worker-denied `worker-launches`, separate from scratch. Both
providers inherit only a minimal development environment, without credential-agent
sockets, inherited secrets or controller authority. Trusted-development behavior is documented in
[trusted development workers](trusted-development-workers.md); installed release behavior
remains separately pinned.

Named MCP grants remain the narrow extension point for restricted jobs and explicit
overrides. Commit a catalog such as:

```json
{
  "servers": {
    "visual": {"command": "tools/visual-server", "args": ["--stdio"]}
  }
}
```

at `.seats/worker-mcp.json`, then pass `--mcp visual` (repeatable). The queue carries names,
not commands, paths, URLs, or environment. Both the primary checkout and the isolated
worktree copy are validated; missing/unknown/malformed grants and secret-bearing environment
keys fail closed. Restricted Claude receives only the requested servers from the strict
generated MCP config and matching permissions. Trusted Claude instead retains native
user/project MCP discovery with an empty static allow list: under `dontAsk` the PreToolUse
hook (matcher `*`, so it sees MCP tools) returns `allow` for an ordinary tool after its
protected-core checks, avoiding an enumerated `mcp__*`/tool catalog that Claude would skip
anyway, while explicit native deny/disable rules still win. Explicit provider selection and
`auto` retain their normal routing. For restricted jobs, a head cannot widen
filesystem access through `delegate`; register/delegate the relevant
repository, expose a bounded committed MCP adapter, or keep the operation controller-side.

On restricted Claude, `--tools` restricts built-in tools; a granted MCP task also enables
`ToolSearch` so deferred server tools can be discovered. Server authorization is
the separate exact `mcp__NAME` permission, not a built-in tool selector. See the
[CLI reference](https://code.claude.com/docs/en/cli-reference) and
[MCP tool search](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search).
In restricted mode, catalogued MCP adapters are trusted extensions: Claude's Bash
sandbox does not confine an MCP server. Grant only adapters with appropriate task-specific
access; a successful config/unit test does not prove live build or simulator readiness.

The restricted-mode write hook distinguishes copy sources and Swift SDK reads only for a small
literal-command subset. Complex shell syntax retains the existing conservative
checks; this is not a general shell parser or a native-sandbox readiness claim.
Trusted mode removes that broad shell parsing and keeps the narrow protected-core
checks described in the current worker guide.

## Why dispatch remains the worker path

Default terminal and Remote Codex heads are not seatbelted: they select the official
`:danger-full-access` profile. Claude heads use a generated policy deny-list rather than an
OS sandbox. Dispatch still provides durable queue ownership, immutable release pinning,
budget-aware seat routing, isolated disposable worktrees, cancellation, receipts, and
ChangeSets.

The opt-in `codex-seat head codex --safe` mode (or an external alias for it) adds another
constraint: macOS will not nest the worker's
seatbelt inside a seatbelted head (`sandbox_apply: Operation not permitted`, exit 71).
Every mode therefore uses the same interface: the head drops a **typed request** in
`dispatch/inbox`; a `launchd` `QueueDirectories` agent starts a short-lived **runner** in
the user's ordinary launchd context, which runs the worker and writes a **receipt**. There
is no persistent daemon; the runner exists only while the queue is non-empty.

Only `dispatch/inbox` and `dispatch/processing` are activation/work queues. Durable
`changesets`, `applied`, and `dropped` records do not keep launchd active and are not
release-activation blockers. Every frame must carry complete release/protocol metadata for
the current dispatch protocol (8); a frame without it, from an older protocol, or with any
other operation (including the removed `goal`) receives exactly one terminal rejected
receipt and is never executed or requeued.

```
head (full access or --safe) ──inbox──► launchd ──► runner ──► worker (disposable worktree)
            ▲                                                       │
            └──────────── receipt / ChangeSet ◄─────────────────────┘
```

The request envelope is deliberately narrower than a GoalSpec: it carries no raw command,
env, `CODEX_HOME`, external path, or acceptance-command field. The repo comes from the
trusted `settings.projects` registry, never the request. MCP grants carry only names from
that repository's committed catalog.

The runner creates each worktree from a safely validated branch and exact base commit,
publishes an owner-only canonical marker in the exact Git admin directory before checkout,
and disables repository hooks for all internal Git operations. Cleanup verifies that marker
against the canonical source, checkout, branch, admin directory, and Git's live worktree
registration before it forces removal. Missing, foreign, linked, permission-weakened, or
tampered markers fail closed; cleanup never falls back to recursive filesystem deletion.

## Codex head permission modes

`codex-seat head codex` selects Codex's official
`default_permissions = ":danger-full-access"` built-in. The optional external `cxh` alias
may invoke that command. `codex-seat config --set-primary-full-access` atomically sets that same top-level
default in the primary `~/.codex/config.toml`, so a terminal head and ChatGPT Remote Control
use the same profile. The operation changes only that assignment (or prepends it when
missing), preserves the rest of the config, and refuses malformed or ambiguous TOML,
legacy `sandbox_mode`/`sandbox_workspace_write` combinations, and all managed-worker
invocations.

`--safe` (or `execution.head_permissions: safe`) selects the configured contained `head`
profile. The launcher first checks that the selected seat's `config.toml` defines
`[permissions.head]` with a non-empty `[permissions.head.filesystem]` table; a missing,
unparsable, or empty profile stops the launch with an operator error rather than
silently running another permission mode. Managed worker access remains
separate from head mode and is selected only by `execution.worker_access`.

The Claude head uses a generated `permissions.deny` document for credentials,
direct pool-file edits and protected production operations. Cached non-secret
state remains readable for review. Claude Code is not OS-sandboxed. Workers keep
separate restrictions on the `dispatch/` tree and runner LaunchAgent.

## Seat routing

- The **Codex head defaults to the configured primary**. “Primary” means default
  interactive/control identity; it does not mean spend-last. A terminal head may
  select another active authenticated identity with `--seat N`.
- The selected Codex head seat is held out of ordinary delegated-worker selection. If all
  ordinary worker capacity is unavailable, the controller may reuse that identity for a
  fresh isolated managed-worker call when `execution.head_worker_fallback` is enabled.
  The selection lasts only for that head process and does not change the configured primary
  or pool roles.
- Select an idle seat. A head launch does not preempt or terminate work that was already
  running on that identity.
- The **Claude head** consumes no Codex identity, so every active, healthy,
  capacity-eligible Codex identity may be a worker. If those identities are exhausted,
  Claude itself may be used as the isolated last-resort worker when policy and quota allow.
- Protection comes from active-head exclusion and explicit per-seat reserves,
  never implicit primary-role priority. For the documented two-seat target, a
  Premium Codex head leaves Standard as one Codex worker; a Claude head leaves
  Premium and Standard available as two Codex workers. Actual admission remains
  bounded by health, authoritative capacity, claims, passive Claude limits, and
  the global safety ceiling of three. Within the Codex registry, 1x rolling identities
  drain before 5x and future higher capacity classes; after 1x is unavailable, authoring
  prefers the family opposite the current head so final acceptance remains cross-family.

Heads receive the configured delegation policy at launch: `always` delegates every
implementation unit, `auto` delegates work estimated above
`execution.inline_max_minutes`, and `manual` delegates when the user requests it.
Delegated units may include `ui_ux`, docs, planning, bounded `research`, and
`deep_research`; the trusted runner resolves the model and effort. Bounded research uses the complex rung (Sol or Opus);
open-ended, multi-source, or high-uncertainty research uses the frontier rung (Astra or
Fable). UI/UX routes to GPT-6 Astra; routine docs route to GPT-5.6 Terra. Heads may do
useful review and coordination while workers run and must not duplicate live work.

The head can explicitly override a job's capability, size, or reasoning effort when the
classification default is insufficient: pass `--tier routine|complex|frontier`, `--size
small|big`, and/or `--effort low|medium|high|xhigh|max|ultra` together with
`--override-reason`. It may also select a provider and declare genuine `--risk` flags.
Exact model IDs remain runner-owned so overrides survive model-version changes and cannot
be smuggled through the natural-language prompt. The typed protocol records requested,
effective, and clamped effort in the receipt/telemetry path.

Kind describes the dominant reasoning work; risk is an additional safety signal. For
example, documenting an already-decided permission change is `--kind docs --risk
permissions`, while analyzing or designing that permission policy is `--kind security`.
The former keeps documentation effort while promoting model capability; the latter receives
the security class's maximum effort.

## Remote Control (phone)

When the primary home's app-server control socket exists, `codex-seat head codex`
(including `cxh`) explicitly connects with `--remote unix://`. Codex's CLI config
overrides otherwise select an embedded server: Remote can list those chats but
opening an active chat can fail with `thread-store conflict: ... already has an
active writer`. The shared connection retains the head's permission overrides and
passes the caller's working directory. Delegation uses the existing Remote
controller identity, which attributes the head to the configured primary seat.
Non-primary seat launches retain their separate process and explicit seat identity.
Without a control socket, the launcher retains its embedded behavior; enable
Remote before starting a head that needs simultaneous phone access. Existing
embedded sessions must exit before they can be resumed through the shared server.

Before attaching to that shared socket, the launcher runs the read-only
`codex app-server daemon version` check. If the daemon is not running, or the CLI,
managed installation, and running app-server versions do not match, the launcher
refuses to attach and prints the versions it saw. It never updates or restarts the
shared daemon itself, because the upstream update restarts it and disconnects every
attached client, including Remote. To repair it, confirm from a trusted terminal that
no attached Remote or terminal session will be interrupted, run
`codex app-server daemon update` (or restart Remote from the ChatGPT desktop app),
then rerun `codex-seat head codex`.

Remote binds to one ChatGPT account per host. Sign the **ChatGPT desktop app** into the
`~/.codex` account, run `codex-seat config --set-primary-full-access`, open *Set up Remote*,
and scan the QR from the phone signed into the same account/workspace. The phone then drives
the local primary identity and dispatch loop with the same official full-access default as
the regular terminal launcher. `--seat N` applies only to a terminal launch. The
pool routes workers behind that single authenticated head; registry size is not
a promise of additional Remote endpoints.

## Managing the runner

```sh
codex-seat dispatch install     # write + bootstrap the LaunchAgent (persistent, reversible)
codex-seat dispatch status      # installed / loaded?
codex-seat dispatch doctor      # verify the plist still matches the safe activation contract
codex-seat dispatch uninstall   # remove it
```
