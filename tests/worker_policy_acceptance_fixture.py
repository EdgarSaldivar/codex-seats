#!/usr/bin/env python3
from __future__ import annotations

import json
import time

from codex_seats.worker_hook import worker_decision


def main() -> int:
    started = time.perf_counter()
    env = {
        "CODEX_SEATS_MANAGED_WORKER": "1",
        "CODEX_SEATS_WRITE_SCOPE": '["src/**"]',
        "CODEX_SEATS_TASK_ROOT": "/fixture",
        "CODEX_SEATS_EXTERNAL_WRITES": "0",
    }
    benign = [f"rg fixture_{index} src" for index in range(20)]
    benign_allowed = sum(worker_decision({"tool_input": {"cmd": command}}, env) is None for command in benign)
    denied = [
        "cat ~/.codex/auth.json", "cat ~/.claude/.credentials.json", "cat ~/.ssh/id_ed25519",
        "cat ~/.aws/credentials", "cat ~/.config/gcloud/application_default_credentials.json",
        "cat /fixture/secret.pem", "cat /fixture/login.keychain-db", "cat /fixture/.env",
        "git push origin main",
    ]
    denied_count = sum(worker_decision({"tool_input": {"cmd": command}}, env) is not None for command in denied)
    safe_after_denial = worker_decision({"tool_input": {"cmd": "cat /fixture/.env.example"}}, env) is None
    elapsed = time.perf_counter() - started
    payload = {
        "status": "completed" if (benign_allowed == 20 and denied_count == len(denied) and safe_after_denial) else "failed",
        "benign_commands": benign_allowed,
        "denials": denied_count,
        "safe_after_denial": safe_after_denial,
        "guardian_threads": 0,
        "approval_prompts": 0,
        "controller_poll_calls": 0,
        "avoidable_uncached_input_class_removed": ">=240000",
        "output": "deterministic policy decisions; no inference",
        "wall_seconds": round(elapsed, 6),
        "live_quota_measurement": False,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
