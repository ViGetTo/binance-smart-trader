"""Read-only Demo account binding. No order endpoint is called here."""
import hashlib
import time

from .config import dec


def identity(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def account_snapshot(raw):
    if not raw.get("canTrade", False):
        raise ValueError("Demo API: отсутствует разрешение Spot trading")
    balances = [{"asset": b["asset"], "free": str(dec(b["free"])), "locked": str(dec(b["locked"]))}
                for b in raw["balances"] if dec(b["free"])+dec(b["locked"]) > 0]
    usdt = next((b for b in balances if b["asset"] == "USDT"), {"free": "0", "locked": "0"})
    return {"free_usdt": usdt["free"], "locked_usdt": usdt["locked"], "balances": balances,
            "can_trade": True, "ts": time.time(), "source": "BINANCE_SPOT_DEMO"}


def bind_account(db, key, raw):
    fingerprint = identity(key)
    previous = db.get("demo_identity")
    if previous is not None and previous != fingerprint:
        db.halt("Demo key differs from the account bound to this database")
        raise ValueError("Другой Demo API key: используйте отдельный профиль через trade config")
    snapshot = account_snapshot(raw)
    if not db.get("demo_balance_initialized", False):
        # Never turn prior simulation or an old allocated ledger into purported exchange history.
        if db.rows("SELECT id FROM intents LIMIT 1") or db.rows("SELECT id FROM trades LIMIT 1") or db.positions():
            raise ValueError("Старая история в БД: создайте отдельный DEMO-профиль через trade config")
        capital = dec(snapshot["free_usdt"])
        if capital <= 0:
            raise ValueError("На Binance Spot Demo нет свободных USDT")
        with db.transaction():
            db.set("initial", str(capital))
            db.set("cash", str(capital))
            db.set("equity", str(capital))
            db.set("started", time.time())
            db.execute("DELETE FROM equity")
            db.execute("DELETE FROM strategy_equity")
            db.execute("DELETE FROM state WHERE key IN ('day_anchor','week_anchor')")
            db.set("demo_identity", fingerprint)
            db.set("demo_balance_initialized", True)
            db.event("ACCOUNT", f"Binance Spot DEMO подтверждён. Стартовый капитал: {capital} USDT", True)
    db.set("demo_account", snapshot)
    db.set("demo_free_usdt", snapshot["free_usdt"])
    return snapshot
