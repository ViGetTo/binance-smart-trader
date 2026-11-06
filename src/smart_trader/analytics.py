import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .strategies import STRATEGIES


def metrics(trades, equity, initial):
    pnls = np.asarray([float(t["pnl"]) for t in trades])
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    curve = pd.Series(equity, dtype=float)
    # Daily returns: never annualize irregular trade returns or count no-trade bars as IID samples.
    if isinstance(equity, pd.Series) and isinstance(equity.index, pd.DatetimeIndex):
        daily = equity.resample("1D").last().pct_change(fill_method=None).dropna()
    else:
        daily = pd.Series(dtype=float)
    volatility = daily.std(ddof=1)
    sharpe = float(daily.mean()/volatility*math.sqrt(365)) if len(daily) >= 30 and volatility > 0 else None
    fees = sum(float(t.get("fees", 0)) for t in trades)
    slippage = sum(float(t.get("slippage", 0)) for t in trades)
    gross_abs = sum(abs(float(t["pnl"])+float(t.get("fees", 0))+float(t.get("slippage", 0))) for t in trades)
    dd = curve/curve.cummax()-1 if not curve.empty else pd.Series([0.])
    return {"trades": len(trades), "net_pnl": float(curve.iloc[-1]-initial) if not curve.empty else 0,
            "realized_pnl": float(pnls.sum()), "win_rate": float((pnls > 0).mean()) if len(pnls) else None,
            "profit_factor": float(wins.sum()/abs(losses.sum())) if len(losses) else None,
            "max_drawdown": float(-dd.min()), "sharpe_daily_365": sharpe,
            "expectancy_r": float(np.mean([float(t["r_multiple"]) for t in trades])) if trades else None,
            "average_win": float(wins.mean()) if len(wins) else None,
            "average_loss": float(losses.mean()) if len(losses) else None,
            "fees": fees, "slippage": slippage,
            "costs_pct_capital": (fees+slippage)/initial*100,
            "costs_pct_gross_absolute_result": (fees+slippage)/gross_abs*100 if gross_abs else None,
            "evidence": "INSUFFICIENT" if len(trades) < 100 or len(daily) < 60 else "REQUIRES_OUT_OF_SAMPLE_AND_FORWARD_VALIDATION"}


def database_report(db):
    trades = [dict(json.loads(t["data"]), strategy=t["strategy"], venue=t["venue"])
              for t in db.rows("SELECT * FROM trades ORDER BY closed")]
    rows = db.rows("SELECT * FROM equity ORDER BY ts")
    curve = pd.Series([r["equity"] for r in rows], index=pd.to_datetime([r["ts"] for r in rows], unit="s", utc=True))
    initial = float(db.get("initial"))
    report = metrics(trades, curve, initial)
    report["by_strategy"] = {}
    for strategy in STRATEGIES:
        subset = [t for t in trades if t["strategy"] == strategy]
        values = db.rows("SELECT ts,equity FROM strategy_equity WHERE strategy=? ORDER BY ts", (strategy,))
        attributed = pd.Series([v["equity"] for v in values],
                               index=pd.to_datetime([v["ts"] for v in values], unit="s", utc=True))
        stats = metrics(subset, attributed, initial)
        stats["equity_basis"] = "Initial portfolio budget plus strategy realized and marked PnL; shared risk limits"
        report["by_strategy"][strategy] = stats
    return report


def completed_day(db, now=None):
    moment = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)
    end = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    start = end-86400
    rows = db.rows("SELECT * FROM equity WHERE ts>=? AND ts<? ORDER BY ts", (start, end))
    if not rows:
        return None
    previous = db.rows("SELECT equity FROM equity WHERE ts<? ORDER BY ts DESC LIMIT 1", (start,))
    initial = float(db.get("initial"))
    baseline = previous[0]["equity"] if previous else initial
    count = db.rows("SELECT COUNT(*) AS n FROM trades WHERE closed>=? AND closed<?", (start, end))[0]["n"]
    peak = db.rows("SELECT MAX(equity) AS peak FROM equity")[0]["peak"] or initial
    current = float(db.get("equity", db.get("cash")))
    return {"date": datetime.fromtimestamp(start, timezone.utc).date().isoformat(),
            "net_pnl": rows[-1]["equity"]-baseline, "trades": count,
            "current_drawdown": max(0, 1-current/max(initial, peak)),
            "observations": len(rows), "first_observation_utc": datetime.fromtimestamp(rows[0]["ts"], timezone.utc).isoformat()}
