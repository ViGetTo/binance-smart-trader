import asyncio
import re

import numpy as np
import pandas as pd

from .models import Rejected
from .strategies import analyze, ema, market_bullish, quality


def eligible(raw, cfg):
    base = raw["baseAsset"]
    return (raw["status"] == "TRADING" and raw["quoteAsset"] == "USDT"
            and raw.get("isSpotTradingAllowed", False) and base not in cfg.excluded_assets
            and raw["symbol"] not in cfg.excluded_symbols
            and not (len(base) > 3 and re.search(r"(UP|DOWN|BULL|BEAR)$", base)))


def normalize(matrix, weights):
    """Winsorized percentile ranks; every component is bounded in [0,1]."""
    f = pd.DataFrame(matrix)
    if not np.isfinite(f.to_numpy()).all():
        raise Rejected("Invalid ranking input")
    normalized = f.apply(lambda c: pd.Series(.5, index=c.index) if c.nunique() == 1 else
                         (c.clip(c.quantile(.05), c.quantile(.95)).rank(method="average")-1)/max(len(c)-1, 1))
    return (normalized.to_numpy() @ np.asarray(weights)).tolist()


class Scanner:
    def __init__(self, market, registry, db, config):
        self.market, self.registry, self.db, self.cfg = market, registry, db, config
        self.history = {}
        self.semaphore = asyncio.Semaphore(4)

    async def scan(self):
        await self.market.refresh()
        await self.registry.update()
        ages = self.registry.ages()
        btc = await self.market.candles("BTCUSDT", "1h", self.cfg.hourly_bars)
        bullish = market_bullish(btc, self.cfg)
        self.db.set("btc_bullish", bullish)
        btc_return = float(self.market.tickers["BTCUSDT"]["priceChangePercent"])
        regular, listings = [], []

        async def inspect(symbol, raw):
            async with self.semaphore:
                listing = ages.get(symbol, 1e9) <= self.cfg.listing_max_hours
                strategy = "new_listings" if listing else "momentum"
                try:
                    if symbol not in ages:
                        raise Rejected("Unknown market age: wait for first-candle verification")
                    ticker = self.market.tickers.get(symbol, {})
                    minimum = self.cfg.listing_min_volume if listing else self.cfg.min_volume
                    if float(ticker.get("quoteVolume", 0)) < minimum:
                        raise Rejected("24h quote volume too low")
                    bars = await self.market.candles(symbol, "15m", self.cfg.quarter_bars)
                    value = quality(bars, self.cfg)
                    # New listings do not require EMA50/200 history.
                    hourly = await self.market.candles(symbol, "1h", self.cfg.hourly_bars)
                    self.history[symbol] = hourly.set_index("close_ms").close.pct_change().dropna()
                    book = await self.market.book(symbol)
                    maximum = self.cfg.listing_max_spread if listing else self.cfg.max_spread
                    if book.spread > maximum:
                        raise Rejected("Spread too wide")
                    if book.depth(self.cfg.depth_band) <= 0:
                        raise Rejected("No depth in allowed band")
                    trend = float((ema(hourly.close, self.cfg.ema_fast).iloc[-1]/ema(hourly.close, self.cfg.ema_slow).iloc[-1]-1)) if len(hourly) >= self.cfg.ema_slow else 0
                    persistence = float((hourly.close.iloc[-self.cfg.ema_fast:] > ema(hourly.close, self.cfg.ema_fast).iloc[-self.cfg.ema_fast:]).mean())
                    metrics = [float(ticker["priceChangePercent"])-btc_return,
                               trend+persistence*.01,
                               float(bars.volume.iloc[-1]/bars.volume.iloc[-self.cfg.volume_bars-1:-1].median()),
                               np.log1p(float(book.depth(self.cfg.depth_band)))/(1+book.spread*1000),
                               -value/float(bars.close.iloc[-1])]
                    try:
                        signal = analyze(symbol, bars, hourly, self.cfg, bullish=bullish,
                                         listing=listing, age_hours=ages.get(symbol))
                        reason = signal.reason
                    except Rejected as exc:
                        signal, reason = None, str(exc)
                    item = {"symbol": symbol, "metrics": metrics, "signal": signal, "reason": reason,
                            "age_hours": ages.get(symbol), "strategy": strategy}
                    (listings if listing else regular).append(item)
                except Rejected as exc:
                    self.db.decision(symbol, strategy, False, str(exc))
                except Exception as exc:
                    self.db.decision(symbol, strategy, False, f"Data/API error: {type(exc).__name__}")
        await asyncio.gather(*(inspect(s, raw) for s, raw in self.market.symbols.items() if eligible(raw, self.cfg)))
        signals = []
        for group in (regular, listings):
            if not group:
                continue
            scores = normalize([g["metrics"] for g in group], self.cfg.rank_weights)
            for item, score in zip(group, scores, strict=True):
                item["score"] = score
            group.sort(key=lambda item: (-item["score"], item["symbol"]))
            for index, item in enumerate(group):
                selected = index < (1 if item["strategy"] == "new_listings" else self.cfg.top_candidates)
                accepted = selected and item["signal"] is not None
                reason = item["reason"] if selected else "Outside top candidates; " + item["reason"]
                self.db.decision(item["symbol"], item["strategy"], accepted, reason, item["score"])
                if accepted:
                    signals.append(item["signal"])
                    alert_key = "candidate_alert:" + item["signal"].key
                    if item["strategy"] == "new_listings" and not self.db.get(alert_key):
                        self.db.event("CANDIDATE", f"Перспективный новый Spot рынок {item['symbol']}: {item['reason']}. "
                                      "Вход разрешён только после Risk Manager.", True)
                        self.db.set(alert_key, True)
        self.db.set("candidates", [{k: v for k, v in x.items() if k != "signal"} for x in regular])
        self.db.set("listing_candidates", [{k: v for k, v in x.items() if k != "signal"} for x in listings])
        return signals
