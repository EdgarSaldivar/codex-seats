# Packaging and CI

`pyproject.toml` is the tracked source of packaging, development dependencies,
and Ruff behavior. It builds the `codex_seats` package and
publishes the `codex-seat` console entry point. Supported Python versions begin
at 3.11.

CI runs the full test, Ruff, subprocess-policy, tracked-file, and likely-secret
gates on Linux and macOS with Python 3.11 and 3.14. A separate job builds both
an sdist and wheel, installs the wheel into a fresh environment, checks
`codex-seat --version`, and confirms the installed default package review gate is
discoverable. The macOS native job pins `@openai/codex@0.153.1`; missing native
containment, including a nested-sandbox result, fails the job.

Built distributions are acceptance artifacts. They do not replace the
documented immutable Git release workflow for operational upgrades, and no CI
job activates a release or writes live pool state.
