import math
import os
import tomllib
from dataclasses import dataclass, field, fields
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

DEMO_URL = "https://demo-api.binance.com/api"
PUBLIC_URL = "https://api.binance.com/api"
PROFILE_KEYS = ("TRADER_MODE", "TRADER_DB", "TRADER_PAPER_LAB", "BINANCE_ENABLED",
                "BINANCE_MARKET_DATA_ENABLED", "PAPER_INITIAL_BALANCE")


class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    DEMO = "DEMO"


@dataclass(frozen=True)
class StrategyConfig:
    signal_interval: str = "15m"
    scan_seconds: int = 300
    monitor_seconds: int = 5
    min_volume: float = 20_000_000
    listing_min_volume: float = 10_000_000
    max_spread: float = .0015
    listing_max_spread: float = .002
    breakout_bars: int = 20
    volume_bars: int = 20
    ema_fast: int = 20
    ema_slow: int = 50
    ema_market: int = 200
    hourly_bars: int = 260
    quarter_bars: int = 120
    volume_multiple: float = 1.7
    pullback_volume_multiple: float = 1.2
    pullback_touch_atr: float = .25
    listing_volume_multiple: float = 2
    atr_period: int = 14
    stop_atr: float = 1.8
    target_r: float = 2.5
    min_net_rr: float = 2
    max_extension_atr: float = .5
    consolidation_bars: int = 16
    consolidation_max_atr: float = 6
    max_bar_move: float = .15
    max_atr_fraction: float = .08
    depth_multiple: float = 5
    depth_band: float = .002
    top_candidates: int = 3
    rank_weights: list[float] = field(default_factory=lambda: [.35, .2, .2, .15, .1])
    fee: float = .001
    slippage: float = .0005
    listing_slippage: float = .002
    paper_participation: float = .05
    quote_max_age: int = 10
    listing_min_hours: int = 24
    listing_max_hours: int = 168
    excluded_assets: list[str] = field(default_factory=lambda: [
        "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "USDD", "USDS", "RLUSD", "USDG",
        "PYUSD", "FRAX", "LUSD", "GHO", "SUSD", "USDTB", "USD0", "USDF", "UST", "USTC", "USDJ",
        "EUR", "AEUR", "EURI", "PAXG", "XAUT", "WBTC", "WBETH", "BETH"])
    excluded_symbols: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.signal_interval not in {"5m", "15m"}:
            raise ValueError("Signal interval must be 5m or 15m")
        for name in ("scan_seconds", "monitor_seconds", "breakout_bars", "volume_bars", "ema_fast", "ema_slow",
                     "ema_market", "hourly_bars", "quarter_bars", "atr_period", "consolidation_bars",
                     "top_candidates", "listing_min_hours", "listing_max_hours", "quote_max_age"):
            if not isinstance(getattr(self, name), int) or isinstance(getattr(self, name), bool):
                raise ValueError(f"Strategy setting requires integer: {name}")
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid strategy setting: {f.name}")
        if len(self.rank_weights) != 5 or any(not math.isfinite(v) or v < 0 for v in self.rank_weights) or abs(sum(self.rank_weights)-1) > 1e-9:
            raise ValueError("Five finite ranking weights must sum to 1")
        if not 1 <= self.ema_fast < self.ema_slow < self.ema_market < self.hourly_bars <= 1000:
            raise ValueError("EMA spans require enough hourly history, at most 1000 bars")
        if not max(97, self.breakout_bars+1, self.volume_bars+1, self.consolidation_bars+1, self.atr_period+1) <= self.quarter_bars <= 1000:
            raise ValueError("Insufficient 15M history configuration")
        if not 1 <= self.top_candidates <= 3 or self.consolidation_bars < 16:
            raise ValueError("Up to 3 candidates; new listings require at least 4h consolidation")
        if self.listing_min_hours < 24 or self.listing_max_hours > 168 or self.listing_min_hours > self.listing_max_hours:
            raise ValueError("New listings must be observed 24h and traded only within 7 days")
        if not 0 < self.paper_participation <= 1 or min(self.scan_seconds, self.monitor_seconds, self.atr_period) <= 0:
            raise ValueError("Invalid timing or participation")
        if not 0 <= self.fee < .1 or not 0 <= self.slippage <= self.listing_slippage < .1:
            raise ValueError("Invalid execution costs")
        if min(self.stop_atr, self.target_r, self.min_net_rr, self.depth_multiple, self.quote_max_age, self.breakout_bars) <= 0:
            raise ValueError("Invalid strategy bounds")


@dataclass(frozen=True)
class RiskConfig:
    initial_balance: float = 10_000
    trade_risk: float = .005
    listing_risk: float = .002
    max_positions: int = 3
    max_exposure: float = .25
    listing_max_position: float = .05
    daily_loss: float = .02
    weekly_loss: float = .05
    consecutive_losses: int = 3
    pause_hours: int = 12
    max_correlation: float = .85
    correlation_bars: int = 48

    def validate(self):
        for f in fields(self):
            if not isinstance(getattr(self, f.name), (int, float)) or not math.isfinite(getattr(self, f.name)):
                raise ValueError(f"Risk setting must be finite numeric: {f.name}")
        for name in ("max_positions", "consecutive_losses", "correlation_bars"):
            if not isinstance(getattr(self, name), int) or isinstance(getattr(self, name), bool):
                raise ValueError(f"Risk setting requires integer: {name}")
        for name in ("trade_risk", "listing_risk", "max_exposure", "listing_max_position",
                     "daily_loss", "weekly_loss", "max_correlation"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"Invalid risk setting: {name}")
        if self.listing_risk > .002 or self.listing_max_position > .05:
            raise ValueError("New listings hard caps: risk .2%, position 5%")
        if not 1 <= self.max_positions <= 3 or self.initial_balance <= 0:
            raise ValueError("Invalid portfolio limits")
        if self.pause_hours < 1 or self.consecutive_losses < 1 or self.correlation_bars < 20:
            raise ValueError("Invalid risk window")


@dataclass(frozen=True)
class SelectionConfig:
    enabled: bool = True
    min_trades: int = 20
    min_days: int = 30
    lookback_days: int = 90
    confidence_z: float = 1.96

    def __post_init__(self):
        if not isinstance(self.enabled, bool) or self.min_trades < 10 or self.min_days < 7:
            raise ValueError("Strategy selection requires at least 10 trades / 7 days")
        if self.lookback_days < self.min_days or not 1 <= self.confidence_z <= 3:
            raise ValueError("Invalid strategy selection window")


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.PAPER
    db_path: str = "data/trader.sqlite"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    demo_key: str = field(default="", repr=False)
    demo_secret: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_chat: str = field(default="", repr=False)
    strict_demo: bool = True
    demo_balance_sync: bool = True
    web_enabled: bool = False
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    paper_lab: bool = False
    market_data_enabled: bool = True
    binance_enabled: bool = True
    auto_update: bool = True
    account_id: str = ""
    account_label: str = ""
    paper_profiles: dict = field(default_factory=dict)

    @classmethod
    def load(cls):
        # Do not inject dotenv values into os.environ: configuration/Telegram reloads must see edits.
        local = dotenv_values(".env", interpolate=False)
        env = {**local, **os.environ}
        # An explicit choice in trade config must survive inherited Termux/proot variables.
        # Recognize the PAPER choice already written by v0.3 as well as new marked profiles.
        saved_paper = (str(local.get("TRADER_MODE", "")).upper() == "PAPER"
                       and str(local.get("TRADER_PAPER_LAB", "")).lower() == "true"
                       and str(local.get("BINANCE_ENABLED", "")).lower() == "false")
        if local.get("TRADER_PROFILE_SOURCE") == "local" or saved_paper:
            env.update({name: local[name] for name in PROFILE_KEYS if name in local})
        p = Path(env.get("TRADER_CONFIG", "config.toml"))
        raw = tomllib.loads(p.read_text("utf-8")) if p.exists() else {}
        def flag(name, default):
            value = str(env.get(name, default)).lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError(f"Invalid boolean: {name}")
            return value in {"true", "1"}
        selection = dict(raw.get("selection", {}))
        if "TRADER_STRATEGY_AUTO" in env:
            selection["enabled"] = flag("TRADER_STRATEGY_AUTO", "true")
        mode = Mode(env.get("TRADER_MODE", "PAPER").upper())
        binance_enabled = flag("BINANCE_ENABLED", "true" if mode == Mode.DEMO else "false")
        risk = dict(raw.get("risk", {}))
        if "PAPER_INITIAL_BALANCE" in env:
            risk["initial_balance"] = float(env["PAPER_INITIAL_BALANCE"])
        lab_default = "true" if env.get("TRADER_DB", "data/paper-lab.sqlite") == "data/paper-lab.sqlite" else "false"
        cfg = cls(mode=mode,
                  db_path=env.get("TRADER_DB", "data/paper-lab.sqlite" if mode == Mode.PAPER else "data/demo.sqlite"),
                  strategy=StrategyConfig(**raw.get("strategy", {})), risk=RiskConfig(**risk),
                  demo_key=env.get("BINANCE_DEMO_API_KEY", ""), demo_secret=env.get("BINANCE_DEMO_API_SECRET", ""),
                  telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""), telegram_chat=env.get("TELEGRAM_CHAT_ID", ""),
                  strict_demo=flag("TRADER_STRICT_DEMO", "true"),
                  demo_balance_sync=flag("TRADER_DEMO_SYNC_BALANCE", "true"),
                  web_enabled=flag("TRADER_WEB_ENABLED", "false"), selection=SelectionConfig(**selection),
                  paper_lab=mode == Mode.PAPER and flag("TRADER_PAPER_LAB", lab_default),
                  market_data_enabled=flag("BINANCE_MARKET_DATA_ENABLED", "true"),
                  binance_enabled=binance_enabled, auto_update=flag("TRADER_AUTO_UPDATE", "true"),
                  paper_profiles=raw.get("paper_profiles", {}))
        cfg.risk.validate()
        s = cfg.strategy
        if len(s.rank_weights) != 5 or abs(sum(s.rank_weights) - 1) > 1e-9 or min(s.rank_weights) < 0:
            raise ValueError("Five nonnegative rank weights must sum to 1")
        if not 0 < s.paper_participation <= 1 or min(s.scan_seconds, s.monitor_seconds, s.atr_period) <= 0:
            raise ValueError("Invalid strategy timing or participation")
        if not 0 <= s.fee < .1 or not 0 <= s.slippage <= s.listing_slippage < .1:
            raise ValueError("Invalid execution costs")
        if min(s.stop_atr, s.target_r, s.min_net_rr, s.depth_multiple, s.quote_max_age) <= 0:
            raise ValueError("Invalid strategy limits")
        return cfg


def dec(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite trading value")
    return result
