"""Cadence TUI."""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

from . import core
from .core import (
    ACTIVE,
    POLL_DEFAULT,
    SETTINGS,
    STATUS,
    Task,
    ago,
    apply_state,
    describe,
    desktop_notify,
    fmt_opts,
    fmt_trigger,
    load_tasks,
    parse_interval,
    parse_opts,
    parse_trigger,
    read_state,
    save_state,
    save_tasks,
    signature,
    slug,
    until,
)


class TaskForm(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, task: Task | None, focus: str = "name") -> None:
        super().__init__()
        self.target, self.focus_id = task, focus

    def compose(self) -> ComposeResult:
        t = self.target
        with Vertical(id="form"):
            yield Label("Edit task" if t else "New task", id="form-title")
            yield Label("name")
            yield Input(t.name if t else "", id="name")
            yield Label("command")
            yield Input(t.cmd if t else "", id="cmd")
            yield Label("trigger  (every 1h | cron 0 9 * * 1-5 | git origin/main | on_change <cmd> | files <glob> | after <task> | failed <task> | manual)")
            yield Input(fmt_trigger(t.trigger) if t else "manual", id="trigger")
            yield Label("options  (timeout=10m retry=2 retry_delay=30s overlap=skip|queue|kill notify=failure|always|never)")
            yield Input(fmt_opts(t.opts) if t else "", id="options")
            yield Static("", id="error")
            yield Label("enter: next/save · esc: cancel", id="form-hint")

    def on_mount(self) -> None:
        self.query_one(f"#{self.focus_id}", Input).focus()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        ev.stop()
        order = ["name", "cmd", "trigger", "options"]
        if ev.input.id != "options":
            self.query_one(f"#{order[order.index(ev.input.id) + 1]}", Input).focus()
            return
        name, cmd = self.query_one("#name", Input).value.strip(), self.query_one("#cmd", Input).value.strip()
        try:
            if not name or not cmd:
                raise ValueError("name and command are required")
            trig = parse_trigger(self.query_one("#trigger", Input).value)
            opts = parse_opts(ev.value)
        except (ValueError, KeyError, IndexError) as e:
            self.query_one("#error", Static).update(Text(str(e), style="red"))
            return
        self.dismiss((name, cmd, trig, opts))


class Confirm(ModalScreen):
    BINDINGS = [Binding("y", "dismiss(True)", "Yes"), Binding("n,escape", "dismiss(False)", "No")]

    def __init__(self, msg: str) -> None:
        super().__init__()
        self.msg = msg

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.msg)
            yield Label("y / n", id="form-hint")


HELP = """\
[b]navigate[/]   j/k move · g/G top/bottom · enter,l output · h list · / filter
[b]run[/]        r run now · x cancel · p pause/resume · c clear output
[b]history[/]    [ older run · ] newer run (→ live) · D diff vs previous run
[b]edit[/]       a add · e edit · t trigger · d delete
[b]other[/]      : command palette · y copy session/log · T theme · ? help · q quit

Config: .cadence/tasks.toml (hot-reloaded). Per-task keys: timeout, retry,
retry_delay, overlap, notify. Global: [cadence] max_parallel."""


class Help(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(HELP)


class CadenceApp(App):
    TITLE = "cadence"
    CSS = """
    #main { height: 1fr; }
    #left { width: 55%; }
    #table { height: 1fr; border: round $primary-darken-2; }
    #table:focus { border: round $accent; }
    #filter { display: none; height: 3; }
    #details { height: auto; min-height: 9; border: round $primary-darken-2; padding: 0 1; }
    #log { width: 1fr; border: round $primary-darken-2; padding: 0 1; }
    #log:focus { border: round $accent; }
    TaskForm, Confirm, Help { align: center middle; }
    #form, #confirm { width: 90; height: auto; background: $surface; border: thick $accent; padding: 1 2; }
    #form-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #form-hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        Binding("j", "down", "↓", show=False),
        Binding("k", "up", "↑", show=False),
        Binding("g", "top", "top", show=False),
        Binding("G", "bottom", "bottom", show=False),
        Binding("l", "focus_log", "output"),
        Binding("h", "focus_table", "list", show=False),
        Binding("r", "run", "run"),
        Binding("x", "cancel", "cancel"),
        Binding("p", "pause", "pause"),
        Binding("c", "clear_log", "clear"),
        Binding("slash", "filter", "filter"),
        Binding("left_square_bracket", "prev_run", "older", show=False),
        Binding("right_square_bracket", "next_run", "newer", show=False),
        Binding("D", "diff", "diff", show=False),
        Binding("y", "yank", "copy", show=False),
        Binding("colon", "command_palette", "palette", show=False),
        Binding("escape", "escape", "", show=False),
        Binding("T", "toggle_theme", "theme", show=False),
        Binding("a", "add", "add"),
        Binding("e", "edit", "edit"),
        Binding("t", "edit_trigger", "trigger", show=False),
        Binding("d", "delete", "delete"),
        Binding("question_mark", "help", "help"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, light: bool = False) -> None:
        super().__init__()
        self.light = light
        self.tasks: list[Task] = load_tasks()
        self.cfg_mtime = core.CONFIG.stat().st_mtime
        self.shown: str | None = None
        self.bg: set[asyncio.Task] = set()
        self.queue: list[Task] = []
        self.filter = ""
        self.view_idx: int | None = None
        self.diff = False

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Input(placeholder="filter…  (enter: keep · esc: clear)", id="filter")
                yield Static(id="details")
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#table", DataTable)
        tbl.border_title = "tasks"
        for key, label, width in [("s", " ", 1), ("name", "name", 18), ("trigger", "trigger", 26), ("last", "last run", 12), ("next", "next", 20)]:
            tbl.add_column(label, key=key, width=width)
        self.theme = "textual-light" if self.light else "textual-dark"
        self.query_one("#details").border_title = "details"
        tbl.focus()
        state = read_state()
        if state and any(t.name in state["tasks"] for t in self.tasks):
            msg = f"Resume previous session from {ago(state['saved'])}?\nSchedules and watchers continue where they left off."
            self.push_screen(Confirm(msg), lambda yes: self.start(state if yes else None))
        else:
            self.start(None)

    def start(self, state: dict | None) -> None:
        if state:
            apply_state(self.tasks, state)
        self.rebuild()
        self.set_interval(1, self.tick)

    def set_title(self) -> None:
        n = sum(t.status == "running" for t in self.tasks)
        title = f"cadence · {core.ROOT.name}" + (f" · {n} running" if n else "")
        if title != self.title:
            self.title = title
            try:
                self._driver.write(f"\x1b]0;{title}\x07")
                self._driver.flush()
            except Exception:
                pass

    def update_details(self) -> None:
        t = self.current
        box = self.query_one("#details", Static)
        if not t:
            box.update("")
            return
        glyph, color = STATUS["paused" if t.paused else t.status]
        out = Text()

        def row(label: str, *parts: str | Text) -> None:
            out.append(f"{label:<9}", style="bold")
            for p in parts:
                out.append(p)
            out.append("\n")

        row("command", Text(t.cmd, style="cyan"))
        row("trigger", describe(t))
        row("status", Text(f"{glyph} {'paused' if t.paused else t.status}", style=color))
        if t.last_run:
            dur = f" · {t.last_dur:.1f}s" if t.last_dur is not None and t.status != "running" else ""
            rc = f" · exit {t.last_rc}" if t.last_rc is not None and t.status != "running" else ""
            row("last run", f"{datetime.fromtimestamp(t.last_run):%H:%M:%S} ({ago(t.last_run)}){dur}{rc}")
        if t.next_run and not t.paused:
            row("next", f"{datetime.fromtimestamp(t.next_run):%H:%M:%S} ({until(t.next_run)})")
        hist = Text()
        for h in t.history:
            g, c = STATUS[h]
            hist.append(g + " ", style=c)
        row("runs", f"{t.runs}  ", hist)
        if t.opts:
            row("options", Text(fmt_opts(t.opts), style="dim"))
        if t.is_claude:
            cost = f"${t.last_cost:.3f} last · ${t.cost:.3f} total" if t.last_cost is not None else (
                "add --output-format json to track cost" if "--output-format" not in t.cmd else "—")
            row("claude", cost)
            if t.session:
                row("session", Text(f"claude --resume {t.session}", style="dim"))
        if t.sig and t.kind in POLL_DEFAULT:
            row("baseline", t.sig[:10])
        if t.last_log:
            row("log", Text(t.last_log, style="dim"))
        box.update(out)

    # ---- helpers
    @property
    def current(self) -> Task | None:
        tbl = self.query_one("#table", DataTable)
        if not self.tasks or tbl.row_count == 0:
            return None
        try:
            key = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((t for t in self.tasks if t.name == key), None)

    def spawn(self, coro) -> None:
        job = asyncio.create_task(coro)
        self.bg.add(job)
        job.add_done_callback(self.bg.discard)

    def row_cells(self, t: Task) -> tuple:
        glyph, color = STATUS["paused" if t.paused else t.status]
        if t.paused:
            nxt = "paused"
        elif t.status == "retrying":
            nxt = f"retry {until(t.next_run)}"
        elif t.kind in ("every", "cron"):
            nxt = until(t.next_run)
        elif t.kind in ("after", "failed"):
            nxt = f"{t.kind} {next(iter(t.trigger.values()))}"
        else:
            nxt = "manual" if t.kind == "manual" else "watching"
        last = "—" if t.last_run is None else f"{t.status} {ago(t.last_run).removesuffix(' ago')}" if t.status not in ("running", "retrying", "queued") else f"{t.status}…"
        trig = fmt_trigger(t.trigger)
        return Text(glyph, style=color), t.name, trig if len(trig) <= 26 else trig[:25] + "…", last, nxt

    def rebuild(self) -> None:
        tbl = self.query_one("#table", DataTable)
        keep = self.current.name if self.current else None
        tbl.clear()
        vis = self.visible()
        for t in vis:
            tbl.add_row(*self.row_cells(t), key=t.name)
        if keep and any(t.name == keep for t in vis):
            tbl.move_cursor(row=[t.name for t in vis].index(keep))
        self.show_selected(force=True)

    def visible(self) -> list[Task]:
        f = self.filter.lower()
        return [t for t in self.tasks if f in t.name.lower()]

    def refresh_rows(self) -> None:
        tbl = self.query_one("#table", DataTable)
        for t in self.visible():
            for col, val in zip(("s", "name", "trigger", "last", "next"), self.row_cells(t)):
                tbl.update_cell(t.name, col, val)

    def show_selected(self, force: bool = False) -> None:
        t = self.current
        name = t.name if t else None
        if name == self.shown and not force:
            return
        self.shown = name
        self.view_idx, self.diff = None, False
        self.update_details()
        self.render_log()

    def run_logs(self, t: Task) -> list[Path]:
        return sorted((core.LOGS / slug(t.name)).glob("*.log"))

    def render_log(self) -> None:
        t = self.current
        log = self.query_one("#log", RichLog)
        log.clear()
        title = f"output · {t.name}" if t else "output"
        if t and self.view_idx is None:
            for line in t.lines:
                log.write(Text.from_ansi(line))
        elif t:
            files = self.run_logs(t)
            self.view_idx = max(0, min(self.view_idx, len(files) - 1)) if files else None
            if self.view_idx is None:
                return self.render_log()
            f = files[self.view_idx]
            title += f" · run {self.view_idx + 1}/{len(files)} · {f.stem}"
            lines = f.read_text(errors="replace").splitlines()
            if self.diff:
                prev = files[self.view_idx - 1].read_text(errors="replace").splitlines() if self.view_idx else []
                title += " · diff"
                lines = list(difflib.unified_diff(prev, lines, "previous", f.stem, lineterm="", n=2)) or ["(no differences)"]
            for line in lines:
                style = "green" if self.diff and line.startswith("+") else "red" if self.diff and line.startswith("-") else "cyan" if self.diff and line.startswith("@@") else ""
                log.write(Text(line, style=style) if self.diff else Text.from_ansi(line))
        log.border_title = title

    def on_data_table_row_highlighted(self, _ev) -> None:
        self.show_selected()

    def on_data_table_row_selected(self, _ev) -> None:
        self.query_one("#log").focus()

    # ---- engine
    async def tick(self) -> None:
        if core.CONFIG.stat().st_mtime != self.cfg_mtime:
            self.cfg_mtime = core.CONFIG.stat().st_mtime
            try:
                self.tasks = load_tasks({t.name: t for t in self.tasks})
                self.rebuild()
            except Exception as e:
                self.notify(f"tasks.toml: {e}", severity="error")
        now = time.time()
        for t in self.tasks:
            if t.paused:
                continue
            if t.next_run is not None and now >= t.next_run and t.status not in ACTIVE:
                self.request_run(t)
            elif t.kind in POLL_DEFAULT and now >= t.next_poll and not t.polling:
                self.spawn(self.poll(t))
        self.dispatch()
        self.refresh_rows()
        self.update_details()
        self.set_title()
        save_state(self.tasks)

    async def poll(self, t: Task) -> None:
        t.polling = True
        try:
            sig = await signature(t)
        finally:
            t.polling = False
            t.next_poll = time.time() + POLL_DEFAULT[t.kind]
        fired = t.sig is not None and sig != t.sig
        t.sig = sig
        if fired and not t.paused:
            if t.opt("notify") == "always":
                desktop_notify(t.name, f"trigger fired: {fmt_trigger(t.trigger)}")
            self.request_run(t)

    def request_run(self, t: Task, manual: bool = False) -> None:
        if t.status in ("running", "retrying"):
            if manual or t.opt("overlap") == "skip":
                return
            t.pending = True
            if t.opt("overlap") == "kill":
                self.cancel_task(t)
            return
        if t.status == "queued":
            return
        t.status = "queued"
        self.queue.append(t)
        self.dispatch()

    def dispatch(self) -> None:
        while self.queue and sum(x.status == "running" for x in self.tasks) < SETTINGS["max_parallel"]:
            t = self.queue.pop(0)
            t.status = "running"
            self.spawn(self.run_task(t))

    def cancel_task(self, t: Task) -> None:
        if t.status == "queued":
            self.queue.remove(t)
            t.status = "idle"
            return
        t.cancelled = True
        if t.proc and t.proc.returncode is None:
            os.killpg(t.proc.pid, signal.SIGTERM)

    async def exec_once(self, t: Task, fh, timeout: int | None) -> int:
        t.proc = await asyncio.create_subprocess_shell(
            t.cmd, cwd=core.ROOT, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )
        assert t.proc.stdout

        async def pump() -> None:
            async for raw in t.proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                self.emit(t, line, fh)
                self.scan_claude(t, line)

        try:
            await asyncio.wait_for(pump(), timeout)
        except asyncio.TimeoutError:
            t.timed_out = True
            self.emit(t, f"── timeout after {t.opt('timeout')}, killing", fh)
            os.killpg(t.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(t.proc.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(t.proc.pid, signal.SIGKILL)
        return await t.proc.wait()

    async def run_task(self, t: Task) -> None:
        try:
            await self.run_attempts(t)
        except Exception as e:
            t.status, t.proc = "failed", None
            self.emit(t, f"── internal error: {e}")
            self.finish(t)

    async def run_attempts(self, t: Task) -> None:
        t.cancelled = t.timed_out = t.pending = False
        t.status, t.last_run, t.last_cost = "running", time.time(), None
        started = t.last_run
        t.lines.clear()
        if self.shown == t.name and self.view_idx is None:
            self.query_one("#log", RichLog).clear()
        logdir = core.LOGS / slug(t.name)
        logdir.mkdir(parents=True, exist_ok=True)
        logfile = logdir / f"{datetime.now():%Y%m%d-%H%M%S}.log"
        t.last_log = os.path.relpath(logfile, core.ROOT)
        attempts = int(t.opt("retry")) + 1
        timeout = parse_interval(t.opt("timeout")) if t.opt("timeout") else None
        with open(logfile, "w") as fh:
            for attempt in range(1, attempts + 1):
                t.status = "running"
                if attempt > 1:
                    self.emit(t, f"── retry {attempt - 1}/{attempts - 1}", fh)
                self.emit(t, f"$ {t.cmd}", fh)
                t.last_rc = await self.exec_once(t, fh, timeout)
                if t.cancelled or t.last_rc == 0 or attempt == attempts:
                    break
                delay = parse_interval(t.opt("retry_delay")) * 2 ** (attempt - 1)
                self.emit(t, f"── exit {t.last_rc}; retrying in {delay}s", fh)
                t.status, t.next_run = "retrying", time.time() + delay
                while time.time() < t.next_run and not t.cancelled:
                    await asyncio.sleep(0.25)
                if t.cancelled:
                    break
            t.status = "cancelled" if t.cancelled else "timeout" if t.timed_out else "ok" if t.last_rc == 0 else "failed"
            self.emit(t, f"── exit {t.last_rc} ({t.status})", fh)
        t.last_run, t.proc = time.time(), None
        t.last_dur, t.runs = t.last_run - started, t.runs + 1
        t.history.append(t.status)
        t.schedule()
        self.finish(t)

    def finish(self, t: Task) -> None:
        bad = t.status in ("failed", "timeout")
        if t.opt("notify") == "always" and t.status != "cancelled" or bad and t.opt("notify") != "never":
            desktop_notify(t.name, f"{t.status} (exit {t.last_rc}) after {t.last_dur:.0f}s")
        if bad:
            self.notify(f"{t.name}: {t.status}", severity="error")
        for d in self.tasks:
            if d is t or d.paused:
                continue
            if t.status == "ok" and d.trigger.get("after") == t.name or bad and d.trigger.get("failed") == t.name:
                self.request_run(d)
        if t.pending:
            t.pending = False
            self.request_run(t)
        self.dispatch()

    def scan_claude(self, t: Task, line: str) -> None:
        if not (t.is_claude and line.startswith("{") and "total_cost_usd" in line):
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        t.last_cost = float(obj.get("total_cost_usd") or 0)
        t.cost += t.last_cost
        t.session = obj.get("session_id") or t.session

    def emit(self, t: Task, line: str, fh=None) -> None:
        t.lines.append(line)
        if fh:
            fh.write(line + "\n")
            fh.flush()
        if self.shown == t.name and self.view_idx is None:
            self.query_one("#log", RichLog).write(Text.from_ansi(line))

    # ---- actions
    def action_down(self) -> None:
        w = self.focused
        (w.scroll_down if isinstance(w, RichLog) else self.query_one("#table", DataTable).action_cursor_down)()

    def action_up(self) -> None:
        w = self.focused
        (w.scroll_up if isinstance(w, RichLog) else self.query_one("#table", DataTable).action_cursor_up)()

    def action_top(self) -> None:
        w = self.focused
        (w.scroll_home if isinstance(w, RichLog) else lambda: self.query_one("#table", DataTable).move_cursor(row=0))()

    def action_bottom(self) -> None:
        w = self.focused
        if isinstance(w, RichLog):
            w.scroll_end()
        else:
            tbl = self.query_one("#table", DataTable)
            tbl.move_cursor(row=tbl.row_count - 1)

    def action_focus_log(self) -> None:
        self.query_one("#log").focus()

    def action_focus_table(self) -> None:
        self.query_one("#table").focus()

    def action_run(self) -> None:
        t = self.current
        if t:
            self.request_run(t, manual=True)

    def action_cancel(self) -> None:
        t = self.current
        if t and t.status in ACTIVE:
            self.cancel_task(t)

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_escape(self) -> None:
        box = self.query_one("#filter", Input)
        if box.display:
            box.value, box.display = "", False
            self.query_one("#table").focus()
        elif self.view_idx is not None:
            self.view_idx, self.diff = None, False
            self.render_log()

    def on_input_changed(self, ev: Input.Changed) -> None:
        if ev.input.id == "filter":
            self.filter = ev.value
            self.rebuild()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id == "filter":
            self.query_one("#table").focus()

    def step_run(self, delta: int) -> None:
        t = self.current
        if not t:
            return
        files = self.run_logs(t)
        live_last = 1 if t.status in ("running", "retrying") else 0
        last = len(files) - 1 - live_last
        if self.view_idx is None:
            if delta > 0 or last < 0:
                return
            self.view_idx = last
        else:
            self.view_idx += delta
            if self.view_idx > last:
                self.view_idx, self.diff = None, False
        self.render_log()

    def action_prev_run(self) -> None:
        self.step_run(-1)

    def action_next_run(self) -> None:
        self.step_run(1)

    def action_diff(self) -> None:
        t = self.current
        if not t:
            return
        if self.view_idx is None:
            self.step_run(-1)
        if self.view_idx is not None:
            self.diff = not self.diff
            self.render_log()

    def action_yank(self) -> None:
        t = self.current
        if t:
            text = f"claude --resume {t.session}" if t.session else (t.last_log or "")
            self.copy_to_clipboard(text)
            self.notify(f"copied: {text}")

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("Run all tasks", "Queue every unpaused task", self.run_all)
        yield SystemCommand("Pause all tasks", "Pause every task", lambda: self.pause_all(True))
        yield SystemCommand("Resume all tasks", "Unpause every task", lambda: self.pause_all(False))
        for t in self.tasks:
            yield SystemCommand(f"Run: {t.name}", t.cmd, lambda t=t: self.request_run(t, manual=True))

    def run_all(self) -> None:
        for t in self.tasks:
            if not t.paused:
                self.request_run(t, manual=True)

    def pause_all(self, paused: bool) -> None:
        for t in self.tasks:
            t.paused = paused
            if not paused:
                t.schedule()
        save_tasks(self.tasks)
        self.cfg_mtime = core.CONFIG.stat().st_mtime

    def action_clear_log(self) -> None:
        t = self.current
        if t:
            t.lines.clear()
            self.query_one("#log", RichLog).clear()

    def action_toggle_theme(self) -> None:
        self.theme = "textual-dark" if self.theme == "textual-light" else "textual-light"

    def action_quit(self) -> None:
        n = sum(t.status == "running" for t in self.tasks)
        msg = "Quit cadence?" + (f"\n{n} running task(s) will be cancelled." if n else "")

        def done(yes: bool) -> None:
            if not yes:
                return
            save_state(self.tasks)
            for t in self.tasks:
                if t.status in ACTIVE:
                    self.cancel_task(t)
            self.exit()

        self.push_screen(Confirm(msg), done)

    def action_pause(self) -> None:
        t = self.current
        if t:
            t.paused = not t.paused
            save_tasks(self.tasks)
            self.cfg_mtime = core.CONFIG.stat().st_mtime
            if not t.paused:
                t.schedule()

    def action_help(self) -> None:
        self.push_screen(Help())

    def edit_form(self, task: Task | None, focus: str = "name") -> None:
        def done(res) -> None:
            if not res:
                return
            name, cmd, trig, opts = res
            if task is None:
                if any(x.name == name for x in self.tasks):
                    self.notify(f"'{name}' exists", severity="error")
                    return
                self.tasks.append(Task(name, cmd, trig, opts=opts))
                self.tasks[-1].schedule()
            else:
                task.name, task.cmd, task.trigger, task.opts, task.sig = name, cmd, trig, opts, None
                task.schedule()
            save_tasks(self.tasks)
            self.cfg_mtime = core.CONFIG.stat().st_mtime
            self.rebuild()

        self.push_screen(TaskForm(task, focus), done)

    def action_add(self) -> None:
        self.edit_form(None)

    def action_edit(self) -> None:
        if self.current:
            self.edit_form(self.current)

    def action_edit_trigger(self) -> None:
        if self.current:
            self.edit_form(self.current, "trigger")

    def action_delete(self) -> None:
        t = self.current
        if not t:
            return

        def done(yes: bool) -> None:
            if yes:
                if t.status in ACTIVE:
                    self.cancel_task(t)
                self.tasks.remove(t)
                save_tasks(self.tasks)
                self.cfg_mtime = core.CONFIG.stat().st_mtime
                self.rebuild()

        self.push_screen(Confirm(f"Delete '{t.name}'?"), done)
