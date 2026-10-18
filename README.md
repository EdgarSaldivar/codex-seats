# codex-seats

> A little air traffic control for your coding agents. 🛫

You have a few Codex accounts, a Claude subscription, maybe a model running on a
box under your desk. codex-seats puts all of them on one board, shows you how much
each one has left, and lets your agents hand work to whichever one has room.
Every handoff comes back with a receipt: the diff, the tests that ran, and any
warnings. Nothing lands in your repo until you apply it.

```text
codex-seats  ● LIVE 13:54 · updated 12s ago
Ready 3/6 · Running 1 · Queued 0 · Next → seat2 · Resets 1

    SEAT                5H LEFT        7D LEFT        RESET   STATUS
CODEX 5 · primary seat1 is used last
▌ P seat1 Premium 5x    █████▏░░  64%  ░░░░░░░░   0%  6d04h   ◐ capped 7d
  W seat2 Standard 1x   ███████▍  92%  █████▌░░  68%  2d07h   ○ idle
  W seat3 Standard 1x   ███▍░░░░  42%  █████▋░░  71%  2h15m   ● Refactor dispatch runner
  W seat4 Standard 1x   ▌░░░░░░░   7%  ████▍░░░  55%  2h15m   ○ idle
  W seat5 Standard 1x   —              —              —       – paused

CLAUDE 1 · Opportunistic
  C claude Max 5x       ██████▏░  76%  ███▎░░░░  40%  3d      ○ idle

LOCAL 1 · global Opportunistic
  L qwen3-27b on studio —              —              —       ◐ stale 2.8h
```

*The seat manager in a terminal, with example data. Bars turn amber below 50%
and red below 20%; stale numbers turn gray so old data never looks healthy.*

## What you get

- **One board for everything.** Codex seats, Claude and local models side by
  side, with both usage windows, reset times, and how fresh each number is.
- **Work goes where there's room.** The router fills workers first, keeps your
  primary account for last, and skips anything stale or out of headroom.
- **Receipts, not surprises.** Workers run in their own Git worktrees and return
  a ChangeSet you can read before applying.
- **Built for agents too.** One command tells an agent how much compute is free
  right now; it can then fan work out across every seat and do light upkeep on
  the pool itself.
- **Optional extras.** Single or cross-provider reviews, banked reset tracking,
  local model hosts, and completion wakes for Remote sessions.

## Install

You'll need **Python 3.11+, Git, [uv](https://docs.astral.sh/uv/)**, and the
Codex or Claude CLI for the providers you plan to use. The background runner uses
macOS LaunchAgents; everything else also runs on Linux.

To try it from a checkout:

```sh
UV_PROJECT_ENVIRONMENT=.venv-dev uv sync --extra dev
.venv-dev/bin/codex-seat --version
```

For day-to-day use, follow the [release bootstrap guide](docs/immutable-release-activation.md).
It installs a pinned release, the stable `codex-seat` command, and the dispatch
runner.

## Add your seats

A new install starts with an empty board. Open the manager and press `a`:

```sh
codex-seat dash --manage        # or `m` from codex-seat dash --watch
```

The manager is for seeing the pool and keeping it tidy. Pick a seat with the
arrow keys, and act on it with one key:

| Key | Does |
| --- | --- |
| `r` | Refresh its usage (`R` refreshes every seat) |
| `p` | Pause or resume it |
| `o` | Make it the primary |
| `e` | Edit its plan and headroom |
| `l` | Sign in again |
| `c` | Redeem a banked reset |
| `/` | Filter the list |
| `g` | Global settings |
| `?` | Help for the screen you're on |

Changes apply as soon as you press the key, and a line at the bottom tells you
what happened. Only the two things you can't undo ask first: redeeming a reset
(it spends a provider credit) and removing a seat. The full key list is in the
[manager guide](docs/terminal-seat-manager.md).

**Roles.** Each Codex seat is a *worker* or the *primary*. The primary is the
account your Codex head signs in with, so routing uses it last: only when no
worker can take the task. To pause or remove the primary, make another seat
primary first.

## Hand off work

Register a project and open a head, the agent you talk to:

```sh
codex-seat projects add example /path/to/git-project
codex-seat head codex --cd /path/to/git-project     # or: head claude
```

From the head, hand over a task and review what comes back:

```sh
codex-seat delegate "Fix the parser edge case and run focused tests"
codex-seat changeset inspect <job-id> --patch
codex-seat changeset apply <job-id>
```

For several independent tasks, start each with `delegate --detach`, keep the job
IDs, then wait for all of them at once with `codex-seat await <id> <id> …`.

## For agents

Claude and Codex agents get a small, predictable CLI. Every command below
answers in the same JSON shape: `{"schema": 1, "ok": true, "data": …}`, or
`"ok": false` with an error code and message.

```sh
codex-seat capacity --json           # every seat: what's left, what fits, what's busy
codex-seat pick-seat --size small --json
codex-seat seats pause seat4         # also: resume, refresh, set-role, set
codex-seat provider claude set --mode opportunistic
codex-seat help agents               # the full one-page guide
```

Agents, including heads, can pause, resume, refresh, change roles and tune
headroom or settings. Agents running a delegated job can't change the pool they
run on. Signing in, removing a seat and redeeming a reset stay with you at a
terminal. See [AGENTS.md](AGENTS.md) and the [agent guide](docs/agents.md).

## Settings

Open them with `g` in the manager.

| Area | Choices |
| --- | --- |
| Reviews | Off, advisory or blocking; one reviewer or a Codex + Claude cross-review |
| Delegation | Automatic, always or manual; how long a task can run inline |
| Head access | Safe or full access; whether the head may fall back to worker capacity |
| Claude | Routing mode, plan label, headroom, and whether to refresh usage actively |

Automatic reviews are **off by default** and only apply to goal supervision.
Ask for one any time with `codex-seat review --strategy single` (or `cross`).

Settings live in one file using **schema 3**, with stable seat IDs like `seat1`.
Start from the [example settings](templates/settings.example.json); older
formats are rejected rather than guessed at.

**Banked resets** are provider credits that reset a usage window early. They are
tracked separately from normal resets. Redeeming one shows exactly which credit
and account it will use before anything is spent.

## Good to know

- **Run it on code you trust.** Trusted development workers, goal acceptance
  commands and custom review commands run as your OS user. Their environment
  drops inherited credentials but doesn't sandbox the filesystem.
- **Safe head mode** (`--safe`) needs a `[permissions.head]` filesystem profile
  and refuses to start without one.
- **Upgrading** never removes installed hooks or live jobs. Follow the
  [release guide](docs/immutable-release-activation.md) when you change the
  installed version.
- **Usage numbers** come from structured Codex account refreshes and Claude's
  statusline, plus an optional bounded `/usage` refresh. Stale numbers are never
  used to admit work.

## Contributing

```sh
.venv-dev/bin/python -m pytest tests -q
.venv-dev/bin/python -m ruff check .
.venv-dev/bin/python scripts/check_subprocess_policy.py
.venv-dev/bin/python scripts/check_tracked_files.py
.venv-dev/bin/python scripts/check_likely_secrets.py
```

Keep changes small and fixtures synthetic. Credentials, runtime state, provider
logs and screenshots of real accounts stay out of the repository. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Further reading

- [Terminal seat manager](docs/terminal-seat-manager.md)
- [Agent guide](docs/agents.md)
- [Dual-head orchestration](docs/dual-head.md)
- [Local hosts and seats](docs/local-seats.md)
- [Trusted development workers](docs/trusted-development-workers.md)
- [Releases and bootstrap](docs/immutable-release-activation.md)
- [Remote completion wakes](docs/remote-head-wake.md)
- [Packaging and CI](docs/packaging-ci.md)

Released under the [MIT license](LICENSE). Happy handoffs.
