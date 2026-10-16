import asyncio
import time

import pytest

from cadence import core
from cadence.cli import SAMPLE, main

from conftest import write_config


def test_parse_interval():
    assert core.parse_interval("90s") == 90
    assert core.parse_interval("2h") == 7200


def test_parse_trigger_roundtrip():
    for text in ("every 1h", "git origin/main", "files app/**/*.php", "after work tickets", "manual"):
        assert core.fmt_trigger(core.parse_trigger(text)) == text


@pytest.mark.parametrize("bad", ["cron nope", "every", "bogus x", "every 5x"])
def test_parse_trigger_rejects(bad):
    with pytest.raises((ValueError, KeyError)):
        core.parse_trigger(bad)


def test_parse_opts():
    assert core.parse_opts("timeout=10m retry=2 overlap=queue") == {"timeout": "10m", "retry": 2, "overlap": "queue"}
    for bad in ("overlap=nope", "bogus=1", "timeout=abc"):
        with pytest.raises((ValueError, KeyError)):
            core.parse_opts(bad)


def test_slug():
    assert core.slug("work tickets/x") == "work-tickets-x"


def test_signature_detects_change(project):
    cmd = {"on_change": "echo $RANDOM$RANDOM$RANDOM"}
    t = core.Task("c", "true", cmd)
    assert asyncio.run(core.signature(t)) != asyncio.run(core.signature(t))
    stable = core.Task("s", "true", {"on_change": "echo x"})
    assert asyncio.run(core.signature(stable)) == asyncio.run(core.signature(stable))


def test_files_signature_tracks_mtime(project):
    f = project / "a.txt"
    f.write_text("1")
    t = core.Task("f", "true", {"files": "*.txt"})
    before = asyncio.run(core.signature(t))
    time.sleep(0.01)
    f.write_text("2")
    assert asyncio.run(core.signature(t)) != before


def test_config_roundtrip_keeps_options(project):
    write_config(project, """
[cadence]
max_parallel = 2

[[task]]
name = "a"
cmd = "echo hi"
trigger = { every = "1h" }
timeout = "5m"
retry = 2
""")
    tasks = core.load_tasks()
    assert core.SETTINGS["max_parallel"] == 2
    assert tasks[0].opts == {"timeout": "5m", "retry": 2}
    core.save_tasks(tasks)
    again = core.load_tasks()
    assert again[0].opts == {"timeout": "5m", "retry": 2}
    assert core.SETTINGS["max_parallel"] == 2


def test_state_roundtrip_and_status_line(project):
    write_config(project, SAMPLE)
    tasks = core.load_tasks()
    tasks[0].runs, tasks[0].status = 3, "ok"
    core.save_state(tasks)
    fresh = core.load_tasks()
    core.apply_state(fresh, core.read_state())
    assert fresh[0].runs == 3 and fresh[0].status == "ok"
    assert core.status_line().startswith("cadence: ")
    assert "ok" in core.status_line()


def test_status_line_off_without_state(project):
    assert core.status_line() == "cadence: off"


def test_init_and_find_root(tmp_path, capsys):
    assert main(["init", "-C", str(tmp_path)]) == 0
    assert main(["init", "-C", str(tmp_path)]) == 1
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert core.find_root(sub) == tmp_path
    assert main(["status", "-C", str(tmp_path)]) == 0
    assert "cadence:" in capsys.readouterr().out


def test_claude_view():
    from cadence.core import claude_view
    assert claude_view("plain") == "plain"
    assert claude_view('{"type":"system"}') is None
    assert claude_view('{"type":"assistant","message":{"content":[{"type":"text","text":"hi"},{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}') == "hi\n▸ Bash ls"
    assert "$0.042" in claude_view('{"type":"result","total_cost_usd":0.042,"num_turns":3}')
