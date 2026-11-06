import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path


def encode(value):
    def default(v):
        if isinstance(v, Decimal):
            return str(v)
        if is_dataclass(v):
            return asdict(v)
        if hasattr(v, "item"):
            return v.item()
        raise TypeError(type(v).__name__)
    return json.dumps(value, default=default, allow_nan=False, ensure_ascii=False)


class Database:
    def __init__(self, path, initial=10_000):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=15000;
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS intents(
          id TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy TEXT NOT NULL, venue TEXT NOT NULL,
          status TEXT NOT NULL, plan TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(
          id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id),
          role TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, response TEXT);
        CREATE TABLE IF NOT EXISTS positions(
          id TEXT PRIMARY KEY REFERENCES intents(id), symbol TEXT NOT NULL, strategy TEXT NOT NULL,
          venue TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trades(
          id INTEGER PRIMARY KEY, position_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
          strategy TEXT NOT NULL, venue TEXT NOT NULL, closed REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS equity(ts REAL PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS strategy_equity(
          ts REAL NOT NULL, strategy TEXT NOT NULL, equity REAL NOT NULL, PRIMARY KEY(ts,strategy));
        CREATE TABLE IF NOT EXISTS exchange_fills(
          symbol TEXT NOT NULL, trade_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(symbol,trade_id));
        CREATE TABLE IF NOT EXISTS candles(
          symbol TEXT NOT NULL, interval TEXT NOT NULL, open_ms INTEGER NOT NULL, close_ms INTEGER NOT NULL,
          data TEXT NOT NULL, PRIMARY KEY(symbol,interval,open_ms));
        CREATE TABLE IF NOT EXISTS exchange_snapshots(
          ts REAL PRIMARY KEY, hash TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS symbols(
          symbol TEXT PRIMARY KEY, first_seen REAL NOT NULL, earliest_ms INTEGER,
          last_seen REAL NOT NULL, active INTEGER NOT NULL, raw TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, symbol TEXT NOT NULL,
          strategy TEXT NOT NULL, score REAL, accepted INTEGER NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, message TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        """)
        if self.get("cash") is None:
            with self.transaction():
                self.set("cash", str(initial))
                self.set("initial", str(initial))
                self.set("started", time.time())
                self.set("kill", False)

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def rows(self, sql, params=()):
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def get(self, key, default=None):
        row = self.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, encode(value)))

    def event(self, kind, message, notify=False):
        self.execute("INSERT INTO events(ts,kind,message) VALUES(?,?,?)", (time.time(), kind, message))
        if notify:
            if kind in {"HALT", "CONNECTION", "RISK"}:
                key = "alert:"+hashlib.sha256((kind+message).encode()).hexdigest()
                if time.time()-self.get(key, 0) < 300:
                    return
                self.set(key, time.time())
            self.execute("INSERT INTO outbox(ts,message) VALUES(?,?)", (time.time(), message))

    def halt(self, reason):
        unchanged = self.get("kill", False) and self.get("kill_reason") == reason
        self.set("kill", True)
        self.set("kill_reason", reason)
        if not unchanged:
            self.event("HALT", reason, True)

    def positions(self):
        return [dict(json.loads(r["data"]), id=r["id"], symbol=r["symbol"],
                     strategy=r["strategy"], venue=r["venue"]) for r in self.rows("SELECT * FROM positions")]

    def save_position(self, p):
        self.execute("INSERT INTO positions VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                     (p["id"], p["symbol"], p["strategy"], p["venue"], encode(p)))

    def decision(self, symbol, strategy, accepted, reason, score=None):
        self.execute("INSERT INTO decisions(ts,symbol,strategy,score,accepted,reason) VALUES(?,?,?,?,?,?)",
                     (time.time(), symbol, strategy, score, int(accepted), reason))

    def close(self):
        self.conn.close()
