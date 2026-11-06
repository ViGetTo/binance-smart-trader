from dataclasses import replace

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from smart_trader.config import Mode, Settings
from smart_trader.db import Database
from smart_trader.lab import open_accounts
from smart_trader.setup import enable_paper, switch_to_paper, write_env
from smart_trader.terminal import TraderApp, snapshot


def test_saved_paper_beats_inherited_demo_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADER_MODE", "DEMO")
    monkeypatch.setenv("TRADER_DB", "data/old-demo.sqlite")
    monkeypatch.setenv("TRADER_PAPER_LAB", "false")
    monkeypatch.setenv("BINANCE_ENABLED", "true")
    monkeypatch.setenv("BINANCE_MARKET_DATA_ENABLED", "false")
    # This is the exact unmarked choice written by v0.3's config wizard.
    write_env(".env", {"TRADER_MODE": "PAPER", "TRADER_DB": "data/paper-lab.sqlite",
                       "TRADER_PAPER_LAB": "true", "BINANCE_ENABLED": "false",
                       "BINANCE_MARKET_DATA_ENABLED": "true", "PAPER_INITIAL_BALANCE": "4900"})
    cfg = Settings.load()
    assert cfg.mode == Mode.PAPER and cfg.paper_lab and not cfg.binance_enabled
    assert cfg.market_data_enabled and cfg.db_path == "data/paper-lab.sqlite"
    assert cfg.risk.initial_balance == 4900
    import os
    assert os.environ["TRADER_MODE"] == "DEMO"  # No mutation of the caller's environment.


def test_marked_demo_choice_also_survives_inherited_paper(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADER_MODE", "PAPER")
    write_env(".env", {"TRADER_PROFILE_SOURCE": "local", "TRADER_MODE": "DEMO",
                       "TRADER_DB": "data/selected-demo.sqlite", "BINANCE_ENABLED": "true"})
    assert Settings.load().mode == Mode.DEMO


def test_paper_recovery_from_stopped_demo_without_keys_or_menu(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADER_MODE", "DEMO")
    monkeypatch.setenv("TRADER_DB", cfg.db_path)
    old = Database(cfg.db_path)
    old.set("mode", "DEMO")
    old.halt("Termux user command: stop")
    old.set("service_stop", True)
    old.close()
    selected = switch_to_paper(replace(cfg, mode=Mode.DEMO))
    assert selected.mode == Mode.PAPER and selected.paper_lab and not selected.binance_enabled
    assert selected.db_path != cfg.db_path
    accounts = open_accounts(selected)
    try:
        assert len(accounts) == 3
        assert all(snapshot(db, c)["mode"] == "PAPER" and snapshot(db, c)["equity"] == 4900 for c, db in accounts)
        assert all(not db.get("kill") for _, db in accounts)
    finally:
        for _, db in accounts:
            db.close()
    old = Database(cfg.db_path)
    assert old.get("mode") == "DEMO" and old.get("initial") == "10000"
    old.close()


def test_recovery_refuses_to_abandon_uncertain_demo_order(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_env(".env", {"TRADER_MODE": "DEMO", "TRADER_DB": cfg.db_path})
    old = Database(cfg.db_path)
    old.execute("INSERT INTO intents VALUES('pending','ETHUSDT','momentum','DEMO','ENTERING','{}',0)")
    with pytest.raises(ValueError, match="открытые позиции"):
        switch_to_paper(replace(cfg, mode=Mode.DEMO))
    assert Settings.load().mode == Mode.DEMO
    assert old.rows("SELECT id FROM intents")[0]["id"] == "pending"
    old.close()


def test_recovery_requests_resume_only_for_normal_stop(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    enable_paper(cfg)
    settings = Settings.load()
    accounts = open_accounts(settings)
    for index, (_, db) in enumerate(accounts):
        db.halt("Termux user command: stop" if index == 0 else "Risk Manager error: entries disabled")
        db.set("service_stop", True)
        db.close()
    switch_to_paper(settings)
    accounts = open_accounts(settings)
    try:
        assert accounts[0][1].get("resume_requested")
        assert not accounts[1][1].get("resume_requested", False)
        assert accounts[1][1].get("kill_reason") == "Risk Manager error: entries disabled"
    finally:
        for _, db in accounts:
            db.close()


@pytest.mark.parametrize("size", [(40, 30), (100, 36)])
async def test_open_demo_screen_rebinds_to_paper_with_new_tables(cfg, size):
    demo = replace(cfg, mode=Mode.DEMO)
    fresh = replace(cfg, db_path=cfg.db_path+"-paper", paper_lab=True,
                    risk=replace(cfg.risk, initial_balance=4900))
    app = TraderApp(demo)
    app.db.set("mode", "DEMO")
    app.db.halt("Termux user command: stop")
    async with app.run_test(size=size) as pilot:
        assert not snapshot(app.db, demo)["verified"]
        worker = app.run_worker(app.reload_profile(fresh))
        await worker.wait()
        await pilot.pause()
        assert app.settings == fresh
        assert app.query_one("#accounts-table", DataTable).row_count == 3
        assert len(app.query_one("#accounts-table", DataTable).columns) == 11
        assert app.query_one("#strategy-table", DataTable).row_count == 3
        assert app.query_one("#tabs", TabbedContent).active == "accounts"
        assert snapshot(app.db, app.account_settings)["mode"] == "PAPER"
        assert snapshot(app.db, app.account_settings)["equity"] == 4900
        assert "Баланс Demo" not in str(app.query_one("#health", Static).render())
        await pilot.press("a")
        assert app.account_settings.account_id == "aggressive"
        worker = app.run_worker(app.reload_profile(demo))
        await worker.wait()
        await pilot.pause()
        assert not app.accounts and "DEMO" in app.sub_title
        assert snapshot(app.db, demo)["equity"] is None
        await pilot.press("q")


async def test_external_config_change_reloads_already_open_panel(cfg, monkeypatch):
    state = [replace(cfg, mode=Mode.DEMO)]
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: state[0]))
    app = TraderApp(state[0], watch_config=True)
    async with app.run_test(size=(60, 30)) as pilot:
        state[0] = replace(cfg, db_path=cfg.db_path+"-new", paper_lab=True)
        app.refresh_data()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.accounts) == 3
        assert app.query_one("#accounts-table", DataTable).row_count == 3
        assert app.settings.db_path == state[0].db_path
