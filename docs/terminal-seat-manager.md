# Terminal seat manager

The seat manager is part of the existing cached-first dashboard. Run
`codex-seat dash --watch` and press `m`, or enter it directly with
`codex-seat dash --manage`. The external `cxw` alias needs no change when it
already runs `dash --watch`.

`codex-seat ui` starts a local browser manager on a random `127.0.0.1` port;
`--port N` chooses the port, `--no-open` prints the URL, and `--no-active-probe`
disables scheduled provider refreshes. Open the printed URL for your own local
session, keep the launching terminal open, and do not share that link. The
browser shows cached seats, Claude and local status, and guarded previews. Seat
login remains in the terminal manager because it requires an interactive
provider authentication handoff. The browser has no command runner or remote
access endpoint.

## Terminal and authority boundary

Management is enabled only when both stdin and stdout are a real TTY in a trusted
human shell. An explicit `--manage` request fails in captured output and in every
controller/head or managed-worker context, including a pseudo-terminal. Ordinary
one-shot and captured watch output remains compatible and read-only.

The regular overview's
summary is deliberately operational: seats ready, jobs running/queued/waiting
for capacity, and grouped problems. The table pairs remaining usage with its
limiting reported quota window/reset, and all live jobs appear task-first in one
deduplicated **Ongoing work** section. Narrow terminals prioritize seat name,
remaining usage, and current work; `--forensic` retains full diagnostics,
lifecycle totals, and concurrency ceilings. Settings must be the schema-3 seat
registry; an older or invalid document is shown as a settings warning and no
capacity is synthesized from it. Seats are identified by their stable `seatN`
id; the role column shows a P/D/W badge for primary, deputy, and worker.

## Keys and workflow

The management screens use the same restrained cyan, purple, gray, and
health-status palette as the regular dashboard. `--no-color` keeps the same
spacing, hierarchy, selection marker, and content without ANSI escapes. The
selected seat is shown with a high-contrast row highlight in color terminals.
At 84 columns and wider, the main screen places the registered-seat table beside
the selected-seat details. Narrower terminals stack the table above the detail
pane. The detail pane groups information under **Account**, **Plan**, **Usage**,
and **Credits** so current usage is easy to locate; Page Up/Down scrolls every
detail line while the selected seat remains visible. The supported 24×6 minimum
uses the same stacked structure in a deliberately compact form.

- Overview: `m` manage, `?` help, `q` quit.
- Global settings: press `g` from the overview, inventory, or Codex seat list,
  or select the **Global settings** inventory row. This screen is separate from
  per-seat settings and works even with no Codex seats registered.
  Choose a preference, inspect the complete before and after settings, and confirm with
  Enter/`y` or cancel with Escape/`n`. A changed settings revision requires a
  new preview. The manager reloads its settings and refresher after a commit.
- Management opens a unified inventory of seats only: every Codex seat, the
  one Claude account, and every local seat. Local hosts are placement for local
  seats, not seats, so they are not rows on this board (with one registered
  host and one local seat, the inventory shows one local row). Enter opens the
  selected item's controls; `h` toggles retired history. Arrows or `j`/`k`
  select a row, and Escape from the inventory returns to the overview.
- The inventory has exactly one **Add seat** row. Enter on it, or `a`, opens a
  type menu: **Codex** opens the existing Add seat form; **Claude** opens the
  existing single account's setup (mode, tier, headroom) because the Claude
  backend is one account and no second login is created; **Local** opens the
  local seat form on a registered host, or, when no host is registered yet, a
  short menu to add a host first or open Local Hosts. Escape from the host
  menu returns to seat types, and Escape from seat types returns to the
  inventory. Escape from a form cancels it and, when the
  form was opened from the inventory, returns there.
- Controls opened from the inventory return to it: Escape from a Codex seat's
  actions, a local seat's actions, or the Claude account actions lands back on
  the inventory with every provider row and the prior selection, not on a
  single-provider list. The footer shows `Esc inventory` on those menus.
- Tab and Shift-Tab cycle through the inventory, Codex seats, Local Hosts,
  Local Seats, and Claude account. Each list remembers its stable selection,
  and lists entered by Tab keep their regular behaviour: Escape returns to the
  overview. Local Hosts is also reachable from a selected local seat's
  **Manage this seat's host** action, and the seat's detail names its host
  under **Placement**. The provider screens retain their existing controls; see
  [Local hosts and seats](#local-hosts-and-seats) for the local workflow.
- Short list, action, and settings panes track the selected item. Page Up/Down
  pages selected-seat detail and jumps through action/settings menus. Resize-only
  screens ignore hidden action and confirmation keys until the terminal is at
  least 24 columns by 6 rows (`q`, EOF, and signals still exit).
- Forms: select a supported field, edit its value, then press Enter for the exact
  before/after preview. Confirm with Enter or `y`; cancel with Escape or `n`.
  Capacity opens a friendly plan-preset list first. `Advanced / custom values`
  retains the three-part profile-slug, multiplier, and source form. In that
  advanced form, printable `j`, `k`, and `q` are text; use the arrow keys to
  change fields.
- Actions, settings, presets, forms, confirmation, help, and result screens use
  consistent headers, dividers, selection treatment, and key footers. Complete
  mutation previews—including warnings and unified-diff lines—are wrapped and
  paged with arrows or page keys. The footer always reports the displayed range
  and keeps confirm/cancel discoverable.

The ordinary list includes active, draining, and inactive identities; retired
identities are available in the explicit `h` history/all view. It shows lifecycle, role,
cached authentication health, provider-reported usage/freshness, configured
capacity, current work, and the selected seat's provider-reported account email
and plan type. Provider workspace name/ID fields are unsupported and are not
parsed, cached, or displayed. A backend account ID remains labeled `Account ID`;
it is not presented as a workspace ID. Terminal control characters in identity
fields, labels, and messages are neutralized, and wide/combining Unicode is
measured in display cells.

Identity is a small, manager-only cached view populated by the provider-owned
`account/read` response during the existing account usage refresh. It is not
added to the generic status, seat-list/show, or doctor JSON reports. The manager
compares metadata (never contents) for the seat's `auth.json` with the version
seen during refresh. After login changes, it suppresses the prior email and IDs
until a refresh verifies the new account. The observed `planType` uses those same
freshness and authentication-change protections. A transient metadata failure
with an unchanged login labels the last verified identity stale; missing provider
fields are shown as unavailable rather than guessed.

## Configured plans and observed usage

Claude has a separate account entry in management. Its editable settings are
the provider mode, the small and big task minimum headroom percentages, and a
subscription tier label. The dashboard shows the configured tier beside Claude,
or an unknown-tier marker until it is set. The label is display metadata and
does not change routing or quota limits. The current Claude Code status-line
data documents usage percentages
and reset times, but no Max 5x/20x plan field, so the manager marks a manually
entered tier as **configured** and shows **unknown** until one is entered. See
[Claude's status-line field reference](https://code.claude.com/docs/en/statusline#available-data).
The Claude account actions also toggle active `/usage` refresh. It defaults on
when Claude is enabled; an off Claude mode launches no probe. The sidecar uses
the shared refresh lock and leaves passive statusline observations intact when
tmux is unavailable or an active refresh fails.

Selected Codex seats show provider-reported banked reset count, credit details,
grant/expiry dates and freshness separately from quota windows and workspace
billing credits. **Redeem one banked reset** reads a fresh credit and account
identity in the background, shows an exact confirmation preview, and rechecks
the target before one provider consume attempt. Canceled previews spend nothing.
An uncertain consume outcome is recorded without automatic retry. The
`codex-seat redeem-reset seatN --preview` command offers the same read-only
preview; an interactive human confirmation or current-request controller
authority is required to redeem.

The existing Claude provider is one account; its account entry does not create an
independent second Claude login.

Local seat removal is available after the seat is inactive and off and has no
running job or held runtime slot. It retires the entry in place, keeping its
history and host configuration. The local seat list still supports adding seats
to registered hosts.

The manager deliberately shows three different facts:

- **Configured plan/capacity** is the operator-selected routing baseline stored
  in settings. It affects baseline-unit comparisons and scheduler admission.
- **Observed account plan** is the literal `planType` returned by
  `account/read`. **Observed usage plan** is the literal optional `planType`
  returned with the rate-limit snapshot. Neither observation edits settings or
  selects a Pro tier or Business subtype.
- **Actual usage** is the provider-reported primary/secondary window data: each
  available window's duration, remaining percentage, and reset time. Missing
  windows, reset metadata, plan types, and credit metadata are shown as
  unavailable—never as defaults or an invented “unlimited.” When supplied, the
  literal workspace-credit balance/availability is displayed. “Unlimited” is
  shown only when the provider explicitly reports it.

Selectable configured presets are:

- Free and Go: capacity remains unverified (`null` multiplier).
- Plus 1x: 1x baseline.
- Pro 5x: 5x baseline.
- Pro 20x: 20x baseline.
- Standard 1x: 1x baseline.
- Premium 5x: 5x estimate, stored under the compatible
  `business-premium` slug.

Broad provider values such as `pro` or `business`, and the shape of observed
quota windows, are not enough to infer a Pro 5x/20x tier or a Business subtype.
Use the preset that matches verified billing, or keep the profile unverified/use
the advanced custom controls. Custom profile slugs remain supported; capacity
for an unknown profile is treated conservatively as unverified 1x.
The baseline choices follow OpenAI's official
[pricing guidance](https://learn.chatgpt.com/docs/pricing); the provider fields
and rate-limit window semantics follow the official
[app-server documentation](https://learn.chatgpt.com/docs/app-server).

## Actions

Selected refresh requests one Codex identity, including an inactive seat. It is
coalesced through the same `.watch-refresh.lock` as periodic refreshes, reloads
current settings only after acquiring the lock, and retains last-good figures on
failure. The account metadata read shares
the already initialized Codex app-server process and uses `refreshToken: false`;
it does not open credentials. With `--no-active-probe`, the watcher runs in
request-only mode: no periodic provider probe is started, while a manual request
is still serviced.

Pause first previews the exact lifecycle transaction. An idle seat becomes
inactive after confirmation. A busy seat becomes draining immediately, stopping
new admission without killing its current job. Only drains confirmed in this UI
session are watched for idle completion. Canceling the wait or confirming quit
leaves the seat draining; it is not completed unattended. Primary/deputy routing
blockers shown by the guarded transaction must be resolved and re-previewed.

Add seat is available directly from the Codex seat list, including an empty
registry, and from the inventory's Add seat type menu by choosing Codex. Its
labeled form accepts a display label, an optional absolute home,
and a plan preset, then shows the complete exact diff before confirm/cancel. It
uses the transactional dynamic allocator: IDs increase beyond `seat5`, retired
IDs are never reused, and a settings or allocation change after preview fails
closed and requires a new preview. A newly added seat is selected in its inactive
state, with next steps to configure capacity, sign in, and reactivate (as primary
when it is the first active seat).

Remove seat uses retirement semantics and the same exact digest-pinned preview.
It is accepted only for a seat that is already inactive, has no live worker
claim, and is neither primary nor deputy. Errors name the prerequisite to resolve
before re-previewing. Removal does not delete or revoke the seat home or
credentials and does not erase machine history or historical attribution; the
retired identity remains available in the explicit history/all view.

Reactivate first previews the exact stable target and role semantics. After
confirmation, login status and a fresh authoritative usage probe run off the key
loop under the shared refresh lock. Any settings/home/role/lifecycle change from
the original preview makes the checks or commit fail closed, leaving the seat
inactive.

Sign in again is separately confirmed for the stable seat. The dashboard cancels
and joins provider work, restores termios and the real screen, keeps its registered
settings-reader heartbeat alive, then revalidates the exact confirmed settings
digest and resolved home before calling the existing guarded `codex login`
contract with the confirmed settings snapshot. A change during quiescence refuses
login and requires a new confirmation. On return it reloads settings, publishes
fresh reader topology metadata, and creates a new watcher; if the old watcher
cannot quiesce within the shutdown bound, login is refused.

Supported forms are label, role, reserve percentage, small/big task headroom
floors, and configured capacity. Capacity provides the presets above plus an
accessible advanced profile slug, relative capacity (`0.1` through `1000`, or
`null`), and source editor. Existing core validation is authoritative. Broader
pool settings are read-only. Billing or membership, credential revocation, and
destructive home removal are intentionally absent.

Global settings offer typed choices for `review.mode` (`off`, `advisory`,
`blocking`), `review.strategy` (`single`, `cross`),
`execution.delegation_policy` (`auto`, `always`, `manual`),
and `execution.head_permissions` (`safe`, `full-access`). They also offer a
boolean choice for `execution.head_worker_fallback` (true by default), and a
1–240 minute integer input for `execution.inline_max_minutes`. The same settings
screen also offers `ui.theme` (system/light/dark), `ui.density`
(comfortable/compact), and `ui.accent` (teal/indigo). All six orchestration and
three appearance preferences live in the existing `settings.json`; unknown
settings and metadata are retained.
`codex-seat probe` uses the structured account usage read under the shared
watcher refresh lock; it never drives a terminal UI.

`review.mode` (shown as Goal review mode) governs automatic goal supervision
only. `delegate`, `await`, and ChangeSet apply review only when a review is
explicitly requested with `codex-seat review`; `review.strategy` is also that
command's default strategy. Every preference is optional, review mode defaults
to off, and no review is mandatory.

## Local hosts and seats

The Local Hosts and Local Seats screens manage the additive `local_hosts` and
`local_seats` registries described in [local seats](local-seats.md) without
leaving the dashboard. They read through `SeatManager.local_manager()`, whose
report uses cached availability and the live probe snapshot only; opening a
screen never probes, writes, or touches the local runtime cache. Both screens
stay fully manageable when no Codex seat is registered.

The host list shows ID, lifecycle, cached availability state, and label. The
selected-host detail groups the global mode; base and lease URLs and the probe
argv/timeout; resource groups with their maximum concurrency and attached seats;
the cached availability state, detail, reason, served model, and observation
age; and the live probe state (probing now, queued, last result with its source
and finish age, periodic on/off, and the last worker error). The seat list shows
ID, lifecycle, effective mode, and label; its detail groups global, seat, and
effective modes; host, resource group maximum, and concurrency; model profile,
harness, task shapes, validation, and runtime fingerprint; cached eligibility
with its machine-readable reason and observation age; and the host's live probe
state. Global `off`, stale or missing cache entries, and non-active lifecycles
are named explicitly (`mode_off`, `availability_stale`, `lifecycle_ineligible`,
and so on) rather than hidden. Values are sanitized and paged exactly like the
frontier detail; the 24×6 minimum uses the same stacked layout.

Keys on both local screens: `a` opens Add host or Add seat (also from an empty
registry), `g` opens the global local mode, arrows or `j`/`k` select a stable
ID, Enter opens actions, Page Up/Down scrolls detail, Tab switches screens, and
Escape returns to the overview. `p` on the host screen queues Probe now.

Actions: a host offers Probe now, Set host lifecycle, Edit host configuration,
Add local seat on this host, and Set global local mode. A seat offers Set seat
mode, Set seat lifecycle, Edit seat configuration, Probe this seat's host now,
Manage this seat's host (opens that host's actions on Local Hosts), Remove
seat, and Set global local mode. Remove requires inactive lifecycle,
off mode, and no active local work; it retains a retired entry. Mode and
lifecycle choices are short lists marked
with the current value; lifecycle lists contain only transitions the registry
allows (retired is terminal) and the transaction rejects invalid combinations,
such as deactivating a host that still has active seats, with the core error.

Add and Edit are field-oriented forms. Host fields are the immutable id, label,
lifecycle, optional `base_url` and `lease_url` (blank removes an optional field),
`resource_groups` as validated JSON, and optional `availability_probe` as
validated JSON. Seat fields are the immutable id, label, lifecycle, mode, host
and resource group chosen from the registered hosts and their groups, model
profile, harness, maximum concurrency, `capabilities` as validated JSON, and the
`sha256:` runtime fingerprint. Every field can be typed or pasted in the TUI;
edit forms carry the current values, exclude lifecycle and mode (use their
actions), and send only the fields that changed. Malformed JSON, non-integer
numbers, invalid URLs or fingerprints, unknown hosts, concurrency above the
group maximum, and every other core validation error are shown verbatim with
the form kept intact for correction.

Every local mutation goes through `LocalSeatManager.preview_*` and the same
CONFIRM CHANGE screen as frontier actions: it shows the action, target, backend
warnings, an explicit note when the write makes local execution less
restrictive, and the exact unified diff of the canonical settings document.
Escape or `n` discards the preview; Enter or `y` commits with the explicit
current-request marker, which is passed only from that confirmation. Previews
are pinned to the settings digest, so any change between preview and confirm
reports a stale-preview error and writes nothing. Commits are refused, with the
core refusal shown, in controller/head and managed-worker contexts even through
a pseudo-terminal, and without a real stdin/stdout terminal.

Probe now queues one coalesced read-only probe through the watch refresher's
local probe worker and returns immediately; nothing runs on the key loop. With
`--no-active-probe`, periodic local probing is off but Probe now is still
serviced. The detail pane reports queued/probing/finished state as the
refresher's snapshot changes and reads the cached result afterwards.

EOF, Ctrl-C, `q`, SIGTERM, and SIGHUP all unwind the same terminal ownership
context. Termios, signal handlers, and the alternate screen are restored in
`finally`; background cancellation and joins are bounded.

If provider work does not quiesce before the shutdown bound, the dashboard
reports failure and does not replace the refresher or proceed to login.
