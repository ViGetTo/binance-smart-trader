import asyncio
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .account import bind_account
from .analytics import completed_day
from .api import APIError, BinanceAPI
from .config import Mode, Settings, dec
from .db import Database
from .execution import Execution
from .market import ListingsRegistry, MarketData, alpha_watchlist
from .models import Rejected, Uncertain
from .notifications import Telegram
from .risk import RiskManager
from .scanner import Scanner
from .selection import StrategySelector


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
        mode = settings.mode
        if mode == Mode.BACKTEST:
            raise ValueError("Use the backtest command for BACKTEST mode")
        if mode == Mode.DEMO and not settings.binance_enabled:
            self.db.close()
            raise ValueError("Binance account access disabled; use PAPER")
        if mode == Mode.DEMO and not (settings.demo_key and settings.demo_secret):
            if settings.strict_demo:
                self.db.set("mode", "DEMO")
                self.db.halt("Demo keys missing. Run trade config; PAPER fallback is disabled")
                self.db.close()
                raise ValueError("DEMO: задайте Demo API key / secret через trade config; PAPER-подмена отключена")
            mode = Mode.PAPER
            self.db.event("MODE", "DEMO credentials absent: switched to PAPER using public real market data", True)
        self.public = public or BinanceAPI()
        self.demo = (demo or BinanceAPI(demo=True, key=settings.demo_key, secret=settings.demo_secret)) if mode == Mode.DEMO else None
        if not self.demo and self.db.rows("SELECT id FROM intents WHERE venue='DEMO' AND status NOT IN ('CLOSED','REJECTED')"):
            self.db.halt("Demo exposure exists: restore Demo credentials to reconcile; PAPER fallback blocked")
            raise ValueError("Cannot abandon persisted Demo orders without credentials")
        self.db.set("mode", mode.value)
        self.db.set("market_data_enabled", settings.market_data_enabled)
        self.db.set("account_id", settings.account_id)
        self.db.set("account_label", settings.account_label)
        self.db.set("configuration", {"strategy": asdict(settings.strategy), "risk": asdict(settings.risk)})
        self.market = MarketData(self.public, self.db, settings.strategy)
        self.registry = ListingsRegistry(self.db, self.market)
        self.scanner = Scanner(self.market, self.registry, self.db, settings.strategy,
                               StrategySelector(self.db, settings.selection, mode.value))
        self.risk = RiskManager(self.db, settings)
        self.execution = Execution(self.db, settings, self.risk, self.market, self.demo)
        self.telegram = Telegram(self.db, settings.telegram_token, settings.telegram_chat)

    async def startup(self):
        if not self.settings.market_data_enabled:
            self.db.set("rest_status", {"ok": False, "disabled": True, "ts": time.time()})
            self.db.set("ws_status", {"ok": False, "disabled": True, "ts": time.time()})
            self.db.set("scan_data_paused", True)
            self.db.event("START", "Binance market data OFF: no requests or simulated fills", True)
            return
        await self.public.sync_time()
        if self.demo:
            await self.demo.sync_time()
            if self.settings.demo_balance_sync:
                bind_account(self.db, self.settings.demo_key, await self.demo.signed("GET", "account"))
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
        if not self.settings.market_data_enabled:
            return
        signals = await self.scanner.scan()
        self.db.set("scan_data_paused", False)
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
            except Uncertain:
                if not self.db.get("monitor_data_paused", False):
                    self.db.halt("Execution state uncertain; entries disabled")
            except APIError:
                if self.demo:
                    self.db.halt("Demo execution API error; reconcile before resuming")
                else:
                    self.db.set("scan_data_paused", True)
                    self.db.event("CONNECTION", "Entry book unavailable; retry on the next scan", True)
            except Exception as exc:
                self.db.halt(f"Execution error {type(exc).__name__}; entries disabled, monitor continues")
        self.db.set("last_scan", time.time())

    async def _scans(self):
        while True:
            if not self.settings.market_data_enabled:
                await asyncio.sleep(5)
                continue
            started = time.monotonic()
            try:
                await self.public.sync_time()
                if self.demo:
                    await self.demo.sync_time()
                    await self.execution.prepare_demo()
                await self.scan_once()
            except Exception as exc:
                self.db.set("rest_status", {"ok": False, "ts": time.time(), "error": type(exc).__name__})
                self.db.set("scan_data_paused", True)
                self.db.event("CONNECTION", f"Scanner data unavailable: {type(exc).__name__}; retrying", True)
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
                if self.settings.market_data_enabled:
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
            except Uncertain:
                # Ledger/risk failures retain their hard halt; temporary book failures retry.
                if not self.db.get("monitor_data_paused", False):
                    self.db.halt("Monitor state uncertain; entries disabled")
            except Exception as exc:
                self.db.halt(f"Monitor/reconciliation error: {type(exc).__name__}; automatic entries disabled")
            await asyncio.sleep(max(1, self.settings.strategy.monitor_seconds-(time.monotonic()-started)))

    async def _messages(self):
        while True:
            # dotenv is read without mutating os.environ, so Telegram edits apply without moving positions.
            fresh = Settings.load()
            self.telegram = Telegram(self.db, fresh.telegram_token, fresh.telegram_chat)
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
                if self.settings.market_data_enabled:
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
