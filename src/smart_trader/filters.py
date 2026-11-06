from decimal import ROUND_DOWN, ROUND_UP, Decimal
from math import lcm

from .config import dec
from .models import Rejected


def floor_step(value, step):
    return (dec(value)/step).to_integral_value(rounding=ROUND_DOWN)*step if step else dec(value)


def ceil_step(value, step):
    return (dec(value)/step).to_integral_value(rounding=ROUND_UP)*step if step else dec(value)


class SymbolRules:
    KNOWN = {"PRICE_FILTER", "PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE", "LOT_SIZE", "MARKET_LOT_SIZE",
             "MIN_NOTIONAL", "NOTIONAL", "ICEBERG_PARTS", "MAX_NUM_ORDERS", "MAX_NUM_ALGO_ORDERS",
             "MAX_NUM_ICEBERG_ORDERS", "MAX_POSITION", "TRAILING_DELTA", "MAX_NUM_ORDER_AMENDS",
             "MAX_NUM_ORDER_LISTS"}

    def __init__(self, symbol):
        self.raw = symbol
        self.symbol = symbol["symbol"]
        self.base = symbol["baseAsset"]
        self.filters = {f["filterType"]: f for f in symbol["filters"]}
        if self.filters.keys() - self.KNOWN:
            raise Rejected("Unknown exchange filter: fail closed")
        if "PRICE_FILTER" not in self.filters or "LOT_SIZE" not in self.filters:
            raise Rejected("Missing mandatory exchange filters")
        self.tick = dec(self.filters["PRICE_FILTER"]["tickSize"])
        steps = [dec(self.filters["LOT_SIZE"]["stepSize"])]
        steps.append(dec(self.filters.get("MARKET_LOT_SIZE", {}).get("stepSize", "0")))
        scale = max(-s.as_tuple().exponent for s in steps)
        factor = Decimal(10)**scale
        integers = [int(s*factor) for s in steps if s > 0]
        self.step = dec(lcm(*integers))/factor if integers else Decimal(0)

    def quantity(self, value):
        return floor_step(value, self.step)

    def price(self, value, up=False):
        return ceil_step(value, self.tick) if up else floor_step(value, self.tick)

    def validate(self, qty, price, *, market=False, side="BUY", reference=None, base_position=Decimal(0)):
        if qty <= 0 or price <= 0:
            raise Rejected("Nonpositive quantity/price")
        for name in ("LOT_SIZE", "MARKET_LOT_SIZE") if market else ("LOT_SIZE",):
            f = self.filters.get(name)
            if not f:
                continue
            for key, compare in (("minQty", lambda a, b: a < b), ("maxQty", lambda a, b: a > b)):
                bound = dec(f[key])
                if bound and compare(qty, bound):
                    raise Rejected(f"{name} {key}")
            step = dec(f["stepSize"])
            if step and qty % step:
                raise Rejected(f"{name} stepSize")
        if not market:
            f = self.filters["PRICE_FILTER"]
            if self.tick and price % self.tick:
                raise Rejected("PRICE_FILTER tickSize")
            if dec(f["minPrice"]) and price < dec(f["minPrice"]):
                raise Rejected("PRICE_FILTER minPrice")
            if dec(f["maxPrice"]) and price > dec(f["maxPrice"]):
                raise Rejected("PRICE_FILTER maxPrice")
        notional = qty*(reference if market and reference is not None else price)
        for name in ("MIN_NOTIONAL", "NOTIONAL"):
            f = self.filters.get(name)
            if not f:
                continue
            if (not market or f.get("applyToMarket", f.get("applyMinToMarket", False))) and notional < dec(f["minNotional"]):
                raise Rejected(f"{name} minimum")
            if name == "NOTIONAL" and (not market or f.get("applyMaxToMarket", False)):
                if dec(f["maxNotional"]) and notional > dec(f["maxNotional"]):
                    raise Rejected("NOTIONAL maximum")
        if not market:
            for name in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
                f = self.filters.get(name)
                if not f:
                    continue
                if reference is None:
                    raise Rejected("Weighted price reference required")
                prefix = "bid" if side == "BUY" else "ask"
                low = f.get("multiplierDown", f.get(prefix+"MultiplierDown"))
                high = f.get("multiplierUp", f.get(prefix+"MultiplierUp"))
                if not reference*dec(low) <= price <= reference*dec(high):
                    raise Rejected(f"{name} outside reference band")
        f = self.filters.get("MAX_POSITION")
        if side == "BUY" and f and base_position+qty > dec(f["maxPosition"]):
            raise Rejected("MAX_POSITION")

    def capacity(self, orders, lists, exchange_filters=()):
        counts = {
            "MAX_NUM_ORDERS": (len([o for o in orders if o["symbol"] == self.symbol]), 2, "maxNumOrders"),
            "MAX_NUM_ALGO_ORDERS": (sum(o["symbol"] == self.symbol and o["type"] in
                                       {"STOP_LOSS", "TAKE_PROFIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}
                                       for o in orders), 2, "maxNumAlgoOrders"),
            "MAX_NUM_ORDER_LISTS": (sum(o["symbol"] == self.symbol for o in lists), 1, "maxNumOrderLists"),
            "EXCHANGE_MAX_NUM_ORDERS": (len(orders), 2, "maxNumOrders"),
            "EXCHANGE_MAX_NUM_ALGO_ORDERS": (sum(o["type"] in {"STOP_LOSS", "TAKE_PROFIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"} for o in orders), 2, "maxNumAlgoOrders"),
            "EXCHANGE_MAX_NUM_ORDER_LISTS": (len(lists), 1, "maxNumOrderLists")}
        for f in [*self.filters.values(), *exchange_filters]:
            t = f["filterType"]
            if t in counts:
                count, extra, key = counts[t]
                if count+extra > int(f[key]):
                    raise Rejected(t)
            elif t.startswith("EXCHANGE_") and t != "EXCHANGE_MAX_NUM_ICEBERG_ORDERS":
                raise Rejected("Unknown exchange-wide filter")
