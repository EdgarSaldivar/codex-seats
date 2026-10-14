# Trusted development workers

`execution.worker_access` supports `restricted` (the default) and
`trusted-development`. The controller freezes the choice per job. A request or
worker cannot enable trusted access. Strict-scope, parallel-safe, and read-only
work stays restricted. Invalid settings stop execution with a warning.

Use a trusted terminal and current user authorization to choose the mode:

```sh
codex-seat config --worker-access trusted-development --from-current-user-request
codex-seat config --worker-access restricted
```

Trusted workers use ordinary native development tools under the current OS
identity. Codex uses its built-in full-access profile; Claude uses its native
discovery and a task-owned adapter for portable skills and MCP connections.
Provider-specific connections may need native configuration, and unavailable
dependencies are reported in the receipt. Existing seat-local capability trees
remain in place. The trusted mode is for trusted assistants and normal mistakes,
not hostile code isolation.

Restricted workers have a writable disposable checkout and a private temporary
directory. Credential families, Git metadata, and pool control files remain
protected. Both modes retain task scope, no-delegation, and production-action
restrictions. Worker tests use checkout-local `uv run --extra dev python -m pytest` and
`uv run --extra dev ruff check`; never borrow the controller's `.venv`.

Goal supervision is different from worker access. `codex-seat supervise` runs
the goal's acceptance commands and any custom goal `review` command itself,
outside every worker sandbox: project code runs unsandboxed as the same OS
account in the goal worktree. Acceptance commands get a minimal allowlisted
environment marked as a managed worker, without API keys, SSH or GPG agents,
or controller launch metadata. Review commands get the minimal environment
plus the diff/worktree/goal variables and only the pool, release, and
controller context the built-in review gate needs; they are not marked as
managed workers because the gate claims review capacity for the controller.
Scrubbing the environment is not filesystem isolation: either command can
still read or change any file the account can reach, including pool state and
provider credential files.

Acceptance evidence belongs in private receipts, not the public repository.
