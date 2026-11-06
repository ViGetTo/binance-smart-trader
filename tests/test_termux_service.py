import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from smart_trader.service import control, supervise, unresolved

ROOT = Path(__file__).parents[1]


def pending(db):
    db.execute("INSERT INTO intents VALUES('pending','ETHUSDT','momentum','PAPER','ENTERING','{}',0)")


def test_service_controls_do_not_resume_implicitly(db):
    assert control(db, "status")["trader_heartbeat_age_seconds"] is None
    control(db, "pause")
    assert db.get("kill")
    assert not db.get("flatten", False)
    control(db, "flatten")
    assert db.get("flatten")
    control(db, "stop")
    assert db.get("service_stop")
    control(db, "prepare")
    assert not db.get("service_stop")
    assert db.get("kill")  # A new start must preserve risk/kill controls.
    control(db, "resume")
    assert db.get("resume_requested")


def test_stop_with_uncertain_entry_cannot_be_cleared(db):
    pending(db)
    control(db, "stop")
    assert unresolved(db)
    control(db, "prepare")
    assert db.get("service_stop")
    with pytest.raises(ValueError):
        control(db, "resume")


class Process:
    def __init__(self, args):
        self.args = args
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode


def test_supervisor_keeps_monitor_until_execution_is_resolved(db, tmp_path):
    processes = []
    ticks = []
    pending(db)

    def launch(args, **kwargs):
        assert kwargs["start_new_session"]
        process = Process(args)
        processes.append(process)
        return process

    def advance(seconds):
        ticks.append(seconds)
        if len(ticks) == 1:
            control(db, "stop")
        elif len(ticks) == 2:
            assert not any(p.signals for p in processes)
            db.execute("UPDATE intents SET status='CLOSED' WHERE id='pending'")
        else:
            pytest.fail("Supervisor failed to stop after confirmed flat state")

    supervise(tmp_path, db, popen=launch, sleep=advance)
    assert len(processes) == 2
    assert all(p.signals == [signal.SIGINT] for p in processes)
    assert db.get("service_pid") is None


def test_stop_received_before_first_spawn_is_not_lost(db, tmp_path):
    control(db, "stop")
    supervise(tmp_path, db, popen=lambda *a, **k: pytest.fail("No process should start"))


def test_failed_trader_restarts_without_stopping_dashboard(db, tmp_path, monkeypatch):
    processes = []
    clock = [1000.]
    monkeypatch.setattr("smart_trader.service.time.time", lambda: clock[0])

    def launch(args, **kwargs):
        process = Process(args)
        processes.append(process)
        return process

    def advance(seconds):
        clock[0] += seconds
        if clock[0] == 1001:
            processes[0].returncode = 2
        if len(processes) == 3:
            assert processes[1].args[3] == "streamlit"
            assert not processes[1].signals
            control(db, "stop")
        if clock[0] > 1020:
            pytest.fail("Restart backoff did not recover")

    supervise(tmp_path, db, popen=launch, sleep=advance)
    assert len(processes) == 3
    assert db.get("service_last_exit")["code"] == 2


@pytest.fixture
def termux(tmp_path):
    project = tmp_path/"repo with spaces $(touch BAD_MARKER)"
    project.mkdir()
    (project/"pyproject.toml").write_text("# fixture")
    scripts = project/"scripts"
    scripts.mkdir()
    for name in ("termux-install.sh", "trade-termux.sh", "ubuntu-install.sh"):
        shutil.copy(ROOT/"scripts"/name, scripts/name)
    prefix = tmp_path/"prefix"
    commands = prefix/"bin"
    commands.mkdir(parents=True)
    calls = tmp_path/"calls.jsonl"
    session = tmp_path/"session"
    config = tmp_path/"config"
    (config/"binance-smart-trader").mkdir(parents=True)
    (config/"binance-smart-trader/project-path").write_text(str(project)+"\n")
    fake = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TRADER_TEST_CALLS'],'a') as f:
    f.write(json.dumps([name,args])+'\\n')
session=Path(os.environ['TRADER_TEST_SESSION'])
if name == 'tmux':
    if args[0] == 'has-session': sys.exit(0 if session.exists() else 1)
    if args[0] == 'new-session': session.touch()
if name == 'proot-distro':
    if args[:2] == ['install','--help']: print('--name NAME')
    elif '/bin/true' in args: sys.exit(1)
    elif args[-1:] == ['stop']: session.unlink(missing_ok=True)
'''
    for command in ("pkg", "proot-distro", "tmux", "termux-wake-lock", "termux-wake-unlock", "sleep"):
        executable = commands/command
        executable.write_text(fake)
        executable.chmod(0o700)
    environment = dict(os.environ, PREFIX=str(prefix), XDG_CONFIG_HOME=str(config),
                       PATH=str(commands)+os.pathsep+os.environ["PATH"],
                       TRADER_TEST_CALLS=str(calls), TRADER_TEST_SESSION=str(session))
    return project, environment, calls, session


def shell(termux, name, *args):
    project, env, _, _ = termux
    return subprocess.run(["bash", str(project/"scripts"/name), *args], env=env,
                          cwd=project, capture_output=True, text=True, timeout=15)


def calls(termux):
    return [json.loads(line) for line in termux[2].read_text().splitlines()]


def test_termux_install_creates_command_and_separate_distro(termux):
    result = shell(termux, "termux-install.sh")
    assert result.returncode == 0, result.stderr
    entries = calls(termux)
    assert ["proot-distro", ["install", "ubuntu:24.04", "--name", "smart-trader"]] in entries
    assert Path(termux[1]["PREFIX"], "bin/trade").is_file()
    assert Path(termux[1]["PREFIX"], "bin/trade").read_text().splitlines()[0] == "#!"+termux[1]["PREFIX"]+"/bin/bash"
    assert "Установка завершена" in result.stdout


def test_trade_starts_once_and_quotes_project_path(termux):
    assert shell(termux, "trade-termux.sh").returncode == 0
    assert shell(termux, "trade-termux.sh").returncode == 0
    entries = calls(termux)
    launches = [args for name, args in entries if name == "tmux" and args[0] == "new-session"]
    assert len(launches) == 1
    # Execute tmux's actual shell string against the mocked proot command.
    result = subprocess.run(["bash", "-c", launches[0][-1]], env=termux[1], cwd=termux[0], timeout=10)
    assert result.returncode == 0
    assert not (termux[0]/"BAD_MARKER").exists()
    proot = [args for name, args in calls(termux) if name == "proot-distro" and args[-1] == "run"][-1]
    assert str(termux[0])+":/opt/binance-smart-trader" in proot


def test_trade_stop_uses_persistent_control_without_killing_tmux(termux):
    termux[3].touch()
    result = shell(termux, "trade-termux.sh", "stop")
    assert result.returncode == 0, result.stderr
    entries = calls(termux)
    assert any(name == "proot-distro" and args[-1] == "stop" for name, args in entries)
    assert not any(name == "tmux" and args[0] == "kill-session" for name, args in entries)


def test_trade_update_refuses_running_session(termux):
    termux[3].touch()
    result = shell(termux, "trade-termux.sh", "update")
    assert result.returncode == 2
    assert "trade stop" in result.stderr


def test_unknown_trade_action_cannot_be_executed(termux):
    result = shell(termux, "trade-termux.sh", "run; touch BAD_MARKER")
    assert result.returncode == 2
    assert not (termux[0]/"BAD_MARKER").exists()
