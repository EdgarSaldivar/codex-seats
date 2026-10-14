"""The tracked-file allowlist admits packaging and workflow metadata narrowly."""

from scripts import check_tracked_files


def test_packaging_and_workflow_paths_are_allowed() -> None:
    assert check_tracked_files.violations(
        ["pyproject.toml", ".github/workflows/ci.yml"]
    ) == []


def test_other_github_metadata_remains_outside_the_allowlist() -> None:
    assert check_tracked_files.violations([".github/actions/setup/action.yml"]) == [
        "outside allowlist: .github/actions/setup/action.yml"
    ]
