"""Command-line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, core

SAMPLE = '''# Cadence tasks. Edit here or inside the TUI (the file is hot-reloaded).
# Reference: see the README (Configuration).

[cadence]
max_parallel = 4

[[task]]
name = "heartbeat"
cmd = "date"
trigger = { every = "1m" }

[[task]]
name = "main-moved"
cmd = "git log -1 --oneline origin/main"
trigger = { git = "origin/main" }
notify = "always"

[[task]]
name = "tests"
cmd = "echo run your test suite here"
trigger = { after = "main-moved" }
timeout = "10m"
retry = 1

[[task]]
name = "cleanup"
cmd = "echo manual task, press r to run"
trigger = { manual = true }
'''


def init(root: Path) -> int:
    d = root / core.DIR_NAME
    if (d / "tasks.toml").exists():
        print(f"{d / 'tasks.toml'} already exists", file=sys.stderr)
        return 1
    d.mkdir(exist_ok=True)
    (d / "tasks.toml").write_text(SAMPLE)
    (d / ".gitignore").write_text("logs/\n")
    print(f"created {d / 'tasks.toml'}\nrun `cadence` to open the dashboard")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cadence", description="Terminal dashboard for tasks that run on triggers.")
    p.add_argument("command", nargs="?", choices=["run", "init", "status"], default="run",
                   help="run (default): open the dashboard · init: create .cadence/tasks.toml · status: print a one-line summary")
    p.add_argument("-C", "--dir", type=Path, default=Path.cwd(), help="project directory (default: search upwards from cwd)")
    p.add_argument("--version", action="version", version=f"cadence {__version__}")
    args = p.parse_args(argv)

    if args.command == "init":
        return init(args.dir.resolve())
    root = core.find_root(args.dir.resolve())
    if root is None:
        if args.command == "status":
            print("cadence: off")
            return 0
        print("no .cadence/tasks.toml found here or in a parent directory. Run `cadence init` first.", file=sys.stderr)
        return 1
    core.configure(root)
    if args.command == "status":
        print(core.status_line())
        return 0

    from .app import CadenceApp

    CadenceApp(light=core.terminal_is_light()).run()
    return 0
