"""Cadence core: config, triggers, state. No UI imports."""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from croniter import croniter

DIR_NAME = ".cadence"
ROOT = Path.cwd()
CONFIG = ROOT / DIR_NAME / "tasks.toml"
LOGS = ROOT / DIR_NAME / "logs"
STATE = LOGS / "state.json"
POLL_DEFAULT = {"on_change": 60, "git": 60, "files": 2}
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
OPT_DEFAULTS = {"timeout": None, "retry": 0, "retry_delay": "30s", "overlap": "skip", "notify": "failure"}
OPT_CHOICES = {"overlap": ("skip", "queue", "kill"), "notify": ("failure", "always", "never")}
SETTINGS = {"max_parallel": 4}
RAW_SETTINGS: dict = {}
ACTIVE = ("running", "retrying", "queued")
NOTIFIED: list[tuple[str, str]] = []

STATUS = {
    "idle": ("○", "grey50"),
    "running": ("●", "dodger_blue1"),
    "ok": ("✔", "green3"),
    "failed": ("✘", "red1"),
    "cancelled": ("■", "orange3"),
    "paused": ("⏸", "yellow3"),
    "queued": ("◌", "grey62"),
    "retrying": ("↻", "orange3"),
    "timeout": ("⏱", "red1"),
}


def configure(root: Path) -> None:
    global ROOT, CONFIG, LOGS, STATE
    ROOT = root
    CONFIG = root / DIR_NAME / "tasks.toml"
    LOGS = root / DIR_NAME / "logs"
    STATE = LOGS / "state.json"


def find_root(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / DIR_NAME / "tasks.toml").is_file():
            return d
    return None


def parse_interval(s: str) -> int:
    s = s.strip()
    return int(s[:-1]) * UNITS[s[-1]]


def parse_trigger(s: str) -> dict:
    kind, _, rest = s.strip().partition(" ")
    rest = rest.strip()
    if kind == "manual" or not kind:
        return {"manual": True}
    if kind not in ("every", "cron", "on_change", "git", "files", "after", "failed") or not rest:
        raise ValueError("trigger: every 1h | cron */5 * * * * | git origin/main | on_change <cmd> | files <glob> | after <task> | failed <task> | manual")
    if kind == "every":
        parse_interval(rest)
    if kind == "cron" and not croniter.is_valid(rest):
        raise ValueError(f"invalid cron: {rest}")
    return {kind: rest}


def fmt_trigger(d: dict) -> str:
    kind, val = next(iter(d.items()))
    return "manual" if kind == "manual" else f"{kind} {val}"


def ago(ts: float | None) -> str:
    if ts is None:
        return "—"
    d = int(time.time() - ts)
    return f"{d}s ago" if d < 60 else f"{d // 60}m ago" if d < 3600 else f"{d // 3600}h ago"


def until(ts: float | None) -> str:
    if ts is None:
        return "—"
    d = max(0, int(ts - time.time()))
    return f"in {d}s" if d < 60 else f"in {d // 60}m" if d < 3600 else f"in {d // 3600}h{d % 3600 // 60:02d}m"


def slug(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name).strip("-") or "task"


def parse_opts(s: str) -> dict:
    out: dict = {}
    for tok in s.split():
        k, _, v = tok.partition("=")
        if k not in OPT_DEFAULTS or not v:
            raise ValueError(f"options: {' '.join(f'{x}=…' for x in OPT_DEFAULTS)}")
        if k in ("timeout", "retry_delay"):
            parse_interval(v)
        elif k == "retry":
            out[k] = int(v)
            continue
        elif v not in OPT_CHOICES[k]:
            raise ValueError(f"{k} must be one of {', '.join(OPT_CHOICES[k])}")
        out[k] = v
    return out


def fmt_opts(d: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in d.items())


def desktop_notify(title: str, msg: str) -> None:
    NOTIFIED.append((title, msg))
    if os.environ.get("CADENCE_NO_NOTIFY"):
        return
    if sys.platform == "darwin":
        argv = ["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run", title, msg]
    elif shutil.which("notify-send"):
        argv = ["notify-send", title, msg]
    else:
        return
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass
class Task:
    name: str
    cmd: str
    trigger: dict
    paused: bool = False
    status: str = "idle"
    last_run: float | None = None
    last_rc: int | None = None
    next_run: float | None = None
    next_poll: float = 0.0
    sig: str | None = None
    proc: asyncio.subprocess.Process | None = None
    cancelled: bool = False
    polling: bool = False
    runs: int = 0
    last_dur: float | None = None
    last_log: str | None = None
    opts: dict = field(default_factory=dict)
    pending: bool = False
    timed_out: bool = False
    cost: float = 0.0
    last_cost: float | None = None
    session: str | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=12))
    lines: deque = field(default_factory=lambda: deque(maxlen=3000))

    @property
    def kind(self) -> str:
        return next(iter(self.trigger))

    def opt(self, key: str):
        return self.opts.get(key, OPT_DEFAULTS[key])

    @property
    def is_claude(self) -> bool:
        return bool(re.search(r"\bclaude\b.*\s-p\b", self.cmd))

    def schedule(self) -> None:
        now = time.time()
        if self.kind == "every":
            self.next_run = now + parse_interval(self.trigger["every"])
        elif self.kind == "cron":
            self.next_run = croniter(self.trigger["cron"], now).get_next(float)
        else:
            self.next_run = None


async def sh(cmd: str) -> str:
    p = await asyncio.create_subprocess_shell(
        cmd, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await p.communicate()
    return out.decode(errors="replace")


async def signature(t: Task) -> str:
    kind, val = next(iter(t.trigger.items()))
    if kind == "on_change":
        raw = await sh(val)
    elif kind == "git":
        await sh("git fetch -q origin")
        raw = await sh(f"git rev-parse {val}")
    elif kind == "files":
        files = sorted(glob.glob(str(ROOT / val), recursive=True))
        raw = "".join(f"{f}{os.stat(f).st_mtime_ns}" for f in files if os.path.exists(f))
    else:
        return ""
    return hashlib.sha1(raw.encode()).hexdigest()


def save_state(tasks: list[Task]) -> None:
    data = {
        "saved": time.time(),
        "tasks": {
            t.name: {
                "trigger": t.trigger,
                "next_run": None if t.status in ("running", "retrying") else t.next_run,
                "last_run": t.last_run,
                "last_rc": t.last_rc,
                "status": "cancelled" if t.status in ("running", "retrying") else "idle" if t.status == "queued" else t.status,
                "live": t.status,
                "paused": t.paused,
                "cost": t.cost,
                "last_cost": t.last_cost,
                "session": t.session,
                "sig": t.sig,
                "runs": t.runs,
                "last_dur": t.last_dur,
                "last_log": t.last_log,
                "history": list(t.history),
            }
            for t in tasks
        },
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data))


def read_state() -> dict | None:
    try:
        data = json.loads(STATE.read_text())
        return data if data.get("tasks") else None
    except (OSError, ValueError):
        return None


def apply_state(tasks: list[Task], state: dict) -> None:
    for t in tasks:
        s = state["tasks"].get(t.name)
        if not s or s["trigger"] != t.trigger:
            continue
        if s["next_run"] is not None:
            t.next_run = s["next_run"]
        t.last_run, t.last_rc, t.status, t.sig = s["last_run"], s["last_rc"], s["status"], s["sig"]
        t.runs, t.last_dur, t.last_log = s["runs"], s["last_dur"], s["last_log"]
        t.history.extend(s["history"])
        t.cost, t.last_cost, t.session = s.get("cost", 0.0), s.get("last_cost"), s.get("session")


def terminal_is_light() -> bool:
    if sys.stdin.isatty():
        fd, old = sys.stdin.fileno(), termios.tcgetattr(sys.stdin.fileno())
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b]11;?\x1b\\")
            sys.stdout.flush()
            buf = ""
            while select.select([fd], [], [], 0.15)[0]:
                buf += os.read(fd, 64).decode(errors="ignore")
                if buf.endswith(("\x07", "\x1b\\")):
                    break
            m = re.search(r"rgb:([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)", buf)
            if m:
                r, g, b = (int(x, 16) / (16 ** len(x) - 1) for x in m.groups())
                return 0.299 * r + 0.587 * g + 0.114 * b > 0.5
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    bg = os.environ.get("COLORFGBG", "").split(";")[-1]
    return bg.isdigit() and int(bg) in (7, 15)


def describe(t: Task) -> str:
    v = next(iter(t.trigger.values()))
    return {
        "every": f"every {v}, counted from the end of each run",
        "cron": f"cron `{v}`",
        "git": f"when `{v}` moves (fetch + check every {POLL_DEFAULT['git']}s)",
        "on_change": f"when output of `{v}` changes (every {POLL_DEFAULT['on_change']}s)",
        "files": f"when files matching `{v}` change (every {POLL_DEFAULT['files']}s)",
        "after": f"after `{v}` succeeds",
        "failed": f"after `{v}` fails",
        "manual": "manual only — press r",
    }[t.kind]


def load_tasks(old: dict[str, Task] | None = None) -> list[Task]:
    old = old or {}
    tasks = []
    doc = tomlkit.parse(CONFIG.read_text())
    RAW_SETTINGS.clear()
    RAW_SETTINGS.update(doc.get("cadence", {}))
    SETTINGS["max_parallel"] = int(RAW_SETTINGS.get("max_parallel", 4))
    for row in doc.get("task", []):
        trig = dict(row.get("trigger", {"manual": True}))
        t = old.get(row["name"]) or Task(row["name"], row["cmd"], trig)
        t.cmd, t.trigger, t.paused = row["cmd"], trig, bool(row.get("paused", False))
        t.opts = {k: row[k] for k in OPT_DEFAULTS if k in row}
        if t.next_run is None and t.kind in ("every", "cron"):
            t.schedule()
        tasks.append(t)
    return tasks


def save_tasks(tasks: list[Task]) -> None:
    doc = tomlkit.document()
    if RAW_SETTINGS:
        doc["cadence"] = dict(RAW_SETTINGS)
    aot = tomlkit.aot()
    for t in tasks:
        tbl = tomlkit.table()
        tbl["name"], tbl["cmd"] = t.name, t.cmd
        trig = tomlkit.inline_table()
        trig.update(t.trigger)
        tbl["trigger"] = trig
        for k, v in t.opts.items():
            tbl[k] = v
        if t.paused:
            tbl["paused"] = True
        aot.append(tbl)
    doc["task"] = aot
    CONFIG.write_text(tomlkit.dumps(doc))


def status_line() -> str:
    state = read_state()
    if not state or time.time() - state["saved"] > 15:
        return "cadence: off"
    counts = Counter("paused" if s.get("paused") else s.get("live", s["status"]) for s in state["tasks"].values())
    order = ("running", "retrying", "queued", "failed", "timeout", "ok", "idle", "cancelled", "paused")
    return "cadence: " + " · ".join(f"{counts[k]} {k}" for k in order if counts[k])
