"""Read-only routing, outcome, and effort metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import Ledger, now, parse_iso

MIN_N = 5
def _number(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _token_count(value: Any) -> int:
    return int(_number(value))


def _length(value: Any) -> int:
    try:
        return len(value or [])
    except TypeError:
        return 0


def _bucket(mapping: dict[str, dict[str, Any]], key: Any) -> dict[str, Any]:
    name = str(key) if key not in (None, "") else "unknown"
    return mapping.setdefault(name, {"jobs": 0, "tokens": 0})


def _job_breakdown(jobs: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for job in jobs:
        bucket = _bucket(result, job.get(field))
        bucket["jobs"] += 1
        bucket["tokens"] += _token_count(job.get("tokens", 0))
    return result


def _provider_breakdown(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = _job_breakdown(jobs, "provider")
    for bucket in result.values():
        bucket["cost_usd"] = 0.0
    for job in jobs:
        provider = str(job.get("provider") or "unknown")
        result[provider]["cost_usd"] += _number(job.get("cost_usd", 0))
    return result


def _outcome_breakdown(jobs: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = str(job.get(field) or "unknown")
        group = result.setdefault(name, {"statuses": {}, "completed_rate": None, "total": 0})
        status = str(job.get("status") or "unknown")
        statuses = group["statuses"]
        statuses[status] = statuses.get(status, 0) + 1
        group["total"] += 1
    for group in result.values():
        completed = group["statuses"].get("completed", 0)
        group["completed_rate"] = completed / group["total"] if group["total"] else None
    return result


def _job_evidence_by_effort(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Failure and fallback evidence keyed by the effort that actually ran."""
    result: dict[str, dict[str, Any]] = {}
    for job in jobs:
        effort = str(job.get("effort") or "unknown")
        group = result.setdefault(
            effort,
            {
                "jobs": 0,
                "statuses": {},
                "completed_rate": None,
                "fallback_count": 0,
                "attempted_model_count": 0,
                "refusal_count": 0,
                "effort_downgrade_count": 0,
            },
        )
        status = str(job.get("status") or "unknown")
        group["jobs"] += 1
        group["statuses"][status] = group["statuses"].get(status, 0) + 1
        group["fallback_count"] += max(0, int(_number(job.get("fallback_count"))))
        group["attempted_model_count"] += _length(job.get("attempted_models"))
        group["refusal_count"] += _length(job.get("refused_models"))
        group["effort_downgrade_count"] += int(
            job.get("effort_downgraded_from") not in (None, "")
        )
    for group in result.values():
        completed = group["statuses"].get("completed", 0)
        group["completed_rate"] = completed / group["jobs"] if group["jobs"] else None
    return result


def _read_archived_jobs(root: Path) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for path in sorted((root / "archive").glob("jobs-*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                jobs.append(item)
    return jobs


def _dedupe_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[Any, dict[str, Any]] = {}
    for index, job in enumerate(jobs):
        job_id = job.get("id")
        key = ("id", str(job_id)) if job_id is not None else ("missing", index)
        if key in deduped:
            del deduped[key]
        deduped[key] = job
    return list(deduped.values())


def _window_jobs(jobs: list[dict[str, Any]], cutoff: float) -> list[dict[str, Any]]:
    selected = []
    for job in jobs:
        stamp = parse_iso(job.get("ts"))
        if stamp is not None and stamp >= cutoff:
            selected.append(job)
    return selected


def collect(
    root: Path,
    settings: dict[str, Any],
    *,
    days: float = 30.0,
    current: float | None = None,
) -> dict[str, Any]:
    """Collect one scorecard while holding the ledger lock only for copying."""
    root = Path(root)
    current = now() if current is None else current
    cutoff = current - days * 86400
    with Ledger(root) as data:
        live_jobs = json.loads(json.dumps(data.get("jobs", [])))

    archived_jobs = _read_archived_jobs(root)
    jobs = _window_jobs(_dedupe_jobs([*archived_jobs, *live_jobs]), cutoff)

    implementation = [job for job in jobs if job.get("kind") == "implementation"]
    # The calibration target measures AUTHORED-code share: who actually ran the
    # job (`provider`), not the router's intent (`route_provider`) — a Claude
    # task that safely spilled to Codex was Codex-authored.
    codex_implementation = [
        job for job in implementation if job.get("provider") == "codex"
    ]
    implementation_tokens = sum(_token_count(job.get("tokens", 0)) for job in implementation)
    codex_tokens = sum(_token_count(job.get("tokens", 0)) for job in codex_implementation)
    implementation_count = len(implementation)

    routing = {
        "by_provider": _provider_breakdown(jobs),
        "codex_share_of_implementation": {
            "count_share": len(codex_implementation) / implementation_count
            if implementation_count
            else None,
            "token_weighted_share": codex_tokens / implementation_tokens
            if implementation_tokens
            else None,
            "codex_jobs": len(codex_implementation),
            "total_jobs": implementation_count,
            "codex_tokens": codex_tokens,
            "total_tokens": implementation_tokens,
        },
        "by_kind": _job_breakdown(jobs, "kind"),
        "by_model": _job_breakdown(jobs, "model"),
        "by_effort": _job_breakdown(jobs, "effort"),
    }
    outcomes = {
        "by_provider": _outcome_breakdown(jobs, "provider"),
        "by_kind": _outcome_breakdown(jobs, "kind"),
        "by_effort": _outcome_breakdown(jobs, "effort"),
    }
    fallbacks = {
        "jobs_with_fallback": sum(
            1 for job in jobs if _number(job.get("fallback_count", 0)) > 0
        ),
        "jobs_with_multiple_attempted_models": sum(
            1 for job in jobs if _length(job.get("attempted_models")) > 1
        ),
        "refusals_total": sum(_length(job.get("refused_models")) for job in jobs),
        "jobs_with_effort_downgrade": sum(
            1 for job in jobs if job.get("effort_downgraded_from") is not None
        ),
        "by_effort": _job_evidence_by_effort(jobs),
    }
    return {
        "window_days": days,
        "routing": routing,
        "outcomes": outcomes,
        "fallbacks": fallbacks,
    }


def _percentage(numerator: float, denominator: float) -> str:
    if denominator < MIN_N:
        return f"insufficient data (n={denominator:g})"
    return f"{100 * numerator / denominator:.1f}%"


def _table(rows: list[tuple[str, str]]) -> list[str]:
    width = max((len(label) for label, _ in rows), default=0)
    return [f"  {label:<{width}}  {value}" for label, value in rows]


def render(report: dict[str, Any]) -> str:
    """Render the scorecard with low-sample ratios explicitly suppressed."""
    lines = [f"routing-quality scorecard — last {report.get('window_days', 0):g}d", "", "routing"]
    routing = report.get("routing") or {}
    provider_rows = []
    for provider, values in sorted((routing.get("by_provider") or {}).items()):
        provider_rows.append(
            (
                str(provider),
                f"{values.get('jobs', 0)} jobs  {values.get('tokens', 0):,} tokens  "
                f"${values.get('cost_usd', 0):,.2f}",
            )
        )
    lines.extend(_table(provider_rows or [("providers", "no jobs")]))
    share = routing.get("codex_share_of_implementation") or {}
    lines.extend(
        _table(
            [
                (
                    "codex implementation share (jobs)",
                    _percentage(share.get("codex_jobs", 0), share.get("total_jobs", 0)),
                ),
                (
                    "codex implementation share (tokens)",
                    _percentage(share.get("codex_tokens", 0), share.get("total_tokens", 0)),
                ),
            ]
        )
    )
    for title, key in (("kind", "by_kind"), ("model", "by_model"), ("effort", "by_effort")):
        rows = [
            (str(name), f"{values.get('jobs', 0)} jobs  {values.get('tokens', 0):,} tokens")
            for name, values in sorted((routing.get(key) or {}).items())
        ]
        lines.extend(["", f"routing by {title}", *_table(rows or [(title, "no jobs")])])

    outcomes = report.get("outcomes") or {}
    lines.extend(["", "outcomes"])
    for title, key in (
        ("provider", "by_provider"),
        ("kind", "by_kind"),
        ("effective effort", "by_effort"),
    ):
        rows = []
        for name, values in sorted((outcomes.get(key) or {}).items()):
            completed = (values.get("statuses") or {}).get("completed", 0)
            total = values.get("total", 0)
            statuses = ", ".join(
                f"{status}={count}"
                for status, count in sorted((values.get("statuses") or {}).items())
            )
            rows.append(
                (str(name), f"completed {_percentage(completed, total)}  {statuses or 'no outcomes'}")
            )
        lines.extend([f"  by {title}", *_table(rows or [(title, "no jobs")])])
    fallbacks = report.get("fallbacks") or {}
    lines.append("  fallback/failure evidence by effective effort")
    effort_fallback_rows = []
    for effort, values in sorted((fallbacks.get("by_effort") or {}).items()):
        statuses = ", ".join(
            f"{status}={count}"
            for status, count in sorted((values.get("statuses") or {}).items())
        )
        effort_fallback_rows.append(
            (
                str(effort),
                f"{values.get('jobs', 0)} jobs; completed "
                f"{_percentage((values.get('statuses') or {}).get('completed', 0), values.get('jobs', 0))}; "
                f"fallbacks={values.get('fallback_count', 0)} "
                f"attempted-models={values.get('attempted_model_count', 0)} "
                f"refusals={values.get('refusal_count', 0)} "
                f"downgrades={values.get('effort_downgrade_count', 0)}; "
                f"{statuses or 'no outcomes'}",
            )
        )
    lines.extend(_table(effort_fallback_rows or [("efforts", "no jobs")]))

    lines.extend(["", "fallback health"])
    lines.extend(
        _table(
            [
                ("jobs with fallback", str(fallbacks.get("jobs_with_fallback", 0))),
                (
                    "jobs trying multiple models",
                    str(fallbacks.get("jobs_with_multiple_attempted_models", 0)),
                ),
                ("model refusals", str(fallbacks.get("refusals_total", 0))),
                (
                    "effort downgrades",
                    str(fallbacks.get("jobs_with_effort_downgrade", 0)),
                ),
            ]
        )
    )

    return "\n".join(lines)


def cmd_stats(args: Any, settings: dict[str, Any], root: Path) -> int:
    report = collect(root, settings, days=args.days)
    print(json.dumps(report, indent=2) if args.as_json else render(report))
    return 0
