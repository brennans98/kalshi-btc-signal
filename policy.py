"""The decision model that was missing.

The previous signal() computed momentum inputs and then returned a hardcoded
NO TRADE, because nothing ever compared a fair value against the Kalshi book.
This module does that comparison.

Approach: estimate realized volatility from the live trade tape, price the
binary as a driftless lognormal, and compare that probability against what the
book is actually asking. Trade only when the ask is enough below fair value to
cover the cost of being wrong about the volatility estimate.

Every rejection returns a specific reason string. A dashboard that says
"edge 0.4c below the 3.0c minimum" is debuggable; a bare NO TRADE is not.
"""

import math
import os
import time
from datetime import datetime, timezone


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def settings():
    return {
        "min_edge_cents": _env_float("MIN_EDGE_CENTS", 3.0),
        "min_confidence": _env_int("MIN_CONFIDENCE", 58),
        "max_spread_cents": _env_int("MAX_SPREAD_CENTS", 8),
        "min_price_cents": _env_int("MIN_PRICE_CENTS", 8),
        "max_price_cents": _env_int("MAX_PRICE_CENTS", 92),
        "min_seconds_to_close": _env_int("MIN_SECONDS_TO_CLOSE", 75),
        "max_seconds_to_close": _env_int("MAX_SECONDS_TO_CLOSE", 900),
        "min_history_seconds": _env_int("MIN_HISTORY_SECONDS", 120),
        "stale_feed_seconds": _env_float("STALE_FEED_SECONDS", 5.0),
    }


def _no_trade(reason, **extra):
    payload = {"action": "NO TRADE", "confidence": 0, "reason": reason}
    payload.update(extra)
    return payload


def _normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_vol_per_second(trades, window_seconds=300):
    """Annualization-free volatility: standard deviation of 1-second log returns.

    Sampling at one second rather than per-trade keeps the estimate from being
    inflated by trade-rate bursts, which is the usual way a naive tick-level
    estimate produces a fake edge.
    """
    if len(trades) < 30:
        return None

    cutoff = time.time() - window_seconds
    buckets = {}

    for timestamp, price in trades:
        if timestamp >= cutoff:
            buckets[int(timestamp)] = price

    if len(buckets) < 30:
        return None

    series = [buckets[key] for key in sorted(buckets)]
    returns = [
        math.log(series[index] / series[index - 1])
        for index in range(1, len(series))
        if series[index] > 0 and series[index - 1] > 0
    ]

    if len(returns) < 20:
        return None

    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    sigma = math.sqrt(variance)

    return sigma if sigma > 0 else None


def probability_above(spot, strike, sigma_per_second, seconds_remaining):
    """P(spot settles above strike) under driftless GBM."""
    if spot <= 0 or strike <= 0 or sigma_per_second <= 0 or seconds_remaining <= 0:
        return None

    total_sigma = sigma_per_second * math.sqrt(seconds_remaining)
    numerator = math.log(spot / strike) - 0.5 * (total_sigma ** 2)

    return _normal_cdf(numerator / total_sigma)


def market_strike(market):
    """Resolve the strike for an 'above X' market.

    Kalshi supplies floor_strike on above-type markets. Ticker parsing is a
    fallback only: a mis-parsed strike produces a confident inverted signal,
    which is the most damaging failure mode in this system.
    """
    if not market:
        return None, "no market"

    for field in ("floor_strike", "strike", "cap_strike"):
        value = market.get(field)
        if isinstance(value, (int, float)) and value > 0:
            return float(value), f"market.{field}"

    ticker = market.get("ticker") or ""
    tail = ticker.rsplit("-", 1)[-1]

    if tail.upper().startswith("T"):
        try:
            return float(tail[1:].replace("_", ".")), "ticker"
        except ValueError:
            pass

    return None, "unresolved"


def seconds_to_close(market):
    close_time = (market or {}).get("close_time")
    if not close_time:
        return None

    try:
        parsed = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except ValueError:
        return None

    return (parsed - datetime.now(timezone.utc)).total_seconds()


def best_levels(orderbook):
    """Return (yes_ask, no_ask, yes_bid, no_bid) in cents.

    Kalshi quotes both sides as bids. The ask for yes is 100 minus the best
    bid for no, and vice versa.
    """
    book = (orderbook or {}).get("orderbook") or {}

    def top(levels):
        prices = [
            int(level[0])
            for level in (levels or [])
            if isinstance(level, (list, tuple)) and len(level) >= 2 and int(level[1]) > 0
        ]
        return max(prices) if prices else None

    yes_bid = top(book.get("yes"))
    no_bid = top(book.get("no"))

    yes_ask = 100 - no_bid if no_bid is not None else None
    no_ask = 100 - yes_bid if yes_bid is not None else None

    return yes_ask, no_ask, yes_bid, no_bid


def evaluate(trades, market, orderbook):
    """Return a signal dict. action is 'BUY YES', 'BUY NO', or 'NO TRADE'."""
    config = settings()

    if not trades:
        return _no_trade("Waiting for live BTC-USD trades")

    spot_age = time.time() - trades[-1][0]
    if spot_age > config["stale_feed_seconds"]:
        return _no_trade(f"Coinbase BTC feed is stale ({spot_age:.0f}s since last trade)")

    history = trades[-1][0] - trades[0][0]
    if history < config["min_history_seconds"]:
        remaining = int(config["min_history_seconds"] - history)
        return _no_trade(f"Building volatility history ({remaining}s remaining)")

    if not market or not market.get("ticker"):
        return _no_trade("No open BTC-15m market discovered")

    strike, strike_source = market_strike(market)
    if strike is None:
        return _no_trade(f"Cannot resolve strike for {market.get('ticker')}")

    remaining = seconds_to_close(market)
    if remaining is None:
        return _no_trade("Market close time is unavailable")

    if remaining < config["min_seconds_to_close"]:
        return _no_trade(f"Too close to settlement ({remaining:.0f}s remaining)")

    if remaining > config["max_seconds_to_close"]:
        return _no_trade(f"Outside the trading window ({remaining:.0f}s to close)")

    sigma = realized_vol_per_second(trades)
    if sigma is None:
        return _no_trade("Insufficient one-second samples to estimate volatility")

    yes_ask, no_ask, yes_bid, no_bid = best_levels(orderbook)
    if yes_ask is None or no_ask is None:
        return _no_trade("Waiting for a two-sided Kalshi book")

    spread = yes_ask - (yes_bid if yes_bid is not None else 0)
    if spread > config["max_spread_cents"]:
        return _no_trade(f"Book spread too wide ({spread}c)")

    spot = trades[-1][1]
    fair_yes = probability_above(spot, strike, sigma, remaining)
    if fair_yes is None:
        return _no_trade("Fair value could not be computed")

    yes_edge = fair_yes * 100 - yes_ask
    no_edge = (1 - fair_yes) * 100 - no_ask

    if yes_edge >= no_edge:
        side, ask, edge, prob = "yes", yes_ask, yes_edge, fair_yes
    else:
        side, ask, edge, prob = "no", no_ask, no_edge, 1 - fair_yes

    diagnostics = {
        "side": side,
        "ticker": market.get("ticker"),
        "price_cents": ask,
        "edge_cents": round(edge, 2),
        "fair_prob": round(prob, 4),
        "strike": strike,
        "strike_source": strike_source,
        "spot": spot,
        "sigma_per_second": round(sigma, 8),
        "seconds_to_close": int(remaining),
    }

    confidence = int(round(prob * 100))

    if not config["min_price_cents"] <= ask <= config["max_price_cents"]:
        return _no_trade(f"Ask {ask}c is outside the tradable price band", **diagnostics)

    if edge < config["min_edge_cents"]:
        return _no_trade(
            f"Edge {edge:.1f}c below the {config['min_edge_cents']:.1f}c minimum",
            **diagnostics,
        )

    if confidence < config["min_confidence"]:
        return _no_trade(
            f"Confidence {confidence}% below the {config['min_confidence']}% floor",
            **diagnostics,
        )

    payload = {
        "action": f"BUY {side.upper()}",
        "confidence": confidence,
        "reason": (
            f"Fair {prob * 100:.1f}% vs {ask}c ask — {edge:.1f}c edge with "
            f"{int(remaining)}s to close"
        ),
    }
    payload.update(diagnostics)
    return payload
