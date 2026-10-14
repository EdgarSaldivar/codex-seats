# Contributing

Use a disposable checkout and Python 3.11+. Keep development dependencies in a
checkout-local `uv` environment or `.venv-dev`; never use a live controller's
`.venv` for worker tests.

```sh
uv run --extra dev python -m pytest -q
uv run --extra dev ruff check .
python scripts/check_subprocess_policy.py
python scripts/check_tracked_files.py
python scripts/check_likely_secrets.py
```

Keep changes scoped, describe the tests run, and include sanitized fixtures only.
Do not contribute credentials, pool runtime state, provider logs, receipts,
screenshots of live accounts, private operational evidence, or personal machine
paths. Do not put real API keys in examples. See [SECURITY.md](SECURITY.md) for
private reports.
Documentation screenshots must use synthetic data only.
