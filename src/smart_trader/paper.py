from dataclasses import dataclass
from decimal import Decimal

from .config import dec
from .models import Rejected


@dataclass(frozen=True)
class Fill:
    qty: Decimal
    price: Decimal
    quote: Decimal
    fee: Decimal
    slippage: Decimal


def simulate(book, qty, side, rules, cfg, listing=False):
    """IOC fill from fresh real depth. Only a conservative share of each level is available."""
    levels = book.asks if side == "BUY" else book.bids
    best = book.ask if side == "BUY" else book.bid
    participation = dec(cfg.paper_participation)
    slip = dec(cfg.listing_slippage if listing else cfg.slippage)
    remaining = qty
    filled = Decimal(0)
    quote = Decimal(0)
    for price, available in levels:
        if abs(price/best-1) > dec(cfg.depth_band):
            break
        take = min(remaining, available*participation)
        filled += take
        quote += take*price
        remaining -= take
        if remaining <= 0:
            break
    rounded = rules.quantity(filled)
    if rounded <= 0:
        raise Rejected("Paper IOC: no executable lot")
    quote *= rounded/filled
    quote *= 1+slip if side == "BUY" else 1-slip
    price = quote/rounded
    rules.validate(rounded, price, market=True)
    fee = quote*dec(cfg.fee)
    return Fill(rounded, price, quote, fee, abs(quote-rounded*best))
