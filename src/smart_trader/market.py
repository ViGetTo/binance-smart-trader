import asyncio
import hashlib
import json
import time

import numpy as np
import pandas as pd
from websockets.asyncio.client import connect

from .db import encode
from .models import Book, Rejected

COLUMNS = ["open_ms", "open", "high", "low", "close", "volume", "close_ms", "quote_volume",
           "trades", "buy_volume", "buy_quote", "ignore"]
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def closed_frame(raw, now_ms, interval):
    f = pd.DataFrame(raw, columns=COLUMNS)
    if f.empty:
        return f
    try:
        f["close_ms"] = pd.to_numeric(f.close_ms, errors="raise")
        f = f[f.close_ms < now_ms].copy().reset_index(drop=True)
        for c in COLUMNS[:-1]:
            f[c] = pd.to_numeric(f[c], errors="raise")
    except (TypeError, ValueError):
        raise Rejected("Unparseable completed candle data") from None
    if f.empty:
        return f
    numeric = f[COLUMNS[:-1]].to_numpy(dtype=float)
    if not np.isfinite(numeric).all() or f.open_ms.duplicated().any():
        raise Rejected("Nonfinite/duplicate candle data")
    if (f[["open", "high", "low", "close"]] <= 0).any().any() or (f.volume < 0).any():
        raise Rejected("Invalid OHLCV")
    if ((f.high < f[["open", "close", "low"]].max(axis=1)) |
            (f.low > f[["open", "close", "high"]].min(axis=1))).any():
        raise Rejected("Inconsistent OHLC")
    if len(f) > 1 and not (f.open_ms.diff().iloc[1:] == INTERVAL_MS[interval]).all():
        raise Rejected("Missing/out-of-order candles")
    if not ((f.close_ms-f.open_ms) == INTERVAL_MS[interval]-1).all():
        raise Rejected("Unexpected candle duration")
    return f


class MarketData:
    def __init__(self, api, db, config):
        self.api, self.db, self.config = api, db, config
        self.cache = {}
        self.symbols = {}
        self.tickers = {}
        self.last_ws = 0

    async def refresh(self):
        raw, tickers = await asyncio.gather(self.api.get("exchangeInfo"), self.api.get("ticker/24hr"))
        self.symbols = {s["symbol"]: s for s in raw["symbols"]}
        self.tickers = {s["symbol"]: s for s in tickers}
        snapshot = encode({"symbols": raw["symbols"], "exchangeFilters": raw.get("exchangeFilters", [])})
        digest = hashlib.sha256(snapshot.encode()).hexdigest()
        if digest != self.db.get("exchange_snapshot_hash"):
            self.db.execute("INSERT INTO exchange_snapshots VALUES(?,?,?)", (time.time(), digest, snapshot))
            self.db.set("exchange_snapshot_hash", digest)
        self.db.set("public_exchange_filters", raw.get("exchangeFilters", []))
        self.db.set("rest_status", {"ok": True, "ts": time.time()})
        return raw

    async def candles(self, symbol, interval, limit=260):
        now_ms = int(time.time()*1000+self.api.offset)
        key = symbol, interval, limit
        cutoff = now_ms // INTERVAL_MS[interval]
        cached = self.cache.get(key)
        if cached and cached[0] == cutoff:
            return cached[1].copy()
        raw = await self.api.get("klines", symbol=symbol, interval=interval, limit=limit)
        f = closed_frame(raw, now_ms, interval)
        if f.empty or now_ms-f.close_ms.iloc[-1] > INTERVAL_MS[interval]+30_000:
            raise Rejected("Stale/missing completed candles")
        self.cache[key] = cutoff, f
        self.db.conn.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?)",
                                 [(symbol, interval, int(row["open_ms"]), int(row["close_ms"]), encode(row))
                                  for row in f.to_dict("records")])
        return f.copy()

    async def book(self, symbol, api=None):
        client = api or self.api
        return Book.parse(await client.get("depth", symbol=symbol, limit=100))

    async def stream(self):
        delay = 1
        while True:
            if not self.db.get("market_data_enabled", True):
                self.db.set("ws_status", {"ok": False, "disabled": True, "ts": time.time()})
                await asyncio.sleep(5)
                continue
            try:
                async with connect("wss://stream.binance.com:443/ws/!miniTicker@arr",
                                   ping_interval=None, open_timeout=15, close_timeout=5) as ws:
                    delay = 1
                    self.db.set("ws_status", {"ok": True, "ts": time.time()})
                    while True:
                        payload = json.loads(await asyncio.wait_for(ws.recv(), 30))
                        if isinstance(payload, dict) and payload.get("e") == "serverShutdown":
                            break
                        if not isinstance(payload, list):
                            continue
                        self.last_ws = time.time()
                        self.db.set("ws_status", {"ok": True, "ts": self.last_ws})
                        # REST depth remains authoritative for execution. Ticker is informational.
                        self.db.set("stream_tickers", {v["s"]: v["c"] for v in payload})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.set("ws_status", {"ok": False, "ts": time.time(), "error": type(exc).__name__})
                self.db.event("CONNECTION", "Public WebSocket disconnected; REST fallback", True)
            await asyncio.sleep(delay)
            delay = min(60, delay*2)


class ListingsRegistry:
    def __init__(self, db, market):
        self.db, self.market = db, market

    async def update(self):
        previous = {r["symbol"]: r for r in self.db.rows("SELECT * FROM symbols")}
        now = time.time()
        self.db.execute("UPDATE symbols SET active=0")
        for symbol, raw in self.market.symbols.items():
            if raw["quoteAsset"] != "USDT" or not raw.get("isSpotTradingAllowed", False):
                continue
            old = previous.get(symbol)
            earliest = old["earliest_ms"] if old else None
            # First candle is an estimate of market age, not first_seen or a verified announcement.
            if earliest is None and raw["status"] == "TRADING":
                rows = await self.market.api.get("klines", symbol=symbol, interval="1m", startTime=0, limit=1)
                earliest = int(rows[0][0]) if rows else None
            self.db.execute("INSERT INTO symbols VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                            "earliest_ms=excluded.earliest_ms,last_seen=excluded.last_seen,"
                            "active=excluded.active,raw=excluded.raw",
                            (symbol, old["first_seen"] if old else now, earliest, now,
                             int(raw["status"] == "TRADING"), encode(raw)))
            if old is None and previous and earliest and 0 <= now*1000-earliest < 7*86400_000:
                self.db.event("LISTING", f"Новый Spot рынок {symbol}; возраст по первой свече, не по first_seen.", True)
            if earliest and 0 <= now*1000-earliest < 7*86400_000 and raw["status"] == "TRADING":
                # Observe and persist even below the minimum trading volume during the first 24h.
                try:
                    await self.market.candles(symbol, "15m", 120)
                except Rejected:
                    self.db.decision(symbol, "new_listings", False, "Observing: no usable completed bars yet")

    def ages(self, now=None):
        now = now or time.time()
        return {r["symbol"]: (now*1000-r["earliest_ms"])/3_600_000
                for r in self.db.rows("SELECT * FROM symbols WHERE earliest_ms IS NOT NULL AND active=1")}


async def alpha_watchlist():
    """Official Alpha public token list; informational only, never a Spot universe input."""
    import httpx
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        r = await client.get("https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list")
        if r.status_code != 200:
            raise Rejected(f"Alpha list HTTP {r.status_code}")
        raw = r.json()
        if raw.get("code") != "000000" or not isinstance(raw.get("data"), list):
            raise Rejected("Unrecognized Alpha list schema")
        return [{k: item.get(k) for k in ("symbol", "name", "alphaId", "chainId", "contractAddress")}
                for item in raw["data"]]
