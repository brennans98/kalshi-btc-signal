"""Signal policy: converts the live BTC tape plus the Kalshi book into a decision.

Every threshold is an environment variable so it can be tuned without a code change.
Every rejection returns a plain-language reason that surfaces on the dashboard.
"""

import math
import os
import re
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


def limits():
    return {
        "min_edge_cents": _env_float("MIN_EDGE_CENTS", 6),
        "min_confidence": _env_float("MIN_CONFIDENCE", 60),
        "max_spread_cents": _env_float("MAX_SPREAD_CENTS", 6),
        "min_book_size": _env_int("MIN_BOOK_SIZE", 20),
        "min_price_cents": _env_float("MIN_PRICE_CENTS", 12),
        "max_price_cents": _env_float("MAX_PRICE_CENTS", 88),
        "min_seconds_to_close": _env_int("MIN_SECONDS_TO_CLOSE", 90),
        "max_seconds_to_close": _env_int("MAX_SECONDS_TO_CLOSE", 900),
        "min_vol_samples": _env_int("MIN_VOL_SAMPLES", 30),
    }


def no_trade(reason, **extra):
    payload = {"action": "NO TRADE", "side": None, "confidence": 0, "reason": reason}
    payload.update(extra)
    return payload


# ---------- market plumbing ----------

def extract_strike(ticker, title=None):
    """Pull the threshold price out of a KXBTC15M ticker.

    Kalshi threshold tickers end in -T<strike>, e.g. KXBTC15M-26AUG23H1345-T77250.
    Falls back to the title when the ticker has no -T segment.
    """
    if ticker:
        match = re.search(r"-T(\d+(?:\.\d+)?)$", ticker.strip())
        if match:
            return float(match.group(1))

    if title:
        match = re.search(r"\$?\s*([0-9][0-9,]{3,})(?:\.\d+)?", title)
        if match:
            return float(match.group(1).replace(",", ""))

    return None


def seconds_to_close(close_time_iso):
    if not close_time_iso:
        return None
    try:
        text = close_time_iso.replace("Z", "+00:00")
        close = datetime.fromisoformat(text)
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        return (close - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


def best_levels(orderbook):
    """Return best yes bid/ask in cents with resting size.

    Kalshi books quote resting BIDS on both sides. A yes ask is therefore
    derived from the best no bid: yes_ask = 100 - best_no_bid.
    """
    book = (orderbook or {}).get("orderbook") or {}
    yes_levels = book.get("yes") or []
    no_levels = book.get("no") or []

    def top(levels):
        best_price = None
        best_size = 0
        for level in levels:
            if not level:
                continue
            try:
                price = float(level[0])
                size = float(level[1]) if len(level) > 1 else 0
            except (TypeError, ValueError, IndexError):
                continue
            if best_price is None or price > best_price:
                best_price, best_size = price, size
        return best_price, best_size

    yes_bid, yes_bid_size = top(yes_levels)
    no_bid, no_bid_size = top(no_levels)

    yes_ask = (100 - no_bid) if no_bid is not None else None
    return {
        "yes_bid": yes_bid,
        "yes_bid_size": yes_bid_size,
        "yes_ask": yes_ask,
        "yes_ask_size": no_bid_size,
        "no_bid": no_bid,
        "no_ask": (100 - yes_bid) if yes_bid is not None else None,
        "no_ask_size": yes_bid_size,
    }


# ---------- model ----------

def realized_vol_per_second(trades, lookback_seconds=300):
    """Standard deviation of one-second log returns over the recent tape."""
    if not trades or len(trades) < 5:
        return None, 0

    now = trades[-1][0]
    cutoff = now - lookback_seconds

    buckets = {}
    for timestamp, price in trades:
        if timestamp < cutoff:
            continue
        buckets[int(timestamp)] = price

    if len(buckets) < 5:
        return None, len(buckets)

    series = [buckets[second] for second in sorted(buckets)]
    returns = []
    for previous, current in zip(series, series[1:]):
        if previous > 0 and current > 0:
            returns.append(math.log(current / previous))

    if len(returns) < 4:
        return None, len(returns)

    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    sigma = math.sqrt(variance)

    return (sigma if sigma > 0 else None), len(returns)


def normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fair_yes_probability(spot, strike, sigma_per_second, seconds_left):
    """Driftless lognormal probability that spot settles above strike."""
    if not spot or not strike or not sigma_per_second or not seconds_left or seconds_left <= 0:
        return None

    sigma_horizon = sigma_per_second * math.sqrt(seconds_left)
    if sigma_horizon <= 0:
        return None

    return normal_cdf(math.log(spot / strike) / sigma_horizon)


def evaluate(spot, trades, market, orderbook):
    """Return a signal dict. action is BUY YES, BUY NO, or NO TRADE."""
    config = limits()

    if not spot:
        return no_trade("Waiting for live BTC-USD trades")

    ticker = (market or {}).get("ticker")
    if not ticker:
        return no_trade("No open BTC-15m market discovered yet")

    strike = extract_strike(ticker, (market or {}).get("title"))
    if strike is None:
        return no_trade(f"Could not read a strike price from market {ticker}")

    remaining = seconds_to_close((market or {}).get("close_time"))
    if remaining is None:
        return no_trade("Market close time unavailable")
    if remaining < config["min_seconds_to_close"]:
        return no_trade(f"Only {int(remaining)}s to close; inside the no-entry window")
    if remaining > config["max_seconds_to_close"]:
        return no_trade(f"{int(remaining)}s to close is outside the traded horizon")

    sigma, samples = realized_vol_per_second(trades)
    if sigma is None or samples < config["min_vol_samples"]:
        return no_trade(f"Building volatility estimate ({samples} samples)")

    fair = fair_yes_probability(spot, strike, sigma, remaining)
    if fair is None:
        return no_trade("Fair value could not be computed")

    fair_cents = fair * 100
    levels = best_levels(orderbook)

    if levels["yes_ask"] is None or levels["yes_bid"] is None:
        return no_trade("Kalshi book is empty on one side")

    spread = levels["yes_ask"] - levels["yes_bid"]
    if spread > config["max_spread_cents"]:
        return no_trade(f"Spread {spread:.0f}c is wider than the {config['max_spread_cents']:.0f}c limit")

    yes_edge = fair_cents - levels["yes_ask"]
    no_ask = levels["no_ask"]
    no_edge = (100 - fair_cents) - no_ask if no_ask is not None else None

    if no_edge is not None and no_edge > yes_edge:
        side, price, edge, size = "no", no_ask, no_edge, levels["no_ask_size"]
    else:
        side, price, edge, size = "yes", levels["yes_ask"], yes_edge, levels["yes_ask_size"]

    context = {
        "ticker": ticker,
        "strike": strike,
        "spot": spot,
        "fair_yes_cents": round(fair_cents, 2),
        "price_cents": price,
        "edge_cents": round(edge, 2),
        "spread_cents": round(spread, 2),
        "book_size": size,
        "seconds_to_close": int(remaining),
        "sigma_per_second": sigma,
        "vol_samples": samples,
    }

    if edge < config["min_edge_cents"]:
        return no_trade(
            f"Best edge {edge:.1f}c is under the {config['min_edge_cents']:.0f}c minimum",
            **context,
        )

    if price is None or price < config["min_price_cents"] or price > config["max_price_cents"]:
        return no_trade(f"Price {price}c is outside the tradable band", **context)

    if not size or size < config["min_book_size"]:
        return no_trade(f"Only {int(size or 0)} contracts resting; under the size floor", **context)

    # Confidence scales the edge against the width of the price band it sits in.
    confidence = min(99.0, 50.0 + (edge / max(config["min_edge_cents"], 1)) * 20.0)
    if confidence < config["min_confidence"]:
        return no_trade(f"Confidence {confidence:.0f}% is under the floor", **context)

    signal = {
        "action": f"BUY {side.upper()}",
        "side": side,
        "confidence": round(confidence, 1),
        "reason": f"Fair {fair_cents:.0f}c vs {side.upper()} ask {price:.0f}c; {edge:.1f}c edge",
    }
    signal.update(context)
    return signal
