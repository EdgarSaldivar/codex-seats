from __future__ import annotations

import os
from collections.abc import Mapping


# Keep this list intentionally small. Provider authentication is supplied by the
# provider runtime (HOME for Claude, an explicit CODEX_HOME for Codex), while
# model-issued tools receive only the ordinary developer context they need.
DEVELOPMENT_ENV_KEYS = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "SHELL",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "USER",
        # Apple SDK selection and deployment targets are non-secret inputs used
        # by xcrun, SwiftPM, Clang, and Xcode builds. Compiler executable/flag
        # variables are deliberately absent because they can inject code.
        "DEVELOPER_DIR",
        "SDKROOT",
        "TOOLCHAINS",
        "MACOSX_DEPLOYMENT_TARGET",
        "IPHONEOS_DEPLOYMENT_TARGET",
        "TVOS_DEPLOYMENT_TARGET",
        "WATCHOS_DEPLOYMENT_TARGET",
        "XROS_DEPLOYMENT_TARGET",
        "DRIVERKIT_DEPLOYMENT_TARGET",
    }
)

SENSITIVE_ENV_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
    "COOKIE",
)

EXECUTABLE_INJECTION_ENV_KEYS = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "CDPATH",
        "CC",
        "CXX",
        "CPP",
        "LD",
        "AR",
        "AS",
        "NM",
        "RANLIB",
        "STRIP",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SUDO_ASKPASS",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "PYTHONHOME",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "RUBYOPT",
        "PERL5OPT",
    }
)

CONTROLLER_ENV_KEYS = frozenset(
    {
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CODEX_CONTROL_SEAT",
        "CODEX_PROJECT",
        "TMUX",
        "TMUX_PANE",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GPG_AGENT_INFO",
        "CLAUDE_CONFIG_DIR",
    }
)


def _unsafe_key(key: str) -> bool:
    upper = key.upper()
    return (
        any(marker in upper for marker in SENSITIVE_ENV_MARKERS)
        or upper in EXECUTABLE_INJECTION_ENV_KEYS
        or upper in CONTROLLER_ENV_KEYS
        or upper.startswith(("CODEX_SEATS_CONTROLLER_", "DYLD_", "LD_"))
        or upper.startswith("GIT_CONFIG_KEY_")
        or upper.startswith("GIT_CONFIG_VALUE_")
    )


def minimal_worker_env(
    source: Mapping[str, str] | None = None,
    **extra: str,
) -> dict[str, str]:
    """Build the common minimal Codex/Claude worker environment.

    Inherited state is selected by allowlist, not by trying to enumerate every
    possible credential name. Explicit launch metadata is accepted only when it
    is neither secret-bearing nor a known executable/authority injection key.
    """
    inherited = os.environ if source is None else source
    env = {
        key: value
        for key, value in inherited.items()
        if (key in DEVELOPMENT_ENV_KEYS or key.startswith("LC_"))
        and not _unsafe_key(key)
    }
    unsafe_extra = sorted(key for key in extra if _unsafe_key(key))
    if unsafe_extra:
        raise ValueError(
            "unsafe worker environment keys: " + ", ".join(unsafe_extra)
        )
    env.update(extra)
    return env
