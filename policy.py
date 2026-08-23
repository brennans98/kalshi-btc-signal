"""
The decision policy: turn live spot history plus the Kalshi book into a
single unambiguous action.

Shape of the model
------------------
A 15-minute BTC market settles YES if spot is above the strike at close. Over
that horizon drift is negligible next to volatility, so spot is treated as a
driftless geometric random walk and the fair YES probability is

    P(YES) = Phi( ln(spot / strike) / (sigma * sqrt(T)) )

where sigma is realized volatility per second estimated from the live trade
tape and T is seconds remaining. The book gives what the market charges for
that outcome; the difference is the edge. Nothing trades unless the edge
clears MIN_EDGE with liquidity and a tight enough spread behind it.

Every return value is a dict with the same keys, so the dashboard and the
trader read one structure. `action` is "BUY YES", "BUY NO" or "NO TRADE", and
`reason` always explains itself in plain language.
"""

from __future__ import annotations

import math
import re
import statistics
from statistics import NormalDist
from typing import Any, Optional, Sequence

_NORMAL = NormalDist()
_MONEY = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def no_trade(reason: str, **extra: Any) -> dict:
    payload = {
        "action": "NO TRADE",
        "side": None,
        "confidence": 0,
        "reason": reason,
        "edge": None,
        "fair_yes": None,
        "price_cents": None,
        "available_size": None,
        "strike": None,
        "seconds_to_close": None,
    }
    payload.update(extra)
    return payload


def extract_strike(market: Optional[dict]) -> Optional[float]:
    """Find the settlement threshold on a market record."""
    if not market:
        return None

    for key in ("floor_strike", "cap_strike", "strike", "strike_value"):
        value = market.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)

    for key in ("yes_sub_title", "subtitle", "title"):
        text = market.get(key)
        if not isinstance(text, str):
            continue
        match = _MONEY.search(text)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def realized_vol_per_second(
    trades: Sequence[tuple[float, float]],
    now: float,
    window_seconds: int,
    min_samples: int,
) -> Optional[float]:
    """Standard deviation of one-second log returns, per second."""
    cutoff = now - window_seconds
    buckets: dict[int, float] = {}
    for timestamp, price in trades:
        if timestamp >= cutoff and price > 0:
            buckets[int(timestamp)] = price

    if len(buckets) < max(min_samples, 3):
        return None

    ordered = [buckets[key] for key in sorted(buckets)]
    returns = [
        math.log(ordered[index] / ordered[index - 1])
        for index in range(1, len(ordered))
        if ordered[index - 1] > 0
    ]
    if len(returns) < 2:
        return None

    sigma = statistics.stdev(returns)
    return sigma if sigma > 0 else None


def _best(levels: Any) -> tuple[Optional[int], int]:
    """Highest bid price and its size from one side of a Kalshi book."""
    if not isinstance(levels, list) or not levels:
        return None, 0
    best_price: Optional[int] = None
    best_size = 0
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        try:
            price = int(level[0])
            size = int(level[1])
        except (TypeError, ValueError):
            continue
        if best_price is None or price > best_price:
            best_price, best_size = price, size
    return best_price, best_size


def fair_yes_probability(spot: float, strike: float, sigma_per_second: float, seconds: float) -> float:
    sigma_t = sigma_per_second * math.sqrt(max(seconds, 1.0))
    if sigma_t <= 0:
        return 1.0 if spot > strike else 0.0
    return _NORMAL.cdf(math.log(spot / strike) / sigma_t)


def evaluate(
    *,
    spot: Optional[float],
    trades: Sequence[tuple[float, float]],
    market: Optional[dict],
    orderbook: Optional[dict],
    seconds_to_close: Optional[float],
    settings: Any,
    now: float,
) -> dict:
    """Produce the current decision. Pure function of its inputs."""

    if not spot or not trades:
        return no_trade("Waiting for live BTC-USD trades")

    if now - trades[-1][0] > 5:
        return no_trade("Coinbase BTC feed is stale")

    if not market:
        return no_trade("No open BTC-15m market discovered")

    strike = extract_strike(market)
    if not strike:
        return no_trade("Could not read the settlement strike for this market")

    if seconds_to_close is None:
        return no_trade("Market close time unavailable", strike=strike)

    if seconds_to_close < settings.min_seconds_to_close:
        return no_trade(
            "Too close to settlement to enter",
            strike=strike,
            seconds_to_close=round(seconds_to_close),
        )

    if seconds_to_close > settings.max_seconds_to_close:
        return no_trade(
            "Waiting for the window to come into range",
            strike=strike,
            seconds_to_close=round(seconds_to_close),
        )

    sigma = realized_vol_per_second(
        trades, now, settings.vol_window_seconds, settings.min_vol_samples
    )
    if sigma is None:
        return no_trade(
            "Building enough live history to measure volatility",
            strike=strike,
            seconds_to_close=round(seconds_to_close),
        )

    book = orderbook or {}
    yes_bid, yes_bid_size = _best(book.get("yes"))
    no_bid, no_bid_size = _best(book.get("no"))

    if yes_bid is None or no_bid is None:
        return no_trade(
            "Kalshi book is empty on one side",
            strike=strike,
            seconds_to_close=round(seconds_to_close),
        )

    # On Kalshi both sides of the book are bids. Buying YES means paying the
    # complement of the best NO bid, and vice versa.
    yes_ask = 100 - no_bid
    no_ask = 100 - yes_bid
    spread = (yes_ask - yes_bid) if yes_bid is not None else 100

    if spread > settings.max_spread_cents:
        return no_trade(
            f"Spread of {spread}c is wider than the limit",
            strike=strike,
            seconds_to_close=round(seconds_to_close),
        )

    fair_yes = fair_yes_probability(spot, strike, sigma, seconds_to_close)

    yes_edge = fair_yes - (yes_ask / 100.0)
    no_edge = (1.0 - fair_yes) - (no_ask / 100.0)

    if yes_edge >= no_edge:
        side, edge, price, size = "yes", yes_edge, yes_ask, no_bid_size
        model_probability = fair_yes
    else:
        side, edge, price, size = "no", no_edge, no_ask, yes_bid_size
        model_probability = 1.0 - fair_yes

    common = {
        "side": side,
        "edge": round(edge, 4),
        "fair_yes": round(fair_yes, 4),
        "price_cents": price,
        "available_size": size,
        "strike": strike,
        "seconds_to_close": round(seconds_to_close),
        "sigma_per_second": round(sigma, 8),
        "ticker": market.get("ticker"),
    }

    confidence = int(round(model_probability * 100))

    if edge < settings.min_edge:
        return no_trade(
            f"Best edge of {edge * 100:.1f}c is below the {settings.min_edge * 100:.0f}c minimum",
            **common,
        )

    if confidence < settings.min_confidence:
        return no_trade(
            f"Model confidence of {confidence}% is below the {settings.min_confidence}% floor",
            **{**common, "confidence": confidence},
        )

    if not (settings.min_price_cents <= price <= settings.max_price_cents):
        return no_trade(f"Price of {price}c is outside the tradable band", **common)

    if size < settings.min_book_size:
        return no_trade(f"Only {size} contracts resting at {price}c", **common)

    return {
        "action": f"BUY {side.upper()}",
        "confidence": confidence,
        "reason": (
            f"Model prices {side.upper()} at {model_probability * 100:.1f}% "
            f"against {price}c in the book, an edge of {edge * 100:.1f}c"
        ),
        **common,
    }
