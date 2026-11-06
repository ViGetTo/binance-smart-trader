import asyncio
import json
from pathlib import Path

import pytest
from conftest import FakeMarket
from streamlit.testing.v1 import AppTest

from smart_trader.analytics import database_report
from smart_trader.config import dec
from smart_trader.market import ListingsRegistry, MarketData


def test_streamlit_renders_and_kill_is_persistent(tmp_path, monkeypatch):
    path = str(tmp_path/"dashboard.sqlite")
    monkeypatch.setenv("TRADER_DB", path)
    monkeypatch.setenv("TRADER_MODE", "PAPER")
    app = AppTest.from_file(Path(__file__).parents[1]/"dashboard.py", default_timeout=15).run()
    assert not app.exception
    assert app.title[0].value == "Binance Smart Trader"
    app.button[0].click().run()
    assert not app.exception
    from smart_trader.db import Database
    db = Database(path)
    assert db.get("kill") is True
    db.close()


async def test_websocket_reconnect_and_restore_status(db, cfg, monkeypatch):
    connections = []
    updated = asyncio.Event()

    class Socket:
        sent = False

        async def recv(self):
            if not self.sent:
                self.sent = True
                return json.dumps([{"s": "BTCUSDT", "c": "100"}])
            updated.set()
            await asyncio.Future()

    class Connection:
        async def __aenter__(self):
            connections.append(1)
            if len(connections) == 1:
                raise ConnectionError("fixture disconnect")
            return Socket()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("smart_trader.market.connect", lambda *args, **kwargs: Connection())
    market = MarketData(None, db, cfg.strategy)
    task = asyncio.create_task(market.stream())
    try:
        await asyncio.wait_for(updated.wait(), 3)
        assert len(connections) == 2
        assert db.get("ws_status")["ok"]
        assert db.get("stream_tickers")["BTCUSDT"] == "100"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_first_seen_is_not_listing_date(db):
    class API:
        async def get(self, *args, **kwargs):
            return [[1_500_000_000_000]]

    market = FakeMarket(api=API())
    registry = ListingsRegistry(db, market)
    await registry.update()
    row = db.rows("SELECT * FROM symbols")[0]
    assert row["first_seen"]*1000-row["earliest_ms"] > 365*86_400_000
    assert registry.ages()["ETHUSDT"] > 7*24


def test_strategy_pnl_attribution_sums_to_portfolio(db, risk):
    db.execute("INSERT INTO intents VALUES('held','ETHUSDT','momentum','PAPER','OPEN','{}',0)")
    db.save_position({"id": "held", "symbol": "ETHUSDT", "strategy": "momentum", "venue": "PAPER",
                      "qty": "10", "entry": "100", "mark": "105", "cost": "1001", "proceeds": "0"})
    db.set("cash", "8999")
    risk.observe(dec(10049))
    report = database_report(db)
    assert report["by_strategy"]["momentum"]["net_pnl"] == 49
    assert report["by_strategy"]["new_listings"]["net_pnl"] == 0
    assert sum(s["net_pnl"] for s in report["by_strategy"].values()) == report["net_pnl"]
