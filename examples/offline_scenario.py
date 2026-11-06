"""Deterministic synthetic fixture. NOT Binance market data or strategy evidence."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from smart_trader.backtest import Backtester, Dataset
from smart_trader.config import Settings
from smart_trader.db import encode
from smart_trader.market import COLUMNS


def make_fixture(root, count=1100):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    start = 1_704_067_200_000  # 2024-01-01 UTC; deliberately synthetic OHLCV.
    symbols = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        prices = 100+np.arange(count)*.005
        f = pd.DataFrame({"open_ms": start+np.arange(count)*900_000, "open": prices,
                          "high": prices+2, "low": prices-2, "close": prices,
                          "volume": np.full(count, 100_000.), "quote_volume": prices*100_000,
                          "trades": 100, "buy_volume": 50_000, "buy_quote": prices*50_000, "ignore": 0})
        f["close_ms"] = f.open_ms+899_999
        if symbol == "ETHUSDT":
            # One eligible signal, then a bar crossing BOTH protection prices: stop must win.
            f.loc[820, ["close", "high", "volume"]] = [prices[819]+2.1, prices[819]+2.2, 250_000]
            f.loc[821, ["open", "high", "low"]] = [prices[819]+2.1, prices[819]+40, prices[819]-40]
        f[COLUMNS].to_csv(root/f"{symbol}.csv", index=False)
        rules = {"symbol": symbol, "status": "TRADING", "baseAsset": symbol[:-4], "quoteAsset": "USDT",
                 "isSpotTradingAllowed": True, "ocoAllowed": True,
                 "orderTypes": ["MARKET", "STOP_LOSS", "TAKE_PROFIT"], "filters": [
                     {"filterType": "PRICE_FILTER", "minPrice": ".01", "maxPrice": "1000000", "tickSize": ".01"},
                     {"filterType": "LOT_SIZE", "minQty": ".001", "maxQty": "100000", "stepSize": ".001"},
                     {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True, "avgPriceMins": 5}]}
        symbols[symbol] = {"listed_ms": 0, "rules": [{"effective_ms": 0, "data": rules}],
                           "assumed_spread": .0001, "assumed_depth_usdt": 100_000}
    (root/"universe.json").write_text(encode({"symbols": symbols,
                          "limitations": ["SYNTHETIC FIXTURE — NOT BINANCE DATA, NOT PROFITABILITY EVIDENCE"]}), "utf-8")
    return root


if __name__ == "__main__":
    root = make_fixture(sys.argv[1] if len(sys.argv) > 1 else "data/synthetic_fixture")
    result = Backtester(Dataset(root), Settings(), allow_modelled_liquidity=True).run()
    print(encode({"source": "SYNTHETIC FIXTURE ONLY", "report": result["report"], "trades": result["trades"]}))
