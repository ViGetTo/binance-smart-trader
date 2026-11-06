import numpy as np
import pandas as pd

from .config import dec
from .models import Rejected, Signal

STRATEGIES = ("momentum", "trend_pullback", "new_listings")


def pullback_signal(symbol, bars, hourly, cfg, *, bullish):
    value = quality(bars, cfg)
    if not bullish:
        raise Rejected("BTC regime blocks new LONG")
    if len(hourly) < cfg.ema_slow+1 or len(bars) < cfg.ema_fast+2:
        raise Rejected("Insufficient EMA history")
    if not (ema(hourly.close, cfg.ema_fast).iloc[-1] > ema(hourly.close, cfg.ema_slow).iloc[-1]
            and hourly.close.iloc[-1] > ema(hourly.close, cfg.ema_slow).iloc[-1]):
        raise Rejected("1H EMA trend not confirmed")
    # EMA before the signal bar is fixed before that bar opens.
    level = float(ema(bars.close, cfg.ema_fast).iloc[-2])
    previous, last = bars.iloc[-2], bars.iloc[-1]
    if not level-value*cfg.pullback_touch_atr <= last.low <= level+value*cfg.pullback_touch_atr:
        raise Rejected("No controlled pullback to EMA")
    if not last.close > max(last.open, previous.high, level):
        raise Rejected("No closed bullish EMA reclaim")
    volume_base = bars.volume.iloc[-cfg.volume_bars-1:-1].median()
    if last.volume < cfg.pullback_volume_multiple*volume_base:
        raise Rejected("Pullback confirmation volume too low")
    if last.close-level > cfg.max_extension_atr*value:
        raise Rejected("Pullback entry overextended")
    entry, risk = dec(last.close), dec(value)*dec(cfg.stop_atr)
    return Signal(symbol, "trend_pullback", int(last.close_ms), entry, entry-risk,
                  entry+risk*dec(cfg.target_r), dec(value), dec(level),
                  f"Closed EMA reclaim {level:.8g}; volume {last.volume/volume_base:.2f}x; ATR {value:.8g}")


def ema(series, span):
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def atr(frame, period=14):
    previous = frame.close.shift(1)
    tr = pd.concat([frame.high-frame.low, (frame.high-previous).abs(), (frame.low-previous).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def market_bullish(hourly, cfg=None):
    slow, market = (cfg.ema_slow, cfg.ema_market) if cfg else (50, 200)
    if len(hourly) < market+1:
        return False
    return bool(hourly.close.iloc[-1] > ema(hourly.close, market).iloc[-1] and
                ema(hourly.close, slow).iloc[-1] > ema(hourly.close, market).iloc[-1])


def quality(frame, cfg):
    if len(frame) < max(cfg.breakout_bars+1, cfg.volume_bars+1, cfg.atr_period+1):
        raise Rejected("Insufficient closed candles")
    recent = frame.iloc[-max(cfg.breakout_bars+1, cfg.volume_bars+1):]
    if recent.volume.median() <= 0 or (recent.volume == 0).any():
        raise Rejected("Illiquid/zero-volume bars")
    if (recent.close.pct_change().abs() > cfg.max_bar_move).any():
        raise Rejected("Anomalous candle return")
    if ((recent.high-recent.low)/recent.open > cfg.max_bar_move*2).any():
        raise Rejected("Anomalous intrabar movement")
    value = float(atr(frame, cfg.atr_period).iloc[-1])
    if not np.isfinite(value) or not 0 < value/float(frame.close.iloc[-1]) <= cfg.max_atr_fraction:
        raise Rejected("Unacceptable ATR")
    return value


def analyze(symbol, bars, hourly, cfg, *, bullish, listing=False, age_hours=None):
    value = quality(bars, cfg)
    if listing:
        if age_hours is None or not cfg.listing_min_hours <= age_hours <= cfg.listing_max_hours:
            raise Rejected("Listings: observe 24h; eligible only up to 7 days")
        lookback = max(cfg.consolidation_bars, cfg.volume_bars)
        previous = bars.iloc[-lookback-1:-1]
        if len(previous) < lookback:
            raise Rejected("Insufficient consolidation history")
        consolidation = bars.iloc[-cfg.consolidation_bars-1:-1]
        if consolidation.high.max()-consolidation.low.min() > cfg.consolidation_max_atr*value:
            raise Rejected("No 4h consolidation")
        level = float(consolidation.high.max())
        multiple = cfg.listing_volume_multiple
    else:
        if not bullish:
            raise Rejected("BTC regime blocks new LONG")
        if len(hourly) < cfg.ema_slow+1:
            raise Rejected("Insufficient 1H history")
        if not (ema(hourly.close, cfg.ema_fast).iloc[-1] > ema(hourly.close, cfg.ema_slow).iloc[-1] and
                hourly.close.iloc[-1] > ema(hourly.close, cfg.ema_slow).iloc[-1]):
            raise Rejected("1H EMA trend not confirmed")
        level = float(bars.high.iloc[-cfg.breakout_bars-1:-1].max())
        multiple = cfg.volume_multiple
    last = bars.iloc[-1]
    volume_base = bars.volume.iloc[-cfg.volume_bars-1:-1].median()
    if not float(last.close) > level:
        raise Rejected("No closed 15M breakout")
    if last.volume < multiple*volume_base:
        raise Rejected("Breakout volume too low")
    if float(last.close)-level > cfg.max_extension_atr*value:
        raise Rejected("Entry overextended from breakout")
    entry = dec(last.close)
    risk = dec(value)*dec(cfg.stop_atr)
    return Signal(symbol, "new_listings" if listing else "momentum", int(last.close_ms),
                  entry, entry-risk, entry+risk*dec(cfg.target_r), dec(value), dec(level),
                  f"Closed 15M breakout {level:.8g}; volume {last.volume/volume_base:.2f}x; ATR {value:.8g}")
