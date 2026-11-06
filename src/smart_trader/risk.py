import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from .config import RiskConfig, dec
from .models import Plan, Rejected
from .strategies import STRATEGIES


class RiskManager:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def config(self):
        cfg = RiskConfig(**self.db.get("risk_overrides", {})) if self.db.get("risk_overrides") else self.settings.risk
        cfg.validate()
        return cfg

    def observe(self, equity, now=None):
        if not equity.is_finite() or equity <= 0:
            raise ValueError("Invalid account equity")
        moment = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc)
        daily = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        weekly = daily-timedelta(days=daily.weekday())
        for name, start in (("day", daily), ("week", weekly)):
            key = start.isoformat()
            anchor = self.db.get(f"{name}_anchor")
            if anchor is None or anchor["key"] != key:
                previous = self.db.rows("SELECT equity FROM equity WHERE ts<=? ORDER BY ts DESC LIMIT 1",
                                        (start.timestamp(),))
                baseline = dec(previous[0]["equity"]) if previous else equity
                self.db.set(f"{name}_anchor", {"key": key, "equity": str(baseline)})
        self.db.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)",
                        (moment.timestamp(), float(equity), float(dec(self.db.get("cash")))))
        self.db.set("equity", str(equity))
        initial = dec(self.db.get("initial"))
        realized = {row["strategy"]: dec(row["pnl"]) for row in self.db.rows(
            "SELECT strategy,SUM(CAST(json_extract(data,'$.pnl') AS REAL)) AS pnl FROM trades GROUP BY strategy")}
        positions = self.db.positions()
        for strategy in STRATEGIES:
            pnl = realized.get(strategy, Decimal(0))
            for p in positions:
                if p["strategy"] == strategy:
                    pnl += dec(p.get("proceeds", 0))+dec(p["qty"])*dec(p.get("mark", p["entry"]))-dec(p.get("cost", dec(p["qty"])*dec(p["entry"])))
            self.db.execute("INSERT OR REPLACE INTO strategy_equity VALUES(?,?,?)",
                            (moment.timestamp(), strategy, float(initial+pnl)))

    def approve(self, signal, rules, book, venue, histories, *, now=None, reference=None,
                base_position=Decimal(0), reserved_key=None):
        try:
            return self._approve(signal, rules, book, venue, histories, now=now,
                                 reference=reference, base_position=base_position, reserved_key=reserved_key)
        except Rejected:
            raise
        except Exception:
            self.db.halt("Risk Manager error: entries disabled")
            raise

    def _approve(self, s, rules, book, venue, histories, *, now=None, reference=None,
                 base_position=Decimal(0), reserved_key=None):
        r, c = self.config(), self.settings.strategy
        if s.strategy not in STRATEGIES:
            raise ValueError("Unknown strategy")
        now = time.time() if now is None else now
        if self.db.get("kill", False):
            raise Rejected("Kill switch is active")
        if now < self.db.get("pause_until", 0):
            raise Rejected("Consecutive losses cooldown")
        if now-book.received > c.quote_max_age or book.received-now > 5:
            raise Rejected("Stale book")
        if not s.stop < s.entry < s.target or s.atr <= 0:
            raise ValueError("Malformed strategy signal")
        if now*1000-s.candle_ms > 900_000+30_000 or s.candle_ms > now*1000:
            raise Rejected("Stale/future signal")
        positions = self.db.positions()
        if any(p["symbol"] == s.symbol for p in positions):
            raise Rejected("Position already exists")
        if len(positions) >= r.max_positions:
            raise Rejected("Maximum open positions")
        if self.db.rows("SELECT id FROM intents WHERE status NOT IN ('OPEN','CLOSED','REJECTED') AND id!=?",
                        (reserved_key or "",)):
            raise Rejected("Pending/uncertain execution blocks entries")
        if reserved_key != s.key and self.db.rows("SELECT id FROM intents WHERE id=?", (s.key,)):
            raise Rejected("Signal already consumed")
        listing = s.strategy == "new_listings"
        if listing and any(p["strategy"] == "new_listings" for p in positions):
            raise Rejected("Only one new-listing position")
        equity = dec(self.db.get("equity", self.db.get("cash")))
        if venue == "DEMO" and self.settings.demo_balance_sync:
            if not self.db.get("demo_balance_initialized", False):
                raise Rejected("Demo account balance has not been verified")
            remote_cash = dec(self.db.get("demo_free_usdt", 0))
            held_value = sum((dec(p["qty"])*dec(p.get("mark", p["entry"])) for p in positions), Decimal(0))
            equity = min(equity, remote_cash+held_value)
        if equity <= 0:
            raise ValueError("Nonpositive equity")
        for name, limit in (("day", r.daily_loss), ("week", r.weekly_loss)):
            anchor = self.db.get(f"{name}_anchor")
            if anchor is None:
                raise ValueError("Missing risk period anchor")
            if equity <= dec(anchor["equity"])*(1-dec(limit)):
                raise Rejected(f"{name} loss limit reached")
        for p in positions:
            a, b = histories.get(s.symbol), histories.get(p["symbol"])
            if a is None or b is None:
                raise Rejected("Correlation data missing")
            aligned = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna().iloc[-r.correlation_bars:]
            if len(aligned) < r.correlation_bars:
                raise Rejected("Insufficient aligned correlation history")
            correlation = aligned.a.corr(aligned.b)
            if not np.isfinite(correlation) or correlation >= r.max_correlation:
                raise Rejected("Correlated exposure")
        maximum = c.listing_max_spread if listing else c.max_spread
        if book.spread > maximum:
            raise Rejected("Spread limit")
        slip = dec(c.listing_slippage if listing else c.slippage)
        fee = dec(c.fee)
        entry = rules.price(book.ask*(1+slip), up=True)
        stop = rules.price(entry-(s.entry-s.stop))
        target = rules.price(entry+(s.target-s.entry))
        if entry < s.breakout:
            raise Rejected("Price fell back below breakout")
        if entry-s.breakout > s.atr*dec(c.max_extension_atr):
            raise Rejected("Entry moved beyond ATR extension")
        # Reserve one tick for rounding levels around the eventual average execution price.
        loss = entry-stop+rules.tick+fee*(entry+stop)+stop*slip
        gain = target-entry-fee*(target+entry)-target*slip
        if stop <= 0 or loss <= 0 or gain/loss < dec(c.min_net_rr):
            raise Rejected("Net reward/risk below minimum")
        exposure = sum((dec(p["qty"])*dec(p.get("mark", p["entry"])) for p in positions), Decimal(0))
        room = equity*dec(r.max_exposure)-exposure
        if listing:
            room = min(room, equity*dec(r.listing_max_position))
        cash = dec(self.db.get("cash"))
        if venue == "DEMO" and self.settings.demo_balance_sync:
            cash = min(cash, dec(self.db.get("demo_free_usdt", 0)))
        budget = equity*dec(r.listing_risk if listing else r.trade_risk)
        qty = rules.quantity(min(budget/loss, room/entry, cash/(entry*(1+fee))))
        for q in (qty, rules.quantity(qty*(1-fee))):
            # Reserve enough tradable base quantity after a potential base-asset fee.
            rules.validate(q, entry, market=True, reference=reference, base_position=base_position)
            rules.validate(q, stop, side="SELL", reference=reference)
            rules.validate(q, target, side="SELL", reference=reference)
        if book.depth(c.depth_band) < qty*entry*dec(c.depth_multiple):
            raise Rejected("Order too large for book depth")
        return Plan(s, qty, entry, stop, target, qty*loss, venue)

    def closed_trade(self, pnl, now=None):
        now = now if now is not None else time.time()
        streak = self.db.get("loss_streak", 0)+1 if pnl < 0 else 0
        self.db.set("loss_streak", streak)
        cfg = self.config()
        if streak >= cfg.consecutive_losses:
            self.db.set("pause_until", now+cfg.pause_hours*3600)
            self.db.set("loss_streak", 0)
            self.db.event("RISK", "Three losses: entries paused", True)
