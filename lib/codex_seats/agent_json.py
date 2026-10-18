"""The one JSON envelope for agent-facing ``--json`` command output.

Success is ``{"schema": 1, "ok": true, "data": ...}``. Failure is printed on
stdout too, as ``{"schema": 1, "ok": false, "error": {"code", "message"}}``,
and the command exits nonzero, so an agent can parse one stream and branch on
``ok`` and the exit code together.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA = 1


def success(data: Any) -> dict[str, Any]:
    return {"schema": SCHEMA, "ok": True, "data": data}


def failure(code: str, message: str, *, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        payload["data"] = data
    return payload


def dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def emit_success(data: Any, *, exit_code: int = 0) -> int:
    print(dumps(success(data)))
    return exit_code


def emit_failure(code: str, message: str, exit_code: int, *, data: Any = None) -> int:
    print(dumps(failure(code, message, data=data)))
    return exit_code
