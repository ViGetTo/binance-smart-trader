import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .analytics import database_report
from .api import APIError, BinanceAPI
from .backtest import Backtester, Dataset
from .config import Settings
from .db import Database, encode
from .market import COLUMNS, alpha_watchlist
from .runtime import ProcessLock, Trader


def millis(text):
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()*1000)


async def check():
    results = {}
    for demo in (False, True):
        # No signed access or credentials during connectivity check.
        import httpx

        from .config import DEMO_URL, PUBLIC_URL
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            try:
                response = await client.get((DEMO_URL if demo else PUBLIC_URL)+"/v3/time")
                result = {"http": response.status_code}
                if response.status_code == 200:
                    result["serverTime"] = response.json()["serverTime"]
                elif response.status_code == 451:
                    result["reason"] = "Binance location restriction; no bypass attempted"
                results["demo" if demo else "public"] = result
            except httpx.HTTPError:
                results["demo" if demo else "public"] = {"error": "network unavailable"}
    print(encode(results))
    return 0 if all(v.get("http") == 200 for v in results.values()) else 2


async def download(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"created_ms": int(time.time()*1000), "symbols": {}, "limitations": [
        "Downloaded symbol set is user-selected, not a reconstructed complete historical universe",
        "Current exchange filters assumed for history; not historically observed. Supply archived rules instead",
        "Earliest available candle estimates market age; relistings/migrations may require manual correction"]}
    api = BinanceAPI()
    try:
        for symbol in dict.fromkeys(["BTCUSDT", *args.symbols]):
            symbol = symbol.upper()
            info = (await api.get("exchangeInfo", symbol=symbol))["symbols"][0]
            earliest = await api.get("klines", symbol=symbol, interval="1m", startTime=0, limit=1)
            if not earliest:
                raise ValueError(f"No first candle for {symbol}")
            rows, cursor = [], millis(args.start)
            end = millis(args.end)
            while cursor < end:
                batch = await api.get("klines", symbol=symbol, interval="15m", startTime=cursor, endTime=end-1, limit=1000)
                if not batch:
                    break
                if int(batch[-1][0]) < cursor:
                    raise ValueError("Historical pagination did not advance")
                rows.extend(row for row in batch if row[6] < end and row[6] < time.time()*1000)
                cursor = int(batch[-1][0])+900_000
            pd.DataFrame(rows, columns=COLUMNS).to_csv(root/f"{symbol}.csv", index=False)
            manifest["symbols"][symbol] = {"listed_ms": int(earliest[0][0]),
                 "rules": [{"effective_ms": 0, "source": "CURRENT_FILTERS_ASSUMED_FOR_HISTORY", "data": info}],
                 "assumed_spread": .001, "assumed_depth_usdt": 100_000}
        (root/"universe.json").write_text(encode(manifest), "utf-8")
        print(f"Saved public Binance OHLCV and explicit metadata assumptions: {root}")
    finally:
        await api.close()


def main():
    parser = argparse.ArgumentParser(description="Binance virtual-only Spot trader (PAPER / DEMO / BACKTEST)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true", help="One read-only scan, no new orders")
    sub.add_parser("check")
    sub.add_parser("report")
    sub.add_parser("alpha")
    kill = sub.add_parser("kill")
    kill.add_argument("--flatten", action="store_true")
    dl = sub.add_parser("download")
    dl.add_argument("--symbols", nargs="+", required=True)
    dl.add_argument("--start", required=True)
    dl.add_argument("--end", required=True)
    dl.add_argument("--output", default="data/history")
    bt = sub.add_parser("backtest")
    bt.add_argument("--data", required=True)
    bt.add_argument("--output", default="reports/backtest")
    bt.add_argument("--allow-modelled-liquidity", action="store_true")
    bt.add_argument("--walk-forward", action="store_true")
    bt.add_argument("--sensitivity", action="store_true")
    bt.add_argument("--train-days", type=int, default=60)
    bt.add_argument("--test-days", type=int, default=30)
    bt.add_argument("--start")
    bt.add_argument("--end")
    args = parser.parse_args()
    try:
        cfg = Settings.load()
        if args.command == "check":
            sys.exit(asyncio.run(check()))
        elif args.command == "run":
            with ProcessLock(cfg.db_path+".lock"):
                asyncio.run(Trader(cfg).run(once=args.once))
        elif args.command == "download":
            asyncio.run(download(args))
        elif args.command == "alpha":
            print(encode(asyncio.run(alpha_watchlist())))
        elif args.command == "backtest":
            runner = Backtester(Dataset(args.data), cfg, allow_modelled_liquidity=args.allow_modelled_liquidity)
            start, end = millis(args.start) if args.start else None, millis(args.end) if args.end else None
            result = runner.run(start, end)
            root = Path(args.output)
            root.mkdir(parents=True, exist_ok=True)
            (root/"report.json").write_text(encode(result["report"]), "utf-8")
            (root/"trades.json").write_text(encode(result["trades"]), "utf-8")
            result["equity"].rename("equity").to_csv(root/"equity.csv")
            if args.sensitivity:
                (root/"sensitivity.json").write_text(encode(runner.sensitivity(start, end)), "utf-8")
            if args.walk_forward:
                (root/"walk_forward.json").write_text(encode(runner.walk_forward(args.train_days, args.test_days)), "utf-8")
            print(encode(result["report"]))
        else:
            db = Database(cfg.db_path, cfg.risk.initial_balance)
            try:
                if args.command == "kill":
                    with db.transaction():
                        db.halt("User kill switch")
                        if args.flatten:
                            db.set("flatten", True)
                    print("Entries stopped. Position management requires the running trader process.")
                else:
                    print(encode(database_report(db)))
            finally:
                db.close()
    except (APIError, ValueError, OSError) as exc:
        # APIError contains only status/code; never dump traceback with signed request URLs.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Trader stopped. PAPER protection needs the process; DEMO OCO remains on exchange.")


if __name__ == "__main__":
    main()
