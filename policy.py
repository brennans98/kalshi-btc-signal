"""The scalp entry model.

What this asks is narrower than "will this contract settle yes". It asks:
is this contract mispriced right now, and can it travel far enough, soon
enough, for the exit ladder to bank a few cents before the hold window or the
settlement guard closes the trade, after fees?

Four gates matter more here than in a hold-to-settlement design:

  1. Expected move. If the contract cannot plausibly move the small target's
     worth of cents inside the hold window, the edge is unharvestable no matter
     how real it is.
  2. Exit liquidity. The entry is only half the trade. If there is no resting
     size on the side we would sell into, we can enter and not get out.
  3. Spread. Crossing a wide spread means starting the scalp several cents
     underwater, which the small target may never recover.
  4. Fees. Kalshi charges ceil(0.07 * C * P * (1-P)) cents per fill, which
     peaks near the 50c midpoint. An edge that looks real before fees can be
     a guaranteed loss after both the entry and exit fill are charged.

On top of the pricing gates sits the chart read (indicators.py): an entry must
agree with the EMA trend, must not chase an RSI-exhausted move, and carries a
volatility-scaled stop instead of a fixed one. The live loss pattern this
kills: enter on a burst against the larger trend, get clipped by noise seconds
later because the fixed 6c stop sat inside the tape's normal wiggle.

Every rejection returns a specific reason. "Expected move 1.8c short of the 3c
small target" is debuggable; a bare NO TRADE is not.

The dip lanes
-------------
The gates above describe a momentum model, and they are explicitly hostile to
buying a falling contract -- correctly, for a momentum trade. But the manual
trade being replicated here is the opposite one: a contract gaps 9c lower, the
fall decelerates, the bid rebuilds, and it recovers. That trade was previously
unreachable for two reasons. The bot had no record of the contract's own price
(only BTC spot), and the trend/RSI/support gates rejected the setup by design.

So the dip lanes are a separate path, reading booktape.py, with their own
gates rather than a relaxation of these ones:

  dislocation -- the contract fell hard while SPOT BARELY MOVED. Nothing
                 justifies the new price, so the model's fair value is the
                 reference and the edge is mechanical.
  reversion   -- spot did move, and the contract overshot it. This is a real
                 fade, so it demands a deeper drop, exhaustion on the spot
                 read, and scores lower conviction.

Both still require, without exception: the fall to have stopped (velocity
decelerating), model edge above the fee floor, a two-sided book inside the
spread cap, real resting size to exit into, and enough time left for the
recovery to happen. A dip with no edge is not a discount, it is a contract
that has been correctly repriced.

Conviction
----------
Every tradable signal carries a 0-100 conviction score blended from several
independent confirmations. risk.py turns it into position size, which is how
one engine covers both the $5 marginal setup and the $20 fully-confirmed one.
"""

import math
import os
import time
from datetime import datetime, timezone

import booktape
import config
import indicators


def _no_trade(reason, **extra):
    payload = {"action": "NO TRADE", "confidence": 0, "reason": reason}
    payload.update(extra)
    return payload


def _env_int(name, default):
    try:
        return int(float((os.getenv(name) or str(default)).strip()))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def _normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _normal_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def realized_vol_per_second(trades, window_seconds=None):
    """Standard deviation of one-second log returns."""
    cfg = config.settings
    window_seconds = window_seconds or cfg.vol_window_seconds

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


def efficiency_ratio(trades, window_seconds=None):
    """Kaufman efficiency ratio: |net move| / sum of |one-second moves|.

    1.0 means price went somewhere in a straight line; a pure random walk
    over N one-second samples scores about sqrt(pi / (2N)) (~0.09 for a
    180-second window). evaluate() refuses to produce a BUY while this is
    below the configured floor: a momentum model in a chopping tape
    systematically buys local extremes and gets clipped by its own stop.
    Returns None when the window holds too few samples to judge.
    """
    cfg = config.settings
    window_seconds = window_seconds or cfg.chop_window_seconds

    cutoff = time.time() - window_seconds
    buckets = {}
    for timestamp, price in trades:
        if timestamp >= cutoff:
            buckets[int(timestamp)] = price

    if len(buckets) < 30:
        return None

    series = [buckets[key] for key in sorted(buckets)]
    net = abs(series[-1] - series[0])
    path = sum(abs(series[i] - series[i - 1]) for i in range(1, len(series)))

    if path <= 0:
        return 0.0
    return net / path


def standard_score(spot, strike, sigma_per_second, seconds_remaining):
    """The z of a driftless lognormal settling above strike."""
    if spot <= 0 or strike <= 0 or sigma_per_second <= 0 or seconds_remaining <= 0:
        return None

    total_sigma = sigma_per_second * math.sqrt(seconds_remaining)
    if total_sigma <= 0:
        return None

    return (math.log(spot / strike) - 0.5 * (total_sigma ** 2)) / total_sigma


def probability_above(spot, strike, sigma_per_second, seconds_remaining):
    z = standard_score(spot, strike, sigma_per_second, seconds_remaining)
    return None if z is None else _normal_cdf(z)


def expected_move_cents(z, horizon_seconds, seconds_remaining):
    """Roughly how many cents this contract's price travels over the horizon."""
    if z is None or seconds_remaining <= 0 or horizon_seconds <= 0:
        return None

    horizon = min(horizon_seconds, seconds_remaining)
    return _normal_pdf(z) * math.sqrt(horizon / seconds_remaining) * 100.0


def market_strike(market):
    """Resolve the strike for an 'above X' market."""
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


def close_epoch(market):
    """Unix epoch of the market close, or None.

    The timezone handling here is not cosmetic. datetime.timestamp() on a NAIVE
    datetime interprets it as LOCAL time, so if Kalshi ever returns a timestamp
    without an offset, the close would land hours away from the truth -- and
    seconds_to_close feeds the settlement guard, the late-entry window, and every
    "is there time to exit" decision. Being wrong by one timezone here means
    holding positions straight through expiry. Kalshi sends RFC3339 with a Z, so
    this should never trigger; it is explicit because the failure is silent and
    total rather than noisy and partial.
    """
    close_time = (market or {}).get("close_time")
    if not close_time:
        return None

    try:
        parsed = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.timestamp()


def seconds_to_close(market, skew_seconds=0.0):
    """Seconds until close, optionally adjusted for measured clock skew.

    skew_seconds is how far our clock is AHEAD of the exchange's matching
    engine. A clock running behind the exchange makes us believe there is more
    time left than there is, which is the dangerous direction: it delays the
    settlement guard and can leave a position unexited at expiry. Subtracting a
    negative skew shortens the remaining time, so the guard fires earlier.
    """
    epoch = close_epoch(market)
    if epoch is None:
        return None
    return epoch - time.time() + min(0.0, float(skew_seconds or 0.0))


def taker_fee_cents(count, price_cents):
    """Kalshi's published taker fee: ceil(0.07 * C * P * (1-P) * 100) cents,
    where P is the price expressed as a fraction of a dollar (price_cents/100).
    """
    if count <= 0 or price_cents is None:
        return 0.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return math.ceil(0.07 * count * p * (1 - p) * 100) / 1.0


def maker_fee_cents(count, price_cents):
    """Fee for a resting (maker) fill.

    Kalshi's fee schedule applies a per-series maker multiplier to the taker
    formula. For KXBTC15M the multiplier is 0: resting orders trade free.
    MAKER_FEE_MULT exists so a future fee change is a config edit, not a
    code change.
    """
    return taker_fee_cents(count, price_cents) * config.settings.maker_fee_multiplier


def leg_fee_cents(count, price_cents, is_maker):
    return maker_fee_cents(count, price_cents) if is_maker else taker_fee_cents(count, price_cents)


def round_trip_fee_cents(
    count, entry_price_cents, exit_price_cents, entry_is_maker=False, exit_is_maker=False
):
    """Fee is charged on both the entry fill and the exit fill.

    Which side of each leg we are on is a configuration choice (ENTRY_STYLE /
    EXIT_STYLE). Maker legs use the series' maker multiplier; taker legs pay
    the full published formula. Stops always cross as takers regardless of
    EXIT_STYLE, so a conservative caller can price the exit leg as taker.
    """
    entry_fee = leg_fee_cents(count, entry_price_cents, entry_is_maker)
    exit_fee = leg_fee_cents(count, exit_price_cents, exit_is_maker)
    return entry_fee + exit_fee


def _normalize_orderbook_fp(raw):
    """Convert Kalshi's orderbook_fp format to the canonical integer-cents format.

    Kalshi's production API returns:
        {"orderbook_fp": {"yes_dollars": [["0.4300", "2514.00"], ...],
                          "no_dollars":  [["0.3800", "2607.00"], ...]}}

    We normalise this to:
        {"orderbook": {"yes": [[43, 2514], ...],
                       "no":  [[38, 2607], ...]}}

    so the rest of the codebase works against a single stable schema.
    """
    fp = (raw or {}).get("orderbook_fp")
    if not fp:
        return raw  # already canonical or None

    def convert(levels):
        result = []
        for level in levels or []:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            try:
                price_cents = round(float(level[0]) * 100)
                size = round(float(level[1]))
            except (TypeError, ValueError):
                continue
            if size > 0:
                result.append([price_cents, size])
        return result

    return {
        "orderbook": {
            "yes": convert(fp.get("yes_dollars")),
            "no": convert(fp.get("no_dollars")),
        }
    }


def book_snapshot(orderbook):
    """Top of book with resting sizes, in cents.

    Handles both the legacy integer format and the current orderbook_fp
    dollar-string format returned by Kalshi's production API.
    """
    normalised = _normalize_orderbook_fp(orderbook)
    book = (normalised or {}).get("orderbook") or {}

    def ladder(levels):
        """Clean, sorted bid ladder: [(price, size), ...] best price first.

        Keeping the whole ladder rather than only the top matters because
        orders are sized in contracts, and how many contracts exist at a price
        is not the same question as what the price is.
        """
        cleaned = {}
        for level in levels or []:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            try:
                price, size = int(level[0]), int(level[1])
            except (TypeError, ValueError):
                continue
            if size > 0 and 0 < price < 100:
                # Duplicate prices are summed rather than overwritten.
                cleaned[price] = cleaned.get(price, 0) + size
        return sorted(cleaned.items(), key=lambda item: -item[0])

    yes_bids = ladder(book.get("yes"))
    no_bids = ladder(book.get("no"))

    yes_bid, yes_bid_size = yes_bids[0] if yes_bids else (None, 0)
    no_bid, no_bid_size = no_bids[0] if no_bids else (None, 0)

    return {
        "yes_bid": yes_bid,
        "yes_bid_size": yes_bid_size,
        "no_bid": no_bid,
        "no_bid_size": no_bid_size,
        "yes_ask": None if no_bid is None else 100 - no_bid,
        "no_ask": None if yes_bid is None else 100 - yes_bid,
        "yes_spread": None if (yes_bid is None or no_bid is None) else (100 - no_bid) - yes_bid,
        # Bid ladders, best first.
        "yes_bids": yes_bids,
        "no_bids": no_bids,
        # Ask ladders, cheapest first. Buying YES means taking the NO bids:
        # a NO bid of q cents for s contracts IS a YES offer at (100 - q) for
        # s contracts. Same book, mirrored.
        # No reversal: the bid ladders are sorted best-first, meaning highest
        # price first, and 100 minus a descending sequence is already ascending.
        # Reversing as well flips them to worst-first, which would silently
        # price every sweep at the most expensive level on the book.
        "yes_asks": [(100 - price, size) for price, size in no_bids],
        "no_asks": [(100 - price, size) for price, size in yes_bids],
    }


def asks_for_side(snapshot, side):
    """The ladder we must lift to BUY `side`, cheapest first."""
    return (snapshot or {}).get("yes_asks" if side == "yes" else "no_asks") or []


def bids_for_side(snapshot, side):
    """The ladder we must hit to SELL `side`, best first."""
    return (snapshot or {}).get("yes_bids" if side == "yes" else "no_bids") or []


def sweep(levels, count):
    """Walk a ladder for `count` contracts and report what it would actually cost.

    This exists because the bot was pricing every order at the top of book
    while sending it as fill-or-kill for a size the top of book could not
    supply. Two separate errors came out of that: orders that were killed
    outright (the whole requested size had to exist at one price), and, if the
    limit were widened, an average fill price worse than the edge calculation
    assumed. Both are answered by walking the ladder before sending anything.

    Fees are accumulated PER LEVEL, because Kalshi rounds its fee up to the
    next cent on each fill. One fill of 60 and three fills of 20 do not cost
    the same, and the single-ceiling estimate was always the optimistic one.

    Returns filled (may be < count when the book is too thin), cost_cents,
    avg_price_cents (float; the number the edge must actually clear),
    worst_price_cents (the limit price required for the order to complete),
    and fee_cents.
    """
    if count <= 0:
        return {
            "filled": 0,
            "cost_cents": 0,
            "avg_price_cents": None,
            "worst_price_cents": None,
            "fee_cents": 0.0,
        }

    filled = 0
    cost = 0
    fee = 0.0
    worst = None

    for price, size in levels or []:
        if filled >= count:
            break
        take = min(int(size), count - filled)
        if take <= 0:
            continue
        filled += take
        cost += take * int(price)
        fee += taker_fee_cents(take, price)
        worst = int(price)

    return {
        "filled": filled,
        "cost_cents": cost,
        "avg_price_cents": None if filled == 0 else cost / filled,
        "worst_price_cents": worst,
        "fee_cents": fee,
    }


def depth_within(levels, max_slippage_cents):
    """Contracts available without paying more than `max_slippage_cents` worse
    than the best price on the ladder.

    The cap is what keeps "size it to the book" from turning into "sweep the
    book". Depth three cents up is depth we do not want.
    """
    levels = levels or []
    if not levels:
        return 0
    best = int(levels[0][0])
    total = 0
    for price, size in levels:
        if abs(int(price) - best) > max_slippage_cents:
            break
        total += int(size)
    return total


def spot_move_bps(trades, seconds):
    """Signed move of the spot tape over the last `seconds`, in bps.

    Used by the dislocation test: a contract that fell 9c while spot barely
    moved is a book event, not a repricing. None when the tape does not reach
    back far enough to answer honestly.
    """
    if not trades:
        return None
    now = trades[-1][0]
    cutoff = now - seconds
    if trades[0][0] > cutoff:
        return None
    reference = None
    for timestamp, price in reversed(trades):
        if timestamp <= cutoff:
            reference = price
            break
    if reference is None or reference <= 0:
        return None
    return (trades[-1][1] - reference) / reference * 10000.0


def _clamp01(value):
    return max(0.0, min(1.0, value))


def _score(*parts):
    """Weighted 0-100 conviction from (weight, 0-1 component) pairs.

    Conviction is what sizing reads: it is the difference between the $5
    version of a trade and the $20 version. It is deliberately a blend of
    several independent confirmations rather than one number scaled up, so a
    single extreme input cannot manufacture full size on its own.
    """
    total_weight = sum(weight for weight, _ in parts)
    if total_weight <= 0:
        return 0
    blended = sum(weight * _clamp01(value) for weight, value in parts) / total_weight
    return int(round(100 * blended))


def bid_for_side(snapshot, side):
    """The price we could sell this side into right now."""
    return (snapshot or {}).get("yes_bid" if side == "yes" else "no_bid")


def evaluate(trades, market, orderbook, spot_status=None, clock_status=None):
    """Return a signal dict. action is 'BUY YES', 'BUY NO', or 'NO TRADE'.

    spot_status is feeds.SpotHub.status(): the per-venue health of the
    consolidated spot tape. Passed in rather than imported so this stays a
    pure function of its inputs and remains testable with synthetic data.

    clock_status is KalshiClient.clock_status(): our measured offset from the
    exchange's clock. Every time-to-close decision is made on the local clock
    but the market expires on theirs, so a drifting clock is treated as a
    trading fault rather than absorbed silently.
    """
    cfg = config.settings

    # Checked before anything else because a wrong clock corrupts every other
    # check that follows: feed age, time-to-close, and hold duration are all
    # differences against the local clock. A test caught this reporting "feed is
    # stale" when the real fault was a clock minutes out of sync -- an accurate
    # veto for a misleading reason is still a debugging trap.
    clock = clock_status or {}
    if clock.get("severe"):
        return _no_trade(
            "Local clock is out of sync with the exchange by "
            f"{clock.get('skew_seconds')}s -- refusing to trade on unreliable timing"
        )

    if not trades:
        return _no_trade("Waiting for live BTC-USD trades")

    spot_age = time.time() - trades[-1][0]
    if spot_age > cfg.stale_feed_seconds:
        return _no_trade(f"Consolidated BTC feed is stale ({spot_age:.0f}s since last print)")

    # ---- cross-feed agreement -------------------------------------------
    # Fair value is a function of spot, so the moment the venues stop
    # agreeing about spot, fair value is fiction. Both of these are no-trade
    # reasons rather than errors: the feeds keep running, the bot just stops
    # acting on them.
    if spot_status:
        fresh_sources = spot_status.get("fresh_sources") or 0
        if fresh_sources < cfg.spot_min_sources:
            return _no_trade(
                f"Only {fresh_sources} fresh spot feed(s), "
                f"{cfg.spot_min_sources} required for cross-confirmation"
            )
        divergence = spot_status.get("divergence_bps")
        if divergence is not None and divergence > cfg.spot_divergence_bps:
            return _no_trade(
                f"Spot feeds disagree by {divergence:.1f}bps "
                f"(limit {cfg.spot_divergence_bps:.1f}bps) -- fair value is not "
                f"trustworthy while the venues are apart"
            )

    history = trades[-1][0] - trades[0][0]
    if history < cfg.min_history_seconds:
        return _no_trade(
            f"Building volatility history ({int(cfg.min_history_seconds - history)}s remaining)"
        )

    if not market or not market.get("ticker"):
        return _no_trade("No open BTC-15m market discovered")

    strike, strike_source = market_strike(market)
    if strike is None:
        return _no_trade(f"Cannot resolve strike for {market.get('ticker')}")

    remaining = seconds_to_close(market, clock.get("guard_adjustment_seconds") or 0.0)
    if remaining is None:
        return _no_trade("Market close time is unavailable")

    # Inside min_seconds_to_close normal scalping is over -- but the late
    # settlement window may still be open: near-certainties bought at a
    # discount and held to settlement (never exited). See config for the
    # guardrails; the extra late-only gates live further down, after the
    # side and fair value are known.
    late_window = remaining < cfg.min_seconds_to_close
    if late_window and not cfg.late_entry:
        return _no_trade(
            f"Too little runway to scalp ({remaining:.0f}s to close, "
            f"{cfg.min_seconds_to_close}s required)"
        )
    if late_window and remaining < cfg.late_min_seconds:
        return _no_trade(
            f"Late window shut ({remaining:.0f}s to close, orders need "
            f"{cfg.late_min_seconds}s to land)"
        )

    if remaining > cfg.max_seconds_to_close:
        return _no_trade(f"Outside the trading window ({remaining:.0f}s to close)")

    sigma = realized_vol_per_second(trades)
    if sigma is None:
        return _no_trade("Insufficient one-second samples to estimate volatility")

    efficiency = efficiency_ratio(trades)

    snapshot = book_snapshot(orderbook)
    yes_ask, no_ask = snapshot["yes_ask"], snapshot["no_ask"]

    if yes_ask is None or no_ask is None:
        return _no_trade("Waiting for a two-sided Kalshi book")

    spread = snapshot["yes_spread"]
    if spread is not None and spread > cfg.max_spread_cents:
        return _no_trade(f"Book spread too wide to scalp ({spread}c)")

    spot = trades[-1][1]
    gap_bps = None if strike <= 0 else (spot - strike) / strike * 10000.0
    z = standard_score(spot, strike, sigma, remaining)
    fair_yes = None if z is None else _normal_cdf(z)

    if fair_yes is None:
        return _no_trade("Fair value could not be computed")

    yes_edge = fair_yes * 100 - yes_ask
    no_edge = (1 - fair_yes) * 100 - no_ask

    if yes_edge >= no_edge:
        side, ask, edge, prob = "yes", yes_ask, yes_edge, fair_yes
    else:
        side, ask, edge, prob = "no", no_ask, no_edge, 1 - fair_yes

    exit_bid = bid_for_side(snapshot, side)
    entry_levels = asks_for_side(snapshot, side)
    exit_levels = bids_for_side(snapshot, side)

    # Exit liquidity, measured as depth we could actually get out through
    # rather than only the size resting at the very top of the bid. Sizing off
    # top-of-book alone understates a deep book and overstates a book whose
    # best bid is one lot in front of nothing.
    exit_size = depth_within(exit_levels, cfg.exit_depth_slippage_cents)
    exit_top_size = snapshot["yes_bid_size" if side == "yes" else "no_bid_size"]
    horizon = min(cfg.max_hold_seconds, max(0.0, remaining - cfg.settlement_guard_seconds))
    move = expected_move_cents(z, horizon, remaining)
    tiers = cfg.tiers()
    confidence = int(round(prob * 100))

    chart = indicators.analyze(trades)

    # The contract's own chart, for the side we would buy, plus how far spot
    # actually travelled over the same window. Together these separate "the
    # contract fell because BTC fell" from "the contract fell because someone
    # dumped into a thin book" -- the distinction the dip lanes are built on.
    tape = booktape.analyze(market.get("ticker"), side)
    spot_bps = spot_move_bps(trades, cfg.dip_lookback_seconds)

    # The stop this entry will carry: STOP_VOL_MULT times the expected
    # one-minute contract move, clamped. A 6c stop in a tape that routinely
    # wiggles 8c a minute is a coin-flip donation; the same 6c in a dead tape
    # is more room than the trade deserves.
    stop_cents = cfg.stop_cents
    move_1min = expected_move_cents(z, 60.0, remaining)
    if move_1min is not None:
        stop_cents = max(
            cfg.stop_min_cents,
            min(cfg.stop_max_cents, int(round(cfg.stop_vol_mult * move_1min))),
        )

    # A late entry risks its whole premium (no exit exists in the final
    # seconds), so the stop IS the ask: sizing divides the risk budget by it,
    # and the lot itself is settle-only.
    if late_window:
        stop_cents = ask

    count_estimate = max(1, cfg.max_contracts_per_trade)
    est_exit_price = exit_bid if exit_bid is not None else ask
    entry_is_maker = cfg.entry_style == "maker" and not late_window
    exit_is_maker = cfg.exit_style == "maker" and not late_window
    fee_cents_total = round_trip_fee_cents(
        count_estimate, ask, est_exit_price, entry_is_maker, exit_is_maker
    )
    fee_cents_per_contract = fee_cents_total / count_estimate

    # The bar this trade's edge must clear, published on the signal so the
    # sizing gate can re-check it against the AVERAGE fill price rather than
    # the top-of-book price the edge was originally measured at.
    min_required_edge = max(cfg.min_edge_cents, fee_cents_per_contract + cfg.fee_safety_margin_cents)

    # The price a maker entry would rest at: join our side's bid, improved by
    # up to maker_improve_cents for queue priority, but never locking or
    # crossing the book (post-only would reject that anyway).
    maker_entry_price = None
    if exit_bid is not None:
        maker_entry_price = max(1, min(exit_bid + cfg.maker_improve_cents, ask - 1))

    diagnostics = {
        "side": side,
        "ticker": market.get("ticker"),
        "price_cents": ask,
        "exit_bid_cents": exit_bid,
        "exit_bid_size": exit_size,
        "exit_bid_top_size": exit_top_size,
        # The ladders themselves, so sizing can walk them instead of assuming
        # the whole order fills at the best price.
        "entry_levels": [[int(price), int(size)] for price, size in entry_levels],
        "entry_depth_at_best": depth_within(entry_levels, 0),
        "entry_depth": depth_within(entry_levels, cfg.max_entry_slippage_cents),
        "min_required_edge_cents": round(min_required_edge, 3),
        "spread_cents": spread,
        "edge_cents": round(edge, 2),
        "fair_prob": round(prob, 4),
        "strike": strike,
        "strike_source": strike_source,
        "spot": spot,
        "gap_bps": None if gap_bps is None else round(gap_bps, 1),
        "sigma_per_second": round(sigma, 8),
        "seconds_to_close": int(remaining),
        "clock_skew_seconds": clock.get("skew_seconds"),
        "expected_move_cents": None if move is None else round(move, 2),
        "fee_cents_per_contract": round(fee_cents_per_contract, 2),
        "efficiency_ratio": None if efficiency is None else round(efficiency, 3),
        "maker_entry_price_cents": maker_entry_price,
        "trend": None if chart is None else chart["trend"],
        "ema_separation_bps": None if chart is None else chart["separation_bps"],
        "rsi": None if chart is None else chart["rsi"],
        "recent_high": None if chart is None else chart["recent_high"],
        "recent_low": None if chart is None else chart["recent_low"],
        "scalp_targets": {
            tier.name: min(99, ask + tier.cents) for tier in tiers
        },
        "stop_cents": stop_cents,
        "stop_price_cents": max(1, ask - stop_cents),
        "late_settlement": late_window,
        "favorite": False,
        "lane": None,
        "conviction": 0,
        "dip": False,
        "contract_chart": tape,
        "spot_move_bps": None if spot_bps is None else round(spot_bps, 1),
    }

    # ---- the favorite fallback ------------------------------------------
    # The second strategy of the split: when the scalp path declines this
    # market at any gate below, buy the strong side at 75-92c if the model
    # still prices it above the ask, the trend is not actively opposing, and
    # hold to fee-free settlement (maker entry, zero fees, full premium at
    # risk). Computed here, used as the fallback return of every scalp gate.
    favorite = None
    if (
        not late_window
        and cfg.favorite_entry
        and chart is not None
        and maker_entry_price is not None
        and cfg.fav_min_price_cents <= ask <= cfg.fav_max_price_cents
        and prob >= cfg.fav_min_fair_prob
        and edge >= cfg.fav_min_edge_cents
        and chart["trend"] != ("down" if side == "yes" else "up")
    ):
        favorite = {
            "action": f"BUY {side.upper()}",
            "confidence": confidence,
            "reason": (
                f"Favorite: fair {prob * 100:.1f}% vs {ask}c ask ({edge:.1f}c edge), "
                f"trend '{chart['trend']}' not opposing -- maker entry, held to "
                f"settlement for the fee-free 100c, full premium at risk"
            ),
        }
        favorite.update(diagnostics)
        favorite["favorite"] = True
        favorite["lane"] = "favorite"
        favorite["stop_cents"] = ask
        favorite["stop_price_cents"] = None
        # A settle-held favorite risks its whole premium, so conviction leans
        # on the model's certainty rather than on how far price might travel.
        favorite["conviction"] = _score(
            (3.0, (prob - cfg.fav_min_fair_prob) / max(0.01, 1.0 - cfg.fav_min_fair_prob)),
            (2.0, (edge - cfg.fav_min_edge_cents) / max(1.0, 3 * cfg.fav_min_edge_cents)),
            (1.0, 1.0 if chart["trend"] == ("up" if side == "yes" else "down") else 0.4),
        )

    # ---- the conviction hold ---------------------------------------------
    # The third lane, and the one that pays for the whole strategy when it
    # is right: early in the round, spot has already cleared the strike by a
    # real margin (the gap, in bps of price), the chart trend agrees, the
    # tape is actually travelling, and the model still prices the side
    # comfortably above a mid-price ask. Locked once and held to fee-free
    # settlement -- no ladder, no early exit, the full premium at risk.
    # Rides the favorite's executor path (maker entry, settle-only), so a
    # hold lock is sized by FAV_RISK_PCT like any other settle-held lot.
    # Tunables (env): HOLD_ENTRY=0 disables; HOLD_MIN/MAX_PRICE_CENTS bound
    # the ask; HOLD_MIN_FAIR_PROB, HOLD_MIN_EDGE_CENTS, HOLD_MIN_GAP_BPS set
    # the conviction bar; HOLD_MIN_SECONDS_TO_CLOSE keeps locks early in the
    # round, where the discount still exists.
    if favorite is None and not late_window and chart is not None:
        hold_on = _env_int("HOLD_ENTRY", 1)
        hold_min_price = _env_int("HOLD_MIN_PRICE_CENTS", 45)
        hold_max_price = _env_int("HOLD_MAX_PRICE_CENTS", 74)
        hold_min_prob = _env_float("HOLD_MIN_FAIR_PROB", 0.68)
        hold_min_edge = _env_float("HOLD_MIN_EDGE_CENTS", 4.0)
        hold_min_gap = _env_float("HOLD_MIN_GAP_BPS", 5.0)
        hold_min_remaining = _env_int("HOLD_MIN_SECONDS_TO_CLOSE", 600)
        gap_clears = gap_bps is not None and (
            gap_bps >= hold_min_gap if side == "yes" else gap_bps <= -hold_min_gap
        )
        if (
            hold_on
            and maker_entry_price is not None
            and hold_min_price <= ask <= hold_max_price
            and remaining >= hold_min_remaining
            and prob >= hold_min_prob
            and edge >= hold_min_edge
            and gap_clears
            and chart["trend"] == ("up" if side == "yes" else "down")
            and (efficiency is None or efficiency >= cfg.min_efficiency_ratio)
        ):
            favorite = {
                "action": f"BUY {side.upper()}",
                "confidence": confidence,
                "reason": (
                    f"Conviction hold: gap {gap_bps:+.1f}bps clears the "
                    f"{hold_min_gap:.1f}bps line with {remaining:.0f}s left, fair "
                    f"{prob * 100:.1f}% vs {ask}c ask ({edge:.1f}c edge), trend "
                    f"'{chart['trend']}' agreeing -- maker entry, held to "
                    f"settlement for the fee-free 100c, full premium at risk"
                ),
            }
            favorite.update(diagnostics)
            favorite["favorite"] = True
            favorite["hold_lock"] = True
            favorite["lane"] = "hold"
            favorite["stop_cents"] = ask
            favorite["stop_price_cents"] = None
            favorite["conviction"] = _score(
                (3.0, (abs(gap_bps) - hold_min_gap) / max(1.0, 3 * hold_min_gap)),
                (2.0, (edge - hold_min_edge) / max(1.0, 2 * hold_min_edge)),
                (2.0, (prob - hold_min_prob) / max(0.01, 1.0 - hold_min_prob)),
                (1.0, 0.5 if efficiency is None else efficiency),
            )

    # The price band and exit-liquidity gates protect round trips. A late
    # settlement snipe never exits -- it buys 90+c contracts on purpose --
    # so those two gates do not apply to it.
    if not late_window and not cfg.min_price_cents <= ask <= cfg.max_price_cents:
        return favorite or _no_trade(
            f"Ask {ask}c is outside the tradable price band", **diagnostics
        )

    if not late_window and exit_bid is None:
        return favorite or _no_trade(
            f"No resting bid on {side}: could enter but not scalp out", **diagnostics
        )

    if not late_window and exit_size < cfg.min_exit_liquidity:
        return favorite or _no_trade(
            f"Exit liquidity too thin ({exit_size} resting on the {side} bid, "
            f"{cfg.min_exit_liquidity} required)",
            **diagnostics,
        )

    # (min_required_edge was computed above, before diagnostics, so the signal
    # can carry it to the sizing gate.)

    # ---- the dip lanes ---------------------------------------------------
    # Evaluated before the momentum gates because those gates exist to refuse
    # exactly this shape of trade. Everything protecting a round trip -- price
    # band, exit liquidity, spread -- has already been enforced above and is
    # NOT bypassed here; only the trend/RSI/support/chop reads are, and only
    # because a dip buy is by definition against the immediate move.
    dip = None
    if (
        cfg.dip_entry
        and not late_window
        and tape is not None
        and chart is not None
        and remaining >= cfg.dip_min_seconds_to_close
    ):
        drop = tape["drop_cents"]
        decel_ok = bool(tape["decelerating"]) or not cfg.dip_require_decel
        imbalance = tape["imbalance"]
        imbalance_ok = imbalance is None or imbalance >= cfg.dip_min_imbalance
        dip_edge_floor = max(cfg.dip_min_edge_cents, fee_cents_per_contract + cfg.fee_safety_margin_cents)

        # Exhaustion on the SPOT read, in the direction that would have pushed
        # this side down. For a YES dip, spot sold off, so oversold RSI is the
        # exhaustion that makes a bounce plausible.
        exhausted = (
            chart["rsi"] <= cfg.rsi_oversold
            if side == "yes"
            else chart["rsi"] >= cfg.rsi_overbought
        )
        # Did spot justify the drop? For a YES dip, spot falling is
        # justification; for a NO dip, spot rising is.
        spot_justifies = spot_bps is not None and (
            spot_bps <= -cfg.dip_max_spot_move_bps
            if side == "yes"
            else spot_bps >= cfg.dip_max_spot_move_bps
        )

        dislocation = (
            cfg.dip_dislocation
            and drop >= cfg.dip_min_drop_cents
            and edge >= dip_edge_floor
            and decel_ok
            and imbalance_ok
            and spot_bps is not None
            and not spot_justifies
        )
        reversion = (
            cfg.dip_reversion
            and drop >= cfg.dip_reversion_min_drop_cents
            and edge >= dip_edge_floor
            and decel_ok
            and imbalance_ok
            and exhausted
        )

        if dislocation or reversion:
            kind = "dislocation" if dislocation else "reversion"
            # Velocities can legitimately be None on a sparse tape (and will
            # be, if DIP_REQUIRE_DECEL is off), so they are never formatted raw.
            fast_text = "n/a" if tape["velocity_fast"] is None else f"{tape['velocity_fast']:+.2f}"
            slow_text = "n/a" if tape["velocity_slow"] is None else f"{tape['velocity_slow']:+.2f}"
            spot_text = "n/a" if spot_bps is None else f"{spot_bps:+.1f}"
            imbalance_text = "n/a" if imbalance is None else f"{imbalance:+.2f}"
            # Dip stops are widened: the tape that just moved 9c in seconds is
            # not going to sit still while the recovery develops, and a normal
            # stop would be taken out by the noise of the dip itself.
            dip_stop = max(
                cfg.stop_min_cents,
                min(99, int(round(stop_cents * cfg.dip_stop_mult))),
            )
            conviction = _score(
                # How far beyond the minimum the dip actually went. A 6c dip
                # at a 6c threshold is a marginal setup; an 18c dip is not.
                (3.0, (drop - cfg.dip_min_drop_cents) / max(1.0, 2 * cfg.dip_min_drop_cents)),
                # Edge beyond the fee floor -- the part that is actually ours.
                (3.0, (edge - dip_edge_floor) / max(1.0, 2 * dip_edge_floor)),
                # Bid rebuilding underneath us.
                (1.5, 0.5 if imbalance is None else (imbalance + 1) / 2),
                # The fall has actually stopped, not just slowed.
                (1.5, 1.0 if tape["decelerating"] else 0.0),
                # Room for the recovery to happen.
                (1.0, remaining / max(1.0, cfg.max_seconds_to_close)),
                # A dislocation is a cleaner read than a fade, so the fade
                # variant is scored down rather than sized like the real thing.
                (1.0, 1.0 if dislocation else 0.35),
            )
            dip = {
                "action": f"BUY {side.upper()}",
                "confidence": confidence,
                "reason": (
                    f"Dip {kind}: {side.upper()} mid fell {drop:.1f}c from its "
                    f"{tape['lookback_seconds']}s high while spot moved "
                    f"{spot_text}bps, fall "
                    f"{'decelerating' if tape['decelerating'] else 'still active'} "
                    f"({fast_text}c/s vs {slow_text}c/s), "
                    f"book imbalance {imbalance_text}, "
                    f"fair {prob * 100:.1f}% vs {ask}c ask ({edge:.1f}c edge) -- "
                    f"buying the recovery with a {dip_stop}c stop, conviction {conviction}"
                ),
            }
            dip.update(diagnostics)
            dip["lane"] = f"dip_{kind}"
            dip["dip"] = True
            dip["conviction"] = conviction
            dip["stop_cents"] = dip_stop
            dip["stop_price_cents"] = max(1, ask - dip_stop)
            return dip

    if edge < min_required_edge:
        return favorite or _no_trade(
            f"Edge {edge:.1f}c below the fee-adjusted minimum {min_required_edge:.1f}c "
            f"(fees ~{fee_cents_per_contract:.1f}c/contract round-trip)",
            **diagnostics,
        )

    if not late_window and efficiency is not None and efficiency < cfg.min_efficiency_ratio:
        return favorite or _no_trade(
            f"Chop filter: efficiency {efficiency:.2f} below {cfg.min_efficiency_ratio:.2f} "
            f"floor -- the tape is wiggling, not trending",
            **diagnostics,
        )

    # ---- the chart read ------------------------------------------------
    # A BUY YES is a bet the tape keeps going up; a BUY NO that it keeps
    # going down. Neither is taken against, or without, an established trend.
    if chart is None:
        return _no_trade(
            "Chart warmup: not enough tape yet for the EMA/RSI read", **diagnostics
        )

    if late_window:
        # The settlement snipe replaces every momentum/mean-reversion gate:
        # RSI exhaustion, S/R stalls and expected-move all reason about where
        # price travels next, but this trade only needs price to stay put.
        # A flat trend is fine; only an actively opposing trend disqualifies.
        opposing = "down" if side == "yes" else "up"
        if chart["trend"] == opposing:
            return _no_trade(
                f"Late window: trend reads '{opposing}' against BUY {side.upper()} -- "
                f"a near-certainty with the tape moving against it is not one",
                **diagnostics,
            )
        if prob < cfg.late_min_fair_prob:
            return _no_trade(
                f"Late window: fair {prob * 100:.1f}% is below the "
                f"{cfg.late_min_fair_prob * 100:.0f}% certainty floor -- this close to "
                f"settlement only near-certainties are bought",
                **diagnostics,
            )
        if ask > cfg.late_max_price_cents:
            return _no_trade(
                f"Late window: ask {ask}c is above the {cfg.late_max_price_cents}c cap -- "
                f"no discount left worth the settlement risk",
                **diagnostics,
            )
        payload = {
            "action": f"BUY {side.upper()}",
            "confidence": confidence,
            "reason": (
                f"Settlement snipe: fair {prob * 100:.1f}% vs {ask}c ask with "
                f"{remaining:.0f}s left, trend '{chart['trend']}' not opposing -- taker in, "
                f"held to settlement for the fee-free 100c, full premium at risk"
            ),
        }
        payload.update(diagnostics)
        payload["lane"] = "late"
        payload["conviction"] = _score(
            (3.0, (prob - cfg.late_min_fair_prob) / max(0.01, 1.0 - cfg.late_min_fair_prob)),
            (2.0, (cfg.late_max_price_cents - ask) / max(1.0, cfg.late_max_price_cents - 50.0)),
        )
        return payload

    wanted_trend = "up" if side == "yes" else "down"
    if chart["trend"] != wanted_trend:
        return favorite or _no_trade(
            f"Trend gate: chart reads '{chart['trend']}' "
            f"(EMA gap {chart['separation_bps']:+.1f}bps), "
            f"BUY {side.upper()} needs '{wanted_trend}'",
            **diagnostics,
        )

    if side == "yes" and chart["rsi"] >= cfg.rsi_overbought:
        return favorite or _no_trade(
            f"RSI gate: {chart['rsi']:.0f} is overbought (>= {cfg.rsi_overbought:.0f}) -- "
            f"buying YES here is chasing an exhausted move",
            **diagnostics,
        )

    if side == "no" and chart["rsi"] <= cfg.rsi_oversold:
        return favorite or _no_trade(
            f"RSI gate: {chart['rsi']:.0f} is oversold (<= {cfg.rsi_oversold:.0f}) -- "
            f"buying NO here is chasing an exhausted move",
            **diagnostics,
        )

    # Support/resistance: do not buy YES with a recent high sitting just
    # overhead, or NO with a recent low just underfoot -- price tends to
    # stall at the level it last rejected. Spot AT or BEYOND the level is a
    # break, not a stall, and passes. The buffer scales with the tape's own
    # expected one-minute move so it adapts to fast and slow tapes alike.
    sr_buffer = cfg.sr_buffer_sigma * sigma * math.sqrt(60.0) * spot
    if side == "yes" and chart["recent_high"] is not None:
        headroom = chart["recent_high"] - spot
        if 0 < headroom < sr_buffer:
            return favorite or _no_trade(
                f"Resistance gate: spot ${spot:,.0f} is only ${headroom:,.0f} under the "
                f"{cfg.sr_lookback_seconds // 60}min high ${chart['recent_high']:,.0f} "
                f"(buffer ${sr_buffer:,.0f}) -- buying YES into a ceiling",
                **diagnostics,
            )
    if side == "no" and chart["recent_low"] is not None:
        footroom = spot - chart["recent_low"]
        if 0 < footroom < sr_buffer:
            return favorite or _no_trade(
                f"Support gate: spot ${spot:,.0f} is only ${footroom:,.0f} above the "
                f"{cfg.sr_lookback_seconds // 60}min low ${chart['recent_low']:,.0f} "
                f"(buffer ${sr_buffer:,.0f}) -- buying NO into a floor",
                **diagnostics,
            )

    if confidence < cfg.min_confidence:
        return favorite or _no_trade(
            f"Confidence {confidence}% below the {cfg.min_confidence}% floor", **diagnostics
        )

    if move is not None and move < tiers[0].cents:
        return favorite or _no_trade(
            f"Expected move {move:.1f}c cannot reach the {tiers[0].cents}c "
            f"small target within {int(horizon)}s",
            **diagnostics,
        )

    payload = {
        "action": f"BUY {side.upper()}",
        "confidence": confidence,
        "reason": (
            f"Fair {prob * 100:.1f}% vs {ask}c ask - {edge:.1f}c edge "
            f"({fee_cents_per_contract:.1f}c fees included), "
            f"trend {chart['trend']} (RSI {chart['rsi']:.0f}), "
            f"~{move:.1f}c expected travel, {stop_cents}c stop, "
            f"{'riding behind a trailing stop' if cfg.exit_profile == 'runner' else 'scalping to ' + '/'.join(str(tier.cents) for tier in tiers) + 'c'}"
        ),
    }
    payload.update(diagnostics)
    payload["lane"] = "trend"
    payload["conviction"] = _score(
        # Edge above the fee-adjusted floor: the part of the mispricing we keep.
        (3.0, (edge - min_required_edge) / max(1.0, 2 * min_required_edge)),
        # How directional the tape is. A momentum entry in a chopping tape is
        # the losing pattern this filter exists for, so it is weighted heavily.
        (2.5, 0.5 if efficiency is None else efficiency / max(0.01, 2 * cfg.min_efficiency_ratio)),
        # Trend conviction, as EMA separation beyond the deadzone.
        (1.5, (abs(chart["separation_bps"]) - cfg.trend_deadzone_bps) / max(1.0, 4 * cfg.trend_deadzone_bps)),
        # Room to travel relative to the stop being risked.
        (2.0, 0.5 if move is None else move / max(1.0, 2.0 * stop_cents)),
    )
    return payload
