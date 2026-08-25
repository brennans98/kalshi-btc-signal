"""Chart structure read from the BTC tape.

The signal model in policy.py prices the contract (fair value from realized
volatility); this module reads the chart the way a discretionary trader would,
and its output is used to refuse trades, not to find them:

  - Trend: EMA(fast) vs EMA(slow) on bar closes. A momentum entry against the
    prevailing trend is the single most reliable way this bot has lost money.
  - RSI: entries in the direction of an already-exhausted move are chasing.
    Buying YES into an overbought tape is buying someone else's exit.

Every live loss so far shares one anatomy: enter on a burst, get clipped by
noise seconds later. Both gates exist to starve that pattern.

All functions take the same trades list policy.py uses: [(timestamp, price)]
for BTC-USD, roughly one entry per second.
"""

import time

import config


def closes(trades, bar_seconds=None, window_seconds=None):
    """Bar closes from the tick tape: last trade price in each bucket.

    Buckets with no trades are skipped rather than forward-filled; EMA and
    RSI tolerate slightly irregular spacing better than they tolerate
    fabricated flat bars, which dampen both indicators toward no-signal.
    """
    cfg = config.settings
    bar_seconds = bar_seconds or cfg.bar_seconds
    window_seconds = window_seconds or (cfg.ema_slow_seconds + cfg.bar_seconds * (cfg.rsi_period + 1))

    cutoff = time.time() - window_seconds
    buckets = {}
    for timestamp, price in trades:
        if timestamp >= cutoff:
            buckets[int(timestamp // bar_seconds)] = price

    return [buckets[key] for key in sorted(buckets)]


def ema(values, period):
    """Standard exponential moving average; None when underfed."""
    if period <= 0 or len(values) < period:
        return None

    alpha = 2.0 / (period + 1.0)
    average = sum(values[:period]) / period
    for value in values[period:]:
        average = alpha * value + (1 - alpha) * average
    return average


def rsi(values, period=None):
    """Wilder's RSI on bar closes; None when underfed.

    100 means every bar in the lookback closed higher; 0 means every bar
    closed lower. The classic exhaustion reads are >70 and <30.
    """
    period = period or config.settings.rsi_period
    if len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def analyze(trades):
    """The chart read: trend direction, EMA separation, RSI.

    Returns None while the tape is too short to judge (about
    EMA_SLOW_SECONDS of feed after boot) -- callers treat that as
    "not enough chart to trade against", not as permission.

    trend is 'up', 'down', or 'flat'. The dead zone keeps a crossing pair of
    EMAs from flapping the trend call bar to bar: separation must exceed
    TREND_DEADZONE_BPS basis points of price before it counts as direction.
    """
    cfg = config.settings
    series = closes(trades)

    fast_period = max(2, cfg.ema_fast_seconds // cfg.bar_seconds)
    slow_period = max(fast_period + 1, cfg.ema_slow_seconds // cfg.bar_seconds)

    if len(series) < slow_period + 1:
        return None

    fast = ema(series, fast_period)
    slow = ema(series, slow_period)
    strength = rsi(series)
    if fast is None or slow is None or strength is None or slow <= 0:
        return None

    separation_bps = (fast - slow) / slow * 10000.0
    if separation_bps > cfg.trend_deadzone_bps:
        trend = "up"
    elif separation_bps < -cfg.trend_deadzone_bps:
        trend = "down"
    else:
        trend = "flat"

    # Support and resistance: the extremes of the recent tape. A chartist
    # does not buy into a ceiling or sell into a floor -- price approaching
    # a recent extreme from below/above tends to stall there, and the few
    # cents of stall are this bot's entire trade. A BREAK of the level
    # (spot at or beyond it) is the opposite read and is not blocked.
    sr_series = closes(trades, window_seconds=cfg.sr_lookback_seconds)
    recent_high = max(sr_series) if sr_series else None
    recent_low = min(sr_series) if sr_series else None

    return {
        "trend": trend,
        "ema_fast": fast,
        "ema_slow": slow,
        "separation_bps": round(separation_bps, 2),
        "rsi": round(strength, 1),
        "bars": len(series),
        "recent_high": recent_high,
        "recent_low": recent_low,
    }
