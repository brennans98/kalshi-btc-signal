"""The contract's own price history -- the chart the trader actually watches.

This module exists because of a specific blind spot. Every read in
indicators.py is computed from the BTC spot tape; nothing in the codebase
ever recorded the price of the Kalshi contract itself. So the bot could not
see the thing a discretionary trader acts on: the contract gapping 9c lower
in four seconds, the bid rebuilding underneath it, the fall decelerating.
It could only see BTC, and infer.

What is recorded, once per published book update (WebSocket, so every change
Kalshi pushes):

    (timestamp, yes_bid, yes_ask, yes_bid_size, no_bid_size)

and what is derived from it, per side:

    mid / ask            the price we would pay now
    high_mid / low_mid   the rolling extremes over the lookback
    drop_cents           how far below the rolling high we are  <- the dip
    velocity             cents per second over the last few samples
    decelerating         the fall has slowed or turned (the entry timing cue)
    imbalance            resting demand on our side vs the other side
    seconds_since_low    how long ago the extreme printed

The tape is per ticker and self-trimming, and every reader tolerates a short
or empty tape by returning None -- a warming tape must never look like a
signal.
"""

import time
from collections import deque

import config

_tapes = {}
_MAX_SAMPLES = 6000


def _tape(ticker):
    if ticker not in _tapes:
        # One 15-minute market at a time; drop the rest when the market rolls
        # so memory cannot grow across a trading day.
        if len(_tapes) > 6:
            oldest = sorted(_tapes.items(), key=lambda item: item[1][-1][0] if item[1] else 0)
            for stale_ticker, _ in oldest[:-3]:
                _tapes.pop(stale_ticker, None)
        _tapes[ticker] = deque(maxlen=_MAX_SAMPLES)
    return _tapes[ticker]


def record(ticker, snapshot, now=None):
    """Append one book sample. Called from the WS publish path.

    Unchanged books are skipped: the tape should measure price movement, and
    duplicate samples flatten the velocity estimate toward zero exactly when
    a burst of identical deltas arrives.
    """
    if not ticker or not snapshot:
        return

    yes_bid = snapshot.get("yes_bid")
    yes_ask = snapshot.get("yes_ask")
    if yes_bid is None or yes_ask is None:
        return

    sample = (
        now or time.time(),
        int(yes_bid),
        int(yes_ask),
        int(snapshot.get("yes_bid_size") or 0),
        int(snapshot.get("no_bid_size") or 0),
    )

    tape = _tape(ticker)
    if tape:
        previous = tape[-1]
        if previous[1:] == sample[1:]:
            return
    tape.append(sample)

    cutoff = sample[0] - config.settings.tape_seconds
    while tape and tape[0][0] < cutoff:
        tape.popleft()


def forget(ticker):
    _tapes.pop(ticker, None)


def samples(ticker, seconds=None, now=None):
    now = now or time.time()
    tape = _tapes.get(ticker) or ()
    if seconds is None:
        return list(tape)
    cutoff = now - seconds
    return [sample for sample in tape if sample[0] >= cutoff]


def _side_prices(sample, side):
    """(bid, ask, our_resting_size, their_resting_size) for one side.

    NO prices are the YES book mirrored: a NO ask of 38c is a YES bid of 62c.
    """
    _, yes_bid, yes_ask, yes_bid_size, no_bid_size = sample
    if side == "yes":
        return yes_bid, yes_ask, yes_bid_size, no_bid_size
    return 100 - yes_ask, 100 - yes_bid, no_bid_size, yes_bid_size


def _mid(sample, side):
    bid, ask, _, _ = _side_prices(sample, side)
    return (bid + ask) / 2.0


def _mid_at(tape, side, when):
    """The side's mid as of a point in time: the last sample at or before it.

    A book is a step function, not a series of points. Between deltas the price
    is still whatever the last delta left it at, and reading it that way is the
    difference between "the price stopped falling" and "we have no data".
    """
    value = None
    for sample in tape:
        if sample[0] <= when:
            value = _mid(sample, side)
        else:
            break
    return value


def velocity_cents_per_second(ticker, side, seconds, now=None):
    """Signed slope of the side's mid over the window, in cents per second.

    Measured between two points in time rather than between the first and last
    SAMPLE in the window. That distinction matters at exactly the moment the
    strategy cares about most: when a falling book stalls, it stops producing
    deltas, so a sample-based slope goes blind (or, worse, keeps reporting the
    stale slope of the fall) precisely when the answer should be "zero -- it
    has stopped falling". Returns None only when the tape does not reach back
    far enough to cover the window.
    """
    now = now or time.time()
    tape = _tapes.get(ticker) or ()
    if len(tape) < 2 or seconds <= 0:
        return None

    end = _mid_at(tape, side, now)
    start = _mid_at(tape, side, now - seconds)
    if end is None or start is None:
        return None

    return (end - start) / float(seconds)


def analyze(ticker, side, lookback_seconds=None, now=None):
    """The contract-chart read for one side. None while the tape is warming."""
    cfg = config.settings
    now = now or time.time()
    lookback_seconds = lookback_seconds or cfg.dip_lookback_seconds

    window = samples(ticker, lookback_seconds, now=now)
    if len(window) < cfg.tape_min_samples:
        return None

    latest = window[-1]
    bid, ask, our_size, their_size = _side_prices(latest, side)
    mid_now = _mid(latest, side)

    mids = [(sample[0], _mid(sample, side)) for sample in window]
    high_ts, high_mid = max(mids, key=lambda item: item[1])
    low_ts, low_mid = min(mids, key=lambda item: item[1])

    total_size = our_size + their_size
    imbalance = None if total_size <= 0 else (our_size - their_size) / total_size

    fast = velocity_cents_per_second(ticker, side, cfg.dip_stabilize_seconds, now=now)
    slow = velocity_cents_per_second(ticker, side, cfg.dip_stabilize_seconds * 4, now=now)

    # Deceleration is the timing cue, and it is deliberately conservative:
    # the fast window must have stopped falling (or turned up) while the
    # wider window is still negative. Buying while the fast window is still
    # printing -2c/s is catching the knife mid-fall.
    decelerating = (
        fast is not None
        and slow is not None
        and slow < 0
        and fast >= -abs(cfg.dip_decel_tolerance)
    )

    return {
        "bid": bid,
        "ask": ask,
        "mid": round(mid_now, 2),
        "high_mid": round(high_mid, 2),
        "low_mid": round(low_mid, 2),
        "drop_cents": round(high_mid - mid_now, 2),
        "rise_cents": round(mid_now - low_mid, 2),
        "range_cents": round(high_mid - low_mid, 2),
        "seconds_since_high": round(now - high_ts, 1),
        "seconds_since_low": round(now - low_ts, 1),
        "velocity_fast": None if fast is None else round(fast, 3),
        "velocity_slow": None if slow is None else round(slow, 3),
        "decelerating": bool(decelerating),
        "imbalance": None if imbalance is None else round(imbalance, 3),
        "our_resting_size": our_size,
        "their_resting_size": their_size,
        "samples": len(window),
        "lookback_seconds": lookback_seconds,
    }


def view(ticker):
    """Compact dashboard payload for both sides."""
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "samples": len(_tapes.get(ticker) or ()),
        "yes": analyze(ticker, "yes"),
        "no": analyze(ticker, "no"),
    }
