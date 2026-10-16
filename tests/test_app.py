"""Drives the real TUI headlessly with Textual's pilot."""
import asyncio

from cadence import core
from cadence.app import CadenceApp

from conftest import write_config

CONFIG = """
[cadence]
max_parallel = 1

[[task]]
name = "a"
cmd = "echo hi"
trigger = { manual = true }

[[task]]
name = "c"
cmd = "echo chained"
trigger = { after = "a" }

[[task]]
name = "f"
cmd = "echo boom; exit 3"
trigger = { manual = true }
retry = 1
retry_delay = "1s"
notify = "always"

[[task]]
name = "t"
cmd = "sleep 30"
trigger = { manual = true }
timeout = "1s"

[[task]]
name = "k"
cmd = "echo '{\\"total_cost_usd\\":0.05,\\"session_id\\":\\"abc\\"}' # claude -p"
trigger = { manual = true }

[[task]]
name = "s1"
cmd = "sleep 1"
trigger = { manual = true }

[[task]]
name = "s2"
cmd = "echo q"
trigger = { manual = true }
"""


def by_name(app, name):
    return next(t for t in app.tasks if t.name == name)


def test_engine_features(project):
    write_config(project, CONFIG)

    async def scenario():
        app = CadenceApp()
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause(0.3)

            app.request_run(by_name(app, "a"), manual=True)
            await pilot.pause(1.5)
            assert by_name(app, "c").status == "ok", "after-trigger chained"

            app.request_run(by_name(app, "f"), manual=True)
            await pilot.pause(3.5)
            f = by_name(app, "f")
            assert (f.status, f.last_rc) == ("failed", 3)
            assert core.NOTIFIED and core.NOTIFIED[-1][0] == "f"
            assert (core.LOGS / "f").glob("*.log") and f.lines.count("boom") == 2, "retried once"

            app.request_run(by_name(app, "t"), manual=True)
            await pilot.pause(3)
            assert by_name(app, "t").status == "timeout"

            app.request_run(by_name(app, "k"), manual=True)
            await pilot.pause(1.5)
            k = by_name(app, "k")
            assert (k.last_cost, k.session) == (0.05, "abc")

            app.request_run(by_name(app, "s1"), manual=True)
            app.request_run(by_name(app, "s2"), manual=True)
            await pilot.pause(0.3)
            assert (by_name(app, "s1").status, by_name(app, "s2").status) == ("running", "queued")
            await pilot.pause(3)
            assert by_name(app, "s2").status == "ok"

            app.request_run(f, manual=True)
            await pilot.pause(3.5)
            app.query_one("#table").move_cursor(row=[t.name for t in app.tasks].index("f"))
            await pilot.pause(0.3)
            await pilot.press("left_square_bracket")
            assert app.view_idx == 1
            await pilot.press("D")
            assert app.diff
            await pilot.press("escape")
            assert app.view_idx is None

            await pilot.press("slash", "k")
            await pilot.pause(0.3)
            assert app.query_one("#table").row_count == 1
            await pilot.press("escape")
            assert app.query_one("#table").row_count == len(app.tasks)

            await pilot.press("q", "y")

    asyncio.run(scenario())


def test_resume_restores_schedule(project):
    write_config(project, """
[[task]]
name = "hourly"
cmd = "echo hi"
trigger = { every = "1h" }
""")

    async def scenario():
        app = CadenceApp()
        async with app.run_test() as pilot:
            await pilot.pause(1.2)
            next_run = app.tasks[0].next_run
            await pilot.press("q", "y")
        app = CadenceApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "Confirm", "asks to resume"
            await pilot.press("y")
            await pilot.pause(1.2)
            assert abs(app.tasks[0].next_run - next_run) < 1
            await pilot.press("q", "y")

    asyncio.run(scenario())
