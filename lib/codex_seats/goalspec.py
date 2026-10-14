"""The goal specification — the machine-checkable contract for one supervised goal.

This is the heart of "approve-and-go": a goal, the acceptance commands that
define *done* (each must exit 0), the pluggable head that reasons, and the loop
bounds that make an unattended run safe to walk away from. The supervisor loads
one of these, then iterates ``propose -> apply-in-worktree -> run acceptance ->
judge`` until every acceptance command passes or a bound trips.

It is deliberately small. Each spec carries one goal plus its acceptance gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .efforts import EFFORTS

HEADS = ("auto", "claude", "codex")

# Keys a goal spec may contain. Anything else is a typo we should surface.
_ALLOWED_KEYS = {
    "goal",
    "repo",
    "acceptance",
    "head",
    "constraints",
    "max_iterations",
    "pause_after_failures",
    "link",
    "protected",
    "preset",
    # A terminal review command run once after acceptance passes (see supervisor).
    "review",
    # Build-head model + reasoning effort (the dev-loop's model router sets these).
    "head_model",
    "effort",
    # Review-gate model override (symmetric with head_model; read by the review
    # command via codex_review's --spec/--review-model — the supervisor ignores it).
    "review_model",
    # MCP servers (by catalog name) to grant this goal's worker (e.g. "visual").
    "mcp",

}

# Per-repo gate preset: generic "don't break anything" gates configured once per
# project and inherited by every goal spec targeting that repo.
PRESET_FILENAME = ".seats-gates.json"


class SpecError(ValueError):
    """A goal spec is malformed. The message says what to fix, not just what broke."""


@dataclass(frozen=True)
class GoalSpec:
    goal: str
    repo: Path
    acceptance: tuple[str, ...]
    head: str = "auto"
    constraints: tuple[str, ...] = ()
    max_iterations: int = 20
    pause_after_failures: int = 3
    # gitignored paths (e.g. ".venv") to symlink from the repo into each run's
    # worktree, so acceptance commands find the tools they expect. A worktree is
    # a fresh checkout and does not carry untracked/ignored dirs.
    link: tuple[str, ...] = ()
    # Paths the head must not change (acceptance tests and gate scripts): made read-only for the worker pre-hoc (via protected_paths) AND
    # restored to base before every judge cycle, so the head cannot pass by
    # weakening the judge. The review command's own repo-relative gate scripts +
    # repo-relative gate scripts are auto-added (see _review_judge_paths).
    protected: tuple[str, ...] = ()
    # a terminal review command run ONCE after every acceptance command passes.
    # Exit 0 = truly done; non-zero feeds its output back as the next failure so
    # the head fixes review findings (bounded by the same iteration/pause limits).
    # EMPTY inherits runtime review.mode (off by default). Explicit commands
    # are blocking gates; literal "none" opts out even when runtime review is on.
    review: str = ""
    # Build-head model id and reasoning effort; empty/None = provider defaults.
    head_model: str = ""
    effort: str = ""
    # Review-gate model override (the palette twin of head_model). Advisory to the
    # supervisor; the review command (codex_review.py) is what consumes it.
    review_model: str = ""
    # MCP server names (from the repo's .seats/worker-mcp.json catalog) to
    # grant this goal's worker. Controller-declared; workers never self-add.
    mcp: tuple[str, ...] = ()
    @classmethod
    def from_dict(cls, raw: Any, *, source: str | None = None) -> GoalSpec:
        where = f" in {source}" if source else ""
        if not isinstance(raw, dict):
            raise SpecError(f"goal spec{where} must be a JSON object, got {type(raw).__name__}")

        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            raise SpecError(
                f"goal spec{where} has unknown key(s): {', '.join(sorted(unknown))}. "
                f"Allowed keys are: {', '.join(sorted(_ALLOWED_KEYS))}"
            )

        goal = raw.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise SpecError(f"goal spec{where} needs a non-empty 'goal' string describing the objective")

        repo_value = raw.get("repo")
        if not isinstance(repo_value, str) or not repo_value.strip():
            raise SpecError(f"goal spec{where} needs a 'repo' path to the working repository")
        repo = Path(repo_value).expanduser()
        if not repo.is_absolute():
            raise SpecError(f"goal spec{where} 'repo' must be an absolute path, got '{repo_value}'")

        acceptance = _parse_acceptance(raw.get("acceptance"), where)

        head = raw.get("head", "auto")
        if head not in HEADS:
            raise SpecError(
                f"goal spec{where} 'head' must be one of {', '.join(HEADS)}, got '{head}'"
            )

        constraints = _parse_str_list(raw.get("constraints", []), "constraints", where)

        link = _parse_relative_paths(raw.get("link", []), "link", where)
        protected = _parse_relative_paths(raw.get("protected", []), "protected", where)

        # Merge the repo's gate preset: preset gates append to the goal-specific
        # acceptance, links/protected union, and the preset file itself becomes
        # protected (it is part of the judge).
        preset = raw.get("preset", True)
        if preset is not False:
            preset_path, preset_data = _load_preset(repo, preset, where)
            if preset_data is not None:
                acceptance = _merged_unique(
                    acceptance, _parse_acceptance(preset_data.get("gates"), where)
                )
                link = _merged_unique(
                    link, _parse_relative_paths(preset_data.get("link", []), "link", where)
                )
                protected = _merged_unique(
                    protected,
                    _parse_relative_paths(preset_data.get("protected", []), "protected", where),
                    (str(preset_path.relative_to(repo)),),
                )
                removed = {"spec_redteam", "spec_redteam_required"} & preset_data.keys()
                if removed:
                    raise SpecError(f"gate preset{where} uses removed key(s): {', '.join(sorted(removed))}; remove them and use acceptance gates")
                unknown = set(preset_data) - {"gates", "link", "protected"}
                if unknown:
                    raise SpecError(f"gate preset{where} has unknown key(s): {', '.join(sorted(unknown))}")

        review = raw.get("review", "")
        if not isinstance(review, str):
            raise SpecError(f"goal spec{where} 'review' must be a shell command string")
        review = review.strip()

        # Judge integrity: the review gate is only trustworthy if the head can't
        # edit it. Auto-protect the gate scripts/config the review command runs
        # from THIS repo (so they're restored before every judge cycle and made
        # read-only pre-hoc via protected_paths). A gate referenced by absolute
        # path lives in another checkout and is already pristine, so it's skipped.
        # Empty review uses the package gate when runtime policy enables it.
        if review.strip().lower() != "none":
            protected = _merged_unique(protected, _review_judge_paths(review, repo))

        head_model = raw.get("head_model", "")
        if not isinstance(head_model, str):
            raise SpecError(f"goal spec{where} 'head_model' must be a model-id string")
        head_model = head_model.strip()

        review_model = raw.get("review_model", "")
        if not isinstance(review_model, str):
            raise SpecError(f"goal spec{where} 'review_model' must be a model-id string")
        review_model = review_model.strip()

        effort = raw.get("effort", "")
        if not isinstance(effort, str):
            raise SpecError(f"goal spec{where} 'effort' must be a string")
        effort = effort.strip()
        if effort and effort not in EFFORTS:
            raise SpecError(
                f"goal spec{where} 'effort' must be one of {', '.join(EFFORTS)}, got '{effort}'"
            )

        mcp = _parse_str_list(raw.get("mcp", []), "mcp", where)
        if mcp:
            _validate_mcp_against_catalog(mcp, repo, where)

        max_iterations = _parse_positive_int(
            raw.get("max_iterations", 20), "max_iterations", where
        )
        pause_after_failures = _parse_positive_int(
            raw.get("pause_after_failures", 3), "pause_after_failures", where
        )
        if pause_after_failures > max_iterations:
            raise SpecError(
                f"goal spec{where} 'pause_after_failures' ({pause_after_failures}) cannot exceed "
                f"'max_iterations' ({max_iterations}) — the breaker would never trip"
            )

        return cls(
            goal=goal.strip(),
            repo=repo,
            acceptance=acceptance,
            head=head,
            constraints=constraints,
            max_iterations=max_iterations,
            pause_after_failures=pause_after_failures,
            link=link,
            protected=protected,
            review=review,
            head_model=head_model,
            effort=effort,
            review_model=review_model,
            mcp=mcp,
        )

    @classmethod
    def load(cls, path: str | Path) -> GoalSpec:
        candidate = Path(path).expanduser()
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise SpecError(f"goal spec is unreadable at {candidate}: {exc}") from exc
        return cls.from_bytes(data, source=str(candidate))

    @classmethod
    def from_bytes(cls, data: bytes, *, source: str | None = None) -> GoalSpec:
        """Parse one already-read snapshot, so a caller can hash and parse the same bytes."""
        where = f" at {source}" if source else ""
        try:
            raw = json.loads(data)
        except ValueError as exc:
            raise SpecError(f"goal spec{where} is not valid JSON: {exc}") from exc
        return cls.from_dict(raw, source=source)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "repo": str(self.repo),
            "acceptance": list(self.acceptance),
            "head": self.head,
            "constraints": list(self.constraints),
            "max_iterations": self.max_iterations,
            "pause_after_failures": self.pause_after_failures,
            "link": list(self.link),
            "protected": list(self.protected),
            "review": self.review,
            "head_model": self.head_model,
            "effort": self.effort,
            "review_model": self.review_model,
            "mcp": list(self.mcp),
            # a serialized spec is already merged; never re-apply the preset
            "preset": False,
        }


def _parse_acceptance(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise SpecError(
            f"goal spec{where} needs 'acceptance': one or more shell commands that must exit 0 "
            f"to count the goal done (a string or a non-empty list of strings)"
        )
    commands: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise SpecError(
                f"goal spec{where} acceptance[{index}] must be a non-empty command string"
            )
        commands.append(item.strip())
    return tuple(commands)


def _load_preset(repo: Path, preset: Any, where: str) -> tuple[Path, dict[str, Any] | None]:
    """Resolve the repo gate preset. ``True`` auto-discovers (absent file is
    fine); an explicit path must exist and parse."""
    if preset is True:
        path = repo / PRESET_FILENAME
        required = False
    elif isinstance(preset, str) and preset.strip():
        candidate = Path(preset)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SpecError(
                f"goal spec{where} 'preset' must be a repo-relative path, true, or false"
            )
        path = repo / candidate
        required = True
    else:
        raise SpecError(f"goal spec{where} 'preset' must be a repo-relative path, true, or false")
    try:
        text = path.read_text()
    except OSError as exc:
        if required:
            raise SpecError(f"goal spec{where} preset is unreadable at {path}: {exc}") from exc
        return path, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"gate preset at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "gates" not in data:
        raise SpecError(f"gate preset at {path} must be an object with a 'gates' list")
    return path, data


def _merged_unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


# Extensions we treat as judge-integrity source (gate scripts + their config).
_JUDGE_EXTS = (".py", ".json", ".toml", ".cfg", ".ini", ".sh")


def _review_gate(module_file: Path = Path(__file__)) -> Path:
    return module_file.resolve().with_name("review.py")


REVIEW_GATE = _review_gate()


def resolve_review_command(
    spec: "GoalSpec", *, mode: str = "off", strategy: str = "single",
) -> str:
    """The review command the supervisor actually runs.

    - explicit command -> as-is (repo presets keep their own commands);
    - the literal "none" -> no gate (explicit, audited opt-out);
    - EMPTY -> runtime mode (off by default); enabled mode uses the package review gate,
      opposing the real implementer via
      $CODEX_SEATS_HEAD_PROVIDER (exported by the supervisor per cycle), with
      the goalspec's review_model pin passed through. Resolution happens at
      RUN time, never by rewriting the frozen spec. A missing review gate errors
      only when an automatic review was actually requested.
    """
    import shlex
    import sys as _sys

    from .policy_settings import normalize_policy_value

    mode = normalize_policy_value("review.mode", mode)
    strategy = normalize_policy_value("review.strategy", strategy)
    explicit = spec.review.strip()
    if explicit.lower() == "none":
        return ""
    if explicit:
        return spec.review
    if mode == "off":
        return ""
    if mode == "blocking" and not REVIEW_GATE.is_file():
        # A required gate must be available. Advisory execution can report a
        # missing script as a failed review without blocking acceptance.
        raise SpecError(
            f"review gate missing at {REVIEW_GATE} — repair the codex-seats checkout "
            f"or set review: \"none\" explicitly")
    cmd = (
        f"{shlex.quote(_sys.executable)} {shlex.quote(str(REVIEW_GATE))}"
        f' --diff-file "$CODEX_SEATS_DIFF"'
        f' --implementer "${{CODEX_SEATS_HEAD_PROVIDER:-codex}}"'
    )
    if spec.review_model:
        cmd += f" --review-model {shlex.quote(spec.review_model)}"
    cmd += f" --strategy {strategy}"
    return cmd


def _review_judge_paths(review: str, repo: Path) -> tuple[str, ...]:
    """Repo-relative gate files referenced by the review command — the scripts
    and config that decide the verdict. Only existing files UNDER ``repo`` and
    not under an ignored/venv dir; an absolute path (a gate in another checkout)
    is already pristine and is skipped. Best-effort token scan, so a project that
    wants more (e.g. a sibling module the gate imports) still declares it in the
    preset's ``protected``."""
    import shlex

    try:
        tokens = shlex.split(review)
    except ValueError:
        tokens = review.split()
    found: list[str] = []
    for token in tokens:
        if token.startswith("-") or not token.lower().endswith(_JUDGE_EXTS):
            continue
        candidate = Path(token)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        resolved = (repo / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(repo.resolve())
        except ValueError:
            continue
        if not resolved.is_file() or ".venv" in relative.parts:
            continue
        found.append(str(relative))
    return tuple(dict.fromkeys(found))


def _parse_relative_paths(value: Any, name: str, where: str) -> tuple[str, ...]:
    entries = _parse_str_list(value, name, where)
    for entry in entries:
        if Path(entry).is_absolute() or ".." in Path(entry).parts:
            raise SpecError(
                f"goal spec{where} {name} entry '{entry}' must be a relative path inside the repo"
            )
    return entries


# The repo-level MCP catalog path. Canonical definition lives in
# worker_config.WORKER_MCP_CATALOG; duplicated here as a stdlib literal so goalspec
# stays import-standalone (importing worker_config pulls in the workers runtime and
# trips a worker_config<->workers import cycle).
_WORKER_MCP_CATALOG = Path(".seats") / "worker-mcp.json"


def _validate_mcp_against_catalog(names: tuple[str, ...], repo: Path, where: str) -> None:
    """Catch a typo'd MCP name (``["visul"]``) at spec-load instead of silently
    failing at runtime. Only fires when the repo actually ships a catalog: a missing or
    malformed catalog is diagnosed by the worker's strict runtime validator, while a
    present catalog listing an unknown name is a useful early spec error."""
    path = repo / _WORKER_MCP_CATALOG
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return  # a malformed catalog is the worker layer's concern, not the spec's
    servers = data.get("servers") if isinstance(data, dict) else None
    catalog = servers if isinstance(servers, dict) else {}
    unknown = [n for n in names if n not in catalog]
    if unknown:
        known = ", ".join(sorted(catalog)) or "(none)"
        raise SpecError(
            f"goal spec{where} 'mcp' names not in the repo catalog "
            f"({path}): {', '.join(sorted(unknown))}. Known servers: {known}"
        )


def _parse_str_list(value: Any, name: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SpecError(f"goal spec{where} '{name}' must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise SpecError(f"goal spec{where} {name}[{index}] must be a non-empty string")
        items.append(item.strip())
    return tuple(items)


def _parse_positive_int(value: Any, name: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"goal spec{where} '{name}' must be an integer, got {type(value).__name__}")
    if value < 1:
        raise SpecError(f"goal spec{where} '{name}' must be at least 1, got {value}")
    return value
