"""The single source of truth for reasoning-effort levels.

One ordered tuple, imported everywhere an effort value is validated or offered as
a CLI choice, so the levels can never drift between modules again (before this,
the goal spec and CLI accepted five levels while provider and state handling
accepted six — the ``ultra`` top level was silently missing from half the codebase).

``ultra`` is the top reasoning level (above ``max``); order is ascending.
"""
from __future__ import annotations

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")


def clamp_effort(effort: str | None, *, supports_ultra: bool) -> tuple[str | None, str | None]:
    """Clamp capability-gated levels to the provider's ceiling.

    Returns ``(effective_effort, downgraded_from)``. "ultra" is a real level
    (Sol Ultra subagent mode) and stays in ``EFFORTS``, but backends without it
    misbehave rather than erroring usefully: the claude CLI ignores it and
    silently runs at its *default* effort (weaker than max), and the Codex API
    rejects it with a 400 unless the account is enrolled. Clamp to "max" unless
    the backend is known-capable; the second element lets callers record the
    downgrade in telemetry.
    """
    if effort == "ultra" and not supports_ultra:
        return "max", "ultra"
    return effort, None
