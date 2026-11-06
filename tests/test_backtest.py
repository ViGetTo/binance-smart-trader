import importlib.util
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from smart_trader.analytics import metrics
from smart_trader.backtest import Backtester, Dataset, hourly_bars

spec = importlib.util.spec_from_file_location("offline_scenario", Path(__file__).parents[1]/"examples/offline_scenario.py")
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


@pytest.fixture(scope="module")
def fixture_root(tmp_path_factory):
    return example.make_fixture(tmp_path_factory.mktemp("history"))


def test_backtest_requires_explicit_liquidity_assumptions(fixture_root, cfg):
    with pytest.raises(ValueError, match="OHLCV"):
        Backtester(Dataset(fixture_root), cfg)


def test_stop_first_and_costs_and_benchmarks(fixture_root, cfg):
    result = Backtester(Dataset(fixture_root), cfg, allow_modelled_liquidity=True).run()
    assert result["trades"], "Fixture must actually exercise an entry and exit"
    first = result["trades"][0]
    assert first["exit_reason"] == "stop_first"
    assert float(first["pnl"]) < 0
    assert float(first["fees"]) > 0
    assert float(first["slippage"]) > 0
    assert result["report"]["evidence"] == "INSUFFICIENT"
    assert result["report"]["benchmarks"]["cash_net_pnl"] == 0
    assert "MODELLED" in result["report"]["warnings"][0]


def test_future_candles_cannot_change_past_trades(fixture_root, cfg):
    a, b = Dataset(fixture_root), Dataset(fixture_root)
    end_ms = int(a.frames["BTCUSDT"].open_ms.iloc[850])
    # Keep future data present, with entirely different prices and volume.
    for symbol, f in b.frames.items():
        mask = f.open_ms >= end_ms
        f.loc[mask, ["open", "high", "low", "close", "volume", "quote_volume"]] *= 4
        b.hours[symbol] = hourly_bars(f)
    one = Backtester(a, cfg, allow_modelled_liquidity=True).run(end_ms=end_ms)
    two = Backtester(b, cfg, allow_modelled_liquidity=True).run(end_ms=end_ms)
    assert one["trades"] == two["trades"]
    pd.testing.assert_series_equal(one["equity"], two["equity"])


def test_higher_costs_reduce_result_for_identical_trade(fixture_root, cfg):
    data = Dataset(fixture_root)
    cheap = Backtester(data, cfg, allow_modelled_liquidity=True).run()
    expensive_cfg = replace(cfg, strategy=replace(cfg.strategy, fee=.0015, slippage=.001))
    expensive = Backtester(data, expensive_cfg, allow_modelled_liquidity=True).run()
    assert len(cheap["trades"]) == len(expensive["trades"]) > 0
    assert expensive["report"]["fees"] > cheap["report"]["fees"]


def test_undefined_metrics_are_not_fake_zeroes():
    result = metrics([], [], 10_000)
    assert result["win_rate"] is None
    assert result["profit_factor"] is None
    assert result["sharpe_daily_365"] is None
    assert result["evidence"] == "INSUFFICIENT"


def test_cost_percentage_uses_gross_result_before_costs():
    result = metrics([{"pnl": -100, "fees": 10, "slippage": 5, "r_multiple": -1}], [], 10000)
    assert result["costs_pct_capital"] == pytest.approx(.15)
    assert result["costs_pct_gross_absolute_result"] == pytest.approx(15/85*100)


def test_no_partial_hour_lookahead():
    from conftest import frame
    f = frame(11, 900_000, end=12*900_000)
    hours = hourly_bars(f)
    assert all(hours.close_ms < f.close_ms.iloc[-1]+1)
    assert len(hours) == 2  # first group has only 3 bars, two complete groups follow.


def test_walk_forward_never_uses_test_for_selection(fixture_root, cfg, monkeypatch):
    data = Dataset(fixture_root)
    calls = []

    def fake_run(self, start_ms=None, end_ms=None):
        calls.append((start_ms, end_ms, self.settings.strategy.volume_multiple))
        return {"report": {"trades": 20, "net_pnl": -abs(self.settings.strategy.volume_multiple-1.5)}}

    monkeypatch.setattr(Backtester, "run", fake_run)
    folds = Backtester(data, cfg, allow_modelled_liquidity=True).walk_forward(train_days=1, test_days=1)
    assert folds
    for fold in folds:
        assert fold["train_end"] == fold["test_start"] < fold["test_end"]
        assert fold["selected_volume_multiple"] == 1.5
    assert all(a["test_end"] <= b["train_start"] for a, b in zip(folds, folds[1:]))
    assert len(calls) == 4*len(folds)
