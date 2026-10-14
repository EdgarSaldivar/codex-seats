# Immutable release activation

`release stage` creates a verified, content-addressed artifact under the runtime
root. A second source checkout or Development staging directory is not required.

## Installed commands and interpreters

Operational use of the macOS `launchd` runner requires this Git immutable-release
bootstrap. A wheel or editable install (`pip install`, `uv sync`, `.venv-dev`) is
for portable use, development, and tests only: it provides no `current` release
pointer or stable shims, and `codex-seat dispatch install` resolves the active
release under the runtime root.

`release install-shims` writes the stable `codex-seat` and `worker-hook` shims
into the runtime root itself (`~/.codex-seats` unless `CODEX_SEATS_ROOT` says
otherwise). Put that directory on `PATH`, for example in your shell profile:

```sh
export PATH="$HOME/.codex-seats:$PATH"
```

Each shim starts with `#!/usr/bin/env python3`, so it runs with the first
`python3` on `PATH`. That interpreter must be Python 3.11 or newer; an older
one stops with a clear error before any release code is imported. The
shims resolve `current` to `releases/<release-id>` and import only that
release's code.

The `launchd` runner does not use your shell. `codex-seat dispatch install`
pins the runner to `<runtime root>/.venv/bin/python` when that interpreter
exists, and otherwise to the Python running the install command. Keep the pool
`.venv` on Python 3.11+, and never point it at, or reuse it as, a development
environment.

## Routine activation

The runner service is named `io.codex-seats.dispatch`. When moving from an
earlier private build with a different service name, stop heads and let accepted
work finish, then run `codex-seat dispatch uninstall` using the old installed
release before activating this one. Install and check the new service afterward
as shown below. Service-name migration is an explicit operator step.

Run these commands from the clean canonical checkout:

```sh
cd ~/Development/codex-seats

codex-seat release preflight
STAGE_JSON=$(codex-seat release stage "$PWD")
RELEASE_ID=$(printf '%s\n' "$STAGE_JSON" | jq -r .release)
codex-seat release verify "$RELEASE_ID"
codex-seat release activate "$RELEASE_ID"

codex-seat dispatch install
codex-seat dispatch doctor
codex-seat release status
codex-seat --version
```

`release stage` prints JSON. The commands above extract its `release` field with
`jq`; it is also safe to copy that 64-character value into `RELEASE_ID`
manually. Activation changes the `current` pointer used by new CLI invocations
and queued work. Already accepted dispatch frames and running processes retain
their recorded release IDs.

If a candidate intentionally removes Python modules shipped by the active release,
repeat `--allow-module-removal NAME` for the exact missing package basenames. Activation
refuses incomplete, extra, duplicate, malformed, or path-like names.

To roll back the current pointer and reinstall the dispatch integration:

```sh
codex-seat release rollback
codex-seat dispatch install
codex-seat dispatch doctor
codex-seat release status
```

## First bootstrap only

The first immutable-release bootstrap must happen from a trusted terminal after
every preexisting interactive head and managed worker has stopped. The read-only
preflight can detect ledger claims, detached goal/campaign PIDs, queued frames,
and the runner lock. It cannot detect an otherwise idle interactive terminal,
so `--confirm-heads-stopped` is a real operator assertion.

Before stable shims exist, use the checkout interpreter directly:

```sh
cd ~/Development/codex-seats

CODEX_SEATS_ROOT="$HOME/.codex-seats" \
  PYTHONPATH=lib .venv-dev/bin/python -m codex_seats.cli release preflight --bootstrap

STAGE_JSON=$(
  CODEX_SEATS_ROOT="$HOME/.codex-seats" \
    PYTHONPATH=lib .venv-dev/bin/python -m codex_seats.cli release stage "$PWD"
)
RELEASE_ID=$(printf '%s\n' "$STAGE_JSON" | jq -r .release)

CODEX_SEATS_ROOT="$HOME/.codex-seats" \
  PYTHONPATH=lib .venv-dev/bin/python -m codex_seats.cli release verify "$RELEASE_ID"

CODEX_SEATS_ROOT="$HOME/.codex-seats" \
  PYTHONPATH=lib .venv-dev/bin/python -m codex_seats.cli \
  release activate "$RELEASE_ID" --confirm-heads-stopped

CODEX_SEATS_ROOT="$HOME/.codex-seats" \
  PYTHONPATH=lib .venv-dev/bin/python -m codex_seats.cli \
  release install-shims --dry-run --confirm-heads-stopped

CODEX_SEATS_ROOT="$HOME/.codex-seats" \
  PYTHONPATH=lib .venv-dev/bin/python -m codex_seats.cli \
  release install-shims --confirm-heads-stopped
```

If activation succeeds but shim installation fails, keep the queue empty and
use the checkout interpreter above to retry `release install-shims` or run
`release rollback`.
