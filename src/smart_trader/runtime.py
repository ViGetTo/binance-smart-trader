import asyncio
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .analytics import completed_day
from .api import BinanceAPI
from .config import Mode, dec
from .db import Database
from .execution import Execution
from .market import ListingsRegistry, MarketData, alpha_watchlist
from .models import Rejected
from .notifications import Telegram
from .risk import RiskManager
from .scanner import Scanner


class ProcessLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if not self.handle.read(1):
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *args):
        self.handle.close()


class Trader:
    def __init__(self, settings, public=None, demo=None):
        self.settings = settings
        self.db = Database(settings.db_path, settings.risk.initial_balance)
        self.public = public or BinanceAPI()
        mode = settings.mode
        if mode == Mode.BACKTEST:
            raise ValueError("Use the backtest command for BACKTEST mode")
        if mode == Mode.DEMO and not (settings.demo_key and settings.demo_secret):
            mode = Mode.PAPER
            self.db.event("MODE", "DEMO credentials absent: switched to PAPER using public real market data", True)
        self.demo = (demo or BinanceAPI(demo=True, key=settings.demo_key, secret=settings.demo_secret)) if mode == Mode.DEMO else None
        if not self.demo and self.db.rows("SELECT id FROM intents WHERE venue='DEMO' AND status NOT IN ('CLOSED','REJECTED')"):
            self.db.halt("Demo exposure exists: restore Demo credentials to reconcile; PAPER fallback blocked")
            raise ValueError("Cannot abandon persisted Demo orders without credentials")
        self.db.set("mode", mode.value)
        self.db.set("configuration", {"strategy": asdict(settings.strategy), "risk": asdict(settings.risk)})
        self.market = MarketData(self.public, self.db, settings.strategy)
        self.registry = ListingsRegistry(self.db, self.market)
        self.scanner = Scanner(self.market, self.registry, self.db, settings.strategy)
        self.risk = RiskManager(self.db, settings)
        self.execution = Execution(self.db, settings, self.risk, self.market, self.demo)
        self.telegram = Telegram(self.db, settings.telegram_token, settings.telegram_chat)

    async def startup(self):
        await self.public.sync_time()
        if self.demo:
            await self.demo.sync_time()
            await self.execution.prepare_demo()
            await self.execution.reconcile()
        await self.market.refresh()
        # A PAPER intent with no broker order cannot have transmitted anything; no automatic replay.
        self.db.execute("UPDATE intents SET status='REJECTED' WHERE venue='PAPER' AND status='ENTERING' "
                        "AND id NOT IN (SELECT id FROM positions)")
        self.risk.observe(dec(self.db.get("cash"))+
                          sum((dec(p["qty"])*dec(p["entry"]) for p in self.db.positions()), dec(0)))
        await self.execution.monitor()
        self.db.event("START", f"Trader started: {self.db.get('mode')}; virtual budget {self.db.get('initial')} USDT", True)

    async def scan_once(self, execute=True):
        signals = await self.scanner.scan()
        if not execute:
            self.db.set("last_scan", time.time())
            return
        # Correlation stays available even if a held coin falls below the scanner's volume filter.
        for p in self.db.positions():
            f = await self.market.candles(p["symbol"], "1h", self.settings.strategy.hourly_bars)
            self.scanner.history[p["symbol"]] = f.set_index("close_ms").close.pct_change().dropna()
        for signal in signals:
            try:
                await self.execution.monitor()
                await self.execution.enter(signal, self.scanner.history)
            except Rejected as exc:
                self.db.decision(signal.symbol, signal.strategy, False, str(exc))
                if "limit" in str(exc).lower() or "cooldown" in str(exc).lower():
                    self.db.event("RISK", f"{signal.symbol}: {exc}", True)
            except Exception as exc:
                self.db.halt(f"Execution error {type(exc).__name__}; entries disabled, monitor continues")
        self.db.set("last_scan", time.time())

    async def _scans(self):
        while True:
            started = time.monotonic()
            try:
                await self.public.sync_time()
                if self.demo:
                    await self.demo.sync_time()
                    await self.execution.prepare_demo()
                await self.scan_once()
            except Exception as exc:
                self.db.set("rest_status", {"ok": False, "ts": time.time(), "error": type(exc).__name__})
                self.db.halt(f"Scanner data error: {type(exc).__name__}")
            await asyncio.sleep(max(1, self.settings.strategy.scan_seconds-(time.monotonic()-started)))

    async def _monitor(self):
        while True:
            started = time.monotonic()
            if self.demo:
                try:
                    await self.execution.reconcile()
                except Exception as exc:
                    self.db.set("demo_status", {"ok": False, "ts": time.time(), "error": type(exc).__name__})
                    self.db.halt(f"Demo reconciliation error: {type(exc).__name__}; retry without order replay")
            try:
                flatten = self.db.get("flatten", False)
                await self.execution.monitor(flatten=flatten)
                if flatten and not self.db.positions():
                    self.db.set("flatten", False)
                if self.db.get("resume_requested", False):
                    pending = self.db.rows("SELECT id FROM intents WHERE status NOT IN ('OPEN','CLOSED','REJECTED')")
                    health = self.db.get("rest_status", {}).get("ok", False)
                    if self.demo:
                        health = health and self.db.get("demo_status", {}).get("ok", False)
                    self.risk.config().validate()
                    if health and not pending and not self.db.get("flatten", False):
                        self.db.set("kill", False)
                        self.db.set("kill_reason", "")
                        self.db.event("RESUME", "Entries resumed after reconciliation and risk validation", True)
                    else:
                        self.db.event("RISK", "Resume refused: health, orders or flatten require attention", True)
                    self.db.set("resume_requested", False)
                self.db.set("heartbeat", time.time())
            except Exception as exc:
                self.db.halt(f"Monitor/reconciliation error: {type(exc).__name__}; automatic entries disabled")
            await asyncio.sleep(max(1, self.settings.strategy.monitor_seconds-(time.monotonic()-started)))

    async def _messages(self):
        while True:
            date = datetime.now(timezone.utc).date().isoformat()
            if self.db.get("daily_report_checked") != date:
                report = completed_day(self.db)
                if report:
                    self.db.event("DAILY", f"{report['date']} UTC {self.db.get('mode')}: "
                                  f"PnL {report['net_pnl']:.2f} USDT; trades {report['trades']}; "
                                  f"current drawdown {report['current_drawdown']:.2%}; "
                                  f"observations {report['observations']}; no profitability conclusion", True)
                self.db.set("daily_report_checked", date)
            await self.telegram.flush()
            await asyncio.sleep(10)

    async def _alpha(self):
        while True:
            try:
                self.db.set("alpha_watchlist", await alpha_watchlist())
                self.db.set("alpha_status", {"ok": True, "ts": time.time(), "purpose": "information only"})
            except Exception as exc:
                self.db.set("alpha_status", {"ok": False, "ts": time.time(), "error": type(exc).__name__})
            # This list never enters the Spot universe or execution gateway.
            for _ in range(60):
                await asyncio.sleep(60)

    async def run(self, once=False):
        try:
            await self.startup()
            if once:
                await self.scan_once(execute=False)
                await self.telegram.flush()
            else:
                async with asyncio.TaskGroup() as tasks:
                    for task in (self.market.stream(), self._scans(), self._monitor(), self._messages(), self._alpha()):
                        tasks.create_task(task)
        finally:
            # On process exit PAPER protection stops; DEMO exchange OCO stays active.
            self.db.set("running", False)
            await self.public.close()
            if self.demo:
                await self.demo.close()
            self.db.close()
