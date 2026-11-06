import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal

from .config import dec


@dataclass(frozen=True)
class Book:
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    received: float

    @classmethod
    def parse(cls, raw):
        b = cls([(dec(p), dec(q)) for p, q in raw["bids"]],
                [(dec(p), dec(q)) for p, q in raw["asks"]], time.time())
        if not b.bids or not b.asks or b.bid <= 0 or b.ask <= b.bid:
            raise ValueError("Invalid/crossed book")
        if any(p <= 0 or q < 0 for p, q in b.bids + b.asks):
            raise ValueError("Invalid book levels")
        if b.bids != sorted(b.bids, reverse=True) or b.asks != sorted(b.asks):
            raise ValueError("Unsorted book")
        return b

    @property
    def bid(self):
        return self.bids[0][0]

    @property
    def ask(self):
        return self.asks[0][0]

    @property
    def spread(self):
        return float((self.ask - self.bid) / ((self.ask + self.bid) / 2))

    def depth(self, band=.002):
        b = sum((p*q for p, q in self.bids if p >= self.bid*(1-dec(band))), Decimal(0))
        a = sum((p*q for p, q in self.asks if p <= self.ask*(1+dec(band))), Decimal(0))
        return min(b, a)


@dataclass(frozen=True)
class Signal:
    symbol: str
    strategy: str
    candle_ms: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    atr: Decimal
    breakout: Decimal
    reason: str

    @property
    def key(self):
        return hashlib.sha256(f"{self.strategy}:{self.symbol}:{self.candle_ms}".encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Plan:
    signal: Signal
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk: Decimal
    venue: str


class Rejected(Exception):
    """Expected refusal to trade."""


class Uncertain(Exception):
    """Exchange outcome unknown; reconcile, never blindly resend."""
