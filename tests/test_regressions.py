import json

import httpx
import pytest
import respx
from conftest import FakeMarket, book_at

from smart_trader.config import dec
from smart_trader.execution import Execution
from smart_trader.notifications import Telegram
from smart_trader.paper import simulate


async def test_crash_between_exit_accounting_and_finalization(db, cfg, risk, signal, rules):
    market = FakeMarket()
    execution = Execution(db, cfg, risk, market)
    await execution.enter(signal, {})
    p = db.positions()[0]
    p["exit_reason"] = "stop_loss"
    market.current = book_at(95)
    fill = simulate(market.current, dec(p["qty"]), "SELL", rules, cfg.strategy)
    execution._apply_exit(p, "paper0", fill.qty, fill.quote-fill.fee, fill.fee, fill.slippage)
    assert dec(db.positions()[0]["qty"]) == 0
    assert not db.rows("SELECT * FROM trades")
    restored = Execution(db, cfg, risk, market)
    await restored.monitor()
    assert not db.positions()
    trade = json.loads(db.rows("SELECT * FROM trades")[0]["data"])
    assert dec(db.get("cash")) == dec(10000)+dec(trade["pnl"])


async def test_new_listing_fallback_is_labelled_paper(db, cfg, risk, signal):
    from dataclasses import replace
    execution = Execution(db, cfg, risk, FakeMarket(), demo=object())
    await execution.enter(replace(signal, strategy="new_listings"), {})
    assert db.positions()[0]["venue"] == "PAPER"
    assert db.rows("SELECT venue FROM intents")[0]["venue"] == "PAPER"
    assert any("explicit PAPER" in e["message"] for e in db.rows("SELECT * FROM events"))


async def test_stale_exit_quote_halts_without_fake_execution(db, cfg, risk, signal):
    from dataclasses import replace
    market = FakeMarket()
    execution = Execution(db, cfg, risk, market)
    await execution.enter(signal, {})
    market.current = replace(book_at(95), received=0)
    from smart_trader.models import Uncertain
    with pytest.raises(Uncertain):
        await execution.monitor()
    assert db.get("kill")
    assert db.positions()
    assert not db.rows("SELECT * FROM trades")


@respx.mock
async def test_telegram_outbox_retries_and_marks_delivery(db):
    db.event("OPEN", "PAPER fixture-only message", True)
    route = respx.post("https://api.telegram.org/botfixture-token/sendMessage").mock(side_effect=[
        httpx.Response(429, json={"ok": False}), httpx.Response(200, json={"ok": True})])
    telegram = Telegram(db, "fixture-token", "123")
    await telegram.flush()
    assert db.rows("SELECT sent FROM outbox")[0]["sent"] == 0
    await telegram.flush()
    assert db.rows("SELECT sent FROM outbox")[0]["sent"] == 1
    assert route.call_count == 2
    assert "fixture-token" not in db.rows("SELECT message FROM events")[0]["message"]


def test_alert_storm_is_deduplicated(db):
    for _ in range(100):
        db.halt("API connection unavailable")
    assert len(db.rows("SELECT * FROM outbox")) == 1


def test_daily_report_uses_completed_utc_day(db):
    from datetime import datetime, timezone

    from smart_trader.analytics import completed_day
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()
    start = datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp()
    for ts, equity in ((start-1, 9000), (start+100, 9100), (start+86399, 9200), (start+86400, 9250)):
        db.execute("INSERT INTO equity VALUES(?,?,?)", (ts, equity, equity))
    db.set("equity", "9250")
    summary = completed_day(db, now)
    assert summary["date"] == "2026-11-04"
    assert summary["net_pnl"] == 200
    assert summary["observations"] == 2
