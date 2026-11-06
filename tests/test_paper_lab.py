import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import FakeMarket, book_at, frame, symbol_info
from dotenv import dotenv_values
from rich.console import Console
from streamlit.testing.v1 import AppTest
from textual.widgets import DataTable, TabbedContent

from smart_trader.api import APIError, BinanceAPI
from smart_trader.config import Mode, Settings, dec
from smart_trader.db import Database
from smart_trader.execution import Execution
from smart_trader.lab import PaperLab, SharedPublicAPI, account_settings, comparison, open_accounts
from smart_trader.market import COLUMNS, closed_frame
from smart_trader.models import Rejected, Uncertain
from smart_trader.risk import RiskManager
from smart_trader.runtime import Trader
from smart_trader.service import control_all
from smart_trader.setup import configure_connection, enable_paper
from smart_trader.terminal import TraderApp


@pytest.fixture
def lab_cfg(cfg):
    return replace(cfg, paper_lab=True, risk=replace(cfg.risk, initial_balance=4900),
                   demo_key="ignored-fixture-key", demo_secret="ignored-fixture-secret")


async def test_three_accounts_trade_same_signal_independently_and_restore(lab_cfg, signal):
    accounts = open_accounts(lab_cfg)
    try:
        assert len({cfg.db_path for cfg, _ in accounts}) == 3
        for cfg, db in accounts:
            assert cfg.mode == Mode.PAPER and not cfg.demo_key and not cfg.demo_secret
            risk = RiskManager(db, cfg)
            risk.observe(dec(4900))
            market = FakeMarket()
            execution = Execution(db, cfg, risk, market)
            await execution.enter(signal, {})
            assert db.positions()[0]["venue"] == "PAPER"
            with pytest.raises(Rejected, match="Position already"):
                await execution.enter(signal, {})
            market.current = book_at(95)
            await execution.monitor()
            assert not db.positions()
            assert db.rows("SELECT * FROM trades")[0]["venue"] == "PAPER"
        rows = comparison(accounts)
        assert all(r["initial"] == 4900 for r in rows)
        assert all(r["net_pnl"] < 0 and r["report"]["trades"] == 1 and r["fees_total"] > 0 for r in rows)
        assert abs(rows[1]["net_pnl"]) > abs(rows[0]["net_pnl"]) > abs(rows[2]["net_pnl"])
        cash = [db.get("cash") for _, db in accounts]
    finally:
        for _, db in accounts:
            db.close()
    reopened = open_accounts(replace(lab_cfg, risk=replace(lab_cfg.risk, initial_balance=1_000_000)))
    try:
        assert [db.get("cash") for _, db in reopened] == cash
        assert all(db.get("initial") == "4900" for _, db in reopened)
    finally:
        for _, db in reopened:
            db.close()


def test_profile_limits_costs_and_override_validation(lab_cfg):
    configs = account_settings(lab_cfg)
    assert [c.risk.trade_risk for c in configs] == [.005, .02, .003]
    assert [c.strategy.signal_interval for c in configs] == ["15m", "15m", "5m"]
    assert len({c.strategy.fee for c in configs}) == len({c.strategy.slippage for c in configs}) == 1
    with pytest.raises(ValueError):
        account_settings(replace(lab_cfg, mode=Mode.DEMO))
    with pytest.raises(ValueError):
        account_settings(replace(lab_cfg, paper_profiles={"aggressive": {"risk": {"max_exposure": 2}}}))
    with pytest.raises(ValueError):
        account_settings(replace(lab_cfg, paper_profiles={"active": {"risk": {"listing_risk": .5}}}))


def test_pause_and_stop_apply_to_every_account_and_freeze_checks_all(lab_cfg, db):
    accounts = open_accounts(lab_cfg)
    ledgers = [db, *(d for _, d in accounts)]
    try:
        control_all(ledgers, "pause")
        assert all(d.get("kill") for d in ledgers)
        accounts[2][1].execute("INSERT INTO intents VALUES('pending','ETHUSDT','momentum','PAPER','ENTERING','{}',0)")
        with pytest.raises(ValueError, match="одном из счетов"):
            control_all(ledgers, "freeze-flat")
        control_all(ledgers, "stop")
        assert all(d.get("service_stop") and d.get("flatten") for d in ledgers)
    finally:
        for _, d in accounts:
            d.close()


async def test_shared_api_coalesces_reads_and_has_no_signed_interface():
    calls = []

    def handler(request):
        calls.append(request)
        assert not request.headers.get("X-MBX-APIKEY") and request.method == "GET"
        return httpx.Response(200, json={"symbols": []})

    client = BinanceAPI(transport=httpx.MockTransport(handler))
    shared = SharedPublicAPI(client)
    try:
        rows = await asyncio.gather(*(shared.get("exchangeInfo") for _ in range(3)))
        assert len(calls) == 1
        rows[0]["symbols"].append("mutated")
        assert rows[1]["symbols"] == []
        assert not hasattr(shared, "signed")
        await shared.close()  # A worker cannot close another worker's connection.
        assert not client.client.is_closed
    finally:
        await shared.shutdown()
    assert client.client.is_closed


async def test_shared_api_does_not_cache_errors():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(451 if count == 1 else 200, json={"symbols": []})

    shared = SharedPublicAPI(BinanceAPI(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(APIError):
            await shared.get("exchangeInfo")
        assert await shared.get("exchangeInfo") == {"symbols": []}
        assert count == 2
    finally:
        await shared.shutdown()


async def test_real_scanner_with_mocked_official_api_three_accounts_no_fabricated_trades(lab_cfg):
    """All scanner/API paths execute. Flat completed bars must produce zero fills."""
    calls = []
    now = int(time.time()*1000)

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.binance.com" and request.method == "GET"
        assert not request.headers.get("X-MBX-APIKEY")
        path, params = request.url.path.split("/v3/")[1], request.url.params
        if path == "time":
            return httpx.Response(200, json={"serverTime": int(time.time()*1000)})
        if path == "exchangeInfo":
            return httpx.Response(200, json={"symbols": [symbol_info("BTCUSDT"), symbol_info()]})
        if path == "ticker/24hr":
            return httpx.Response(200, json=[{"symbol": s, "quoteVolume": "50000000", "priceChangePercent": "1"}
                                            for s in ("BTCUSDT", "ETHUSDT")])
        if path == "depth":
            return httpx.Response(200, json={"bids": [["100", "100000"]], "asks": [["100.01", "100000"]]})
        if path == "klines":
            if params.get("startTime") == "0":
                return httpx.Response(200, json=[[now-365*86400_000]])
            step = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}[params["interval"]]
            f = frame(int(params["limit"]), step=step, trend=params["interval"] == "1h")
            for c in COLUMNS:
                if c not in f:
                    f[c] = 0
            return httpx.Response(200, json=f[COLUMNS].values.tolist())
        pytest.fail(f"Unexpected endpoint {path}")

    api = BinanceAPI(transport=httpx.MockTransport(handler))
    api.min_interval = 0
    lab = PaperLab(lab_cfg, public=api)
    await lab.run(once=True)
    accounts = open_accounts(lab_cfg)
    try:
        assert all(not d.positions() and not d.rows("SELECT * FROM trades") for _, d in accounts)
        assert all(d.rows("SELECT * FROM decisions") and d.get("last_scan") for _, d in accounts)
        assert all(d.get("cash") == "4900" for _, d in accounts)
    finally:
        for _, d in accounts:
            d.close()
    assert sum("exchangeInfo" in str(r.url) for r in calls) == 1
    report = json.loads((Path(lab_cfg.db_path).parent/"reports/paper-comparison.json").read_text())
    assert len(report["accounts"]) == 3
    assert all(r["net_pnl"] == 0 for r in report["accounts"])


async def test_fully_disabled_binance_makes_zero_requests(lab_cfg):
    api = BinanceAPI(transport=httpx.MockTransport(lambda _: pytest.fail("Binance must be OFF")))
    lab = PaperLab(replace(lab_cfg, market_data_enabled=False), public=api)
    await lab.run(once=True)
    accounts = open_accounts(lab_cfg)
    try:
        assert all(d.get("scan_data_paused") and not d.get("last_scan") for _, d in accounts)
        assert all(not d.rows("SELECT * FROM intents") for _, d in accounts)
    finally:
        for _, d in accounts:
            d.close()


async def test_temporary_book_failure_recovers_without_clearing_user_kill(cfg, db, risk, signal):
    market = FakeMarket()
    execution = Execution(db, cfg, risk, market)
    await execution.enter(signal, {})
    market.current = replace(book_at(95), received=0)
    with pytest.raises(Uncertain):
        await execution.monitor()
    assert db.get("monitor_data_paused") and not db.get("kill")
    db.halt("user pause survives recovery")
    market.current = book_at(95)
    await execution.monitor()
    assert not db.get("monitor_data_paused")
    assert not db.positions() and db.get("kill_reason") == "user pause survives recovery"


def test_short_interval_signal_is_rejected_when_stale(cfg, db, signal, rules):
    r = RiskManager(db, replace(cfg, strategy=replace(cfg.strategy, signal_interval="5m")))
    r.observe(dec(10000))
    stale = replace(signal, candle_ms=int(time.time()*1000)-340_000)
    with pytest.raises(Rejected, match="Stale/future"):
        r.approve(stale, rules, book_at(), "PAPER", {})


def test_switch_to_paper_preserves_keys_but_disables_account(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    enable_paper(replace(cfg, mode=Mode.DEMO))
    values = dotenv_values(".env")
    assert values["BINANCE_ENABLED"] == "false"
    assert values["TRADER_MODE"] == "PAPER" and values["PAPER_INITIAL_BALANCE"] == "4900"
    assert Settings.load().paper_lab


def test_config_can_disable_all_binance_requests(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("smart_trader.setup.Prompt.ask", lambda *a, **k: "3")
    monkeypatch.setattr("smart_trader.setup.Confirm.ask", lambda *a, **k: False)
    configure_connection(Console(quiet=True), cfg)
    assert not Settings.load().market_data_enabled


def test_5m_frame_discards_open_candles():
    end = int(time.time()*1000)//300_000*300_000
    raw = [[end-300_000, 100, 101, 99, 100, 1, end-1, 100, 1, 1, 100, 0],
           [end, 100, 101, 99, 100, 1, end+299_999, 100, 1, 1, 100, 0]]
    result = closed_frame(raw, end+1000, "5m")
    assert len(result) == 1 and result.close_ms.iloc[-1] == end-1


@pytest.mark.parametrize("size", [(40, 30), (100, 36)])
async def test_terminal_three_accounts_switching_and_global_pause(lab_cfg, size):
    app = TraderApp(lab_cfg)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.query_one("#accounts-table", DataTable).row_count == 3
        assert app.query_one("#tabs", TabbedContent).active == "accounts"
        await pilot.press("a")
        assert app.account_settings.account_id == "aggressive"
        await pilot.press("t")
        assert app.account_settings.account_id == "active"
        await pilot.press("p")
        assert all(d.get("kill") for _, d in app.accounts)
        await pilot.press("0")
        assert app.query_one("#tabs", TabbedContent).active == "accounts"
        await pilot.press("b")
        assert app.account_settings.account_id == "base"
        await pilot.press("q")


def test_dashboard_can_display_all_three_and_control_selected(lab_cfg, monkeypatch):
    monkeypatch.setenv("TRADER_MODE", "PAPER")
    monkeypatch.setenv("TRADER_DB", lab_cfg.db_path)
    monkeypatch.setenv("TRADER_PAPER_LAB", "true")
    app = AppTest.from_file(Path(__file__).parents[1]/"dashboard.py", default_timeout=15).run()
    assert not app.exception
    app.selectbox[0].select("Рискованный").run()
    assert not app.exception
    app.button[0].click().run()
    accounts = open_accounts(lab_cfg)
    try:
        assert not accounts[0][1].get("kill")
        assert accounts[1][1].get("kill")
    finally:
        for _, d in accounts:
            d.close()


def test_lab_refuses_to_abandon_legacy_single_account_positions(lab_cfg):
    db = Database(lab_cfg.db_path)
    db.execute("INSERT INTO intents VALUES('pending','ETHUSDT','momentum','PAPER','ENTERING','{}',0)")
    db.close()
    with pytest.raises(ValueError, match="Legacy coordinator"):
        PaperLab(lab_cfg)


async def test_scan_entry_book_network_failure_is_a_data_pause(cfg, signal):
    trader = Trader(cfg)
    trader.risk.observe(dec(10000))
    trader.market.symbols = {"ETHUSDT": symbol_info()}

    async def signals():
        return [signal]

    async def unavailable(*args, **kwargs):
        raise APIError(503)

    trader.scanner.scan = signals
    trader.market.book = unavailable
    try:
        await trader.scan_once()
        assert trader.db.get("scan_data_paused")
        assert not trader.db.get("kill")
        assert not trader.db.positions()
    finally:
        await trader.public.close()
        trader.db.close()
