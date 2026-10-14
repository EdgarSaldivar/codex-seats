"""Contract tests for the additive local-seat and routing-fact foundation."""

from __future__ import annotations

import json

import pytest

from codex_seats import dispatch_client
from codex_seats import dispatch_protocol as dp
from codex_seats import dispatch_runner
from codex_seats import state
from codex_seats.seat_registry import SeatRegistryError, validate_local_registries
from codex_seats.task_policy import resolve_task_policy


NOW = 1_200.0
RELEASE_ID = "a" * 64
FINGERPRINT = "sha256:" + "b" * 64


def _local_settings():
    settings = state.default_settings()
    settings["local_hosts"] = {
        "workstation": {
            "label": "Workstation",
            "lifecycle": "active",
            "resource_groups": {"gpu0": {"max_concurrency": 1}},
        }
    }
    settings["local_seats"] = {
        "coder-small": {
            "label": "Local seat: coder small",
            "lifecycle": "active",
            "mode": "shadow",
            "host": "workstation",
            "resource_group": "gpu0",
            "model_profile": "coder-small",
            "harness": "local-cli",
            "capabilities": {
                "task_shapes": ["execute", "investigate"],
                "validation": ["weak"],
            },
            "max_concurrency": 1,
            "runtime_fingerprint": FINGERPRINT,
        },
        "coder-large": {
            "label": "Local seat: coder large",
            "lifecycle": "active",
            "mode": "canary",
            "host": "workstation",
            "resource_group": "gpu0",
            "model_profile": "coder-large",
            "harness": "local-cli",
            "capabilities": {
                "task_shapes": ["design"],
                "validation": ["strong", "weak"],
            },
            "max_concurrency": 1,
            "runtime_fingerprint": "sha256:" + "c" * 64,
        },
    }
    return settings


def _protocol_settings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return {"projects": {"sample": {"path": str(repo)}}}, repo


def _task(**overrides):
    request = {
        "schema": 1,
        "release_id": RELEASE_ID,
        "protocol_version": dp.PROTOCOL_VERSION,
        "protocol_min": dp.PROTOCOL_VERSION,
        "protocol_max": dp.PROTOCOL_VERSION,
        "request_id": "d" * 32,
        "operation": "task",
        "repo_id": "sample",
        "prompt": "Do bounded work.",
        "provider": "auto",
        "kind": "implementation",
        "size": "auto",
        "origin": {"head": "codex", "seat": "seat1"},
        "created_at": 1_000.0,
        "expires_at": 1_600.0,
    }
    request.update(overrides)
    return request


def test_defaults_keep_local_and_account_registries_separate():
    defaults = state.default_settings()
    assert defaults["local_hosts"] == defaults["local_seats"] == {}
    assert defaults["pool"]["seats"] == {}


def test_valid_local_registries_load(tmp_path):
    configured = _local_settings()
    (tmp_path / "settings.json").write_text(json.dumps(configured))
    loaded, warnings = state.load_settings(tmp_path)
    assert loaded["schema"] == 3
    assert loaded["local_seats"]["coder-small"]["host"] == "workstation"
    assert not [warning for warning in warnings if "invalid settings shape" in warning]


def test_shared_resource_group_is_an_explicit_exclusivity_boundary():
    settings = _local_settings()
    validate_local_registries(settings)
    small = settings["local_seats"]["coder-small"]
    large = settings["local_seats"]["coder-large"]
    assert (small["host"], small["resource_group"]) == (
        large["host"],
        large["resource_group"],
    )
    assert settings["local_hosts"]["workstation"]["resource_groups"]["gpu0"] == {
        "max_concurrency": 1
    }


def test_model_profiles_accept_version_dots_without_widening_seat_ids():
    settings = _local_settings()
    settings["local_seats"]["coder-small"]["model_profile"] = "qwen3.8-27b"
    validate_local_registries(settings)
    settings["local_seats"]["bad.id"] = settings["local_seats"].pop("coder-small")
    with pytest.raises(SeatRegistryError, match="local seat id"):
        validate_local_registries(settings)


@pytest.mark.parametrize("value", [16_384, 32768, 1_048_576])
def test_optional_context_window_size_accepts_bounded_integers(value):
    settings = _local_settings()
    validate_local_registries(settings)  # Existing seats omit the field.
    settings["local_seats"]["coder-small"]["context_window_size"] = value
    validate_local_registries(settings)


@pytest.mark.parametrize("value", [True, False, None, 16_383, 1_048_577, 32768.0, "32768"])
def test_context_window_size_rejects_invalid_values(value):
    settings = _local_settings()
    settings["local_seats"]["coder-small"]["context_window_size"] = value
    with pytest.raises(SeatRegistryError, match="context_window_size"):
        validate_local_registries(settings)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda value: value["local_seats"]["coder-small"].update(host="missing"),
            "configured local host",
        ),
        (
            lambda value: value["local_seats"]["coder-small"].update(
                resource_group="gpu1"
            ),
            "resource group",
        ),
        (
            lambda value: value["local_seats"]["coder-small"].update(
                max_concurrency=2
            ),
            "resource group maximum",
        ),
        (
            lambda value: value["local_seats"]["coder-small"].update(mode="manual"),
            "mode must be one of",
        ),
        (
            lambda value: value["local_seats"]["coder-small"].update(
                runtime_fingerprint="latest"
            ),
            "runtime_fingerprint",
        ),
        (
            lambda value: value["local_seats"]["coder-small"]["capabilities"].update(
                task_shapes=["chat"]
            ),
            "task_shapes",
        ),
        (
            lambda value: value["local_seats"]["coder-small"].update(
                credential="secret"
            ),
            "forbidden",
        ),
    ],
)
def test_malformed_local_metadata_and_references_are_rejected(mutate, message):
    settings = _local_settings()
    mutate(settings)
    with pytest.raises(SeatRegistryError, match=message):
        validate_local_registries(settings)


@pytest.mark.parametrize("mode", ["off", "shadow", "canary", "opportunistic"])
def test_active_local_seat_accepts_each_planned_mode(mode):
    settings = _local_settings()
    settings["local_seats"]["coder-small"]["mode"] = mode
    validate_local_registries(settings)


def test_non_active_local_seat_is_fail_closed_to_off():
    settings = _local_settings()
    settings["local_seats"]["coder-small"].update(
        lifecycle="inactive", mode="opportunistic"
    )
    with pytest.raises(SeatRegistryError, match="mode off"):
        validate_local_registries(settings)


def test_legacy_dispatch_defaults_are_conservative_and_normalized(tmp_path):
    settings, repo = _protocol_settings(tmp_path)
    resolved, normalized = dp.validate_request(_task(), settings, now=NOW)
    assert resolved == repo
    assert normalized["task_shape"] == "execute"
    assert normalized["validation"] == "unspecified"
    profile = resolve_task_policy({}, "implementation")
    assert (profile.task_shape, profile.validation) == ("execute", "unspecified")


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("task_shape", "chat", "task_shape must be one of"),
        ("validation", "none", "validation must be one of"),
    ],
)
def test_protocol_rejects_malformed_routing_facts(tmp_path, field, value, message):
    settings, _repo = _protocol_settings(tmp_path)
    request = _task(
        release_id=RELEASE_ID,
        protocol_version=8,
        protocol_min=8,
        protocol_max=8,
        **{field: value},
    )
    with pytest.raises(dp.DispatchError, match=message):
        dp.validate_request(request, settings, now=NOW)


def test_explicit_routing_facts_and_task_identity_are_normalized(tmp_path):
    settings, _repo = _protocol_settings(tmp_path)
    request = _task(
        release_id=RELEASE_ID,
        protocol_version=8,
        protocol_min=8,
        protocol_max=8,
        task_shape="investigate",
        validation="weak",
        task_id="logical-task:17",
    )
    _repo, normalized = dp.validate_request(request, settings, now=NOW)
    assert normalized["task_shape"] == "investigate"
    assert normalized["validation"] == "weak"
    assert normalized["task_id"] == "logical-task:17"



def test_protocol_accepts_local_as_controller_routed_provider(tmp_path):
    settings, _repo = _protocol_settings(tmp_path)
    request = _task(provider="local")
    _repo, normalized = dp.validate_request(request, settings, now=NOW)
    assert normalized["provider"] == "local"


@pytest.mark.parametrize("field", ["side_effect_class", "language", "domain"])
def test_caller_cannot_supply_deferred_routing_dimensions(tmp_path, field):
    settings, _repo = _protocol_settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="forbidden field"):
        dp.validate_request(_task(**{field: "caller-choice"}), settings, now=NOW)


def test_client_and_internal_request_preserve_routing_and_attempt_identity(
    tmp_path,
):
    settings, repo = _protocol_settings(tmp_path)
    package = tmp_path / "releases" / RELEASE_ID / "lib" / "codex_seats"
    package.mkdir(parents=True)
    (tmp_path / "current").symlink_to(f"releases/{RELEASE_ID}")
    request_id = dispatch_client.submit_task(
        tmp_path,
        settings,
        repo_id="sample",
        prompt="Investigate the failure.",
        origin_head="codex",
        task_shape="investigate",
        validation="weak",
        task_id="logical-task:17",
        now=NOW,
    )
    ready = dp.queue_paths(tmp_path)["inbox"] / f"{request_id}.ready"
    frame = dp.read_request_secure(ready)
    assert frame["protocol_version"] == frame["protocol_min"] == 8
    _repo, normalized = dp.validate_request(frame, settings, now=NOW)
    internal = dispatch_runner._internal_request(normalized, repo, settings)
    assert internal["task_shape"] == "investigate"
    assert internal["validation"] == "weak"
    assert internal["task_id"] == "logical-task:17"
    assert internal["attempt_id"] == request_id
