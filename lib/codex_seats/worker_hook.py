from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from .development_policy import (
    TRUSTED_DEVELOPMENT_ACCESS,
    WORKER_ACCESS_ENV_KEY,
)
from .managed import (
    command_secret_paths,
    credential_path_denied,
    write_allowed,
)


WRITE_COMMAND = re.compile(r"(?:^|\s)(?:rm|mv|cp|install|touch|mkdir|tee|truncate|chmod|chown|sed\s+-i|git\s+(?:commit|push|merge|checkout|switch)|apply_patch)\b")
PRODUCTION = re.compile(r"\b(?:git\s+(?:merge|checkout|switch)|git\s+push\s+(?:[^\n]*\s)?(?:main|master)|gh\s+pr\s+merge|deploy|release|kubectl|terraform\s+apply)\b", re.I)
GIT_WRITE = re.compile(r"\bgit\s+(?:commit|push)\b", re.I)
DISPATCH_CONTROL = re.compile(
    r"(?:\bcodex-seat\s+dispatch\b|\blaunchctl\b[^\n]*io\.codex-seats\.)",
    re.I,
)
READ_ONLY_COMMANDS = {"cat", "egrep", "fgrep", "grep", "head", "less", "more", "rg", "tail"}
SHELL_WRAPPERS = {"command", "exec", "nohup"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
SEARCH_COMMANDS = {"egrep", "fgrep", "grep", "rg"}
NON_FILESYSTEM_COMMANDS = {"echo", "printf", "true", "false"}
SHELL_EXECUTORS = {"bash", "dash", "ksh", "sh", "zsh"}
PATH_ENV_KEYS = {
    "CODEX_SEATS_CONTROL_ROOT",
    "CODEX_SEATS_PRIVATE_TEMP",
    "CODEX_SEATS_TASK_ROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
}


def _command_substitutions(command: str) -> list[str]:
    """Return shell fragments that execute inside substitutions, ignoring single quotes."""
    fragments: list[str] = []
    index = 0
    single = double = escaped = False
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and not single:
            escaped = True
            index += 1
            continue
        if character == "'" and not double:
            single = not single
            index += 1
            continue
        if character == '"' and not single:
            double = not double
            index += 1
            continue
        if not single and command.startswith(("$(", "<(", ">("), index):
            start = index + 2
            cursor = start
            depth = 1
            inner_single = inner_double = inner_escaped = False
            while cursor < len(command):
                current = command[cursor]
                if inner_escaped:
                    inner_escaped = False
                elif current == "\\" and not inner_single:
                    inner_escaped = True
                elif current == "'" and not inner_double:
                    inner_single = not inner_single
                elif current == '"' and not inner_single:
                    inner_double = not inner_double
                elif not inner_single and not inner_double:
                    if command.startswith(("$(", "<(", ">("), cursor):
                        depth += 1
                        cursor += 1
                    elif current == "(":
                        depth += 1
                    elif current == ")":
                        depth -= 1
                        if depth == 0:
                            fragments.append(command[start:cursor])
                            index = cursor
                            break
                cursor += 1
        elif not single and character == "`":
            cursor = index + 1
            backtick_escaped = False
            while cursor < len(command):
                current = command[cursor]
                if backtick_escaped:
                    backtick_escaped = False
                elif current == "\\":
                    backtick_escaped = True
                elif current == "`":
                    fragments.append(command[index + 1 : cursor])
                    index = cursor
                    break
                cursor += 1
        index += 1
    return fragments


def _effective_arguments(arguments: list[str]) -> tuple[list[str], list[str]]:
    """Unwrap shell assignments and common transparent command wrappers."""
    values = list(arguments)
    assignments: list[str] = []
    while values and ASSIGNMENT.fullmatch(values[0]):
        assignments.append(values.pop(0))
    while values:
        executable = Path(values[0]).name
        if executable == "env":
            index = 1
            while index < len(values):
                value = values[index]
                if value == "--":
                    index += 1
                    break
                if value in {"-u", "--unset", "-C", "--chdir"}:
                    index += 2
                    continue
                if value == "-S" or value == "--split-string":
                    if index + 1 >= len(values):
                        return [], assignments
                    try:
                        split = shlex.split(values[index + 1])
                    except ValueError:
                        return [], assignments
                    values = [*split, *values[index + 2 :]]
                    break
                if value.startswith(("--unset=", "--chdir=")) or value in {
                    "-i", "--ignore-environment", "-0", "--null"
                }:
                    index += 1
                    continue
                if ASSIGNMENT.fullmatch(value):
                    assignments.append(value)
                    index += 1
                    continue
                if value.startswith("-"):
                    return [], assignments
                break
            else:
                return [], assignments
            if Path(values[0]).name == "env":
                values = values[index:]
            while values and ASSIGNMENT.fullmatch(values[0]):
                assignments.append(values.pop(0))
            continue
        if executable in SHELL_WRAPPERS:
            values.pop(0)
            while values and values[0].startswith("-"):
                values.pop(0)
            continue
        break
    return values, assignments


def _git_alias_override(arguments: list[str], assignments: list[str]) -> bool:
    if any(value.split("=", 1)[0].startswith(("GIT_CONFIG", "GIT_EXEC_PATH")) for value in assignments):
        return True
    for index, value in enumerate(arguments):
        candidate = ""
        if value == "-c" and index + 1 < len(arguments):
            candidate = arguments[index + 1]
        elif value.startswith("-calias."):
            candidate = value[2:]
        elif value.startswith("--config-env="):
            candidate = value.removeprefix("--config-env=")
        if candidate.lower().startswith("alias."):
            return True
    return False


def _git_subcommand(arguments: list[str]) -> str:
    index = 1
    value_options = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
    while index < len(arguments):
        argument = arguments[index]
        if argument in value_options:
            index += 2
            continue
        if argument.startswith("--git-dir=") or argument.startswith("--work-tree="):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument
    return ""


def _policy_match(
    pattern: re.Pattern[str],
    command: str,
    *,
    git_subcommands: frozenset[str] = frozenset(),
    depth: int = 0,
) -> bool:
    if depth < 8 and any(
        _policy_match(
            pattern,
            fragment,
            git_subcommands=git_subcommands,
            depth=depth + 1,
        )
        for fragment in _command_substitutions(command)
    ):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace_split = True
        lexer.whitespace = " \t\r"
        lexer.commenters = ""
        segments: list[list[str]] = [[]]
        for token in lexer:
            if token and all(character in ";&|\n" for character in token):
                segments.append([])
            else:
                segments[-1].append(token)
    except ValueError:
        return bool(pattern.search(command))
    for raw_arguments in segments:
        arguments, assignments = _effective_arguments(raw_arguments)
        if not arguments:
            if pattern.search(shlex.join(raw_arguments)):
                return True
            continue
        executable = Path(arguments[0]).name if arguments else ""
        if executable in READ_ONLY_COMMANDS:
            continue
        if executable == "git":
            if _git_alias_override(arguments, assignments):
                return True
            subcommand = _git_subcommand(arguments)
            if subcommand in git_subcommands:
                return True
            if subcommand in {"diff", "grep", "log", "show", "status"}:
                continue
        if executable == "terraform" and "apply" in arguments[1:]:
            return True
        if executable == "gh" and "pr" in arguments[1:] and "merge" in arguments[1:]:
            return True
        if pattern.search(shlex.join(arguments)):
            return True
    return False


GIT_MUTATION_REASON = "managed workers cannot mutate Git metadata"
PRODUCTION_REASON = "managed workers cannot perform production actions"
DISPATCH_REASON = "managed workers cannot operate dispatch controls"
SHIPPING_REASON = "managed workers cannot perform delegation or shipping actions"


def _substitution_end(command: str, index: int) -> int:
    """Return the index just past the $(...) or backtick group at index."""
    length = len(command)
    if command[index] == "`":
        cursor = index + 1
        escaped = False
        while cursor < length:
            character = command[cursor]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "`":
                return cursor + 1
            cursor += 1
        return length
    cursor = index + 2
    depth = 1
    single = double = escaped = False
    while cursor < length:
        character = command[cursor]
        if escaped:
            escaped = False
        elif character == "\\" and not single:
            escaped = True
        elif character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif not single and not double:
            if command.startswith("$(", cursor):
                depth += 1
                cursor += 1
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return cursor + 1
        cursor += 1
    return length


def _heredoc_delimiter(command: str, cursor: int) -> tuple[str | None, bool, int]:
    """Parse a heredoc delimiter word; quoting anywhere makes the body literal."""
    length = len(command)
    start = cursor
    delimiter: list[str] = []
    quoted = False
    while cursor < length:
        character = command[cursor]
        if character in " \t\n;&|<>()":
            break
        if character in "'\"":
            end = command.find(character, cursor + 1)
            if end == -1:
                return None, False, start
            delimiter.append(command[cursor + 1 : end])
            quoted = True
            cursor = end + 1
            continue
        if character == "\\":
            if cursor + 1 >= length:
                return None, False, start
            delimiter.append(command[cursor + 1])
            quoted = True
            cursor += 2
            continue
        delimiter.append(character)
        cursor += 1
    if not delimiter:
        return None, False, start
    return "".join(delimiter), quoted, cursor


def _consume_heredoc_bodies(
    command: str,
    index: int,
    pending: list[tuple[str, bool, bool]],
    unquoted_bodies: list[str],
) -> int:
    """Skip queued heredoc bodies; keep unquoted bodies for substitution scans."""
    length = len(command)
    for delimiter, quoted, strip_tabs in pending:
        body: list[str] = []
        while index < length:
            end = command.find("\n", index)
            if end == -1:
                end = length
            line = command[index:end]
            index = min(end + 1, length)
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
            body.append(line)
        if not quoted and body:
            unquoted_bodies.append("\n".join(body))
    return index


def _split_heredoc_bodies(command: str) -> tuple[str, list[str]]:
    """Remove heredoc bodies from a command, keeping every actual command.

    Heredoc bodies are data to the receiving command, not commands: the
    sanitized text drops them entirely. Substitutions still execute inside an
    UNQUOTED heredoc body, so those bodies are returned separately for the
    substitution scan; quoted-delimiter bodies are fully literal. Here-strings
    keep their word in place (it is an expanded argument, not a body).
    """
    sanitized: list[str] = []
    unquoted_bodies: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    index = 0
    single = double = escaped = False
    word_start = True
    length = len(command)
    while index < length:
        character = command[index]
        if escaped:
            escaped = False
            sanitized.append(character)
            if character != "\n":
                word_start = False
            index += 1
            continue
        if character == "\\" and not single:
            escaped = True
            sanitized.append(character)
            index += 1
            continue
        if character == "'" and not double:
            single = not single
            word_start = False
            sanitized.append(character)
            index += 1
            continue
        if character == '"' and not single:
            double = not double
            word_start = False
            sanitized.append(character)
            index += 1
            continue
        if single or double:
            sanitized.append(character)
            index += 1
            continue
        if character == "#" and word_start:
            # Only an unquoted hash at the start of a word starts a comment.
            # Leave its newline to process any real pending heredoc normally.
            end = command.find("\n", index)
            index = length if end == -1 else end
            continue
        if command.startswith("$(", index) or character == "`":
            # Copy substitutions verbatim; the substitution scan recurses into
            # each fragment and re-runs this scanner there.
            end = _substitution_end(command, index)
            sanitized.append(command[index:end])
            word_start = False
            index = end
            continue
        if command.startswith("<<<", index):
            sanitized.append("<<<")
            word_start = True
            index += 3
            continue
        if command.startswith("<<", index):
            cursor = index + 2
            strip_tabs = cursor < length and command[cursor] == "-"
            if strip_tabs:
                cursor += 1
            while cursor < length and command[cursor] in " \t":
                cursor += 1
            delimiter, quoted, cursor = _heredoc_delimiter(command, cursor)
            if delimiter is None:
                sanitized.append(command[index:cursor])
                index = cursor if cursor > index else index + 2
                continue
            pending.append((delimiter, quoted, strip_tabs))
            word_start = False
            index = cursor
            continue
        sanitized.append(character)
        word_start = character in " \t\r\n;&|()<>"
        index += 1
        if character == "\n" and pending:
            index = _consume_heredoc_bodies(command, index, pending, unquoted_bodies)
            pending = []
    return "".join(sanitized), unquoted_bodies


def _advisory_blocked_reason(command: str, *, depth: int = 0) -> str | None:
    """Command-position checks for trusted-development workers.

    The trusted worker runs unsandboxed, so this deliberately
    inspects only explicit shipping/delegation/control/Git operations in
    supported syntax: no absolute-path harvesting, no write-intent guessing,
    no matching words inside arguments or heredoc data, and no raw-text regex
    fallback when the shell syntax is unsupported.
    """
    stripped, unquoted_bodies = _split_heredoc_bodies(command)
    if depth < 8:
        fragments = list(_command_substitutions(stripped))
        for body in unquoted_bodies:
            fragments.extend(_command_substitutions(body))
        for fragment in fragments:
            reason = _advisory_blocked_reason(fragment, depth=depth + 1)
            if reason:
                return reason
    try:
        lexer = shlex.shlex(stripped, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace_split = True
        lexer.whitespace = " \t\r"
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(character in ";&|\n" for character in token):
            segments.append([])
        else:
            segments[-1].append(token)
    for raw_arguments in segments:
        arguments, assignments = _effective_arguments(raw_arguments)
        if not arguments:
            continue
        executable = Path(arguments[0]).name
        if executable in READ_ONLY_COMMANDS or executable in NON_FILESYSTEM_COMMANDS:
            continue
        if depth < 8 and executable in SHELL_EXECUTORS and "-c" in arguments[1:]:
            command_index = arguments.index("-c") + 1
            if command_index < len(arguments):
                reason = _advisory_blocked_reason(
                    arguments[command_index], depth=depth + 1
                )
                if reason:
                    return reason
            continue
        if executable == "git":
            if _git_alias_override(arguments, assignments):
                return GIT_MUTATION_REASON
            subcommand = _git_subcommand(arguments)
            if subcommand == "commit":
                return GIT_MUTATION_REASON
            if subcommand in {"push", "merge", "checkout", "switch"}:
                return PRODUCTION_REASON
            continue
        if executable == "gh" and "pr" in arguments[1:] and "merge" in arguments[1:]:
            return PRODUCTION_REASON
        if executable == "kubectl":
            return PRODUCTION_REASON
        if executable == "terraform" and "apply" in arguments[1:]:
            return PRODUCTION_REASON
        if executable in {"deploy", "release"} or executable.startswith(
            ("deploy.", "release.")
        ):
            return PRODUCTION_REASON
        if executable == "codex-seat":
            subcommand = next(
                (value for value in arguments[1:] if not value.startswith("-")), ""
            )
            if subcommand == "dispatch":
                return DISPATCH_REASON
            if subcommand in {"delegate", "await", "ship"}:
                return SHIPPING_REASON
            continue
        if executable == "launchctl" and any(
            "io.codex-seats." in value for value in arguments[1:]
        ):
            return DISPATCH_REASON
    return None


def _command(payload: dict[str, Any]) -> str:
    tool = payload.get("tool_input") or payload.get("input") or {}
    if isinstance(tool, dict):
        return str(tool.get("cmd") or tool.get("command") or tool.get("patch") or "")
    return str(tool)


def _expand_path_token(value: str, env: dict[str, str]) -> str:
    """Expand only trusted path environment variables, never shell syntax."""
    variables = {key: env.get(key, "") for key in PATH_ENV_KEYS}

    def replacement(match: re.Match[str]) -> str:
        return variables.get(match.group(1) or match.group(2), match.group(0))

    expanded = re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        replacement,
        value,
    )
    derived = re.fullmatch(r"\$\(dirname\s+(?:--\s+)?([^)]+)\)(.*)", expanded)
    if derived:
        operand = derived.group(1).strip("'\"")
        if operand and "$" not in operand:
            return str(Path(operand).expanduser().parent) + derived.group(2)
    return expanded


def _path_like(value: str) -> bool:
    return (
        value in {".", ".."}
        or value.startswith(("/", "./", "../", "~/"))
        or "/" in value
    )


def _outside_assigned_private_temp(
    value: str,
    cwd: str,
    env: dict[str, str],
) -> bool:
    private_value = env.get("CODEX_SEATS_PRIVATE_TEMP")
    if not private_value:
        return False
    private_lexical = Path(os.path.abspath(os.path.expanduser(private_value)))
    private_resolved = private_lexical.resolve(strict=False)
    expanded = _expand_path_token(value, env)
    if "$" in expanded or not _path_like(expanded):
        return False
    candidate = Path(expanded).expanduser()
    # Relative shell operands are interpreted from the tool's actual cwd.
    # Trying additional synthetic bases makes harmless repository paths such
    # as ../README look like an escape from the assigned private directory.
    bases = [Path(cwd)]
    lexical_values = (
        [Path(os.path.abspath(candidate))]
        if candidate.is_absolute()
        else [Path(os.path.abspath(base / candidate)) for base in bases]
    )
    values = [
        (path, private_lexical, private_lexical.parent)
        for path in lexical_values
    ]
    values.extend(
        (path.resolve(strict=False), private_resolved, private_resolved.parent)
        for path in lexical_values
    )
    for path, private, parent in values:
        try:
            path.relative_to(parent)
        except ValueError:
            continue
        if path != private and not path.is_relative_to(private):
            return True
    return False


def _search_path_operands(arguments: list[str]) -> list[str]:
    """Return grep/rg filesystem operands while excluding the search expression."""
    operands: list[str] = []
    pattern_supplied = False
    index = 1
    value_options = {
        "-A", "-B", "-C", "-D", "-E", "-F", "-G", "-M", "-T", "-e", "-f",
        "-g", "-j", "-m", "-r", "-t", "--after-context", "--before-context",
        "--context", "--encoding", "--engine", "--file", "--glob", "--iglob",
        "--ignore-file", "--max-columns", "--max-count", "--max-depth", "--regexp",
        "--replace", "--sort", "--sortr", "--threads", "--type", "--type-add",
        "--type-clear", "--type-not",
    }
    path_value_options = {"-f", "--file", "--ignore-file"}
    files_mode = any(value in {"--files", "--type-list"} for value in arguments[1:])
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            index += 1
            break
        option = value.split("=", 1)[0]
        if option in value_options:
            if option in {"-e", "--regexp", "-f", "--file"}:
                pattern_supplied = True
            if option in path_value_options:
                if "=" in value:
                    operands.append(value.split("=", 1)[1])
                elif index + 1 < len(arguments):
                    operands.append(arguments[index + 1])
            index += 1 if "=" in value else 2
            continue
        if value.startswith("-"):
            index += 1
            continue
        if files_mode or pattern_supplied:
            operands.append(value)
        else:
            pattern_supplied = True
        index += 1
    operands.extend(arguments[index:])
    return operands


def _shell_private_temp_access(
    command: str,
    cwd: str,
    env: dict[str, str],
    *,
    depth: int = 0,
) -> bool:
    """Detect shell filesystem operands crossing the assigned temp boundary."""
    if depth < 8 and any(
        _shell_private_temp_access(fragment, cwd, env, depth=depth + 1)
        for fragment in _command_substitutions(command)
    ):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>\n")
        lexer.whitespace_split = True
        lexer.whitespace = " \t\r"
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    redirected: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token and all(character in ";&|\n" for character in token):
            segments.append([])
        elif token and all(character in "<>" for character in token):
            if index + 1 < len(tokens):
                redirected.append(tokens[index + 1])
                index += 1
        else:
            segments[-1].append(token)
        index += 1
    candidates = list(redirected)
    for raw_arguments in segments:
        arguments, assignments = _effective_arguments(raw_arguments)
        candidates.extend(value.split("=", 1)[1] for value in assignments)
        if not arguments:
            continue
        executable = Path(arguments[0]).name
        if depth < 8 and executable in SHELL_EXECUTORS and "-c" in arguments[1:]:
            command_index = arguments.index("-c") + 1
            if command_index < len(arguments) and _shell_private_temp_access(
                arguments[command_index], cwd, env, depth=depth + 1
            ):
                return True
        if executable in NON_FILESYSTEM_COMMANDS:
            continue
        if executable in SEARCH_COMMANDS:
            candidates.extend(_search_path_operands(arguments))
        else:
            candidates.extend(arguments)
    return any(_outside_assigned_private_temp(value, cwd, env) for value in candidates)


def _simple_write_paths(command: str) -> list[str] | None:
    """Narrow literal-command fast path; None retains legacy conservative checks.

    This is deliberately not a shell parser. Any quoting, expansion, redirect,
    grouping, wrapper, unknown command or option rejects the entire fast path.
    Native confinement remains authoritative for arbitrary subprocess writes.
    """
    paths: list[str] = []
    for segment in re.split(r";|&&|\|\|", command):
        if not re.fullmatch(r"[-A-Za-z0-9_./ \t]+", segment):
            return None
        words = segment.split()
        if not words:
            return None
        executable, arguments = words[0], words[1:]
        if executable == "mkdir":
            if arguments[:1] == ["-p"]:
                arguments = arguments[1:]
            if len(arguments) != 1 or arguments[0].startswith("-"):
                return None
            paths.extend(arguments)
        elif executable in {"cp", "mv"}:
            if len(arguments) != 2 or any(value.startswith("-") for value in arguments):
                return None
            paths.extend(arguments if executable == "mv" else arguments[-1:])
        elif executable == "swiftc":
            if arguments[:1] not in (["-typecheck"], ["-print-target-info"]):
                return None
            typecheck = arguments[0] == "-typecheck"
            index = 1
            while index < len(arguments):
                argument = arguments[index]
                if argument in {"-sdk", "-module-cache-path"}:
                    if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                        return None
                    if argument == "-module-cache-path":
                        paths.append(arguments[index + 1])
                    index += 2
                elif typecheck and not argument.startswith("-") and argument.endswith(".swift"):
                    index += 1
                else:
                    return None
        else:
            return None
    return paths


def _blocked(reason: str) -> dict[str, Any]:
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def _allow(reason: str) -> dict[str, Any]:
    """Authorize an ordinary trusted tool that passed every protected-core check.

    Trusted Claude settings intentionally pre-approve nothing, so an ``allow``
    here is what lets native/MCP tool discovery work under ``dontAsk`` without a
    per-server catalog.  Explicit native deny rules still take precedence.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        },
    }


_CAPABILITY_READ_TOOLS = {"read", "glob", "grep"}


def _pool_control_path(path: str, cwd: str, env: dict[str, str]) -> bool:
    from .write_boundary import (
        launch_agent_control_paths,
        pool_runtime_paths,
        protected_path_variants,
    )

    control_root = Path(
        env.get("CODEX_SEATS_CONTROL_ROOT") or (Path.home() / ".codex-seats")
    ).resolve()
    targets = [
        *launch_agent_control_paths(),
        *pool_runtime_paths(control_root),
    ]
    try:
        configured_homes = json.loads(env.get("CODEX_SEATS_PROTECTED_HOMES", "[]"))
    except ValueError:
        configured_homes = []
    if isinstance(configured_homes, list):
        # Keep both the lexical alias and the canonical target of each home;
        # wildcard patterns are never resolved as literal files.
        targets.extend(
            variant
            for item in configured_homes
            if isinstance(item, str) and item
            for variant in protected_path_variants(item)
        )
    value = Path(path)
    value = (
        (Path(cwd) / value).resolve(strict=False)
        if not value.is_absolute()
        else value.resolve(strict=False)
    )
    private_value = env.get("CODEX_SEATS_PRIVATE_TEMP")
    if private_value:
        private = Path(private_value).resolve(strict=False)
        if value == private or value.is_relative_to(private):
            return False
    try:
        relative = value.relative_to(control_root)
    except ValueError:
        relative = None
    if relative is not None and relative.parts:
        # Default homes are open-ended seatN identities.  This remains effective
        # when no settings snapshot is available to the hook.
        if re.fullmatch(r"seat[1-9][0-9]*", relative.parts[0]):
            return True
        if relative.parts[0].startswith("settings.json.backup-"):
            return True
    rendered = str(value)
    return any(
        (
            fnmatch.fnmatchcase(rendered, str(target))
            if "*" in str(target)
            else value == target or value.is_relative_to(target)
        )
        for target in targets
    )


def _git_metadata_path(path: str, cwd: str, env: dict[str, str]) -> bool:
    encoded = env.get("CODEX_SEATS_GIT_METADATA_PATHS")
    if encoded is None:
        task_root = Path(env.get("CODEX_SEATS_TASK_ROOT") or cwd).resolve(strict=False)
        targets = [task_root / ".git", task_root / ".git-meta"]
    else:
        try:
            values = json.loads(encoded)
        except (TypeError, ValueError):
            return True
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            return True
        targets = [Path(value).expanduser().resolve(strict=False) for value in values]
    value = Path(path).expanduser()
    value = (
        (Path(cwd) / value).resolve(strict=False)
        if not value.is_absolute()
        else value.resolve(strict=False)
    )
    return any(value == target or value.is_relative_to(target) for target in targets)


def _trusted_capability_read(path: str, cwd: str, env: dict[str, str]) -> bool:
    """Allow non-secret skill/plugin resources below any configured capability root.

    The controller passes every resolved capability root — the primary home and
    any custom seat homes, not just default ``seatN`` homes — in
    ``CODEX_SEATS_PROTECTED_HOMES``.  A read is permitted only when it lands in a
    ``skills`` or ``plugins`` subtree of one of those roots; credential names are
    still denied by the separate credential check.
    """
    try:
        homes = json.loads(env.get("CODEX_SEATS_PROTECTED_HOMES", "[]"))
    except ValueError:
        return False
    if not isinstance(homes, list):
        return False
    patterns = [home for home in homes if isinstance(home, str) and home]
    value = Path(path).expanduser()
    value = (
        (Path(cwd) / value).absolute()
        if not value.is_absolute()
        else value.absolute()
    )
    for candidate in dict.fromkeys((value, value.resolve(strict=False))):
        for parent in candidate.parents:
            rendered = str(parent)
            matched = any(
                fnmatch.fnmatchcase(rendered, home) if "*" in home else rendered == home
                for home in patterns
            )
            if not matched:
                continue
            relative = candidate.relative_to(parent)
            if (
                len(relative.parts) >= 2
                and relative.parts[0] in {"skills", "plugins"}
            ):
                return True
    return False


def _trusted_scratch_write(path: str, cwd: str, env: dict[str, str]) -> bool:
    value = Path(path).expanduser()
    value = (
        (Path(cwd) / value).resolve(strict=False)
        if not value.is_absolute()
        else value.resolve(strict=False)
    )
    roots = [
        Path(env[key]).expanduser().resolve(strict=False)
        for key in ("TMPDIR", "TMP", "TEMP")
        if env.get(key)
    ]
    home = Path(env.get("HOME") or Path.home()).expanduser().resolve(strict=False)
    roots.extend((home / ".cache", home / "Library" / "Caches"))
    return any(value == root or value.is_relative_to(root) for root in roots)


def worker_decision(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    if env.get("CODEX_SEATS_MANAGED_WORKER") != "1":
        return None
    command = _command(payload).strip()
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    is_patch = (
        tool_name.lower() == "apply_patch" or command.startswith("*** Begin Patch")
    )
    tool = payload.get("tool_input") or payload.get("input") or {}
    tool = tool if isinstance(tool, dict) else {}
    cwd = str(payload.get("cwd") or env.get("CODEX_SEATS_TASK_ROOT") or os.getcwd())
    trusted = env.get(WORKER_ACCESS_ENV_KEY) == TRUSTED_DEVELOPMENT_ACCESS
    advisory_mode = trusted
    disabled_tools = (
        {"agent", "teamcreate", "teamdelete"}
        if trusted
        else {"agent", "computer", "mcp", "teamcreate", "teamdelete", "webfetch", "websearch"}
    )
    if tool_name.lower() in disabled_tools:
        return _blocked(f"managed worker tool is disabled: {tool_name}")
    direct_path = tool.get("file_path") or tool.get("path")
    if (
        isinstance(direct_path, str)
        and tool_name.lower() in {"edit", "write"}
        and _git_metadata_path(direct_path, cwd, env)
    ):
        return _blocked("Git metadata is read-only to managed workers")
    if (
        isinstance(direct_path, str)
        and _pool_control_path(direct_path, cwd, env)
        and not (
            trusted
            and tool_name.lower() in _CAPABILITY_READ_TOOLS
            and _trusted_capability_read(direct_path, cwd, env)
        )
    ):
        return _blocked("pool controls are unavailable to managed workers")
    if isinstance(direct_path, str) and tool_name.lower() == "read":
        if trusted:
            if credential_path_denied(direct_path, cwd):
                return _blocked("managed worker credential read denied")
        elif command_secret_paths(f"read {direct_path}", cwd):
            return _blocked("managed worker credential read denied")
    if (
        trusted
        and isinstance(direct_path, str)
        and tool_name.lower() in {"edit", "write"}
        and credential_path_denied(direct_path, cwd)
    ):
        return _blocked("managed worker credential write denied")
    if not advisory_mode:
        denied = [] if is_patch else command_secret_paths(command, cwd)
        if denied:
            return _blocked(
                "managed worker credential read denied: " + ", ".join(denied)
            )
        if not is_patch and _shell_private_temp_access(command, cwd, env):
            return _blocked("other private-temp jobs are unavailable to managed workers")
        if not is_patch and _policy_match(
            PRODUCTION,
            command,
            git_subcommands=frozenset({"merge", "checkout", "switch", "push"}),
        ):
            return _blocked(PRODUCTION_REASON)
        if not is_patch and _policy_match(DISPATCH_CONTROL, command):
            return _blocked(DISPATCH_REASON)
        if not is_patch and _policy_match(
            GIT_WRITE,
            command,
            git_subcommands=frozenset({"commit", "push"}),
        ):
            return _blocked(GIT_MUTATION_REASON)
    elif not is_patch:
        advisory = _advisory_blocked_reason(command)
        if advisory:
            return _blocked(advisory)
    try:
        scopes = json.loads(env.get("CODEX_SEATS_WRITE_SCOPE", "[]"))
        external_scopes = json.loads(env.get("CODEX_SEATS_EXTERNAL_WRITE_SCOPE", "[]"))
        protected_paths = json.loads(env.get("CODEX_SEATS_PROTECTED_PATHS", "[]"))
    except ValueError:
        scopes, external_scopes, protected_paths = [], [], []
    private_scopes = (
        [{"path": env["CODEX_SEATS_PRIVATE_TEMP"], "type": "tree"}]
        if env.get("CODEX_SEATS_PRIVATE_TEMP")
        else []
    )
    if tool_name.lower() in {"edit", "write"}:
        if not isinstance(direct_path, str):
            return _blocked("managed worker write omitted an exact path")
        if any(
            write_allowed(direct_path, [{"path": value, "type": "file"}], cwd)
            for value in protected_paths
            if isinstance(value, str)
        ):
            return _blocked("protected dirty baseline is read-only: " + direct_path)
        if (
            not write_allowed(direct_path, scopes, cwd)
            and not write_allowed(direct_path, external_scopes, cwd)
            and not write_allowed(direct_path, private_scopes, cwd)
            and not (trusted and _trusted_scratch_write(direct_path, cwd, env))
        ):
            return _blocked("write outside frozen write_scope: " + direct_path)
    if is_patch or (not advisory_mode and WRITE_COMMAND.search(command)):
        # Move destinations receive the written bytes and must pass every
        # check the declared source paths pass.
        patch_paths = re.findall(
            r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.M
        ) + re.findall(r"^\*\*\* Move to: (.+)$", command, re.M)
        paths = patch_paths if is_patch else re.findall(
            r"(?:^|[\s'\"])(/[^\s'\"|;&<>]+)", command
        )
        if is_patch:
            denied = [
                path
                for path in paths
                if credential_path_denied(path, cwd)
            ]
            if denied:
                return _blocked(
                    "managed worker credential write denied: " + ", ".join(denied)
                )
        simple_paths = None if is_patch else _simple_write_paths(command)
        if simple_paths is not None:
            paths = simple_paths
        elif not is_patch:
            for match in re.finditer(r"(?:^|[;&|]\s*)(touch|rm|mkdir|tee|truncate|chmod|chown|cp|mv)\s+([^;&|]+)", command):
                try:
                    args = [value for value in shlex.split(match.group(2)) if not value.startswith("-")]
                except ValueError:
                    args = []
                if match.group(1) in {"cp", "mv"}:
                    args = args[-1:]
                paths.extend(args)
        protected = [
            path
            for path in paths
            if any(
                write_allowed(path, [{"path": value, "type": "file"}], cwd)
                for value in protected_paths
                if isinstance(value, str)
            )
        ]
        if protected:
            return _blocked("protected dirty baseline is read-only: " + ", ".join(protected))
        git_metadata = [path for path in paths if _git_metadata_path(path, cwd, env)]
        if git_metadata:
            return _blocked("Git metadata is read-only to managed workers: " + ", ".join(git_metadata))
        control_paths = [path for path in paths if _pool_control_path(path, cwd, env)]
        if control_paths:
            return _blocked("pool controls are unavailable to managed workers")
        outside = [
            path
            for path in paths
            if not write_allowed(path, scopes, cwd)
            and not write_allowed(path, external_scopes, cwd)
            and not write_allowed(path, private_scopes, cwd)
        ]
        if outside:
            return _blocked("write outside frozen write_scope: " + ", ".join(outside))
    if trusted:
        # Under dontAsk an undecided tool is silently denied, so an ordinary
        # trusted tool that cleared every protected-core check is authorized
        # here instead of being enumerated in a static allow catalog.
        return _allow("trusted development tool permitted")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, json.JSONDecodeError):
        payload = {}
    decision = worker_decision(payload if isinstance(payload, dict) else {}, dict(os.environ))
    if decision:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
