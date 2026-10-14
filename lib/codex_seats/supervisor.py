"""The supervisor loop — the "approve-and-go" engine.

Given a :class:`~codex_seats.goalspec.GoalSpec`, drive one goal to completion
without a human in the loop:

    create a worktree
    repeat up to max_iterations:
        run the acceptance commands   -> all exit 0?
            no  -> ask the head to fix; loop
            yes -> run the optional terminal `review` command on the diff
                   -> exit 0?  DONE   (else feed its output back as the failure)
    stop early if the same failure recurs pause_after_failures times (PAUSED)

Two design choices make this trustworthy enough to walk away from:

* The **acceptance commands are the judge.** "Done" means every command exits 0,
  not that a model said so. There is no model verdict in the success path. The
  optional `review` command is still command-as-judge — it runs ONCE per
  acceptance-green cycle (not per iteration), and its exit code, not its prose,
  decides done. A cross-model reviewer plugged in there adds a real verdict while
  keeping the same exit-0 contract.
* The **supervisor, not a model, does the waiting.** The head is one injected
  call per iteration; between calls this is plain Python. No token-burning poll
  loop, and the head is swappable (Claude or Codex) because it is just a
  callable — the real one wraps ``router.dispatch_run``; tests pass a fake.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import bounded_process
from . import worktree as worktree_mod
from . import goalspec as goalspec_mod
from .goalspec import GoalSpec
from .policy_settings import policy_report
from .worker_environment import minimal_worker_env


@dataclass
class HeadResult:
    ok: bool
    summary: str = ""
    tokens: int = 0
    # The provider that ACTUALLY ran this iteration ("claude"/"codex"/""). Under
    # provider="auto" the router picks — and may fall back mid-run — so the head's
    # requested provider isn't the one that wrote the diff. The review gate opposes
    # the implementer's family, so it needs the real one, not the request.
    provider: str = ""
    status: str = ""
    worker: str = ""
    capacity_exhausted: bool = False


# A head takes the worktree path and a prompt, edits files in the worktree, and
# reports how it went. The success path does not depend on `ok` — the acceptance
# commands decide done — but it is surfaced for progress and diagnostics.
Head = Callable[[Path, str], HeadResult]


@dataclass
class AcceptanceResult:
    passed: bool
    evidence: str


@dataclass
class Outcome:
    status: str          # "done" | "paused" | "exhausted"
    iterations: int
    reason: str
    branch: str
    diff: str = ""
    worktree: str | None = None
    tokens: int = 0
    # protected paths the head modified at any point (reverted before judging);
    # a non-empty list means the head tried to weaken the judge — review it.
    tampered_paths: tuple[str, ...] = ()


# Context the controller-owned built-in review gate needs from the supervisor:
# the pool and release it belongs to, and the controller launch markers that
# decide its dispatch origin (so it never claims the controller's own seat).
# Containment markers are carried forward only when already present, so a
# supervisor running inside a managed worker still cannot claim review capacity.
REVIEW_CONTEXT_ENV_KEYS = (
    "CODEX_SEATS_ROOT",
    "CODEX_SEATS_RELEASE_ID",
    "CODEX_SEATS_HEAD",
    "CODEX_CONTROL_SEAT",
    "CODEX_THREAD_ID",
    "CODEX_TURN_ID",
    "CODEX_SESSION_ID",
    "CODEX_HOME",
    "CODEX_SEATS_MANAGED_WORKER",
    "CLAUDE_LEAD_WORKER",
)


def acceptance_env() -> dict[str, str]:
    """Allowlisted environment for GoalSpec acceptance commands.

    Acceptance commands are project code: they get ordinary developer context
    and the managed-worker marker, never API secrets, SSH/GPG agents, or
    controller launch metadata. This is an environment scrub only; the commands
    still run unsandboxed as the same OS account.
    """
    return minimal_worker_env(CODEX_SEATS_MANAGED_WORKER="1")


def review_env(
    tree: worktree_mod.Worktree,
    diff_path: str,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Allowlisted environment for the terminal review command.

    Starts from the minimal worker environment, adds the diff/worktree/goal
    context (rejecting secret-bearing or injection keys), then preserves only
    the named controller context in :data:`REVIEW_CONTEXT_ENV_KEYS`. It is not
    marked managed-worker: the built-in gate claims review capacity as the
    controller, which ``claim_review`` refuses to managed workers.
    """
    env = minimal_worker_env(
        CODEX_SEATS_DIFF=diff_path,
        CODEX_SEATS_WORKTREE=str(tree.path),
        CODEX_SEATS_BASE=tree.base,
        **(extra_env or {}),
    )
    for key in REVIEW_CONTEXT_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def run_acceptance(
    commands: tuple[str, ...] | list[str],
    cwd: str | Path,
    *,
    timeout: int = 1800,
) -> AcceptanceResult:
    """Run each acceptance command in ``cwd``; pass only if all exit 0.

    Short-circuits on the first failure and returns its tail as evidence, so the
    head gets a concrete, current error to work against. Commands receive only
    :func:`acceptance_env`.
    """
    env = acceptance_env()
    for command in commands:
        try:
            proc = bounded_process.run(
                ["/bin/sh", "-c", command],
                cwd=str(cwd),
                timeout=timeout,
                env=env,
            )
        except bounded_process.ProcessTimeoutError:
            return AcceptanceResult(False, f"$ {command}\n(timed out after {timeout}s)")
        except bounded_process.ProcessLaunchError as exc:
            return AcceptanceResult(False, f"$ {command}\n(could not launch: {exc})")
        if proc.returncode != 0:
            output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            tail = "\n".join(output.strip().splitlines()[-25:])
            return AcceptanceResult(False, f"$ {command}\n(exit {proc.returncode})\n{tail}")
    return AcceptanceResult(True, "all acceptance commands passed")


def run_review(
    review_cmd: str,
    tree: worktree_mod.Worktree,
    *,
    timeout: int = 1800,
    extra_env: dict[str, str] | None = None,
) -> AcceptanceResult:
    """Run the terminal ``review`` command once against the worktree diff.

    The command runs in the worktree with the current diff (including new files)
    written to a temp file exposed as ``$CODEX_SEATS_DIFF`` (``$CODEX_SEATS_WORKTREE``
    and ``$CODEX_SEATS_BASE`` too). Exit 0 means approved; any other exit turns the
    command's output into the next failure the head must resolve, so a reviewer's
    findings drive the same bounded fix loop as an acceptance failure. The
    command receives only :func:`review_env`, not the supervisor's environment.
    """
    diff_text = tree.diff()
    handle, diff_path = tempfile.mkstemp(prefix="codex-seat-review-diff-", suffix=".patch")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(diff_text)
        env = review_env(tree, diff_path, extra_env)
        try:
            proc = bounded_process.run(
                ["/bin/sh", "-c", review_cmd],
                cwd=str(tree.path),
                timeout=timeout,
                env=env,
            )
        except bounded_process.ProcessTimeoutError:
            return AcceptanceResult(False, f"$ {review_cmd}\n(review timed out after {timeout}s)")
        except bounded_process.ProcessLaunchError as exc:
            return AcceptanceResult(False, f"$ {review_cmd}\n(review could not launch: {exc})")
        output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        tail = "\n".join(output.strip().splitlines()[-25:])
        if proc.returncode != 0:
            return AcceptanceResult(False, f"$ {review_cmd}\n(review blocked, exit {proc.returncode})\n{tail}")
        return AcceptanceResult(True, f"review approved\n{tail}")
    finally:
        Path(diff_path).unlink(missing_ok=True)


def _build_prompt(spec: GoalSpec, evidence: str, iteration: int) -> str:
    lines = [
        f"Goal: {spec.goal}",
        "",
        f"This is iteration {iteration}. Make the smallest change that gets every "
        "acceptance command below to exit 0. Edit files in this working directory.",
        "",
        "Acceptance commands (each MUST exit 0 for the goal to be done):",
    ]
    lines += [f"  - {command}" for command in spec.acceptance]
    if spec.constraints:
        lines += ["", "Constraints you must respect:"]
        lines += [f"  - {constraint}" for constraint in spec.constraints]
    lines += ["", "Current failure to fix:", evidence]
    return "\n".join(lines)


def supervise(
    spec: GoalSpec,
    head: Head,
    *,
    branch: str | None = None,
    goal_id: str | None = None,
    progress_path: str | Path | None = None,
    create_worktree: Callable[[str], worktree_mod.Worktree] | None = None,
    run_acceptance_fn: Callable[..., AcceptanceResult] = run_acceptance,
    run_review_fn: Callable[..., AcceptanceResult] = run_review,
    clock: Callable[[], float] = time.time,
    reader_heartbeat: Callable[[], dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> Outcome:
    """Drive ``spec`` to done, paused, or exhausted. Returns an :class:`Outcome`.

    The worktree is left in place on return so the caller can review the diff and
    decide whether to merge it; the caller owns cleanup via ``Outcome.branch`` /
    ``Outcome.worktree``.
    """
    branch = branch or f"run-{int(clock())}"
    goal_id = goal_id or branch
    make = create_worktree or (lambda name: worktree_mod.create(spec.repo, name, link=spec.link))
    tree = make(branch)

    total_tokens = 0
    stuck = 0
    last_failure: str | None = None
    tampered: list[str] = []
    # Telemetry totals (persisted via emit -> progress JSON):
    # review_rounds = how many times the review gate actually ran;
    # review_blocked = how many of those runs blocked. Distinct from the
    # consecutive-block breaker below, which resets on productive progress.
    counters = {"review_rounds": 0, "review_blocked": 0, "review_advisory_failed": 0}
    policy = policy_report(settings or {})
    review_mode = policy["review.mode"] if not spec.review.strip() else "blocking"
    def checkpoint(iteration: int) -> dict[str, Any]:
        return {
            "iteration": iteration,
            "branch": branch,
            "worktree": str(tree.path),
            "base": tree.base,
            "diff_sha256": hashlib.sha256(tree.diff().encode()).hexdigest(),
        }

    def emit(status: str, iteration: int, evidence: str) -> None:
        reader_metadata = reader_heartbeat() if reader_heartbeat is not None else None
        if progress_path is not None:
            current_checkpoint = checkpoint(iteration)
            Path(progress_path).write_text(
                json.dumps(
                    {
                        "status": status,
                        # Lets a progress reader tie the heartbeat to this process.
                        "pid": os.getpid(),
                        "iteration": iteration,
                        "goal": spec.goal,
                        "branch": branch,
                        "head": spec.head,
                        "heartbeat_at": clock(),
                        "settings_reader": reader_metadata,
                        "evidence": evidence[-600:],
                        "tokens": total_tokens,
                        "tampered": sorted(set(tampered)),
                        "repo": str(spec.repo),
                        "worktree": str(tree.path),
                        "base": tree.base,
                        "review_rounds": counters["review_rounds"],
                        "review_blocked": counters["review_blocked"],
                        "review_advisory_failed": counters["review_advisory_failed"],
                        "checkpoint": current_checkpoint,
                    },
                    indent=2,
                )
            )

    def outcome(status: str, iterations: int, reason: str) -> Outcome:
        if status == "done" and counters["review_advisory_failed"]:
            reason += (
                f"; advisory review failed {counters['review_advisory_failed']} "
                "time(s) (see progress evidence)"
            )
        return Outcome(
            status, iterations, reason, branch, tree.diff(), str(tree.path),
            total_tokens, tuple(sorted(set(tampered))),
        )

    # Resolved ONCE per run: empty spec.review defaults to the in-repo
    # package review gate, "none" opts out — see goalspec.resolve_review_command.
    review_cmd = goalspec_mod.resolve_review_command(
        spec, mode=policy["review.mode"], strategy=policy["review.strategy"],
    )
    judge_label = (
        "acceptance and review" if review_cmd and review_mode == "blocking"
        else "acceptance"
    )
    review_blocks = 0  # consecutive review-stage blocks (separate breaker)

    def judge(iteration: int, head_provider: str = "") -> tuple[AcceptanceResult, bool]:
        """Acceptance is the judge; when it passes, the optional terminal review
        command is the final gate. Review therefore runs once per acceptance-green
        cycle, never per iteration.

        ``head_provider`` is the family that actually implemented the diff under
        review (empty at iteration 0, before any head has run). It's exported as
        ``CODEX_SEATS_HEAD_PROVIDER`` so a cross-family review gate opposes the real
        implementer instead of a static guess — the point of the auto router is that
        the family isn't known until dispatch. Multi-provider caveat: if auto fell
        back mid-run, this is the LAST provider (who produced the passing state), not
        every family that touched the diff; a goal needing a guaranteed-independent
        reviewer should pin the head provider rather than use auto.

        Returns ``(result, from_review)`` — ``from_review`` is True only when the
        review command produced this result (acceptance had already passed). The
        loop uses it to run a *review-block* breaker that does not depend on the
        review prose being byte-identical run-to-run (LLM review output rarely is,
        so the evidence-equality breaker alone lets review-driven failures burn the
        full ``max_iterations``)."""
        acc = run_acceptance_fn(spec.acceptance, tree.path)
        if not acc.passed or not review_cmd:
            return acc, False
        counters["review_rounds"] += 1
        extra_env = {
            "CODEX_SEATS_GOAL_ID": goal_id,
            "CODEX_SEATS_ITERATION": str(iteration),
            "CODEX_SEATS_REVIEW_ROUND": str(counters["review_rounds"]),
        }
        # Always set (empty when unknown): a stale value inherited from the
        # parent environment would defeat the ${CODEX_SEATS_HEAD_PROVIDER:-codex}
        # fallback and pick the wrong reviewer family.
        extra_env["CODEX_SEATS_HEAD_PROVIDER"] = head_provider or ""
        reviewed = run_review_fn(review_cmd, tree, extra_env=extra_env)
        if review_mode == "advisory" and not reviewed.passed:
            counters["review_advisory_failed"] += 1
            return AcceptanceResult(True, f"advisory review failed: {reviewed.evidence}"), False
        return reviewed, True

    # Publish a heartbeat (with this process's pid) BEFORE the first acceptance
    # run, which can be slow (a full test suite), so progress readers see the
    # run immediately.
    emit("running", 0, "starting")

    acc, from_review = judge(0)
    if acc.passed:
        emit("done", 0, acc.evidence)
        return outcome("done", 0, f"{judge_label} was already passing")
    if from_review:
        # Acceptance was green but the terminal review blocked at iteration 0.
        # Count it in the telemetry total. It does NOT seed the consecutive-block
        # breaker: that counts failed *fix attempts*, and iteration 0 runs before
        # the head has attempted anything.
        counters["review_blocked"] += 1

    iteration = 0
    while iteration < spec.max_iterations:
        next_iteration = iteration + 1
        emit("running", next_iteration, acc.evidence)
        result = head(tree.path, _build_prompt(spec, acc.evidence, next_iteration))
        total_tokens += max(0, int(getattr(result, "tokens", 0) or 0))
        head_provider = str(getattr(result, "provider", "") or "")
        iteration = next_iteration

        # The judge must stay pristine: revert any head edits to protected
        # paths before running acceptance, and tell the head it happened.
        tampered_now = tree.restore_paths(spec.protected) if spec.protected else []
        tampered.extend(tampered_now)

        acc, from_review = judge(iteration, head_provider)
        if acc.passed:
            emit("done", iteration, acc.evidence)
            return outcome("done", iteration, f"{judge_label} passed after {iteration} iteration(s)")
        if tampered_now:
            acc = AcceptanceResult(
                False,
                "NOTE: your changes to protected file(s) "
                f"{', '.join(sorted(tampered_now))} were reverted — acceptance files "
                "must not be modified; fix the implementation instead.\n" + acc.evidence,
            )

        # Review-block breaker: acceptance is green but the review keeps blocking.
        # Its prose varies run-to-run, so the evidence-equality breaker below rarely
        # trips on it — count consecutive review blocks directly and pause after N.
        # A productive acceptance failure in between resets the count (the head is
        # making concrete progress, not churning on review).
        if from_review:
            review_blocks += 1
            counters["review_blocked"] += 1
            if review_blocks >= spec.pause_after_failures:
                emit("paused", iteration, acc.evidence)
                return outcome(
                    "paused", iteration,
                    f"paused: review blocked {review_blocks}x in a row "
                    f"(breaker={spec.pause_after_failures}) with acceptance already green. "
                    "The head is not converging on an approvable diff; a human should look.",
                )
        else:
            review_blocks = 0

        if acc.evidence == last_failure:
            stuck += 1
        else:
            stuck = 1
            last_failure = acc.evidence
        if stuck >= spec.pause_after_failures:
            emit("paused", iteration, acc.evidence)
            return outcome(
                "paused", iteration,
                f"paused: the same failure recurred {stuck}x (breaker={spec.pause_after_failures}). "
                "Head is stuck; a human should look.",
            )

    emit("exhausted", spec.max_iterations, acc.evidence)
    return outcome(
        "exhausted", spec.max_iterations,
        f"hit max_iterations ({spec.max_iterations}) without passing acceptance",
    )
