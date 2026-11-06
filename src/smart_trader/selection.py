"""Bounded strategy selection from past closed trades, without changing risk limits."""
import json
import math
import time

from .config import SelectionConfig
from .strategies import STRATEGIES


class StrategySelector:
    def __init__(self, db, config=None, venue=None):
        self.db, self.config, self.venue = db, config or SelectionConfig(), venue

    def evidence(self, cutoff=None):
        cutoff = cutoff if cutoff is not None else time.time()
        start = cutoff-self.config.lookback_days*86400
        rows = self.db.rows("SELECT strategy,venue,closed,data FROM trades WHERE closed>=? AND closed<? ORDER BY closed", (start, cutoff))
        if self.venue:
            rows = [r for r in rows if r["venue"] == self.venue]
        result = {}
        for name in STRATEGIES:
            sample = [r for r in rows if r["strategy"] == name]
            values = [float(json.loads(r["data"])["r_multiple"]) for r in sample]
            if not all(math.isfinite(x) for x in values):
                raise ValueError("Nonfinite strategy outcome")
            count = len(values)
            days = (sample[-1]["closed"]-sample[0]["closed"])/86400 if count else 0
            enough = count >= self.config.min_trades and days >= self.config.min_days
            mean = sum(values)/count if count else 0
            error = math.sqrt(sum((x-mean)**2 for x in values)/(count-1)/count) if count > 1 else 0
            lower, upper = mean-self.config.confidence_z*error, mean+self.config.confidence_z*error
            result[name] = {"trades": count, "days": round(days, 1), "mean_r": mean if count else None,
                            "lower_r": lower if enough else None, "upper_r": upper if enough else None,
                            "eligible": not (enough and upper < 0),
                            "state": "negative_history" if enough and upper < 0 else "measured" if enough else "insufficient",
                            "reason": "Нет достаточной выборки; исходные правила, без повышения риска" if not enough else
                                      "Исторический верхний доверительный предел R < 0; входы этой стратегии пропущены" if upper < 0 else
                                      "Оценка по прошлым закрытым сделкам; будущая доходность не доказана"}
        return result

    def choose(self, signals, *, cutoff=None):
        if not signals:
            return None
        if not self.config.enabled:
            return signals[0]
        evidence = self.evidence(cutoff)
        self.db.set("strategy_selection", evidence)
        allowed = [s for s in signals if evidence[s.strategy]["eligible"]]
        if not allowed:
            return None
        # No forced exploration, no future results, no increase to position size.
        priority = {"momentum": 2, "trend_pullback": 1, "new_listings": 0}
        return max(allowed, key=lambda s: (evidence[s.strategy]["lower_r"] if evidence[s.strategy]["lower_r"] is not None else 0,
                                           priority[s.strategy]))
