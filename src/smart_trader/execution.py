import asyncio
import json
import time
from dataclasses import asdict
from decimal import Decimal

from .api import APIError
from .config import dec
from .db import encode
from .filters import SymbolRules
from .models import Plan, Rejected, Signal, Uncertain
from .paper import simulate

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def restore_plan(raw):
    p = json.loads(raw) if isinstance(raw, str) else raw
    s = dict(p["signal"])
    for k in ("entry", "stop", "target", "atr", "breakout"):
        s[k] = dec(s[k])
    return Plan(Signal(**s), *(dec(p[k]) for k in ("qty", "entry", "stop", "target", "risk")), p["venue"])


class Execution:
    """Only gateway accepting signals. Strategies never receive broker references."""
    def __init__(self, db, settings, risk, market, demo=None):
        self.db, self.settings, self.risk, self.market, self.demo = db, settings, risk, market, demo
        self.lock = asyncio.Lock()
        self.demo_symbols = {}
        self.exchange_filters = []

    @staticmethod
    def client_id(key, role):
        return f"st_{key}_{role}"

    async def prepare_demo(self):
        if self.demo:
            raw = await self.demo.get("exchangeInfo")
            self.demo_symbols = {x["symbol"]: x for x in raw["symbols"]}
            self.exchange_filters = raw.get("exchangeFilters", [])
            self.db.set("demo_status", {"ok": True, "ts": time.time()})

    async def preflight(self, symbol, venue):
        if venue == "PAPER":
            return SymbolRules(self.market.symbols[symbol]), None, Decimal(0)
        raw = await self.demo.get("exchangeInfo", symbol=symbol)
        rules = SymbolRules(raw["symbols"][0])
        if rules.raw["status"] != "TRADING" or not rules.raw.get("isSpotTradingAllowed", False):
            raise Rejected("Demo symbol not TRADING Spot")
        if not rules.raw.get("ocoAllowed", False) or not {"MARKET", "STOP_LOSS", "TAKE_PROFIT"} <= set(rules.raw["orderTypes"]):
            raise Rejected("Market-trigger OCO not supported for symbol")
        account, orders, lists, average, commission, filters = await asyncio.gather(
            self.demo.signed("GET", "account"), self.demo.signed("GET", "openOrders"),
            self.demo.signed("GET", "openOrderList"), self.demo.get("avgPrice", symbol=symbol),
            self.demo.signed("GET", "account/commission", symbol=symbol),
            self.demo.signed("GET", "myFilters", symbol=symbol))
        # Account-specific filter views can be stricter than public exchangeInfo.
        if filters.get("symbolFilters"):
            merged = dict(rules.filters)
            merged.update({f["filterType"]: f for f in filters["symbolFilters"]})
            rules = SymbolRules(dict(rules.raw, filters=list(merged.values())))
        if not account.get("canTrade", False):
            raise Rejected("Demo account cannot trade")
        expected_fee = sum((max(dec(commission.get(k, {}).get("maker", 0)), dec(commission.get(k, {}).get("taker", 0)))+
                            max(dec(commission.get(k, {}).get("buyer", 0)), dec(commission.get(k, {}).get("seller", 0)))
                            for k in ("standardCommission", "taxCommission", "specialCommission")), Decimal(0))
        if expected_fee > dec(self.settings.strategy.fee):
            raise Rejected("Configured fee is below exchange undiscounted commission")
        rules.capacity(orders, lists, filters.get("exchangeFilters", raw.get("exchangeFilters", self.exchange_filters)))
        balances = {b["asset"]: b for b in account["balances"]}
        self.db.set("demo_free_usdt", balances.get("USDT", {}).get("free", "0"))
        self.db.set("demo_asset_filters", filters.get("assetFilters", []))
        base = balances.get(rules.base, {})
        # Existing BUY orders are included in MAX_POSITION.
        base_position = dec(base.get("free", 0))+dec(base.get("locked", 0))
        base_position += sum((dec(o["origQty"])-dec(o["executedQty"]) for o in orders
                              if o["symbol"] == symbol and o["side"] == "BUY"), Decimal(0))
        reference = dec(average["price"])
        for f in rules.filters.values():
            if "avgPriceMins" in f and int(f["avgPriceMins"]) != int(average.get("mins", -1)):
                raise Rejected("Reference price averaging window does not match filter")
        return rules, reference, base_position

    async def enter(self, signal, histories):
        async with self.lock:
            venue = "DEMO" if self.demo else "PAPER"
            if venue == "DEMO" and signal.symbol not in self.demo_symbols:
                if signal.strategy != "new_listings":
                    raise Rejected("Symbol unavailable on Demo")
                venue = "PAPER"
                self.db.event("MODE", f"{signal.symbol}: unavailable on Demo; explicit PAPER execution", True)
            rules, reference, base_position = await self.preflight(signal.symbol, venue)
            book = await self.market.book(signal.symbol, self.demo if venue == "DEMO" else None)
            plan = self.risk.approve(signal, rules, book, venue, histories,
                                     reference=reference, base_position=base_position)
            if venue == "DEMO":
                if plan.qty*plan.entry*(1+dec(self.settings.strategy.fee)) > dec(self.db.get("demo_free_usdt", "0")):
                    raise Rejected("Insufficient free Demo USDT")
                for f in self.db.get("demo_asset_filters", []):
                    if f["filterType"] != "MAX_ASSET":
                        raise Rejected("Unknown account asset filter")
                    amount = plan.qty if f["asset"] == rules.base else plan.qty*plan.entry if f["asset"] == "USDT" else Decimal(0)
                    if amount > dec(f["limit"]):
                        raise Rejected("MAX_ASSET")
                # Official validation endpoint; still no order has been placed.
                await self.demo.signed("POST", "order/test", symbol=signal.symbol, side="BUY", type="MARKET", quantity=str(plan.qty))
                book = await self.market.book(signal.symbol, self.demo)
                plan = self.risk.approve(signal, rules, book, venue, histories,
                                         reference=reference, base_position=base_position)
            with self.db.transaction():
                self.db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?)",
                                (signal.key, signal.symbol, signal.strategy, venue, "ENTERING", encode(asdict(plan)), time.time()))
            if venue == "PAPER":
                try:
                    fill = simulate(book, plan.qty, "BUY", rules, self.settings.strategy, signal.strategy == "new_listings")
                    if fill.price > plan.entry:
                        raise Rejected("Paper execution worse than approved entry")
                    self._open(plan, fill.qty, fill.price, fill.quote+fill.fee, fill.fee, fill.slippage, rules)
                    self.db.execute("UPDATE intents SET status='OPEN' WHERE id=?", (signal.key,))
                except Rejected:
                    self.db.execute("UPDATE intents SET status='REJECTED' WHERE id=?", (signal.key,))
                    raise
            else:
                def guard():
                    current = self.risk.approve(signal, rules, book, venue, histories,
                                                reference=reference, base_position=base_position,
                                                reserved_key=signal.key)
                    if current.qty < plan.qty or current.risk < plan.risk:
                        raise Rejected("Risk changed while waiting to transmit; order not sent")
                await self._submit(signal.key, "b", "order", {"symbol": signal.symbol, "side": "BUY", "type": "MARKET",
                                                              "quantity": str(plan.qty), "newOrderRespType": "FULL"}, guard=guard)
                await self._recover_entry(plan, rules)

    async def _submit(self, key, role, path, payload, guard=None):
        cid = self.client_id(key, role)
        existing = self.db.rows("SELECT * FROM orders WHERE id=?", (cid,))
        if existing:
            if existing[0]["status"] == "REJECTED":
                raise Rejected(f"Order {role} was rejected")
            return await self._query(key, role, path, payload["symbol"])
        payload = dict(payload)
        payload["listClientOrderId" if path == "orderList/oco" else "newClientOrderId"] = cid
        self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,NULL)",
                        (cid, key, role, "PREPARED", encode(payload)))
        try:
            raw = await self.demo.signed("POST", path, before_send=guard, **payload)
        except Uncertain:
            self.db.execute("UPDATE orders SET status='UNKNOWN' WHERE id=?", (cid,))
            self.db.halt(f"Demo {payload['symbol']}: {role} outcome unknown; reconciling without resend")
            raise
        except (APIError, Rejected):
            self.db.execute("UPDATE orders SET status='REJECTED' WHERE id=?", (cid,))
            raise
        self.db.execute("UPDATE orders SET status=?,response=? WHERE id=?",
                        (raw.get("status", raw.get("listOrderStatus", "ACK")), encode(raw), cid))
        return raw

    async def _query(self, key, role, path, symbol):
        cid = self.client_id(key, role)
        list_order = path == "orderList/oco"
        try:
            raw = await self.demo.signed("GET", "orderList" if list_order else "order",
                                         **({"origClientOrderId": cid} if list_order else
                                            {"symbol": symbol, "origClientOrderId": cid}))
        except APIError as exc:
            if exc.code in (-2013, -2026):
                raise Uncertain("Order not visible yet: never infer failed placement or resend") from None
            raise
        self.db.execute("UPDATE orders SET status=?,response=? WHERE id=?",
                        (raw.get("status", raw.get("listOrderStatus", "ACK")), encode(raw), cid))
        return raw

    async def _terminal_order(self, raw, symbol):
        if raw["status"] not in TERMINAL:
            try:
                await self.demo.signed("DELETE", "order", symbol=symbol, orderId=raw["orderId"])
            except APIError as exc:
                if exc.code != -2011:
                    raise
            raw = await self.demo.signed("GET", "order", symbol=symbol, orderId=raw["orderId"])
        if raw["status"] not in TERMINAL:
            raise Uncertain("Order cancellation not terminal")
        return raw

    async def _fills(self, raw, rules):
        qty = dec(raw["executedQty"])
        if qty == 0:
            return {"qty": Decimal(0), "quote": Decimal(0), "fee": Decimal(0), "base_fee": Decimal(0), "cash_fee": Decimal(0)}
        trades = []
        cursor = None
        while True:
            params = {"symbol": rules.symbol, "orderId": raw["orderId"], "limit": 1000}
            if cursor is not None:
                params["fromId"] = cursor
            batch = await self.demo.signed("GET", "myTrades", **params)
            trades.extend(batch)
            if len(batch) < 1000:
                break
            next_cursor = int(batch[-1]["id"])+1
            if cursor is not None and next_cursor <= cursor:
                raise Uncertain("Trade history pagination did not advance")
            cursor = next_cursor
        for index, trade in enumerate(trades):
            trade_id = str(trade.get("id", f"{raw['orderId']}:{index}"))
            self.db.execute("INSERT OR REPLACE INTO exchange_fills VALUES(?,?,?)", (rules.symbol, trade_id, encode(trade)))
        if sum((dec(t["qty"]) for t in trades), Decimal(0)) != qty:
            raise Uncertain("Trade ledger incomplete; wait for matching executedQty")
        quote, fee, base_fee, cash_fee = (Decimal(0) for _ in range(4))
        for trade in trades:
            price, q, commission = dec(trade["price"]), dec(trade["qty"]), dec(trade["commission"])
            quote += dec(trade.get("quoteQty", price*q))
            asset = trade["commissionAsset"]
            if commission == 0:
                continue
            if asset == rules.base:
                base_fee += commission
                fee += commission*price
            elif asset == "USDT":
                fee += commission
                cash_fee += commission
            else:
                conversion = await self.market.api.get("avgPrice", symbol=asset+"USDT")
                converted = commission*dec(conversion["price"])
                fee += converted
                cash_fee += converted
        return {"qty": qty, "quote": quote, "fee": fee, "base_fee": base_fee, "cash_fee": cash_fee}

    def _open(self, plan, qty, price, cost, fee, slippage, rules):
        s = plan.signal
        stop = rules.price(price-(s.entry-s.stop))
        target = rules.price(price+(s.target-s.entry))
        p = {"id": s.key, "symbol": s.symbol, "strategy": s.strategy, "venue": plan.venue,
             "qty": qty, "initial_qty": qty, "entry": price, "cost": cost, "entry_fee": fee,
             "exit_fee": Decimal(0), "proceeds": Decimal(0), "slippage": slippage,
             "stop": stop, "target": target, "risk": plan.risk,
             "opened": time.time(), "accounted_exits": {}, "exit_reason": None}
        with self.db.transaction():
            if self.db.rows("SELECT id FROM positions WHERE id=?", (s.key,)):
                return
            self.db.set("cash", str(dec(self.db.get("cash"))-cost))
            self.db.save_position(p)
            self.db.event("OPEN", f"{plan.venue} {s.symbol} {s.strategy}: entry={price}, qty={qty}, "
                          f"stop={stop}, target={target}, risk<={plan.risk}. {s.reason}", True)

    async def _recover_entry(self, plan, rules):
        key, symbol = plan.signal.key, plan.signal.symbol
        positions = {p["id"]: p for p in self.db.positions()}
        if key not in positions:
            existing = self.db.rows("SELECT * FROM orders WHERE id=?", (self.client_id(key, "b"),))
            if not existing:
                # Intent persisted, but no order intent existed: no transmission was possible.
                self.db.execute("UPDATE intents SET status='REJECTED' WHERE id=?", (key,))
                return
            if existing[0]["status"] == "REJECTED":
                self.db.execute("UPDATE intents SET status='REJECTED' WHERE id=?", (key,))
                return
            buy = await self._query(key, "b", "order", symbol)
            buy = await self._terminal_order(buy, symbol)
            fill = await self._fills(buy, rules)
            if fill["qty"] == 0:
                self.db.execute("UPDATE intents SET status='REJECTED' WHERE id=?", (key,))
                return
            qty = fill["qty"]-fill["base_fee"]
            price = fill["quote"]/fill["qty"]
            self._open(plan, qty, price, fill["quote"]+fill["cash_fee"], fill["fee"],
                       abs(price-plan.signal.entry)*fill["qty"], rules)
        p = next(p for p in self.db.positions() if p["id"] == key)
        oco = self.db.rows("SELECT * FROM orders WHERE id=?", (self.client_id(key, "oc"),))
        if not oco:
            try:
                book = await self.market.book(symbol, self.demo)
                qty = rules.quantity(dec(p["qty"]))
                if dec(p["qty"])-qty > 0:
                    # Retain exact dust in position; don't silently remove base fees/rounding.
                    p["dust"] = str(dec(p["qty"])-qty)
                    self.db.save_position(p)
                if not dec(p["stop"]) < book.bid < dec(p["target"]):
                    raise Rejected("Price crossed protection before OCO installation")
                if dec(p["entry"]) > plan.entry or dec(p["cost"]) > plan.qty*plan.entry*(1+dec(self.settings.strategy.fee)):
                    raise Rejected("Actual buy exceeded approved costs")
                average = await self.demo.get("avgPrice", symbol=symbol)
                for price in (dec(p["stop"]), dec(p["target"])):
                    rules.validate(qty, price, side="SELL", reference=dec(average["price"]))
                await self._submit(key, "oc", "orderList/oco", {
                    "symbol": symbol, "side": "SELL", "quantity": str(qty),
                    "aboveType": "TAKE_PROFIT", "aboveStopPrice": str(p["target"]),
                    "aboveClientOrderId": self.client_id(key, "tp"),
                    "belowType": "STOP_LOSS", "belowStopPrice": str(p["stop"]),
                    "belowClientOrderId": self.client_id(key, "sl"), "newOrderRespType": "FULL"})
            except (Rejected, APIError):
                self.db.halt(f"{symbol}: protection rejected; safe flatten requested")
                p["exit_reason"] = "protection_failure"
                self.db.save_position(p)
                self.db.execute("UPDATE intents SET status='EXITING' WHERE id=?", (key,))
                await self._demo_exit(p, rules)
                return
        elif oco[0]["status"] == "REJECTED":
            p["exit_reason"] = "protection_failure"
            self.db.save_position(p)
            await self._demo_exit(p, rules)
            return
        await self._verify_protection(p, rules)

    async def _leg(self, key, role, symbol):
        return await self.demo.signed("GET", "order", symbol=symbol, origClientOrderId=self.client_id(key, role))

    async def _verify_protection(self, p, rules):
        raw = await self._query(p["id"], "oc", "orderList/oco", p["symbol"])
        legs = await asyncio.gather(*(self._leg(p["id"], role, p["symbol"]) for role in ("tp", "sl")))
        executed = any(dec(leg["executedQty"]) > 0 for leg in legs)
        valid = raw["listOrderStatus"] == "EXECUTING" and raw.get("contingencyType") == "OCO" and all(leg["status"] == "NEW" and
                    leg.get("symbol") == p["symbol"] and leg.get("type") == kind and
                    leg["side"] == "SELL" and dec(leg["origQty"]) == rules.quantity(dec(p["qty"]))
                    and dec(leg["stopPrice"]) == dec(p[level])
                    for leg, level, kind in zip(legs, ("target", "stop"), ("TAKE_PROFIT", "STOP_LOSS"), strict=True))
        if valid and not executed:
            self.db.execute("UPDATE intents SET status='OPEN' WHERE id=?", (p["id"],))
            return
        if executed:
            p["exit_reason"] = "target" if dec(legs[0]["executedQty"]) > 0 else "stop_loss"
        else:
            p["exit_reason"] = "protection_missing"
            self.db.halt(f"{p['symbol']}: protective order is not confirmed active")
        self.db.save_position(p)
        self.db.execute("UPDATE intents SET status='EXITING' WHERE id=?", (p["id"],))
        await self._demo_exit(p, rules)

    def _apply_exit(self, p, order_key, qty, proceeds, fee, slippage=Decimal(0)):
        prior = p["accounted_exits"].get(order_key, {"qty": "0", "proceeds": "0", "fee": "0", "slippage": "0"})
        dq, dp, df = qty-dec(prior["qty"]), proceeds-dec(prior["proceeds"]), fee-dec(prior["fee"])
        ds = slippage-dec(prior.get("slippage", "0"))
        if dq < 0 or qty < 0 or dq > dec(p["qty"]):
            raise ValueError("Non-monotonic or oversold exit ledger")
        p["qty"] = dec(p["qty"])-dq
        p["proceeds"] = dec(p["proceeds"])+dp
        p["exit_fee"] = dec(p["exit_fee"])+df
        p["slippage"] = dec(p["slippage"])+ds
        p["accounted_exits"][order_key] = {"qty": str(qty), "proceeds": str(proceeds), "fee": str(fee), "slippage": str(slippage)}
        with self.db.transaction():
            self.db.set("cash", str(dec(self.db.get("cash"))+dp))
            self.db.save_position(p)

    def _finish(self, p):
        if dec(p["qty"]) != 0:
            return
        pnl = dec(p["proceeds"])-dec(p["cost"])
        p.update(pnl=str(pnl), r_multiple=str(pnl/dec(p["risk"])),
                 fees=str(dec(p["entry_fee"])+dec(p["exit_fee"])))
        with self.db.transaction():
            self.db.execute("INSERT INTO trades(position_id,symbol,strategy,venue,closed,data) VALUES(?,?,?,?,?,?)",
                            (p["id"], p["symbol"], p["strategy"], p["venue"], time.time(), encode(p)))
            self.db.execute("DELETE FROM positions WHERE id=?", (p["id"],))
            self.db.execute("UPDATE intents SET status='CLOSED' WHERE id=?", (p["id"],))
            self.risk.closed_trade(pnl)
            self.db.event("CLOSE", f"{p['venue']} {p['symbol']} {p['exit_reason']}: Net PnL {pnl:.4f} USDT", True)

    async def _demo_exit(self, p, rules):
        key, symbol = p["id"], p["symbol"]
        oco = self.db.rows("SELECT * FROM orders WHERE id=?", (self.client_id(key, "oc"),))
        if oco and oco[0]["status"] != "REJECTED":
            raw = await self._query(key, "oc", "orderList/oco", symbol)
            if raw["listOrderStatus"] != "ALL_DONE":
                try:
                    await self.demo.signed("DELETE", "orderList", symbol=symbol, listClientOrderId=self.client_id(key, "oc"))
                except APIError as exc:
                    if exc.code != -2011:
                        raise
            raw = await self._query(key, "oc", "orderList/oco", symbol)
            if raw["listOrderStatus"] != "ALL_DONE":
                raise Uncertain("OCO cancellation unconfirmed; no conflicting market sell")
            for role in ("tp", "sl"):
                leg = await self._leg(key, role, symbol)
                if leg["status"] not in TERMINAL:
                    raise Uncertain("OCO leg is still active")
                f = await self._fills(leg, rules)
                self._apply_exit(p, role, f["qty"]+f["base_fee"], f["quote"]-f["cash_fee"], f["fee"])
        exits = self.db.rows("SELECT * FROM orders WHERE intent_id=? AND role LIKE 'x%' ORDER BY rowid", (key,))
        for row in exits:
            if row["status"] == "REJECTED":
                continue
            raw = await self._query(key, row["role"], "order", symbol)
            raw = await self._terminal_order(raw, symbol)
            f = await self._fills(raw, rules)
            self._apply_exit(p, row["role"], f["qty"]+f["base_fee"], f["quote"]-f["cash_fee"], f["fee"])
        if dec(p["qty"]) == 0:
            self._finish(p)
            return
        qty = rules.quantity(dec(p["qty"]))
        book = await self.market.book(symbol, self.demo)
        try:
            rules.validate(qty, book.bid, market=True, side="SELL")
        except Rejected:
            self.db.halt(f"{symbol}: residual/dust {p['qty']} cannot satisfy lot/notional; retained in ledger")
            self.db.execute("UPDATE intents SET status='DUST' WHERE id=?", (key,))
            return
        role = f"x{len(exits)}"
        self.db.execute("UPDATE intents SET status='EXITING' WHERE id=?", (key,))
        await self._submit(key, role, "order", {"symbol": symbol, "side": "SELL", "type": "MARKET",
                                               "quantity": str(qty), "newOrderRespType": "FULL"})
        raw = await self._query(key, role, "order", symbol)
        raw = await self._terminal_order(raw, symbol)
        f = await self._fills(raw, rules)
        self._apply_exit(p, role, f["qty"]+f["base_fee"], f["quote"]-f["cash_fee"], f["fee"])
        self._finish(p)
        # Residual partial fills remain EXITING; next monitor pass sends only the confirmed remainder.

    async def reconcile(self):
        async with self.lock:
            rows = self.db.rows("SELECT * FROM intents WHERE venue='DEMO' AND status NOT IN ('CLOSED','REJECTED')")
            failed = False
            for row in rows:
                try:
                    plan = restore_plan(row["plan"])
                    raw = self.demo_symbols.get(row["symbol"])
                    if raw is None:
                        raw = (await self.demo.get("exchangeInfo", symbol=row["symbol"]))["symbols"][0]
                        self.demo_symbols[row["symbol"]] = raw
                    rules = SymbolRules(raw)
                    positions = {p["id"]: p for p in self.db.positions()}
                    if row["id"] in positions and (row["status"] in {"EXITING", "DUST"} or positions[row["id"]].get("exit_reason")):
                        await self._demo_exit(positions[row["id"]], rules)
                    elif row["status"] == "OPEN" and row["id"] in positions:
                        await self._verify_protection(positions[row["id"]], rules)
                    else:
                        await self._recover_entry(plan, rules)
                except Exception as exc:
                    failed = True
                    self.db.halt(f"{row['symbol']} reconciliation {type(exc).__name__}; other positions still monitored")
            orders = await self.demo.signed("GET", "openOrders")
            known = {x["id"] for x in self.db.rows("SELECT id FROM orders")}
            known |= {self.client_id(x["id"], role) for x in rows for role in ("tp", "sl")}
            if any(o["clientOrderId"].startswith("st_") and o["clientOrderId"] not in known for o in orders):
                failed = True
                self.db.halt("Unknown open Demo bot order: restore original database before resuming")
            account = await self.demo.signed("GET", "account")
            balances = {b["asset"]: dec(b["free"])+dec(b["locked"]) for b in account["balances"]}
            for p in self.db.positions():
                if p["venue"] == "DEMO":
                    base = self.demo_symbols[p["symbol"]]["baseAsset"]
                    if balances.get(base, Decimal(0)) < dec(p["qty"]):
                        failed = True
                        self.db.halt(f"Demo balance mismatch for {p['symbol']}; manual/reset change suspected")
            if failed:
                raise Uncertain("One or more Demo exposures failed reconciliation")
            self.db.set("demo_status", {"ok": True, "ts": time.time()})

    async def monitor(self, flatten=False):
        async with self.lock:
            failed = False
            for p in self.db.positions():
                try:
                    await self._monitor_position(p, flatten)
                except Exception as exc:
                    failed = True
                    self.db.halt(f"{p['symbol']} monitor {type(exc).__name__}; other positions still monitored")
            if failed:
                raise Uncertain("Position marks/management incomplete; no fresh equity asserted")
            # Exits can change cash during the loop.
            equity = dec(self.db.get("cash"))+sum((dec(p["qty"])*dec(p.get("mark", p["entry"]))
                                                   for p in self.db.positions()), Decimal(0))
            self.risk.observe(equity)

    async def _monitor_position(self, p, flatten):
        if dec(p["qty"]) == 0:
            # A crash may have occurred after atomic fill accounting and before trade finalization.
            self._finish(p)
            return
        api = self.demo if p["venue"] == "DEMO" else None
        book = await self.market.book(p["symbol"], api)
        if time.time()-book.received > self.settings.strategy.quote_max_age:
            raise Rejected("Stale mark/exit book")
        p["mark"] = str(book.bid)
        self.db.save_position(p)
        rules = SymbolRules((self.demo_symbols if api else self.market.symbols)[p["symbol"]])
        if flatten:
            p["exit_reason"] = p.get("exit_reason") or "kill_flatten"
        elif p["venue"] == "PAPER" and not p.get("exit_reason"):
            if book.bid <= dec(p["stop"]):
                p["exit_reason"] = "stop_loss"
            elif book.bid >= dec(p["target"]):
                p["exit_reason"] = "target"
        if p.get("exit_reason"):
            self.db.save_position(p)
            if p["venue"] == "DEMO":
                self.db.execute("UPDATE intents SET status='EXITING' WHERE id=?", (p["id"],))
                await self._demo_exit(p, rules)
            else:
                try:
                    fill = simulate(book, dec(p["qty"]), "SELL", rules, self.settings.strategy, p["strategy"] == "new_listings")
                except Rejected:
                    self.db.halt(f"{p['symbol']}: Paper residual below lot/notional; retained")
                    self.db.execute("UPDATE intents SET status='DUST' WHERE id=?", (p["id"],))
                    return
                role = f"paper{len(p['accounted_exits'])}"
                self._apply_exit(p, role, fill.qty, fill.quote-fill.fee, fill.fee, fill.slippage)
                self._finish(p)
