# Remote head completion and capacity wake (explicit canary)

Remote Codex controllers can opt into a detached completion wake. No activation,
daemon update, or live queue delivery is part of this source change. Existing
`delegate`, `delegate --detach`, and `await` behavior is unchanged without the flag.
Claude heads keep their existing blocking/native path and unchanged injected policy.

For independent jobs, submit with `delegate --detach`, retain each returned job ID,
then arm **one grouped boundary**:

```sh
codex-seat await <first-job-id> <second-job-id> --wake-head --wait-timeout 7200 --json
```

For one job, `delegate "<bounded task>" --wake-head --wait-timeout 7200` submits once
and arms a wake. This implies detachment; `--detach` may also be supplied. Arming
returns promptly, with a wake ID and `armed` or `duplicate_suppressed` status (JSON),
or the submitted job ID on stdout for non-JSON delegation. Continue useful
independent orchestration, then let the turn end. Do not hold an outer wait, schedule
model wakeups, poll status, or arm overlapping boundaries for the same work.

The watcher may first queue one advisory capacity notification when it observes the
first job in the boundary enter durable `waiting_capacity`. Read that job's exact
state once with the command included in the message. The original job remains queued:
the wake does not cancel, restart, reassign, or duplicate it. The head may keep waiting,
or use the supported cancellation path before reassigning or taking over the work.
Never start duplicate work while the original job is live.

On the later completion/deadline notification, read/review the exact durable receipts
once:

```sh
codex-seat await <first-job-id> <second-job-id> --wait-timeout 0 --json
```

Review the receipts, diffs, test evidence, and warnings before proceeding. A wake
deadline does not cancel a job or authorize resubmission. Zero-timeout review returns
available receipts plus exit 2 for missing receipts. A worker failure remains in its
durable receipt; arbitrary receipt summaries and logs never enter the wake message.

## Ownership and compatibility

The trusted caller must be the existing seatless Remote Codex controller identified
by exact `CODEX_THREAD_ID` plus either the launcher's `CODEX_HOME` or the ChatGPT
Remote runtime's `CODEX_SESSION_ID`, using the shared controller-context helper.
The wake feature additionally requires a canonical lowercase UUID thread ID, an
absolute owned home, and an owned socket at
`CODEX_HOME/app-server-control/app-server-control.sock`. Symlink endpoints and
group/world-writable home/control directories are rejected. Managed workers, Claude
heads, explicit seat/terminal controllers, plain shells, missing identities, and
non-kqueue/inotify platforms are refused before submission or arming. Existing
worker environment filtering continues to strip both Remote identity markers.

When `CODEX_HOME` is absent, the controller resolves the OS account's standard
`.codex` directory without trusting the inherited `HOME` value, then applies the same
ownership, permission, and socket checks. It never searches other homes or sockets.

The Codex executable uses the launcher's existing `workers.resolve_executable`
selection. The controller's exact home is retained, with no replacement by a worker
seat home. The only delivery command is an argv (no shell):

```text
<resolved-codex> queue --thread <exact-thread> --message <bounded-message> --remote unix://
```

No thread search/adoption, start/resume/fork, direct model-turn invocation, parallel
App Server, authentication reconciliation, or credential read is performed. Other
endpoints are unsupported. Environment inputs to both child processes are
allowlisted; inherited credentials and executable-injection variables are excluded.
All streams are detached to `/dev/null`; no pipe keeps the head's tool call open.

OpenAI App Server/WebSocket surfaces may be experimental, including gated methods
and fields. See the [official App Server documentation](https://learn.chatgpt.com/docs/app-server).
This integration uses the **installed `codex queue` client against the already-owned
Remote session**, not its own protocol client or parallel app-server/thread. The
installed client must support that command and endpoint. Unsupported CLI versions
fail closed at delivery; there is no updater or fallback to an embedded server.
A zero CLI exit records queue success, not proof that a model turn actually ran.

## Durable evidence and failure handling

The controller stores owner-only evidence under
`<control-root>/remote-wakes/<wake-id>/` (directories 0700, files 0600). The wake ID
hashes the exact root, thread, home, and sorted unique job set. The original deadline
is retained on duplicates; changing order or timeout does not create another waiter.
There are at most 32 jobs per boundary, a deadline of 0–604800 seconds, and a 4096-byte
message limit. The child calls the existing exact multi-receipt wait with
kqueue/inotify selected explicitly. Only an immediate watcher setup failure while
every job is still `unknown` gets one retry after a 100ms delay, and only within a
two-second initial discovery grace; there is no polling loop. It queues a capacity
advisory on the first durable `waiting_capacity` transition, then continues the same
receipt wait. It separately queues after all receipts, an actionable watcher failure,
or the shared deadline, including receipts published before arming.

`owner.json` contains only routing paths/identity, job IDs, executable, and deadline.
Fixed-size event records contain timestamps, allowlisted statuses/error categories,
and bounded process exit codes. Capacity evidence uses `capacity_waiting`,
`capacity_queue_attempt`, `capacity_queue_success`, `capacity_queue_failed`, or
`capacity_queue_uncertain`; terminal delivery retains `terminal_set`, `deadline`,
`watcher_failed`, `queue_attempt`, `queue_success`, `queue_failed`, and
`queue_uncertain`. Arming/lifecycle evidence also includes `armed`, `launch_failed`,
`wait_started`, `waiter_failed`, and `duplicate_suppressed`. Only applicable events are
written, once each; repeated suppression cannot grow an unbounded log. These timestamps
support wait/delivery latency and cycle/failure analysis without prompts, raw logs,
arbitrary receipt text, or secrets.

The exclusive, fsynced `wait_started`, `capacity_queue_attempt`, and terminal
`queue_attempt` records are written before their side effects. Concurrent duplicate
arms/entries cannot add another wait or delivery attempt. Capacity and terminal
delivery have separate claims, so the advisory does not consume the eventual terminal
wake. A failed CLI launch is definite failure; a timeout, nonzero exit, or exception
after launch is an **uncertain** outcome, even if delivery may have succeeded. Queue
execution has a 30-second limit and bounded isolated-group cleanup. Delivery failures
retain evidence and **never retry automatically**. A capacity delivery failure does
not abort the receipt watcher. If the socket is lost, delivery is unavailable and the
corresponding `*_queue_failed` record captures that fact.

Crash safety is at-most-once, not guaranteed delivery. A crash between reservation
and spawn, during state persistence, while waiting, or after write-ahead queue claim
may lose a wake. An incomplete record or `queue_attempt` without a final outcome is
not permission to replay; the same applies to `capacity_queue_attempt`. The wake is
not automatically restarted after host reboot.
Inspect the exact wake directory and receipts from a trusted controller; do not
delete the deduplication record to force retry. Retain records while duplicate
invocations are possible. No automatic pruning or recovery daemon is included.
If combined delegation fails after submission, stderr retains the job ID and warns
not to resubmit. Source rollback means stop using `--wake-head`; it does not revoke
already-armed wakes or restart/cancel workers.

## Offline acceptance

All live Codex/App Server interactions must be stubbed. From the checkout:

```sh
PYTHONPATH=lib python -m pytest -q \
  tests/test_remote_wake.py tests/test_dispatch_cli.py tests/test_head_launcher.py \
  tests/test_controller_context.py tests/test_worker_environment.py \
  tests/test_completion_wait_acceptance.py tests/test_dispatch_client.py \
  tests/test_dispatch_integration.py
python -m ruff check .
```

The new suite covers rejection before submission, parsing, exact argv/home binding,
detachment and isolated Python bootstrap, the independent at-most-once capacity and
terminal attempts, real durable capacity artifacts, real OS grouped receipt
notifications, initial discovery retry bounds, deadlines and watcher failures, bounded
sanitized messages, write-ahead failures, concurrent duplicates, uncertain delivery,
endpoint loss, owner-only state, and unchanged Claude/non-wake behavior. Its detached
integration test runs a disposable fake Codex executable, never a live queue.

## Later controller acceptance (not executed by this change)

After separate review and explicit authorization to activate and queue into a
disposable Remote conversation, retain its authoritative invocation trace and exact
thread/home identity. Confirm the installed CLI supports `queue --thread --message
--remote unix://` using local help. Use only a registered disposable repository.

1. Submit two bounded read-only fixture jobs detached; arm one grouped wake. Let the
   head turn end. Verify no periodic model/tool invocations while receipts are pending.
2. Through a disposable controller fixture, cause one job to enter durable
   `waiting_capacity`. Verify exactly one advisory reaches the owning head, its message
   contains only the job ID and fixed instructions, and the original job remains live.
   Inspect it once but do not duplicate it.
3. Publish/finish one receipt: no terminal continuation. Finish the second: exactly one
   queue attempt and one queued completion message to the original conversation.
   Verify that Remote access remains usable and no parallel thread/server appeared.
4. On that message, review both exact receipts once with zero timeout. Correlate the
   authoritative turn trace with the durable wake timestamps; UI silence alone does
   not prove the absence of model invocations.
5. Repeat with a failed job, an explicit deadline, and a duplicate arm. Each boundary
   produces at most one capacity attempt and one terminal attempt; failure/deadline
   text contains only IDs, allowlisted statuses, and fixed error categories. No worker
   is resubmitted.
6. Test ambiguous delivery only against a stub/fault fixture: retain uncertainty and
   prove no retry. Keep real queue operations out of offline test runs.
