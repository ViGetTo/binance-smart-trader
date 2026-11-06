import time

import numpy as np
import pandas as pd
import pytest

from smart_trader.config import Mode, Settings, dec
from smart_trader.db import Database
from smart_trader.filters import SymbolRules
from smart_trader.models import Book, Signal
from smart_trader.risk import RiskManager


def symbol_info(symbol="ETHUSDT"):
    return {"symbol": symbol, "status": "TRADING", "baseAsset": symbol[:-4], "quoteAsset": "USDT",
            "isSpotTradingAllowed": True, "ocoAllowed": True,
            "orderTypes": ["MARKET", "STOP_LOSS", "TAKE_PROFIT"], "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": ".01", "minPrice": ".01", "maxPrice": "1000000"},
                {"filterType": "LOT_SIZE", "stepSize": ".001", "minQty": ".001", "maxQty": "100000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0", "maxQty": "100000"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True, "avgPriceMins": 5}]}


def book_at(price=100, available=100_000):
    return Book([(dec(price), dec(available))], [(dec(price)+dec(".01"), dec(available))], time.time())


def frame(count=240, step=3_600_000, trend=True, end=None):
    end = end or int(time.time()*1000)//step*step
    opens = np.arange(end-count*step, end, step)
    closes = np.linspace(80, 100, count) if trend else np.full(count, 100.)
    return pd.DataFrame({"open_ms": opens, "close_ms": opens+step-1, "open": closes-.02,
                         "high": closes+1, "low": closes-1, "close": closes,
                         "volume": np.full(count, 100_000.), "quote_volume": np.full(count, 10_000_000.)})


@pytest.fixture
def cfg(tmp_path):
    # Single-account fixture; three-account PAPER defaults are exercised separately.
    return Settings(mode=Mode.PAPER, db_path=str(tmp_path/"trader.sqlite"), strict_demo=False, demo_balance_sync=False)


@pytest.fixture
def db(cfg):
    db = Database(cfg.db_path, cfg.risk.initial_balance)
    yield db
    db.close()


@pytest.fixture
def risk(db, cfg):
    r = RiskManager(db, cfg)
    r.observe(dec(10_000))
    return r


@pytest.fixture
def signal():
    return Signal("ETHUSDT", "momentum", int(time.time()*1000)-1000,
                  dec(100), dec(96), dec(110), dec(2), dec("99.9"), "SYNTHETIC TEST SIGNAL")


@pytest.fixture
def rules():
    return SymbolRules(symbol_info())


class FakeMarket:
    def __init__(self, book=None, symbols=None, api=None):
        self.current = book or book_at()
        self.symbols = symbols or {"ETHUSDT": symbol_info()}
        self.api = api

    async def book(self, symbol, api=None):
        return self.current
