import json
import time
from dataclasses import replace

import httpx
import numpy as np
import pandas as pd
import pytest
import respx
from conftest import FakeMarket, book_at, symbol_info

from smart_trader.api import BinanceAPI
from smart_trader.config import DEMO_URL, dec
from smart_trader.db import encode
from smart_trader.execution import Execution
from smart_trader.models import Rejected, Uncertain
from smart_trader.runtime import ProcessLock, Trader


def test_position_sizing_costs_and_caps(risk, signal, rules, cfg):
    p = risk.approve(signal, rules, book_at(), "PAPER", {})
    assert p.risk <= dec(50)
    assert p.qty*p.entry <= dec(2500)
    assert p.qty % rules.step == 0
    listing = replace(signal, strategy="new_listings")
    p = risk.approve(listing, rules, book_at(), "PAPER", {})
    assert p.risk <= dec(20)
    assert p.qty*p.entry <= dec(500)


@pytest.mark.parametrize("case", ["kill", "stale", "wide_spread", "depth", "day", "week", "rr", "pause", "duplicate", "future"])
def test_risk_rejections(case, db, risk, signal, rules):
    book = book_at()
    if case == "kill":
        db.set("kill", True)
    elif case == "stale":
        book = replace(book, received=time.time()-100)
    elif case == "wide_spread":
        book = replace(book, asks=[(dec(101), dec(100_000))])
    elif case == "depth":
        book = book_at(100, 1)
    elif case == "day":
        db.set("equity", "9799")
    elif case == "week":
        db.set("equity", "9499")
        db.set("day_anchor", {"key": "test", "equity": "9499"})
    elif case == "rr":
        signal = replace(signal, target=dec(104))
    elif case == "pause":
        for _ in range(3):
            risk.closed_trade(dec(-1))
    elif case == "duplicate":
        db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?)", (signal.key, signal.symbol, signal.strategy,
                                                               "PAPER", "CLOSED", "{}", time.time()))
    elif case == "future":
        signal = replace(signal, candle_ms=int(time.time()*1000)+10000)
    with pytest.raises(Rejected):
        risk.approve(signal, rules, book, "PAPER", {})


def test_risk_errors_halt(db, risk, signal, rules):
    db.set("risk_overrides", {"listing_risk": .5})
    with pytest.raises(ValueError):
        risk.approve(signal, rules, book_at(), "PAPER", {})
    assert db.get("kill")


def test_correlation_enforced(db, risk, signal, rules):
    db.execute("INSERT INTO intents VALUES('held','BTCUSDT','momentum','PAPER','OPEN','{}',0)")
    db.save_position({"id": "held", "symbol": "BTCUSDT", "strategy": "momentum", "venue": "PAPER",
                      "qty": "1", "entry": "100", "mark": "100"})
    with pytest.raises(Rejected, match="Correlation data"):
        risk.approve(signal, rules, book_at(), "PAPER", {})
    returns = pd.Series(np.arange(60, dtype=float))
    with pytest.raises(Rejected, match="Correlated"):
        risk.approve(signal, rules, book_at(), "PAPER", {"BTCUSDT": returns, "ETHUSDT": returns})


async def test_paper_full_lifecycle_and_no_duplicate(db, cfg, risk, signal):
    market = FakeMarket()
    execution = Execution(db, cfg, risk, market)
    await execution.enter(signal, {})
    assert len(db.positions()) == 1
    with pytest.raises(Rejected):
        await execution.enter(signal, {})
    market.current = book_at(111)
    await execution.monitor()
    assert not db.positions()
    trades = db.rows("SELECT * FROM trades")
    assert len(trades) == 1
    t = json.loads(trades[0]["data"])
    assert dec(t["pnl"]) > 0
    assert t["exit_reason"] == "target"
    assert t["venue"] == "PAPER"
    assert dec(db.get("cash")) == dec(10000)+dec(t["pnl"])
    with pytest.raises(Rejected, match="consumed"):
        await execution.enter(signal, {})


async def test_paper_partial_fill_restart_and_kill_flatten(db, cfg, risk, signal):
    market = FakeMarket(book_at(100, 100))
    execution = Execution(db, cfg, risk, market)
    await execution.enter(signal, {})
    assert dec(db.positions()[0]["qty"]) == dec(5)
    db.halt("test")
    restored = Execution(db, cfg, risk, market)
    market.current = book_at(99, 50)
    await restored.monitor(flatten=True)
    assert dec(db.positions()[0]["qty"]) == dec("2.5")
    market.current = book_at(99)
    await restored.monitor(flatten=True)
    assert not db.positions()
    assert len(db.rows("SELECT * FROM trades")) == 1


def test_ledger_transaction_rollback(db):
    original = db.get("cash")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.set("cash", "0")
            raise RuntimeError("crash")
    assert db.get("cash") == original


def test_single_writer_lock(tmp_path):
    with ProcessLock(str(tmp_path/"worker.lock")):
        with pytest.raises(OSError):
            with ProcessLock(str(tmp_path/"worker.lock")):
                pytest.fail("Second worker must not acquire lock")


async def test_demo_missing_keys_falls_back(cfg):
    from smart_trader.config import Mode
    trader = Trader(replace(cfg, mode=Mode.DEMO))
    try:
        assert trader.db.get("mode") == "PAPER"
        assert trader.demo is None
        assert "switched to PAPER" in trader.db.rows("SELECT * FROM events")[0]["message"]
    finally:
        await trader.public.close()
        trader.db.close()


@respx.mock
async def test_unknown_post_never_replayed_after_restart(db, cfg, risk, signal, rules):
    plan = risk.approve(signal, rules, book_at(), "DEMO", {})
    db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?)", (signal.key, signal.symbol, signal.strategy,
                                                           "DEMO", "ENTERING", encode(plan), time.time()))
    route = respx.post(DEMO_URL+"/v3/order").mock(side_effect=httpx.ReadTimeout("lost response"))
    api = BinanceAPI(demo=True, key="demo", secret="secret")
    execution = Execution(db, cfg, risk, FakeMarket(), api)
    try:
        with pytest.raises(Uncertain):
            await execution._submit(signal.key, "b", "order", {"symbol": signal.symbol, "side": "BUY", "type": "MARKET", "quantity": str(plan.qty)})
        assert db.get("kill")
        assert route.call_count == 1
        respx.get(DEMO_URL+"/v3/order").respond(400, json={"code": -2013})
        restored = Execution(db, cfg, risk, FakeMarket(), api)
        with pytest.raises(Uncertain):
            await restored._submit(signal.key, "b", "order", {"symbol": signal.symbol})
        assert route.call_count == 1
    finally:
        await api.close()


@respx.mock
async def test_base_asset_commission_not_oversold(db, cfg, risk):
    api = BinanceAPI(demo=True, key="demo", secret="secret")
    execution = Execution(db, cfg, risk, FakeMarket(), api)
    respx.get(DEMO_URL+"/v3/myTrades").respond(200, json=[{
        "qty": "1", "price": "100", "quoteQty": "100", "commission": ".001", "commissionAsset": "ETH"}])
    try:
        from smart_trader.filters import SymbolRules
        fill = await execution._fills({"orderId": 1, "executedQty": "1"}, SymbolRules(symbol_info()))
        assert fill["qty"]-fill["base_fee"] == dec(".999")
        assert fill["cash_fee"] == 0
        assert fill["fee"] == dec(".1")
    finally:
        await api.close()


class DemoExchange:
    """Stateful simulated API. Prices and fills here are explicitly test fixtures."""
    def __init__(self, qty):
        self.qty = str(qty)
        self.oco = False
        self.stop = "96.00"
        self.target = "110.00"
        self.sells = 0

    def handle(self, request):
        path, method = request.url.path, request.method
        params = dict(request.url.params)
        if path.endswith("/order") and method == "GET":
            cid = params.get("origClientOrderId", "")
            if cid.endswith("_b") or params.get("orderId") == "1":
                return httpx.Response(200, json={"orderId": 1, "status": "FILLED", "executedQty": self.qty})
            role = cid.split("_")[-1]
            if role in ("tp", "sl"):
                return httpx.Response(200, json={"orderId": 2 if role == "tp" else 3, "status": "NEW", "side": "SELL", "symbol": "ETHUSDT",
                   "type": "TAKE_PROFIT" if role == "tp" else "STOP_LOSS",
                   "origQty": self.qty, "executedQty": "0", "stopPrice": self.target if role == "tp" else self.stop})
        if path.endswith("/myTrades"):
            return httpx.Response(200, json=[{"qty": self.qty, "quoteQty": str(dec(self.qty)*100), "price": "100", "commission": "0", "commissionAsset": "USDT"}])
        if path.endswith("/avgPrice"):
            return httpx.Response(200, json={"price": "100", "mins": 5})
        if path.endswith("/orderList/oco") and method == "POST":
            assert params["aboveType"] == "TAKE_PROFIT"
            assert params["belowType"] == "STOP_LOSS"
            self.oco = True
            self.qty = params["quantity"]
            return httpx.Response(200, json={"listOrderStatus": "EXECUTING", "contingencyType": "OCO"})
        if path.endswith("/orderList"):
            assert self.oco
            return httpx.Response(200, json={"listOrderStatus": "EXECUTING", "contingencyType": "OCO"})
        raise AssertionError(f"Unmocked exchange call: {method} {path}")


async def test_restart_after_buy_before_oco_installs_and_confirms_protection(db, cfg, risk, signal, rules):
    plan = risk.approve(signal, rules, book_at(), "DEMO", {})
    db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?)", (signal.key, signal.symbol, signal.strategy,
                                                           "DEMO", "ENTERING", encode(plan), time.time()))
    db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?)", (Execution.client_id(signal.key, "b"), signal.key,
                                                         "b", "UNKNOWN", "{}", None))
    exchange = DemoExchange(plan.qty)
    api = BinanceAPI(demo=True, key="demo", secret="secret", transport=httpx.MockTransport(exchange.handle))
    api.min_interval = 0
    execution = Execution(db, cfg, risk, FakeMarket(), api)
    try:
        await execution._recover_entry(plan, rules)
        assert exchange.oco
        assert db.rows("SELECT status FROM intents")[0]["status"] == "OPEN"
        assert len(db.positions()) == 1
        cash = db.get("cash")
        await execution._recover_entry(plan, rules)
        assert len(db.positions()) == 1
        assert db.get("cash") == cash
    finally:
        await api.close()
