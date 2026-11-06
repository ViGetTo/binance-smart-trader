import io
import json
import stat
import time
from dataclasses import replace

import httpx
import pytest
from conftest import FakeMarket, frame
from dotenv import dotenv_values
from rich.console import Console

from smart_trader.account import bind_account, identity
from smart_trader.config import Mode, SelectionConfig, Settings, dec
from smart_trader.execution import Execution
from smart_trader.health import health_state, network_checks
from smart_trader.models import Rejected
from smart_trader.runtime import Trader
from smart_trader.selection import StrategySelector
from smart_trader.service import control
from smart_trader.setup import configure_demo, verify_demo, verify_telegram, write_env
from smart_trader.strategies import pullback_signal


def account(amount="4900"):
    return {"canTrade": True, "balances": [{"asset": "USDT", "free": amount, "locked": "0"}]}


def test_demo_balance_comes_from_account_once_and_never_resets(db):
    bind_account(db, "fixture-key", account())
    assert db.get("initial") == "4900"
    assert db.get("cash") == "4900"
    assert db.get("demo_account")["source"] == "BINANCE_SPOT_DEMO"
    db.set("cash", "4800")
    bind_account(db, "fixture-key", account("4800"))
    assert db.get("initial") == "4900"
    assert db.get("cash") == "4800"
    assert db.get("demo_account")["free_usdt"] == "4800"


def test_risk_budget_uses_verified_4900_not_initial_paper_default(db, cfg, signal, rules):
    from conftest import book_at

    from smart_trader.risk import RiskManager
    settings = replace(cfg, mode=Mode.DEMO, demo_balance_sync=True, strict_demo=True)
    bind_account(db, "fixture-key", account())
    risk = RiskManager(db, settings)
    risk.observe(dec(4900))
    plan = risk.approve(signal, rules, book_at(), "DEMO", {})
    assert plan.risk <= dec("24.5")
    assert plan.qty*plan.entry <= dec("1225")


def test_demo_refuses_other_key_or_simulation_history(db):
    bind_account(db, "fixture-key", account())
    with pytest.raises(ValueError, match="Другой"):
        bind_account(db, "different-key", account())
    assert db.get("kill")


def test_demo_does_not_relabel_paper_history(db):
    db.execute("INSERT INTO intents VALUES('old','ETHUSDT','momentum','PAPER','CLOSED','{}',0)")
    with pytest.raises(ValueError, match="Старая история"):
        bind_account(db, "fixture-key", account())
    assert not db.get("demo_balance_initialized", False)


def test_zero_demo_quote_is_not_replaced_by_fake_capital(db):
    with pytest.raises(ValueError, match="нет свободных"):
        bind_account(db, "fixture-key", account("0"))


def test_strict_demo_missing_keys_never_falls_back(cfg):
    with pytest.raises(ValueError, match="PAPER-подмена"):
        Trader(replace(cfg, mode=Mode.DEMO, strict_demo=True))


async def test_strict_demo_missing_symbol_never_simulates(db, cfg, risk, signal):
    execution = Execution(db, replace(cfg, strict_demo=True), risk, FakeMarket(), demo=object())
    with pytest.raises(Rejected, match="PAPER-сделка запрещена"):
        await execution.enter(replace(signal, strategy="new_listings"), {})
    assert not db.positions()


def test_dotenv_write_is_atomic_private_and_reloads_without_polluting_process(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path/".env"
    write_env(path, {"TRADER_MODE": "DEMO", "TRADER_DB": "data/demo.sqlite", "BINANCE_DEMO_API_KEY": "fixture-key",
                     "BINANCE_DEMO_API_SECRET": "fixture-secret", "TELEGRAM_BOT_TOKEN": "123:fixture-a"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Settings.load().telegram_token == "123:fixture-a"
    write_env(path, {"TELEGRAM_BOT_TOKEN": "123:fixture-b"})
    assert Settings.load().telegram_token == "123:fixture-b"
    assert Settings.load().demo_secret == "fixture-secret"
    assert not list(tmp_path.glob(".env-*"))
    with pytest.raises(ValueError):
        write_env(path, {"BINANCE_DEMO_API_SECRET": "bad\nTRADER_MODE=LIVE"})
    assert Settings.load().mode == Mode.DEMO


async def test_verify_demo_only_uses_read_only_requests():
    calls = []

    class API:
        def __init__(self, **kwargs):
            assert kwargs["demo"] is True

        async def sync_time(self):
            calls.append(("GET", "time"))

        async def signed(self, method, endpoint):
            calls.append((method, endpoint))
            return account()

        async def close(self):
            calls.append(("CLOSE", ""))

    result = await verify_demo("key", "secret", factory=API)
    assert result["free_usdt"] == "4900"
    assert all(method in {"GET", "CLOSE"} for method, _ in calls)


async def test_telegram_test_notification_and_error_never_exposes_token():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "fixture_bot"}})
        assert json.loads(request.content)["chat_id"] == "42"
        return httpx.Response(200, json={"ok": True})

    token = "123:fixture-secret"
    assert await verify_telegram(token, "42", send=True, transport=httpx.MockTransport(handle)) == "fixture_bot"
    assert len(calls) == 2
    with pytest.raises(ValueError) as error:
        await verify_telegram(token, "42", transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"ok": False})))
    assert token not in str(error.value)


def test_setup_saves_strict_demo_profile_without_echoing_secrets(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)
    answers = iter(["key-fixture-private", "secret-fixture-private"])
    monkeypatch.setattr("smart_trader.setup.Prompt.ask", lambda *a, **k: next(answers))
    monkeypatch.setattr("smart_trader.setup.Confirm.ask", lambda *a, **k: True)

    async def fake_verify(*args):
        from smart_trader.account import account_snapshot
        return account_snapshot(account())

    monkeypatch.setattr("smart_trader.setup.verify_demo", fake_verify)
    configure_demo(console, cfg)
    values = dotenv_values(".env")
    assert values["TRADER_MODE"] == "DEMO"
    assert values["TRADER_STRICT_DEMO"] == "true"
    assert values["TRADER_DB"] == f"data/demo-{identity('key-fixture-private')}.sqlite"
    assert "4900" in stream.getvalue()
    assert "key-fixture-private" not in stream.getvalue()
    assert "secret-fixture-private" not in stream.getvalue()


def seed_trade(db, n, strategy, closed, r, venue="DEMO"):
    db.execute("INSERT INTO trades(position_id,symbol,strategy,venue,closed,data) VALUES(?,?,?,?,?,?)",
                (str(n), "ETHUSDT", strategy, venue, closed, json.dumps({"pnl": r*10, "r_multiple": r})))


def test_selector_ignores_future_and_other_venues(db, signal):
    now = time.time()
    for n in range(25):
        seed_trade(db, n, "momentum", now+10+n, -5)
        seed_trade(db, 100+n, "momentum", now-35*86400+n*86400, -5, venue="PAPER")
    selector = StrategySelector(db, venue="DEMO")
    assert selector.evidence(now)["momentum"]["trades"] == 0
    assert selector.choose([signal], cutoff=now) == signal


def test_selector_rejects_negative_past_edge_and_keeps_risk_unchanged(db, signal):
    now = time.time()
    for n in range(35):
        seed_trade(db, n, "momentum", now-(40-n)*86400, -1)
        seed_trade(db, 100+n, "trend_pullback", now-(40-n)*86400, .3+(n % 2)*.1)
    selector = StrategySelector(db, venue="DEMO")
    alternative = replace(signal, strategy="trend_pullback")
    chosen = selector.choose([signal, alternative], cutoff=now)
    assert chosen == alternative
    assert chosen.stop == signal.stop and chosen.target == signal.target
    assert selector.choose([signal], cutoff=now) is None


def test_insufficient_history_is_not_declared_profitable(db):
    selector = StrategySelector(db, SelectionConfig(), "DEMO")
    assert selector.evidence()["momentum"]["state"] == "insufficient"
    assert selector.evidence()["momentum"]["lower_r"] is None


def test_pullback_requires_closed_confirmation_and_market_filter(cfg):
    bars, hourly = frame(120, 900_000, trend=False), frame()
    bars.loc[bars.index[-2], ["high", "low", "close"]] = [100.2, 99., 100.]
    bars.loc[bars.index[-1], ["open", "high", "low", "close", "volume"]] = [99.9, 100.5, 99.9, 100.3, 150_000]
    signal = pullback_signal("ETHUSDT", bars, hourly, cfg.strategy, bullish=True)
    assert signal.strategy == "trend_pullback"
    assert signal.stop < signal.entry < signal.target
    with pytest.raises(Rejected, match="BTC"):
        pullback_signal("ETHUSDT", bars, hourly, cfg.strategy, bullish=False)


def test_freeze_flat_refuses_open_orders_and_keeps_kill_switch(db):
    db.execute("INSERT INTO intents VALUES('pending','ETHUSDT','momentum','DEMO','ENTERING','{}',0)")
    with pytest.raises(ValueError):
        control(db, "freeze-flat")
    assert db.get("kill") is True


def test_stale_pid_is_not_reported_as_healthy(db, monkeypatch):
    db.set("service_pid", 42)
    db.set("service_heartbeat", time.time()-636)
    db.set("heartbeat", time.time()-636)
    monkeypatch.setattr("smart_trader.health.service_process_alive", lambda pid: False)
    state = health_state(db)
    assert not state["ready"]
    assert not state["service_process_alive"]


async def test_doctor_reports_451_and_missing_local_web_without_fake_success():
    def handler(request):
        return httpx.Response(503 if request.url.host == "127.0.0.1" else 451)
    result = await network_checks(transport=httpx.MockTransport(handler))
    assert len(result) == 3
    assert not any(row["ok"] for row in result)
    assert all("451" in row["reason"] for row in result[:2])
