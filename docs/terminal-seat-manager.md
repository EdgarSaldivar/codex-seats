# Terminal seat manager

The seat manager is an ops view of every seat in the pool: Codex seats, the
Claude account and local seats. It is for **visibility, diagnosis and light
management** — seeing where work can run, why a seat is not taking work, and
making small reversible changes such as pausing a seat or editing its plan. It
is not an approval layer: ordinary changes apply on the keypress.

Open it from the dashboard with `codex-seat dash --watch` and `m`, or directly
with `codex-seat dash --manage`. The external `cxw` alias needs no change when
it already runs `dash --watch`.

## Layout

Every manager screen has the same frame:

```
codex-seats  ● LIVE 13:54   Ready 4/6 · Running 1 · Queued 0 · Next → seat2 · Resets 2      updated 12s ago
▸ Inventory  Codex seats  Local hosts  Local seats  Claude

    SEAT                  5H LEFT     7D LEFT     RESET   STATUS         │ seat1  Primary · Premium 5x
CODEX 5 · primary seat1 is used last                                     │ ◐ Capped by the 7-day window
▌ P seat1 Premium 5x      ███▉░  64%  ░░░░░   0%  6d04h   ◐ capped 7d    │ ── USAGE ──────── updated 3m ago ──
  W seat2 Standard 1x     █████  92%  ████▏  68%  4h10m   ○ idle         │ 5-hour  ███████▋░░░░  64%  resets in 2h15m
  W seat3 Standard 1x     ██▌░░  42%  ████▎  71%  2h15m   ● Refactor dis…│ 7-day   ░░░░░░░░░░░░   0%  resets in 6d04h
  …                                                                      │ ── ACCOUNT ─── … ── ACTIONS ─── …

↑↓ select  Enter open  c r e o p l seat actions  a add  / filter  Tab screens  g settings  ? help  Esc overview
```

- **Summary strip.** The first line uses the dashboard's own header
  (`● LIVE` / `↻ REFRESHING` / `○ CACHED`, local time and "updated 12s ago"),
  followed by Ready (seats that can take work now / enabled seats), Running,
  Queued, Waiting (when work waits for capacity), **Next →** the seat
  `pick-seat` would choose for a small task, and the banked Resets. It is the
  same read-only model `codex-seat capacity` gives agents
  (`pool_view.collect`), so people and agents see the same numbers. On narrow
  terminals the strip takes two lines and drops the least important counts.
- **Screen line.** The five screens, the current one marked `▸`. Tab and
  Shift-Tab move between them.
- **Seat table.** One table for Codex, Claude and local seats under group
  headers (`CODEX 5 · primary seat1 is used last`, `CLAUDE 1 · Opportunistic`,
  `LOCAL 1 · global Opportunistic`). Columns are the role badge (`P` primary,
  `W` worker, `C` Claude, `L` local), the seat with its plan, 5-hour and 7-day
  remaining usage bars, the binding window's reset and a status chip. Bars are
  green at 50% or more, amber from 20% to 49%, red below 20%, and gray when the
  reading is stale or missing. Status chips use one vocabulary: `●` working
  (the chip names the task), `○` idle or ready, `◐` stale, capped or draining,
  `×` error or needs sign-in, `–` paused or off. Local seats show the model and
  host instead of bars. The table ends with `+ Add seat   a`. Retired seats are
  hidden until `h`.
- **Detail pane.** At 110 columns and wider it sits to the right of the table;
  narrower terminals stack it below, starting with a compact block (status,
  usage and the direct keys) and continuing on PgDn. A Codex seat's detail
  shows a header (seat · role · plan), its status in words, **Usage** (each
  window's bar and "resets in …", banked resets), **Account** (email,
  "✓ verified 3m ago", configured plan and the provider-observed plan),
  **Actions** and **Details** (what it fits and, in one plain sentence, why
  not — "7-day window has 0% left; small tasks need 10%", "Usage data is 4.3h
  old", "Signed out" — then capacity, credits, account ID and refresh state;
  `d` adds the raw capacity diagnostic, which `dash --forensic` also shows). Every action is listed with its key; one that
  cannot run is dimmed and says why, for example
  `p Pause  make another seat primary first`. Claude and local seats get the
  same treatment.
- **Ongoing work.** At 110 columns and wider, running work is listed under the
  table when there is room.
- **Footer.** The footer is generated from the keymap below and keeps the most
  important keys when space runs out. After an action, a one-line result
  replaces it until the next key (`✓ Paused seat4  ·  new work skips it  ·  p
  resumes`); errors are a red line with the reason.

`--no-color` (or `NO_COLOR`) keeps exactly the same layout: the selected row
keeps its `▌` marker and the current screen its `▸`, so the coloured frame with
its escapes stripped is identical to the plain frame. Colour is an explicit flag
from the command line, never guessed from the dashboard output. Untrusted text
(labels, emails, provider messages) is sanitized, and wide or combining Unicode
is measured in display cells. Below 60 columns each seat takes two lines (name
and status, then the bars). The supported minimum is 24×6; smaller terminals
show a resize message and only `q`, EOF and signals act.

## Keys

The keymap is declared once per screen in `seat_manager_tui.KEYMAP`; the footer,
the `?` help and this table are generated from it.

<!-- keymap:start -->
| Where | Key | What it does |
| --- | --- | --- |
| Everywhere | `Esc` | Go back one step |
| Everywhere | `Tab` | Next screen |
| Everywhere | `Shift-Tab` | Previous screen |
| Everywhere | `/` | Filter rows by seat or label |
| Everywhere | `g` | Global settings |
| Everywhere | `R` | Refresh every seat |
| Everywhere | `?` | Help for this screen |
| Everywhere | `q` | Quit |
| Overview | `m` | Open the seat manager |
| Overview | `R` | Refresh every seat |
| Overview | `g` | Global settings |
| Overview | `?` | Show help |
| Overview | `q` | Quit |
| Inventory | `↑↓ j k` | Select a row |
| Inventory | `Enter` | Open the row's actions |
| Inventory | `c` | Redeem a banked reset |
| Inventory | `r` | Refresh usage for this seat |
| Inventory | `e` | Edit plan and headroom |
| Inventory | `o` | Make primary (used last) |
| Inventory | `p` | Pause or resume |
| Inventory | `l` | Sign in again |
| Inventory | `a` | Add a seat of any kind |
| Inventory | `h` | Show or hide retired seats |
| Inventory | `d` | Show raw capacity diagnostic |
| Inventory | `PgUp PgDn` | Scroll the detail |
| Codex seats | `↑↓ j k` | Select a row |
| Codex seats | `Enter` | Open the row's actions |
| Codex seats | `c` | Redeem a banked reset |
| Codex seats | `r` | Refresh usage for this seat |
| Codex seats | `e` | Edit plan and headroom |
| Codex seats | `o` | Make primary (used last) |
| Codex seats | `p` | Pause or resume |
| Codex seats | `l` | Sign in again |
| Codex seats | `a` | Add a Codex seat |
| Codex seats | `h` | Show or hide retired seats |
| Codex seats | `d` | Show raw capacity diagnostic |
| Codex seats | `PgUp PgDn` | Scroll the detail |
| Local hosts | `↑↓ j k` | Select a row |
| Local hosts | `Enter` | Open the row's actions |
| Local hosts | `r` | Probe the host now (on the refresher) |
| Local hosts | `e` | Edit the host's settings |
| Local hosts | `p` | Pause or resume the host |
| Local hosts | `L` | Set the global local mode |
| Local hosts | `a` | Add a local host |
| Local hosts | `h` | Show or hide retired hosts |
| Local hosts | `PgUp PgDn` | Scroll the detail |
| Local seats | `↑↓ j k` | Select a row |
| Local seats | `Enter` | Open the row's actions |
| Local seats | `r` | Probe this seat's host now |
| Local seats | `e` | Edit the seat's settings |
| Local seats | `p` | Pause or resume the seat |
| Local seats | `a` | Add a local seat |
| Local seats | `h` | Show or hide retired seats |
| Local seats | `PgUp PgDn` | Scroll the detail |
| Claude | `↑↓ j k` | Select a row |
| Claude | `Enter` | Change the selected setting |
| Claude | `PgUp PgDn` | Scroll the detail |
| Global settings | `↑↓ j k` | Select a row |
| Global settings | `Enter` | Change the selected setting |
<!-- keymap:end -->

- The direct keys act on the selected row. On the Inventory, `r`, `e` and `p`
  on a local seat probe its host, edit it and pause or resume it, exactly as on
  Local seats; `Enter` or `e` on the Claude row opens the Claude screen. A key
  that does not apply to the row says why on the result line.
- `Enter` opens the selected item's actions menu: the direct actions again
  (with their keys, so they also work inside the menu) plus the rarer ones —
  Remove seat, Pause now for a draining seat, Cancel this session's drain wait,
  and for local seats Set seat mode, Set lifecycle and Manage this seat's host.
- **Esc walks a back stack.** Help, menus, forms and choices return to exactly
  where they were opened from; Esc on a screen opened from the Inventory (for
  example a local seat's actions) returns to the Inventory with its selection.
  Esc on a top-level screen returns to the dashboard overview, and `m` from the
  overview always lands on the Inventory. Tab starts a fresh stack.
- `/` filters the rows by seat id, label, plan, model or host as you type;
  Enter keeps the filter, Esc clears it.
- In text fields every printable key, including `q`, `j`, `k` and `?`, is
  typed. Use the arrow keys to move between the advanced capacity fields.
- `?` shows help for the screen you are on — the keys for **this screen** and
  the keys that work **everywhere** — and returns there on Esc or `?`.

## What applies at once, and what asks first

Reversible changes apply on the keypress or form submit and report one result
line: pause and resume, drain, refresh, role, plan and headroom edits, adding a
seat, Claude settings, local host and seat changes, and global settings. Under
the hood each is the manager's digest-pinned preview immediately followed by
its commit, so a settings file that changed underneath still fails closed with
nothing written and an error line.

Only the two **irreversible** actions open a confirmation dialog:

- **Redeem a banked reset** (`c`): the fresh credit and account are read in the
  background first ("Working… Esc cancels"); then the dialog says which seat
  and account, what changes (usage windows reset, banked resets 2 → 1) and
  warns that it spends a provider credit. Canceled previews spend nothing; the
  login, registry and credit are checked again before the single consume
  attempt, and an uncertain outcome is recorded without a retry.
- **Remove a seat** (Codex or local, from the Enter menu): the dialog lists the
  lifecycle change (Paused → Retired) and what is kept (home, sign-in,
  history).

Both dialogs take `Enter` to confirm, `Esc` to cancel and `d` to show the raw
settings diff (the exact before/after, headers muted). If settings changed after
the preview, Enter writes nothing, the dialog says so, and the next Enter shows
a fresh preview.

Resuming a paused Codex seat runs its sign-in and a fresh usage probe off the
key loop before committing; a "Working" dialog shows progress and Esc cancels,
leaving the seat paused. Pausing a busy seat drains it: new work skips it at
once and this session pauses it when its task ends. Quitting with a drain still
waiting asks first and leaves the seat draining; it is never completed
unattended.

## Roles: primary and worker

Codex seats are either the **primary** (`P`) or a **worker** (`W`). Routing
spends every fitting worker first and uses the primary only when no worker
fits, so the group header says "primary seat1 is used last". `o` makes the
selected active worker the primary. The primary cannot be paused, drained or
removed until another seat is made primary; the detail pane and the result line
name that blocker. There is no deputy role.

## Codex seats

- **Add** (`a`, or Add seat → Codex) takes a label, an optional absolute home
  and a plan. The new seat is allocated transactionally (ids increase, retired
  ids are never reused), starts paused, and is selected; sign it in with `l`
  and resume it with `p` (as primary when it is the only active seat).
- **Edit plan & headroom** (`e`) edits the label, reserve percentage, small and
  big task headroom floors, and the configured plan. Each saved field applies
  at once and the form stays open. The plan opens a preset list; `Advanced /
  custom values` keeps the three-part profile slug, relative capacity
  (`0.1`–`1000` or `null`) and source editor.
- **Sign in again** (`l`) hands the terminal to the guarded `codex login`
  contract: the dashboard stops and joins provider work, restores the real
  screen, revalidates the seat's settings digest and home, runs the login, then
  reloads settings and starts a new refresher. A change during quiescence, or a
  refresher that does not stop within the bound, refuses the login.
- **Refresh** (`r`) queues one coalesced usage refresh for the seat through the
  shared refresh lock; last-good figures stay visible. `R` asks for a full
  refresh of every seat, also with `--no-active-probe`.
- **Remove** retires a paused, idle, non-primary seat. Its home, credentials
  and history are kept; `h` shows retired seats.

The detail keeps three facts apart:

- **Configured plan** is the operator-selected baseline in settings (Free, Go,
  Plus 1x, Pro 5x, Pro 20x, Standard 1x, Premium 5x estimate, or unverified).
  It drives baseline-unit comparisons and admission.
- **Observed plan** is the provider's `planType` from `account/read` (and, under
  Details, the optional plan type returned with the usage snapshot). Neither is
  used to infer a Pro 5x/20x tier or a Business subtype.
- **Usage** is the provider-reported window data: each window's remaining
  percentage and reset time. Missing windows, plans and credits are shown as
  unavailable, never as defaults; "unlimited" appears only when the provider
  reports it.

Identity is a small manager-only cached view refreshed with the account usage
read. After a login change the previous email and IDs are suppressed until a
refresh verifies the new account, and a transient read failure marks the last
verified identity stale.

## Claude

The Claude screen lists the account's settings as rows — Mode, Subscription
tier, Small-task floor, Big-task floor and Active `/usage` refresh — with the
account detail beside them (status, usage windows, settings and provider
details). Enter on a row changes it at once. The tier is a display label only
and does not change routing or quota limits; the status-line data has no plan
field, so a typed tier is shown as configured. Claude is one account: Add seat →
Claude opens these settings rather than a second login. No provider client runs
and no credentials are read; the usage cache is not read while Claude is off.

## Global settings

`g` opens the six optional preferences from anywhere in the manager; they apply
at once.

| Label | Setting | Values |
| --- | --- | --- |
| Goal review mode | `review.mode` | Off (default), Advisory, Blocking |
| Review strategy | `review.strategy` | Single reviewer, Cross-provider |
| Delegation | `execution.delegation_policy` | Automatic, Always delegate, Manual |
| Inline minutes | `execution.inline_max_minutes` | 1–240 |
| Head mode | `execution.head_permissions` | Safe, Full access |
| Head fallback | `execution.head_worker_fallback` | Enabled (default), Disabled |

Goal review mode governs automatic goal supervision only; `delegate`, `await`
and ChangeSet apply review only when a review is explicitly requested with
`codex-seat review`, whose default strategy is the review strategy. Unknown
settings and metadata in `settings.json` are retained; a legacy `ui` section is
kept as-is and ignored.

## Local hosts and seats

The Local hosts and Local seats screens manage the `local_hosts` and
`local_seats` registries described in [local seats](local-seats.md). They read
the cached availability and the live probe snapshot only: opening a screen
never probes, writes or touches the local runtime cache, and both work with no
Codex seats registered.

- A host's detail shows its label and the global mode, the endpoint (base and
  lease URLs, probe argv and timeout), capacity (resource groups and attached
  seats), the cached **availability** (status, the routing reason in words with
  its machine name, the observation age) and the **live probe** (probing now,
  queued, the last result with its source and age, periodic on or off, the last
  worker error).
- A local seat's detail shows its effective, global and seat modes and
  lifecycle; placement (host, resource group and its maximum, slots); the model
  (profile, context, harness, task shapes, checks, runtime fingerprint); its
  cached availability with eligibility; and the host's live probe. Global off,
  stale or missing observations and non-active lifecycles are named explicitly
  instead of being shown as failures.
- `r` queues one read-only probe on the refresher's local probe worker and
  returns at once (also with `--no-active-probe`); the detail follows the probe
  as it runs. `p` pauses or resumes; pausing a local seat also sets its mode
  off, and a resumed seat keeps mode off until you set it. `L` sets the global
  local mode from the Local hosts screen. `e` opens the edit form; `a` adds a
  host or a seat.
- The Enter menu adds Set lifecycle (only transitions the registry allows;
  retired is terminal), Set seat mode, Add local seat on this host, Manage this
  seat's host, and Remove seat. Removal needs the seat inactive with mode off
  and no running local job; it is confirmed in a dialog and keeps the entry as
  retired history.
- Add and Edit are field forms. Host fields are the immutable id, label,
  lifecycle, optional `base_url` and `lease_url` (blank removes them),
  `resource_groups` as JSON and an optional `availability_probe` as JSON. Seat
  fields are the immutable id, label, lifecycle, mode, host and resource group
  (chosen from the registered hosts), model profile, context window, harness,
  maximum concurrency, `capabilities` as JSON and the `sha256:` runtime
  fingerprint. Edit forms exclude lifecycle and mode (use their actions) and
  send only the fields that changed. Validation errors are shown on the result
  line with the form kept for correction.

Every local change goes through `LocalSeatManager.preview_*` and commits with
the explicit current-request marker on the same keypress; a settings change in
between fails with nothing written.

## Terminal and authority boundary

The interactive manager needs a real TTY on both stdin and stdout. An explicit
`--manage` fails in captured output and in managed workers, even on a
pseudo-terminal; managed workers are refused every mutation and probe in the
manager. A human in a head pane with a real terminal may open it with
`dash --manage`, and so may an agent driving a head pane with a real terminal;
`dash --watch` inside a head's command pane stays captured and read-only.

Agents usually prefer the command line, which offers the same light management
from any head or plain shell: `codex-seat capacity --json` for the model behind
the summary strip, `codex-seat seats pause|resume|refresh|set|set-role`,
`codex-seat provider claude set` and `codex-seat config policy set --apply` (see
the [agent guide](agents.md)). Seat login, retire/remove and redeeming a banked
reset keep their human-terminal rules.

EOF, Ctrl-C, `q`, SIGTERM and SIGHUP all unwind the same terminal ownership:
termios, signal handlers and the alternate screen are restored in `finally`, and
background cancellation and joins are bounded.
