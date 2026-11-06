import subprocess
from pathlib import Path

import pytest

from smart_trader.updates import update


class GitFixture:
    def __init__(self, *, target="new-sha", dirty="", fail=None, remote=None):
        self.target, self.dirty, self.fail = target, dirty, fail
        self.remote = remote or "https://github.com/ViGetTo/binance-smart-trader.git"
        self.calls = []

    def __call__(self, args, root, timeout=20):
        self.calls.append(args)
        if self.fail and self.fail in args:
            raise subprocess.CalledProcessError(1, args)
        if args[:3] == ["git", "remote", "get-url"]:
            return self.remote
        if args[:2] == ["git", "branch"]:
            return "main"
        if args[:2] == ["git", "rev-parse"]:
            return self.target if args[-1] != "HEAD" else "old-sha"
        if args[:2] == ["git", "status"]:
            return self.dirty
        return ""


def test_update_checks_before_install_and_preserves_env_and_ledgers(cfg, tmp_path):
    (tmp_path/".env").write_text("LOCAL_SECRET=fixture-only")
    ledger = Path(cfg.db_path)
    original = b"do not replace local ledger"
    ledger.write_bytes(original)
    # Active check is independent here: no service database exists at this path.
    from dataclasses import replace
    cfg = replace(cfg, db_path=str(tmp_path/"service.sqlite"))
    git = GitFixture()
    state, _ = update(tmp_path, cfg, runner=git)
    assert state == "updated"
    assert (tmp_path/".env").read_text() == "LOCAL_SECRET=fixture-only"
    assert ledger.read_bytes() == original
    commands = git.calls
    assert next(i for i, c in enumerate(commands) if "fetch" in c) < next(i for i, c in enumerate(commands) if "install" in c)
    assert ["git", "merge", "--ff-only", "new-sha"] in commands
    assert not any("reset" in c or "stash" in c or "clean" in c for c in commands)
    assert not (tmp_path/"data/update-install-pending").exists()


@pytest.mark.parametrize("busy,dirty,expected", [(True, "", "deferred"), (False, " M config.toml", "skipped")])
def test_update_never_installs_while_busy_or_overwrites_local_edits(cfg, tmp_path, busy, dirty, expected):
    git = GitFixture(dirty=dirty)
    assert update(tmp_path, cfg, busy=busy, runner=git)[0] == expected
    assert not any("merge" in c or "install" in c for c in git.calls)


def test_current_version_does_not_reinstall(cfg, tmp_path):
    git = GitFixture(target="old-sha")
    assert update(tmp_path, cfg, runner=git)[0] == "current"
    assert not any("install" in c for c in git.calls)


def test_network_failure_keeps_current_version(cfg, tmp_path):
    git = GitFixture(fail="fetch")
    assert update(tmp_path, cfg, runner=git)[0] == "unavailable"
    assert not any("merge" in c for c in git.calls)


def test_install_failure_blocks_start_until_retried(cfg, tmp_path):
    git = GitFixture(fail="install")
    assert update(tmp_path, cfg, runner=git)[0] == "install_failed"
    marker = tmp_path/"data/update-install-pending"
    assert marker.read_text() == "new-sha"
    # Next launch must retry dependencies even if Git is already at the new commit.
    git = GitFixture(target="old-sha")
    assert update(tmp_path, cfg, runner=git)[0] == "updated"
    assert any("install" in c for c in git.calls)
    assert not marker.exists()


def test_unexpected_remote_cannot_supply_auto_update(cfg, tmp_path):
    git = GitFixture(remote="https://example.com/other/repo.git")
    assert update(tmp_path, cfg, runner=git)[0] == "skipped"
    assert not any("fetch" in c for c in git.calls)


def test_config_can_disable_auto_update(cfg, tmp_path):
    from dataclasses import replace
    git = GitFixture()
    assert update(tmp_path, replace(cfg, auto_update=False), runner=git)[0] == "disabled"
    assert not git.calls
    assert update(tmp_path, replace(cfg, auto_update=False), force=True, runner=git)[0] == "updated"


def test_update_is_deferred_for_unresolved_account_even_when_service_died(cfg, tmp_path):
    from dataclasses import replace

    from smart_trader.lab import open_accounts
    lab = replace(cfg, paper_lab=True)
    accounts = open_accounts(lab)
    accounts[2][1].execute("INSERT INTO intents VALUES('pending','ETHUSDT','momentum','PAPER','ENTERING','{}',0)")
    for _, db in accounts:
        db.close()
    git = GitFixture()
    assert update(tmp_path, lab, runner=git)[0] == "deferred"
    assert not any("merge" in c or "install" in c for c in git.calls)


def test_real_git_fast_forward_preserves_local_settings(cfg, tmp_path, monkeypatch):
    """Exercise Git itself with an isolated local remote; only pip is substituted."""
    from smart_trader import updates
    remote, author, checkout = (tmp_path/name for name in ("remote.git", "author", "checkout"))

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()

    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(author))
    git(author, "config", "user.name", "QA Fixture")
    git(author, "config", "user.email", "qa@example.invalid")
    (author/".gitignore").write_text(".env\ndata/\n")
    (author/"source.txt").write_text("version one")
    git(author, "add", ".")
    git(author, "commit", "-m", "Fixture base")
    git(author, "remote", "add", "origin", str(remote))
    git(author, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "-b", "main", str(remote), str(checkout))
    (checkout/".env").write_text("TOKEN=local-fixture-only")
    (author/"source.txt").write_text("version two")
    git(author, "commit", "-am", "Fixture update")
    git(author, "push")
    monkeypatch.setattr(updates, "OFFICIAL_REMOTES", {str(remote)})
    installs = []

    def runner(args, root, timeout=20):
        if args[0] == "git":
            return updates.run(args, root, timeout)
        installs.append(args)
        return ""

    assert update(checkout, cfg, runner=runner)[0] == "updated"
    assert (checkout/"source.txt").read_text() == "version two"
    assert (checkout/".env").read_text() == "TOKEN=local-fixture-only"
    assert git(checkout, "rev-parse", "HEAD") == git(author, "rev-parse", "HEAD")
    assert len(installs) == 2
