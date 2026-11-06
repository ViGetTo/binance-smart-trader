import json

import httpx
import pytest
from conftest import FakeMarket, symbol_info

from smart_trader.api import BinanceAPI
from smart_trader.config import dec
from smart_trader.execution import Execution
from smart_trader.models import Rejected, Uncertain


class StatefulDemo:
    def __init__(self):
        self.orders = {}
        self.qty = dec(0)
        self.oco = None
        self.fills = {}
        self.calls = []
        self.reject_protection = False
        self.lose_oco_response = False
        self.cancel_unknown = False
        self.after_test = None

    def handle(self, request):
        method, path = request.method, request.url.path.split("/v3/")[-1]
        p = dict(request.url.params)
        self.calls.append((method, path, p))
        if path == "exchangeInfo":
            return httpx.Response(200, json={"symbols": [symbol_info()], "exchangeFilters": []})
        if path == "account":
            return httpx.Response(200, json={"canTrade": True, "balances": [
                {"asset": "USDT", "free": "100000", "locked": "0"},
                {"asset": "ETH", "free": str(self.qty), "locked": "0"}]})
        if path == "openOrders":
            return httpx.Response(200, json=[o for o in self.orders.values() if o["status"] == "NEW"])
        if path == "openOrderList":
            return httpx.Response(200, json=[self.oco] if self.oco and self.oco["listOrderStatus"] == "EXECUTING" else [])
        if path == "avgPrice":
            return httpx.Response(200, json={"mins": 5, "price": "100"})
        if path == "account/commission":
            return httpx.Response(200, json={"standardCommission": {"maker": ".001", "taker": ".001", "buyer": "0", "seller": "0"}})
        if path == "myFilters":
            return httpx.Response(200, json={"exchangeFilters": [], "symbolFilters": [], "assetFilters": []})
        if path == "order/test":
            if self.after_test:
                self.after_test()
            return httpx.Response(200, json={})
        if path == "order" and method == "POST":
            cid = p["newClientOrderId"]
            assert cid not in self.orders, "duplicate new order transmission"
            qty = dec(p["quantity"])
            price = dec(100) if p["side"] == "BUY" else dec(99)
            oid = len(self.orders)+1
            raw = {"orderId": oid, "clientOrderId": cid, "symbol": "ETHUSDT", "side": p["side"], "type": "MARKET",
                   "status": "FILLED", "origQty": str(qty), "executedQty": str(qty)}
            self.orders[cid] = raw
            self.qty += qty if p["side"] == "BUY" else -qty
            self.fills[oid] = [{"id": oid, "qty": str(qty), "price": str(price), "quoteQty": str(price*qty),
                                "commission": str(price*qty*dec(".001")), "commissionAsset": "USDT"}]
            return httpx.Response(200, json=raw)
        if path == "order" and method == "GET":
            raw = self.orders.get(p.get("origClientOrderId"))
            if raw is None:
                raw = next((o for o in self.orders.values() if str(o["orderId"]) == p.get("orderId")), None)
            return httpx.Response(200, json=raw) if raw else httpx.Response(400, json={"code": -2013})
        if path == "myTrades":
            return httpx.Response(200, json=self.fills.get(int(p["orderId"]), []))
        if path == "orderList/oco":
            if self.reject_protection:
                return httpx.Response(400, json={"code": -2010})
            assert self.oco is None, "duplicate OCO transmission"
            self.oco = {"symbol": "ETHUSDT", "listClientOrderId": p["listClientOrderId"], "listOrderStatus": "EXECUTING", "contingencyType": "OCO"}
            for role, prefix, kind in (("tp", "above", "TAKE_PROFIT"), ("sl", "below", "STOP_LOSS")):
                cid = p[prefix+"ClientOrderId"]
                oid = len(self.orders)+1
                self.orders[cid] = {"orderId": oid, "clientOrderId": cid, "symbol": "ETHUSDT", "side": "SELL", "type": kind,
                                    "status": "NEW", "origQty": p["quantity"], "executedQty": "0", "stopPrice": p[prefix+"StopPrice"]}
            if self.lose_oco_response:
                self.lose_oco_response = False
                raise httpx.ReadTimeout("response lost after OCO acceptance")
            return httpx.Response(200, json=self.oco)
        if path == "orderList":
            if self.oco is None:
                return httpx.Response(400, json={"code": -2013})
            if method == "DELETE":
                if self.cancel_unknown:
                    raise httpx.ReadTimeout("cancellation unknown")
                self.oco["listOrderStatus"] = "ALL_DONE"
                for o in self.orders.values():
                    if o["status"] in {"NEW", "PARTIALLY_FILLED"}:
                        o["status"] = "CANCELED"
            return httpx.Response(200, json=self.oco)
        raise AssertionError(f"Unmocked call {method} {path}")

    def trigger_stop(self, fraction=dec(1)):
        for o in self.orders.values():
            if o["type"] == "STOP_LOSS":
                q = (dec(o["origQty"])*fraction // dec(".001"))*dec(".001")
                o["status"] = "FILLED" if fraction == 1 else "PARTIALLY_FILLED"
                o["executedQty"] = str(q)
                self.qty -= q
                self.fills[o["orderId"]] = [{"id": o["orderId"], "qty": str(q), "price": "95", "quoteQty": str(95*q),
                          "commission": str(95*q*dec(".001")), "commissionAsset": "USDT"}]
            elif o["type"] == "TAKE_PROFIT":
                o["status"] = "CANCELED"
        if fraction == 1:
            self.oco["listOrderStatus"] = "ALL_DONE"


def build_execution(db, cfg, risk):
    exchange = StatefulDemo()
    demo = BinanceAPI(demo=True, key="demo-only-fixture", secret="test-secret", transport=httpx.MockTransport(exchange.handle))
    demo.min_interval = 0
    execution = Execution(db, cfg, risk, FakeMarket(), demo)
    execution.demo_symbols = {"ETHUSDT": symbol_info()}
    return execution, demo, exchange


async def test_complete_demo_lifecycle_uses_exchange_fills(db, cfg, risk, signal):
    execution, api, exchange = build_execution(db, cfg, risk)
    try:
        await execution.enter(signal, {})
        assert len(db.positions()) == 1
        assert db.rows("SELECT status FROM intents")[0]["status"] == "OPEN"
        exchange.trigger_stop()
        await execution.reconcile()
        assert not db.positions()
        t = json.loads(db.rows("SELECT * FROM trades")[0]["data"])
        assert t["venue"] == "DEMO"
        assert t["exit_reason"] == "stop_loss"
        assert dec(t["pnl"]) < 0
        assert dec(db.get("cash")) == dec(10000)+dec(t["pnl"])
        assert len([c for c in exchange.calls if c[:2] == ("POST", "order")]) == 1
    finally:
        await api.close()


async def test_rejected_oco_flattens_actual_position(db, cfg, risk, signal):
    execution, api, exchange = build_execution(db, cfg, risk)
    exchange.reject_protection = True
    try:
        await execution.enter(signal, {})
        assert db.get("kill")
        await execution.reconcile()
        assert not db.positions()
        assert any(c[0] == "POST" and c[1] == "order" and c[2]["side"] == "SELL" for c in exchange.calls)
        assert json.loads(db.rows("SELECT * FROM trades")[0]["data"])["exit_reason"] == "protection_failure"
    finally:
        await api.close()


async def test_accepted_oco_timeout_is_recovered_without_new_oco(db, cfg, risk, signal):
    execution, api, exchange = build_execution(db, cfg, risk)
    exchange.lose_oco_response = True
    try:
        with pytest.raises(Uncertain):
            await execution.enter(signal, {})
        restored = Execution(db, cfg, risk, FakeMarket(), api)
        restored.demo_symbols = execution.demo_symbols
        await restored.reconcile()
        assert db.rows("SELECT status FROM intents")[0]["status"] == "OPEN"
        assert len([c for c in exchange.calls if c[:2] == ("POST", "orderList/oco")]) == 1
    finally:
        await api.close()


async def test_partial_protective_fill_then_cancel_and_close_residual(db, cfg, risk, signal):
    execution, api, exchange = build_execution(db, cfg, risk)
    try:
        await execution.enter(signal, {})
        exchange.trigger_stop(dec(".5"))
        await execution.reconcile()
        await execution.reconcile()
        assert not db.positions()
        assert exchange.qty == 0
        assert len(db.rows("SELECT * FROM trades")) == 1
    finally:
        await api.close()


async def test_ambiguous_cancel_does_not_send_conflicting_sell(db, cfg, risk, signal):
    execution, api, exchange = build_execution(db, cfg, risk)
    try:
        await execution.enter(signal, {})
        exchange.cancel_unknown = True
        with pytest.raises(Uncertain):
            await execution.monitor(flatten=True)
        assert len(db.positions()) == 1
        assert not any(c[0] == "POST" and c[1] == "order" and c[2]["side"] == "SELL" for c in exchange.calls)
    finally:
        await api.close()


async def test_kill_during_preflight_prevents_buy(db, cfg, risk, signal):
    execution, api, exchange = build_execution(db, cfg, risk)
    exchange.after_test = lambda: db.halt("Kill while awaiting network")
    try:
        with pytest.raises(Rejected, match="Kill"):
            await execution.enter(signal, {})
        assert not any(c[:2] == ("POST", "order") for c in exchange.calls)
    finally:
        await api.close()


async def test_kill_in_final_transport_guard_prevents_http_mutation(db, cfg, risk, signal, rules):
    execution, api, exchange = build_execution(db, cfg, risk)
    try:
        def guard():
            raise Rejected("Stopped at transmission boundary")
        db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,0)", (signal.key, signal.symbol, signal.strategy, "DEMO", "ENTERING", "{}"))
        with pytest.raises(Rejected):
            await execution._submit(signal.key, "b", "order", {"symbol": signal.symbol, "side": "BUY"}, guard=guard)
        assert not exchange.calls
        assert db.rows("SELECT status FROM orders")[0]["status"] == "REJECTED"
    finally:
        await api.close()
