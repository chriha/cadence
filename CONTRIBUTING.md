# Contributing

```sh
git clone <repository-url> && cd cadence
uv run --group dev pytest        # unit tests + headless TUI tests
uv run cadence -C /path/to/project
```

- `core.py` has no UI imports and holds the config, trigger and state logic. Put anything testable without Textual there.
- `app.py` is the Textual UI plus the run engine (`request_run` → `dispatch` → `run_task`).
- Add or update a test with every behavior change. `tests/test_app.py` drives the real UI with Textual's pilot.
- Keep dependencies minimal. Open an issue before adding one.
