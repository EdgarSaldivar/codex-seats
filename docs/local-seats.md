# Local seats: management and availability

`codex-seat local` manages the additive `local_hosts` and `local_seats`
registries in schema-3 settings. Account identities and `pool.seats` keep their
own meaning. The local provider runs Qwen Code against an explicitly
configured OpenAI-compatible endpoint and participates in controller routing.
It does not wake machines, change models, manage services, read inherited
credentials, or modify frontier provider/account runtime state.

`providers.local` defaults to:

```json
{"mode":"off","command":"qwen","max_turns":100,"repair_budget":2}
```

`command` is one executable name/path, `max_turns` is 1–100, and
`repair_budget` is 0–10 completed auto-routed local author cycles for one stable
`task_id`; an explicit local request remains an operator override.
The 100-turn default gives broad tasks room to explore and reach Qwen's
automatic context compaction threshold. The bounded process timeout and output
limit still apply. Set `max_turns` lower when a local deployment needs a tighter
budget.
Modes are `off`, `shadow`,
`canary`, and `opportunistic`, ordered from most restrictive to least. The
effective seat mode is the lower of the global and seat modes. Global `off`
performs no probe lookup, subprocess, network, or local runtime cache access.
Seat `off` never claims capacity; a host with no active, enabled seats is not
probed. Shadow allows availability observation but never acquires a runtime
claim. Canary auto-routing is limited to small routine work. Opportunistic
auto-routing accepts non-frontier, no-risk authoring work whose task shape is
`execute` or `investigate` and whose validation is strong. Review, audit,
security, frontier, design, weak-validation, and risk-flagged work stays on
frontier seats. Requests without an explicit classification normalize validation
to `unspecified` and also stay on frontier seats; local-auto eligibility always
requires an explicit strong classification. An explicit `--provider local` may
use the seat's broader declared task-shape/validation capability, but a local
author cannot fill a review, audit, or security role.

An immediately ready local seat is routing phase zero, ahead of every frontier
seat. Local availability never queues. When multiple seats are configured, a
claim conflict or other failure proven to occur before Qwen launches advances
to the next distinct eligible local seat; exhausted, asleep, gaming, loading,
busy, or degraded candidates fall through to the existing frontier route in the
same clean worktree. Once Qwen starts, ordinary failure does not fall through in
the dirty worktree. Gaming preemption or admission lease loss returns the
immutable dispatch frame to the durable queue; the worktree is discarded and
that exact attempt bypasses local execution on its next clean route. Automatic
routing continues on a frontier seat. An explicit-local request instead ends
with a terminal failure at that clean retry boundary rather than re-entering an
unavailable/requeue loop.

## Host contract

Existing foundation host records without connection/probe fields still load,
but return `probe_not_configured`. New usable hosts supply both fields:

```json
{
  "label": "Local host",
  "lifecycle": "active",
  "resource_groups": {"gpu0": {"max_concurrency": 1}},
  "base_url": "http://192.0.2.10:8000/v1",
  "lease_url": "http://192.0.2.10:8002",
  "availability_probe": {
    "argv": [
      "/path/to/local-seat-probe",
      "http://192.0.2.10:8002/status"
    ],
    "timeout_seconds": 3
  }
}
```

Install a host-specific probe separately and put its absolute executable path in
`availability_probe.argv`. It must answer promptly for each route. Qwen Code is
an independently managed controller dependency.

`base_url` is the OpenAI-compatible endpoint Qwen uses. Optional `lease_url`
implements the optional `/lease`, `/lease/<id>/renew`, and
`DELETE /lease/<id>` contract; the adapter holds and renews it for the entire
Qwen process. Userinfo, query, fragment, malformed ports, and embedded
credentials are forbidden. Keep API credentials outside settings; an endpoint
configured without authentication receives only a controller-created dummy key.

A seat on the example host can be registered with a model profile containing the
dot in the served name:

```json
{
  "label": "Local Qwen seat",
  "lifecycle": "active",
  "mode": "opportunistic",
  "host": "local-host",
  "resource_group": "gpu0",
  "model_profile": "qwen3.8-27b",
  "context_window_size": 180224,
  "harness": "qwen",
  "capabilities": {
    "task_shapes": ["execute", "investigate"],
    "validation": ["strong"]
  },
  "max_concurrency": 1,
  "runtime_fingerprint": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}
```

Replace the zero fingerprint with an operator-supplied digest identifying the
model plus runtime configuration, and change it whenever either changes. The
example seat still does nothing while the global mode is `off`.
`context_window_size` is optional; when present it must be an integer from
16,384 to 1,048,576 tokens. Set it to the server's **effective context limit**
for this model, accounting for the server's configured limit rather than only
the model's advertised maximum. Qwen receives it as
`modelProviders.openai[].generationConfig.contextWindowSize`. Existing seats
without the field keep Qwen's default behavior.

The probe must be a controller-authored, read-only executable with direct argv,
not a shell string or shell invocation. There is no shell interpolation. It
runs from `/` with only `PATH=/usr/bin:/bin`, `LANG=C`, and `LC_ALL=C`; use an
absolute path for other executables. Do not place credentials in argv or output.
This is a trusted command interface, not a sandbox for untrusted programs.
Remote probes must likewise only observe the host and must not wake it or
change services/models. The example host's probe maps a timeout to `asleep`, but treats
DNS failure, connection refusal, HTTP failure, and other transport errors as
`degraded`; those failures do not prove the host is sleeping. The host service
reports `gaming`, `busy`, `loading`, `ready`, or `degraded` from systemd health
and active inference leases. Probe timeout is 0.1–30 seconds.

A probe prints exactly one JSON object to stdout, exits zero, and emits no more
than 4096 bytes of combined stdout/stderr:

```json
{"state":"ready","detail":"GPU idle","model_profile":"coder-small"}
```

`state` is required and is one of `asleep`, `gaming`, `loading`, `ready`, `busy`,
or `degraded`. Optional `detail` is at most 512 visible characters and optional
`model_profile` is a local slug. An optional `observed_at` UNIX timestamp must
be finite and no more than 15 seconds old, with no future timestamps. Missing
timestamps are controller-stamped, conservatively using probe start time.
Unknown/duplicate fields, malformed JSON, excess output, stale data, nonzero
exit, launch failure, and timeout all fail closed. Failed probes replace earlier
ready observations with degraded state. Only ready can be eligible. Every
other state means immediate no-capacity; nothing is queued.

Cache files live in owner-only `local-runtime/` under the selected control root,
expire after 15 seconds, and are bound to a hash of host configuration. Status
reads but never refreshes this cache. Unsafe, missing, changed, and stale cache
entries fail closed. Routing and execution reuse a fresh observation and perform
at most one active refresh per host after a cache miss or expiry. A supplied
model profile must match the seat. Task shape and validation must both occur in
the seat's declared capability lists.

## Commands

```sh
codex-seat local list --json
codex-seat local status --json
codex-seat local probe local-host --json --from-current-user-request
codex-seat local probe --all --json --from-current-user-request
codex-seat local add-host local-host --config-json '{"label":"Local host","lifecycle":"inactive","resource_groups":{"gpu0":{"max_concurrency":1}}}' --preview
codex-seat local add-seat coder-small --config-json '<complete seat JSON>' --preview
codex-seat local set-global-mode shadow --preview
codex-seat local set-global-mode shadow --from-current-user-request --expected-digest '<before_digest>'
codex-seat local set-seat-mode coder-small shadow --from-current-user-request
codex-seat local set-seat-lifecycle coder-small inactive --preview
codex-seat local set-host-lifecycle local-host inactive --preview
```

Every mutation supports `--preview`, `--json`, and `--expected-digest`. Preview
returns before/after documents and the exact settings-byte digest without
writing even a settings lock. Apply uses the shared secure settings transaction,
rechecks the digest after locked reload, and preserves unrelated/unknown document
fields. Omitting the expected digest retains normal serialized transaction
semantics. Enabling any non-off mode, including adding an enabled seat, requires
`--from-current-user-request`. Managed workers cannot apply changes or execute
probes. Because a host probe executes configured argv and `add-host` installs
that trusted command surface, those two actions additionally require the
current-user flag from an interactive trusted controller terminal. All
mutations pass canonical settings validation. If an installed settings document
has an invalid local registry, settings load preserves valid frontier topology
and Claude mode while forcing only local routing off.

Lifecycles follow the account registry transition rules: active ↔ draining,
active/draining → inactive, inactive → active/retired; retired is terminal.
Deactivating a seat sets its mode off. Deactivate attached seats before
deactivating their host; invalid combinations are rejected atomically. Complete
seat objects use the foundation fields `label`, `lifecycle`, `mode`, `host`,
`resource_group`, `model_profile`, `harness`, `capabilities`, `max_concurrency`,
and `runtime_fingerprint` (`sha256:` plus 64 lowercase hex digits). Optional
`context_window_size` can be set or removed with `edit-seat` (`null` removes it).

JSON status exposes seat, logical host, resource group, effective mode, observed
state, eligibility, and a machine-readable reason. Status eligibility is based
on cached availability; it is not a capacity reservation. `local_seats.claim`
is a context manager that attempts nonblocking `flock(LOCK_EX | LOCK_NB)` slots
for both the seat maximum and its `(host, resource_group)` maximum. The example host's
`gpu0` maximum is initially 1, so profiles sharing it cannot claim concurrently.
Files are owner checked and opened without following symlinks. Held descriptors
are the authority: persistent lock files or stale PID metadata confer no claim.
Context exit releases deterministically, and process death releases OS locks.
Claim contention returns `capacity_claimed` immediately with no polling loop.
These locks coordinate controllers sharing one control root; coordinating
independent control roots on different machines is outside this package.

## Terminal manager

The same registries can be managed natively from the dashboard. Run
`codex-seat dash --watch` and press `m`, or `codex-seat dash --manage`, then
press Tab to reach **Local Hosts** and **Local Seats**. The screens show every
host and seat with its label, ID, lifecycle, global/seat/effective mode, model
profile, host and resource group, cached availability with its reason and age,
and the live probe state, and they work even with no Codex seat registered.

Available actions are Probe now, global mode, seat mode, host and seat
lifecycle, Add host, Add seat, Edit host, and Edit seat. Forms are field
oriented: IDs, labels, URLs, model profile, harness, concurrency, and the
runtime fingerprint are typed directly, while `resource_groups`,
`availability_probe`, and `capabilities` are validated JSON fields. Core
validation errors are shown verbatim. Each write is previewed through
`LocalSeatManager` with the exact before/after diff and applied only after
explicit confirmation, using the same digest-pinned transaction, the same
trusted-terminal eligibility, and the same refusal for controller/head and
managed-worker contexts as the commands above. Probe now runs on the watch
refresher, never on the key loop, and remains available with
`--no-active-probe`. See the
[terminal seat manager guide](terminal-seat-manager.md#local-hosts-and-seats).

## Qwen execution and telemetry

The adapter uses Qwen Code's native workspace, shell, edit, web-fetch, and
repository-approved MCP tools. Qwen's `--allowed-tools` is not treated as a
capability boundary—it only bypasses approval—so managed execution relies on
an explicit exclusion list plus the OS sandbox. Subagents, memory, hooks, cron,
interactive questions, worktree management, and messaging are excluded. Qwen
receives an isolated home/runtime, no inherited credential environment,
thinking enabled, Qwen's optional loop heuristic skipped, one model slot, the
managed result JSON schema, and a hard turn/output/time bound. The CLI's
independent tool-call cap remains active. Restricted macOS workers use a
controller-owned Seatbelt profile with exact write scopes plus the same Git
metadata, credential-store, home-wide secret-name, seat-home, and control-plane
denials as the frontier workers. Local Qwen is always narrowed to restricted
mode and always requires the managed macOS Seatbelt profile, even when the
controller's frontier-worker setting is `trusted-development`.

Qwen runs with `--output-format stream-json`. The adapter keeps the bounded,
owner-only JSONL transcript and accepts completion only from one final `result`
event with a valid structured result and consistent usage. Exit code 53 without
a final result is reported as a max-session-turns failure. Partial streams
contribute only event and tool-call counts, tool-error counts, and safe tool names
to metadata;
token usage is marked unavailable when no validated successful result supplies it.
The terminal receipt retains its numeric `tokens` field for compatibility and
sets `local_usage_available=false` in that case; zero does not mean Qwen used no tokens.

Every started local attempt appends job telemetry with `task_id`,
`attempt_id`, `review_cycle`, host, resource group, model profile, runtime
fingerprint, local policy version, turns, wall time, outcome, routing reason,
and precise cached/uncached/input/output token counts when Qwen supplies them.
`codex-seat stats` keeps
local tokens separate under the `local` provider while preserving the existing
frontier provider totals. Per-attempt raw metadata is also written beside the
bounded Qwen transcript under the control root's `logs/` directory.

Explicit routing is available through:

```sh
codex-seat delegate --provider local --task-id issue-123 \
  --task-shape execute --validation strong \
  'Implement and test the bounded change.'
```

The global default remains `off`; adding registry entries alone cannot start a
local model or route work.
