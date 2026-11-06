
import pytest
from conftest import frame, symbol_info

from smart_trader.config import dec
from smart_trader.filters import SymbolRules, floor_step
from smart_trader.market import closed_frame
from smart_trader.models import Rejected
from smart_trader.scanner import eligible, normalize
from smart_trader.strategies import analyze, market_bullish


def breakout_bars():
    f = frame(120, 900_000, trend=False)
    f.loc[f.index[-1], ["open", "close", "high", "volume"]] = [100, 101.1, 101.2, 220_000]
    return f


def test_momentum_and_market_filter(cfg):
    bars, hourly = breakout_bars(), frame()
    assert market_bullish(hourly)
    s = analyze("ETHUSDT", bars, hourly, cfg.strategy, bullish=True)
    assert s.stop < s.entry < s.target
    with pytest.raises(Rejected, match="BTC"):
        analyze("ETHUSDT", bars, hourly, cfg.strategy, bullish=False)


def test_listing_observation_and_short_history(cfg):
    bars = breakout_bars()
    with pytest.raises(Rejected, match="observe"):
        analyze("NEWUSDT", bars, frame(24), cfg.strategy, bullish=False, listing=True, age_hours=23.99)
    s = analyze("NEWUSDT", bars, frame(24), cfg.strategy, bullish=False, listing=True, age_hours=25)
    assert s.strategy == "new_listings"
    with pytest.raises(Rejected):
        analyze("NEWUSDT", bars, frame(24), cfg.strategy, bullish=False, listing=True, age_hours=169)


def test_low_volume_no_breakout_anomaly(cfg):
    f = breakout_bars()
    f.loc[f.index[-1], "volume"] = 100_000
    with pytest.raises(Rejected, match="volume"):
        analyze("ETHUSDT", f, frame(), cfg.strategy, bullish=True)
    f = breakout_bars()
    f.loc[f.index[-1], "close"] = 100
    with pytest.raises(Rejected, match="breakout"):
        analyze("ETHUSDT", f, frame(), cfg.strategy, bullish=True)
    f = breakout_bars()
    f.loc[f.index[-2], "close"] *= 2
    with pytest.raises(Rejected, match="Anomalous"):
        analyze("ETHUSDT", f, frame(), cfg.strategy, bullish=True)


def raw_klines():
    step = 900_000
    return [[i*step, "100", "101", "99", "100", "20", (i+1)*step-1, "2000", 10, "10", "1000", "0"] for i in range(25)]


def test_open_candle_excluded_and_future_cannot_change_signal(cfg):
    raw = raw_klines()
    now = raw[-1][0]+100
    a = closed_frame(raw, now, "15m")
    raw[-1][4] = "9999999"
    b = closed_frame(raw, now, "15m")
    assert a.equals(b)
    assert len(a) == 24


@pytest.mark.parametrize("mutation", ["duplicate", "gap", "nan", "bad_ohlc"])
def test_data_quality(mutation):
    raw = raw_klines()
    if mutation == "duplicate":
        raw[3] = raw[2]
    elif mutation == "gap":
        raw.pop(4)
    elif mutation == "nan":
        raw[3][4] = "NaN"
    else:
        raw[3][2] = "90"
    with pytest.raises(Rejected):
        closed_frame(raw, 10**15, "15m")


def test_bounded_normalization():
    scores = normalize([[1, 1, 1, 1, 1], [2, 2, 2, 2, 2], [1e300, 3, 3, 3, 3]], [.35, .2, .2, .15, .1])
    assert 0 <= min(scores) <= max(scores) <= 1
    assert scores[2] <= 1
    assert normalize([[999, 1, 1, 1, 1]], [.35, .2, .2, .15, .1]) == [.5]


def test_universe_excludes_nonspot_stables_leverage(cfg):
    for symbol in ("USDCUSDT", "USDSUSDT", "RLUSDUSDT", "BTCUPUSDT", "ETHDOWNUSDT"):
        assert not eligible(symbol_info(symbol), cfg.strategy)
    assert eligible(symbol_info("BTCUSDT"), cfg.strategy)
    assert eligible(symbol_info("JUPUSDT"), cfg.strategy)


def test_decimal_filters_and_lattice(rules):
    assert floor_step(dec("1.23456789"), dec(".00001")) == dec("1.23456")
    with pytest.raises(Rejected, match="tickSize"):
        rules.validate(dec(1), dec("100.001"))
    with pytest.raises(Rejected, match="stepSize"):
        rules.validate(dec("1.0001"), dec(100))
    with pytest.raises(Rejected, match="minimum"):
        rules.validate(dec(".001"), dec(100), market=True)
    raw = symbol_info()
    raw["filters"][1]["stepSize"] = ".003"
    raw["filters"][2]["stepSize"] = ".002"
    r = SymbolRules(raw)
    assert r.step == dec(".006")
    assert r.quantity(dec("1.001")) % dec(".003") == 0


def test_unknown_percent_and_max_position_filters():
    raw = symbol_info()
    raw["filters"].append({"filterType": "NEW_UNKNOWN_FILTER"})
    with pytest.raises(Rejected, match="Unknown"):
        SymbolRules(raw)
    raw = symbol_info()
    raw["filters"].extend([{"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierDown": ".8", "bidMultiplierUp": "1.2",
                           "askMultiplierDown": ".8", "askMultiplierUp": "1.2", "avgPriceMins": 5},
                          {"filterType": "MAX_POSITION", "maxPosition": "2"}])
    rules = SymbolRules(raw)
    with pytest.raises(Rejected, match="reference"):
        rules.validate(dec(1), dec(100))
    with pytest.raises(Rejected, match="band"):
        rules.validate(dec(1), dec(200), reference=dec(100))
    with pytest.raises(Rejected, match="MAX_POSITION"):
        rules.validate(dec(1), dec(100), reference=dec(100), base_position=dec(2))


def test_order_capacity():
    r = SymbolRules(symbol_info())
    with pytest.raises(Rejected, match="EXCHANGE_MAX_NUM_ORDERS"):
        r.capacity([], [], [{"filterType": "EXCHANGE_MAX_NUM_ORDERS", "maxNumOrders": 1}])
