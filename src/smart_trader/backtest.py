import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from .analytics import metrics
from .config import dec
from .db import Database, encode
from .filters import SymbolRules
from .market import COLUMNS, closed_frame
from .models import Book, Rejected
from .risk import RiskManager
from .scanner import eligible, normalize
from .strategies import analyze, ema, market_bullish, quality


def hourly_bars(f):
    data = f.copy()
    data["group"] = data.open_ms//3_600_000
    hours = data.groupby("group").agg(open_ms=("open_ms", "first"), open=("open", "first"),
              high=("high", "max"), low=("low", "min"), close=("close", "last"),
              volume=("volume", "sum"), quote_volume=("quote_volume", "sum"),
              close_ms=("close_ms", "last"), count=("open_ms", "count"))
    return hours[(hours["count"] == 4) & (hours.open_ms % 3_600_000 == 0)].reset_index(drop=True)


class Dataset:
    def __init__(self, directory):
        root = Path(directory)
        self.manifest = json.loads((root/"universe.json").read_text("utf-8"))
        self.frames, self.hours = {}, {}
        for symbol, meta in self.manifest["symbols"].items():
            path = root/f"{symbol}.csv"
            if not path.exists():
                raise ValueError(f"Missing history for manifest symbol {symbol}")
            if meta.get("listed_ms") is None:
                raise ValueError("Actual first available candle estimate required; CSV start is not listing time")
            f = pd.read_csv(path)
            f = closed_frame(f[COLUMNS].values.tolist(), 10**16, "15m")
            if f.empty:
                raise ValueError(f"Empty history for {symbol}")
            self.frames[symbol] = f
            self.hours[symbol] = hourly_bars(f)
        if "BTCUSDT" not in self.frames:
            raise ValueError("BTCUSDT history is mandatory")

    def rules(self, symbol, timestamp):
        meta = self.manifest["symbols"][symbol]
        if not meta["listed_ms"] <= timestamp < meta.get("delisted_ms", 10**16):
            raise Rejected("Outside historical listing window")
        intervals = meta.get("trading_intervals")
        if intervals is not None and not any(start <= timestamp < end for start, end in intervals):
            raise Rejected("Not historically TRADING")
        snapshots = [s for s in meta["rules"] if s["effective_ms"] <= timestamp]
        if not snapshots:
            raise Rejected("No exchange rules known at this time")
        return SymbolRules(max(snapshots, key=lambda s: s["effective_ms"])["data"])


class Backtester:
    def __init__(self, dataset, settings, *, allow_modelled_liquidity=False):
        if not allow_modelled_liquidity:
            raise ValueError("OHLCV cannot establish historical spread/depth. Pass --allow-modelled-liquidity explicitly")
        self.data, self.settings = dataset, settings

    def run(self, start_ms=None, end_ms=None):
        cfg = self.settings.strategy
        initial = dec(self.settings.risk.initial_balance)
        db = Database(":memory:", initial)
        risk = RiskManager(db, self.settings)
        trades, curve = [], {}
        strategy_curves = {s: {} for s in ("momentum", "new_listings")}
        btc = self.data.frames["BTCUSDT"]
        # 201 complete hours of causal indicator warmup before the evaluated period.
        first = int(self.data.hours["BTCUSDT"].close_ms.iloc[cfg.ema_market])+1 if len(self.data.hours["BTCUSDT"]) > cfg.ema_market else 10**16
        begin = max(first, start_ms or first)
        stop = min(end_ms or 10**16, int(btc.close_ms.iloc[-1])+1)
        timeline = btc[(btc.open_ms >= begin) & (btc.close_ms < stop)].open_ms.astype(int).tolist()
        if len(timeline) < 2:
            raise ValueError("Not enough evaluation data after 201h warmup")
        indexed = {s: f.set_index("open_ms", drop=False) for s, f in self.data.frames.items()}

        def mark(timestamp, use_open=False):
            equity = dec(db.get("cash"))
            for p in db.positions():
                frame = self.data.frames[p["symbol"]]
                eligible_rows = frame[frame.open_ms <= timestamp] if use_open else frame[frame.close_ms < timestamp]
                if eligible_rows.empty:
                    raise ValueError("Cannot mark historical position")
                last = eligible_rows.iloc[-1]
                p["mark"] = str(last.open if use_open and last.open_ms == timestamp else last.close)
                db.save_position(p)
                equity += dec(p["qty"])*dec(p["mark"])
            return equity

        def close(p, price, timestamp, reason):
            slip = dec(cfg.listing_slippage if p["strategy"] == "new_listings" else cfg.slippage)
            quoted = dec(price)*dec(p["qty"])
            proceeds = quoted*(1-slip)
            fee = proceeds*dec(cfg.fee)
            proceeds -= fee
            pnl = proceeds-dec(p["cost"])
            trade = dict(p, pnl=str(pnl), r_multiple=str(pnl/dec(p["risk"])),
                         fees=str(dec(p["entry_fee"])+fee), slippage=str(dec(p["slippage"])+quoted*slip),
                         closed_ms=timestamp, exit_reason=reason)
            trades.append(trade)
            db.set("cash", str(dec(db.get("cash"))+proceeds))
            db.execute("DELETE FROM positions WHERE id=?", (p["id"],))
            db.execute("UPDATE intents SET status='CLOSED' WHERE id=?", (p["id"],))
            risk.closed_trade(pnl, timestamp/1000)

        for timestamp in timeline:
            # At bar open only preceding completed bars and this open are visible.
            risk.observe(mark(timestamp, use_open=True), timestamp/1000)
            prefixes = {s: f[f.close_ms < timestamp].tail(cfg.quarter_bars) for s, f in self.data.frames.items()}
            hours = {s: h[h.close_ms < timestamp].tail(cfg.hourly_bars) for s, h in self.data.hours.items()}
            histories = {s: h.set_index("close_ms").close.pct_change().dropna() for s, h in hours.items()}
            bullish = market_bullish(hours["BTCUSDT"], cfg)
            groups = {"momentum": [], "new_listings": []}
            btc_past = prefixes["BTCUSDT"]
            btc_return = float(btc_past.close.iloc[-1]/btc_past.close.iloc[-97]-1)*100 if len(btc_past) >= 97 else 0
            for symbol, f in prefixes.items():
                try:
                    if timestamp not in indexed[symbol].index or len(f) < 97 or f.close_ms.iloc[-1] != timestamp-1:
                        continue
                    rules = self.data.rules(symbol, timestamp-1)
                    if not eligible(rules.raw, cfg):
                        continue
                    meta = self.data.manifest["symbols"][symbol]
                    age = (timestamp-meta["listed_ms"])/3_600_000
                    listing = age <= cfg.listing_max_hours
                    quote_volume = float(f.quote_volume.iloc[-96:].sum())
                    if quote_volume < (cfg.listing_min_volume if listing else cfg.min_volume):
                        continue
                    value = quality(f, cfg)
                    h = hours[symbol]
                    trend = float(ema(h.close, cfg.ema_fast).iloc[-1]/ema(h.close, cfg.ema_slow).iloc[-1]-1) if len(h) >= cfg.ema_slow else 0
                    persistence = float((h.close.iloc[-cfg.ema_fast:] > ema(h.close, cfg.ema_fast).iloc[-cfg.ema_fast:]).mean())
                    liquidity = float(meta.get("assumed_depth_usdt", 100_000))
                    spread = float(meta.get("assumed_spread", .001))
                    if spread > (cfg.listing_max_spread if listing else cfg.max_spread):
                        continue
                    components = [float(f.close.iloc[-1]/f.close.iloc[-97]-1)*100-btc_return,
                                  trend+persistence*.01, float(f.volume.iloc[-1]/f.volume.iloc[-cfg.volume_bars-1:-1].median()),
                                  np.log1p(liquidity)/(1+spread*1000), -value/float(f.close.iloc[-1])]
                    try:
                        signal = analyze(symbol, f, h, cfg, bullish=bullish, listing=listing, age_hours=age)
                    except Rejected:
                        signal = None
                    groups["new_listings" if listing else "momentum"].append((components, signal, rules))
                except Rejected:
                    continue
            selected = []
            for name, group in groups.items():
                if group:
                    scores = normalize([x[0] for x in group], cfg.rank_weights)
                    ranked = sorted(zip(scores, group, strict=True), key=lambda x: -x[0])
                    selected.extend(x[1] for x in ranked[:1 if name == "new_listings" else cfg.top_candidates])
            for _, signal, rules in selected:
                if signal is None:
                    continue
                row = indexed[signal.symbol].loc[timestamp]
                meta = self.data.manifest["symbols"][signal.symbol]
                spread = dec(meta.get("assumed_spread", .001))
                ask = dec(row.open)*(1+spread/2)
                bid = dec(row.open)*(1-spread/2)
                depth = dec(meta.get("assumed_depth_usdt", 100_000))
                book = Book([(bid, depth/bid)], [(ask, depth/ask)], timestamp/1000)
                try:
                    plan = risk.approve(signal, rules, book, "BACKTEST", histories,
                                        now=timestamp/1000, reference=dec(prefixes[signal.symbol].close.iloc[-1]))
                    # Conservative bar-level participation uses the preceding bar's volume.
                    max_qty = rules.quantity(dec(prefixes[signal.symbol].volume.iloc[-1])*dec(cfg.paper_participation))
                    qty = min(plan.qty, max_qty)
                    rules.validate(qty, plan.entry, market=True)
                    if qty <= 0:
                        continue
                    cost = qty*plan.entry
                    fee = cost*dec(cfg.fee)
                    p = {"id": signal.key, "symbol": signal.symbol, "strategy": signal.strategy, "venue": "BACKTEST",
                         "qty": str(qty), "entry": str(plan.entry), "cost": str(cost+fee), "entry_fee": str(fee),
                         "stop": str(plan.stop), "target": str(plan.target), "risk": str(plan.risk*qty/plan.qty),
                         "slippage": str(qty*(plan.entry-ask)), "opened_ms": timestamp, "mark": str(plan.entry)}
                    db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?)", (signal.key, signal.symbol, signal.strategy,
                               "BACKTEST", "OPEN", encode(plan), timestamp/1000))
                    db.set("cash", str(dec(db.get("cash"))-cost-fee))
                    db.save_position(p)
                    risk.observe(mark(timestamp, use_open=True), timestamp/1000)
                except Rejected:
                    continue
            # Only after entries are decided do we reveal this bar's high/low/close.
            for p in db.positions():
                f = indexed[p["symbol"]]
                if timestamp not in f.index:
                    # Delisted/data unavailable: liquidate at last observable close with costs, disclose assumption.
                    previous = prefixes[p["symbol"]].iloc[-1]
                    close(p, previous.close, timestamp, "data_end_assumed_liquidation")
                    continue
                row = f.loc[timestamp]
                if dec(row.low) <= dec(p["stop"]):
                    close(p, min(dec(row.open), dec(p["stop"])), int(row.close_ms), "stop_first")
                elif dec(row.high) >= dec(p["target"]):
                    close(p, dec(p["target"]), int(row.close_ms), "target")
            risk.observe(mark(timestamp+900_000), (timestamp+900_000)/1000)
            curve[timestamp+900_000] = float(db.get("equity"))
            for strategy in strategy_curves:
                strategy_curves[strategy][timestamp+900_000] = db.rows(
                    "SELECT equity FROM strategy_equity WHERE strategy=? ORDER BY ts DESC LIMIT 1", (strategy,))[0]["equity"]
        for p in db.positions():
            last = self.data.frames[p["symbol"]]
            last = last[last.close_ms < timeline[-1]+900_000].iloc[-1]
            close(p, last.close, timeline[-1]+900_000, "end_of_window")
        curve[timeline[-1]+900_000] = float(db.get("cash"))
        risk.observe(dec(db.get("cash")), (timeline[-1]+900_000)/1000)
        for strategy in strategy_curves:
            strategy_curves[strategy][timeline[-1]+900_000] = db.rows(
                "SELECT equity FROM strategy_equity WHERE strategy=? ORDER BY ts DESC LIMIT 1", (strategy,))[0]["equity"]
        # Backtest realized trades live in the research ledger, not the runtime trades table.
        # Attribute cumulative PnL below directly to the historical trade and position records.
        for strategy in strategy_curves:
            for ts in strategy_curves[strategy]:
                realized = sum(float(t["pnl"]) for t in trades if t["strategy"] == strategy and t["closed_ms"] <= ts)
                strategy_curves[strategy][ts] += realized
        equity = pd.Series(curve)
        equity.index = pd.to_datetime(equity.index, unit="ms", utc=True)
        equity = pd.concat([pd.Series([float(initial)], index=pd.to_datetime([timeline[0]], unit="ms", utc=True)), equity])
        report = metrics(trades, equity, float(initial))
        report.update(start_ms=timeline[0], end_ms=timeline[-1]+900_000,
                      by_strategy={}, warnings=["MODELLED spread/depth, historical executions are not observed",
                      "Coverage limited to supplied universe; include delisted symbols to reduce survivorship bias",
                      "No OHLCV reconstruction of order queues, outages, halts, or extreme gap execution",
                      "At data end positions assume liquidation at last observable close; delisting may be worse",
                      "Require 30–60 days forward PAPER/DEMO and sufficient trades; no profitability claim"])
        report["warnings"].extend(self.data.manifest.get("limitations", []))
        for strategy in groups:
            subset = [t for t in trades if t["strategy"] == strategy]
            attributed = pd.Series(strategy_curves[strategy])
            attributed.index = pd.to_datetime(attributed.index, unit="ms", utc=True)
            attributed = pd.concat([pd.Series([float(initial)], index=equity.index[:1]), attributed])
            stats = metrics(subset, attributed, float(initial))
            stats["equity_basis"] = "Initial portfolio budget + attributed realized and marked strategy PnL; shared limits"
            report["by_strategy"][strategy] = stats
        report["benchmarks"] = self._benchmarks(timeline)
        db.close()
        return {"report": report, "trades": trades, "equity": equity}

    def _benchmarks(self, timeline):
        cfg = self.settings.strategy
        f = self.data.frames["BTCUSDT"]
        f = f[(f.open_ms >= timeline[0]) & (f.open_ms <= timeline[-1])]
        cost = cfg.fee+cfg.slippage
        initial = self.settings.risk.initial_balance
        hold = initial*(1-cost)*float(f.close.iloc[-1]/f.open.iloc[0])*(1-cost)-initial
        hours = self.data.hours["BTCUSDT"]
        hours = hours.assign(fast=ema(hours.close, 20), slow=ema(hours.close, 50))
        cash, qty = initial, 0.
        for _, row in f.iterrows():
            past = hours[hours.close_ms < row.open_ms]
            enter = not past.empty and past.fast.iloc[-1] > past.slow.iloc[-1]
            if enter and not qty:
                qty, cash = cash*(1-cost)/row.open, 0.
            elif not enter and qty:
                cash, qty = qty*row.open*(1-cost), 0.
        ending = cash+qty*float(f.close.iloc[-1])*(1-cost)
        return {"btc_buy_hold_net_pnl": hold, "btc_ema20_50_net_pnl": ending-initial, "cash_net_pnl": 0.,
                "note": "Same evaluation window, 100% allocation, explicit fees/slippage; exposure differs from strategy"}

    def sensitivity(self, start_ms=None, end_ms=None):
        rows = []
        for fee_multiple in (1, 1.5, 2):
            for slip_multiple in (1, 2, 4):
                cfg = replace(self.settings.strategy, fee=self.settings.strategy.fee*fee_multiple,
                              slippage=self.settings.strategy.slippage*slip_multiple,
                              listing_slippage=self.settings.strategy.listing_slippage*slip_multiple)
                result = Backtester(self.data, replace(self.settings, strategy=cfg), allow_modelled_liquidity=True).run(start_ms, end_ms)
                rows.append({"fee_multiple": fee_multiple, "slippage_multiple": slip_multiple, **result["report"]})
        return rows

    def walk_forward(self, train_days=60, test_days=30):
        if train_days < 1 or test_days < 1:
            raise ValueError("Positive train/test windows required")
        h = self.data.hours["BTCUSDT"]
        if len(h) < self.settings.strategy.ema_market+2:
            raise ValueError("Insufficient warmup")
        cursor = int(h.close_ms.iloc[self.settings.strategy.ema_market])+1
        end = int(self.data.frames["BTCUSDT"].close_ms.iloc[-1])+1
        folds = []
        day = 86_400_000
        while cursor+(train_days+test_days)*day <= end:
            train_end = cursor+train_days*day
            test_end = train_end+test_days*day
            choices = []
            for multiplier in (1.5, 1.7, 2.0):
                cfg = replace(self.settings.strategy, volume_multiple=multiplier)
                runner = Backtester(self.data, replace(self.settings, strategy=cfg), allow_modelled_liquidity=True)
                train = runner.run(cursor, train_end)["report"]
                choices.append((multiplier, train))
            valid = [choice for choice in choices if choice[1]["trades"] >= 10]
            chosen = max(valid, key=lambda x: x[1]["net_pnl"])[0] if valid else self.settings.strategy.volume_multiple
            cfg = replace(self.settings.strategy, volume_multiple=chosen)
            test = Backtester(self.data, replace(self.settings, strategy=cfg), allow_modelled_liquidity=True).run(train_end, test_end)
            folds.append({"train_start": cursor, "train_end": train_end, "test_start": train_end, "test_end": test_end,
                          "selected_volume_multiple": chosen, "train_choices": choices,
                          "selection_evidence": "insufficient; default retained" if not valid else "train only; research",
                          "test": test["report"]})
            cursor = test_end  # All train/test blocks are disjoint, also across folds.
        if not folds:
            raise ValueError("No complete disjoint train/test fold fits the history")
        return folds
