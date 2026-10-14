from __future__ import annotations

import pytest

from codex_seats import dispatch_admission as da


def _job(
    request_id,
    *,
    repo="repo-a",
    scopes=("src",),
    safe=True,
    serial=False,
    identity="coord-a",
):
    identities = (
        frozenset({("coordination_id", identity)}) if identity is not None else frozenset()
    )
    return da.AdmissionFrame(
        request_id=request_id,
        repo=repo,
        scopes=tuple(scopes),
        parallel_safe=safe,
        serial=serial,
        identities=identities,
    )


@pytest.mark.parametrize(
    "left,right,overlap",
    [
        ("src/app", "src/app/model", True),
        ("src/app", "src/apple", False),
        ("a", "ab", False),
        (".", "anything", True),
        ("docs/api", "docs/web", False),
    ],
)
def test_scope_overlap_is_component_aware(left, right, overlap):
    assert da.scopes_overlap(left, right) is overlap


@pytest.mark.parametrize("bad", ["../src", "/src", "src/*", "", "src/[ab]"])
def test_uncertain_scope_is_rejected(bad):
    with pytest.raises(da.ScopeError):
        da.normalize_scope(bad)


def test_different_repos_may_run_concurrently_without_parallel_markers():
    active = [_job("a", repo="repo-a", safe=False, identity=None)]
    assert da.can_admit(_job("b", repo="repo-b", safe=False, identity=None), active)


def test_same_repo_requires_both_safe_nonempty_disjoint_and_known_identity():
    base = _job("a", scopes=("src/a",), identity="one")
    assert da.can_admit(_job("b", scopes=("src/b",), identity="two"), [base])
    assert not da.can_admit(_job("b", scopes=("src/a/sub",), identity="two"), [base])
    assert not da.can_admit(_job("b", scopes=(), identity="two"), [base])
    assert not da.can_admit(_job("b", safe=False, identity="two"), [base])
    assert not da.can_admit(_job("b", identity=None), [base])


def test_validated_request_ids_enable_disjoint_same_repo_work_but_goal_stays_single():
    first = da.AdmissionFrame.from_frame(
        {
            "request_id": "a" * 32,
            "estimated_minutes": 10,
            "parallel_safe": True,
            "scope_hints": ["src/a"],
        },
        "/repo",
    )
    second = da.AdmissionFrame.from_frame(
        {
            "request_id": "b" * 32,
            "estimated_minutes": 10,
            "parallel_safe": True,
            "scope_hints": ["src/b"],
        },
        "/repo",
    )
    assert da.can_admit(second, [first])

    goal_first = da.AdmissionFrame.from_frame(
        {
            "request_id": "c" * 32,
            "goal_id": "goal-1",
            "estimated_minutes": 10,
            "parallel_safe": True,
            "scope_hints": ["src/a"],
        },
        "/repo",
    )
    goal_second = da.AdmissionFrame.from_frame(
        {
            "request_id": "d" * 32,
            "goal_id": "goal-1",
            "estimated_minutes": 10,
            "parallel_safe": True,
            "scope_hints": ["src/b"],
        },
        "/repo",
    )
    assert not da.can_admit(goal_second, [goal_first])


def test_same_coordination_identity_never_parallelizes_even_across_repos():
    active = [_job("a", repo="repo-a", identity="same")]
    assert not da.can_admit(_job("b", repo="repo-b", identity="same"), active)


def test_global_max_three_and_short_job_exclusivity():
    active = [
        _job("a", repo="a"),
        _job("b", repo="b"),
        _job("c", repo="c"),
    ]
    assert not da.can_admit(_job("d", repo="d"), active)
    assert not da.can_admit(_job("short", repo="d", serial=True), active[:1])
    assert da.can_admit(_job("short", repo="d", serial=True), [])
    assert not da.can_admit(_job("long", repo="d"), [_job("short", serial=True)])


def test_waiting_short_job_is_a_deterministic_barrier():
    active = [_job("running", repo="a")]
    candidates = [
        _job("short", repo="b", serial=True),
        _job("younger", repo="c"),
    ]
    assert da.select_admissible(candidates, active) == ()


def test_candidate_specific_capacity_does_not_leak_to_ordinary_request():
    ordinary = _job("ordinary", repo="a")
    reserve_authorized = _job("reserve", repo="b")
    selected = da.select_admissible(
        [ordinary, reserve_authorized],
        [],
        candidate_max_active={
            ordinary.request_id: 0,
            reserve_authorized.request_id: 1,
        },
    )
    assert selected == (reserve_authorized,)
