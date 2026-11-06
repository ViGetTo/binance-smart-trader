"""Three isolated virtual portfolios, sharing one public market-data connection."""
import asyncio
import copy
import json
import time
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path

from .analytics import database_report
from .api import APIError, BinanceAPI
from .config import Mode
from .db import Database, encode
from .models import Rejected
from .runtime import ProcessLock, Trader

PROFILES = {
    "base": ("Базовый", {}, {}),
    "aggressive": ("Рискованный", {"trade_risk": .02, "max_exposure": .75,
                                   "daily_loss": .10, "weekly_loss": .20,
                                   "consecutive_losses": 5, "pause_hours": 2},
                   {"target_r": 3.5, "max_extension_atr": .75, "volume_multiple": 1.4}),
    "active": ("Активный", {"trade_risk": .003, "max_exposure": .40},
               {"signal_interval": "5m", "scan_seconds": 60, "breakout_bars": 12,
                "volume_multiple": 1.2, "pullback_volume_multiple": 1.05,
                "max_extension_atr": .8, "stop_atr": 1.5, "target_r": 3.0}),
}


def account_settings(settings):
    if settings.mode != Mode.PAPER:
        raise ValueError("Parallel accounts are PAPER only")
    root = Path(settings.db_path)
    directory = root.parent/(root.stem+"-accounts")
    result = []
    if set(settings.paper_profiles)-set(PROFILES):
        raise ValueError("Unknown PAPER profile")
    for name, (label, risk, strategy) in PROFILES.items():
        custom = settings.paper_profiles.get(name, {})
        if set(custom)-{"risk", "strategy"}:
            raise ValueError("Profile accepts only risk and strategy settings")
        r = replace(settings.risk, **{**risk, **custom.get("risk", {})})
        # Equal deposits and execution costs are essential to this comparison.
        r = replace(r, initial_balance=settings.risk.initial_balance)
        r.validate()
        s = replace(settings.strategy, **{**strategy, **custom.get("strategy", {})})
        s = replace(s, fee=settings.strategy.fee, slippage=settings.strategy.slippage,
                    listing_slippage=settings.strategy.listing_slippage)
        result.append(replace(settings, db_path=str(directory/(name+".sqlite")), paper_lab=False,
                              demo_key="", demo_secret="", binance_enabled=False,
                              account_id=name, account_label=label, risk=r, strategy=s))
    return result


def open_accounts(settings):
    """Caller owns these connections; no balance is reset when reopening a ledger."""
    result = []
    try:
        for cfg in account_settings(settings):
            db = Database(cfg.db_path, cfg.risk.initial_balance)
            db.set("mode", "PAPER")
            db.set("account_id", cfg.account_id)
            db.set("account_label", cfg.account_label)
            result.append((cfg, db))
        return result
    except BaseException:
        for _, db in result:
            db.close()
        raise


def comparison(accounts):
    result = []
    for cfg, db in accounts:
        report = database_report(db)
        positions = db.positions()
        fees = report["fees"]+sum(float(p.get("entry_fee", 0))+float(p.get("exit_fee", 0)) for p in positions)
        slip = report["slippage"]+sum(float(p.get("slippage", 0)) for p in positions)
        initial = float(db.get("initial"))
        equity = float(db.get("equity", db.get("cash")))
        config = db.get("risk_overrides") or asdict(cfg.risk)
        result.append({"id": cfg.account_id, "name": cfg.account_label, "venue": "PAPER",
                       "initial": initial, "cash": float(db.get("cash")), "equity": equity,
                       "position_value": equity-float(db.get("cash")),
                       "net_pnl": equity-initial, "return_pct": (equity/initial-1)*100,
                       "positions": len(positions), "risk_pct": config["trade_risk"]*100,
                       "fees_total": fees, "slippage_total": slip,
                       "interval": cfg.strategy.signal_interval, "started": db.get("started"),
                       "last_scan": db.get("last_scan"), "heartbeat": db.get("heartbeat"),
                       "kill": db.get("kill", False), "reason": db.get("kill_reason", ""),
                       "rest": db.get("rest_status", {}), "ws": db.get("ws_status", {}),
                       "data_paused": db.get("scan_data_paused", False) or db.get("monitor_data_paused", False),
                       "pause_until": db.get("pause_until", 0), "report": report})
    return result


def account_status(row):
    if row["kill"]:
        return "СТОП"
    if row["data_paused"]:
        return "Нет котировок"
    if time.time()-float(row["heartbeat"] or 0) > 30:
        return "Не запущен"
    if row["pause_until"] > time.time():
        return "Пауза риска"
    return "Наблюдение"


class SharedPublicAPI:
    """Coalesce concurrent identical GETs; never accept or forward trading credentials."""
    def __init__(self, api=None, enabled=True):
        self.api = api or BinanceAPI()
        self.enabled = enabled
        self.cache = {}
        self.pending = {}
        self.time_lock = asyncio.Lock()
        self.last_sync = float("-inf")

    @property
    def offset(self):
        return self.api.offset

    async def get(self, path, **params):
        if not self.enabled:
            raise Rejected("Public Binance data disabled in trade config")
        key = (path, json.dumps(params, sort_keys=True))
        now = time.monotonic()
        if key in self.cache and now < self.cache[key][0]:
            return copy.deepcopy(self.cache[key][1])
        if key not in self.pending:
            async def fetch():
                try:
                    value = await self.api.get(path, **params)
                    ttl = 1 if path == "depth" else 86400 if params.get("startTime") == 0 else 5
                    # Bound candles by the next wall-clock boundary; never reuse an old closed-bar set.
                    if path == "klines" and "startTime" not in params:
                        from .market import INTERVAL_MS
                        period = INTERVAL_MS[params["interval"]]/1000
                        ttl = min(30, period-(time.time()+self.offset/1000) % period)
                    self.cache[key] = (time.monotonic()+ttl, value)
                    return value
                finally:
                    self.pending.pop(key, None)
            self.pending[key] = asyncio.create_task(fetch())
        return copy.deepcopy(await asyncio.shield(self.pending[key]))

    async def sync_time(self):
        if not self.enabled:
            raise Rejected("Public Binance data disabled")
        async with self.time_lock:
            if time.monotonic()-self.last_sync >= 5:
                await self.api.sync_time()
                self.last_sync = time.monotonic()

    async def close(self):
        # Worker shutdown must not close a connection still used by another account.
        return None

    async def shutdown(self):
        pending = list(self.pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.api.close()


class PaperLab:
    def __init__(self, settings, *, public=None):
        if settings.mode != Mode.PAPER or not settings.paper_lab:
            raise ValueError("PaperLab requires PAPER with three-account mode enabled")
        self.settings = settings
        self.db = Database(settings.db_path, settings.risk.initial_balance)
        if self.db.positions() or self.db.rows("SELECT id FROM intents WHERE status NOT IN ('CLOSED','REJECTED')"):
            self.db.close()
            raise ValueError("Legacy coordinator has exposure: use its original single-account profile to close it before PAPER Lab")
        self.public = SharedPublicAPI(public, settings.market_data_enabled)
        self.traders = [Trader(cfg, public=self.public) for cfg in account_settings(settings)]
        self.db.set("mode", "PAPER")
        self.db.set("accounts", [{"id": t.settings.account_id, "name": t.settings.account_label,
                                  "path": t.settings.db_path} for t in self.traders])

    async def worker(self, trader, once=False):
        delay = 5
        while True:
            try:
                await trader.startup()
                if once:
                    await trader.scan_once(execute=False)
                    return
                async with asyncio.TaskGroup() as tasks:
                    for task in (trader._scans(), trader._monitor(), trader._messages()):
                        tasks.create_task(task)
            except (APIError, Rejected) as exc:
                trader.db.set("rest_status", {"ok": False, "ts": time.time(), "error": str(exc)})
                trader.db.set("scan_data_paused", True)
                trader.db.event("CONNECTION", f"Waiting for public market data: {type(exc).__name__}", True)
                if once:
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                trader.db.halt(f"Account worker {type(exc).__name__}: entries disabled")
                if once:
                    raise
            trader.db.set("heartbeat", time.time())
            await asyncio.sleep(delay)
            delay = min(60, delay*2)

    def save_report(self):
        rows = comparison([(t.settings, t.db) for t in self.traders])
        self.db.set("paper_comparison", rows)
        self.db.set("heartbeat", time.time())
        path = Path(self.settings.db_path).parent/"reports"/"paper-comparison.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(encode({"updated_utc": time.time(), "accounts": rows,
                                    "note": "Virtual PAPER results; one night is insufficient evidence"}), "utf-8")
        temporary.replace(path)

    async def reports(self):
        while True:
            primary = self.traders[0].db
            for trader in self.traders[1:]:
                trader.db.set("ws_status", primary.get("ws_status", {}))
            self.save_report()
            await asyncio.sleep(30)

    async def run(self, once=False):
        try:
            with ExitStack() as stack:
                for trader in self.traders:
                    stack.enter_context(ProcessLock(trader.settings.db_path+".lock"))
                if once:
                    async with asyncio.TaskGroup() as tasks:
                        for trader in self.traders:
                            tasks.create_task(self.worker(trader, once=True))
                else:
                    async with asyncio.TaskGroup() as tasks:
                        for trader in self.traders:
                            tasks.create_task(self.worker(trader))
                        tasks.create_task(self.traders[0].market.stream())
                        tasks.create_task(self.traders[0]._alpha())
                        tasks.create_task(self.reports())
        finally:
            self.save_report()
            for trader in self.traders:
                trader.db.set("running", False)
                trader.db.close()
            self.db.close()
            await self.public.shutdown()
