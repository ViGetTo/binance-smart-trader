from dataclasses import replace

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from smart_trader.account import bind_account
from smart_trader.config import Mode
from smart_trader.terminal import TraderApp, snapshot


def test_unverified_demo_never_displays_fake_ten_thousand(db, cfg):
    value = snapshot(db, replace(cfg, mode=Mode.DEMO, demo_balance_sync=True))
    assert value["mode"] == "DEMO"
    assert not value["verified"]
    assert value["equity"] is None
    assert value["net_pnl"] is None


@pytest.mark.parametrize("size", [(40, 30), (100, 36)])
async def test_terminal_mount_tabs_pause_and_quit_keeps_monitor_state(cfg, size):
    app = TraderApp(cfg)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.query_one("#strategy-table", DataTable).row_count == 3
        await pilot.press("2")
        assert app.query_one("#tabs", TabbedContent).active == "positions"
        await pilot.press("p")
        assert app.db.get("kill")
        await pilot.press("r")
        assert app.db.get("resume_requested")
        await pilot.press("5")
        assert app.query_one("#tabs", TabbedContent).active == "strategies"
        assert not app.db.get("service_stop", False)
        await pilot.press("q")


async def test_terminal_flatten_requires_confirmation(cfg):
    app = TraderApp(cfg)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.press("f")
        await pilot.click("#no")
        assert not app.db.get("flatten", False)
        await pilot.press("f")
        await pilot.click("#yes")
        assert app.db.get("flatten")
        assert app.db.get("kill")
        assert not app.db.get("service_stop", False)


async def test_terminal_shows_verified_demo_account_balance(cfg):
    demo = replace(cfg, mode=Mode.DEMO, demo_balance_sync=True)
    app = TraderApp(demo)
    bind_account(app.db, "fixture-key", {"canTrade": True, "balances": [{"asset": "USDT", "free": "4900", "locked": "0"}]})
    async with app.run_test(size=(60, 30)) as pilot:
        await pilot.pause()
        value = snapshot(app.db, demo)
        assert value["equity"] == 4900
        assert value["account"]["free_usdt"] == "4900"
        assert value["net_pnl"] == 0
        assert app.query_one("#account", Static).size.height >= 7
