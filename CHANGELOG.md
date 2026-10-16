# Changelog

## 0.1.0

First release.

- Dashboard with task list, live output and a details pane
- Triggers: `every`, `cron`, `git`, `on_change`, `files`, `after`, `failed`, `manual`
- Per-task `timeout`, `retry`/`retry_delay`, `overlap`, `notify`; global `max_parallel`
- Run history browser with diff against the previous run
- Resume schedules and watcher baselines between sessions
- Desktop notifications (macOS, Linux)
- Cost and session tracking for `claude -p` tasks
- `cadence init`, `cadence status`
