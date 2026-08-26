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
"""

import math
import os
import time
from datetime import datetime

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
    close_time = (market or {}).get("close_time")
    if not close_time:
        return None

    try:
        parsed = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    return parsed.timestamp()


def seconds_to_close(market):
    epoch = close_epoch(market)
    return None if epoch is None else epoch - time.time()


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

    def top(levels):
        best_price = None
        best_size = 0

        for level in levels or []:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            try:
                price, size = int(level[0]), int(level[1])
            except (TypeError, ValueError):
                continue
            if size > 0 and (best_price is None or price > best_price):
                best_price, best_size = price, size

        return best_price, best_size

    yes_bid, yes_bid_size = top(book.get("yes"))
    no_bid, no_bid_size = top(book.get("no"))

    return {
        "yes_bid": yes_bid,
        "yes_bid_size": yes_bid_size,
        "no_bid": no_bid,
        "no_bid_size": no_bid_size,
        "yes_ask": None if no_bid is None else 100 - no_bid,
        "no_ask": None if yes_bid is None else 100 - yes_bid,
        "yes_spread": None if (yes_bid is None or no_bid is None) else (100 - no_bid) - yes_bid,
    }


def bid_for_side(snapshot, side):
    """The price we could sell this side into right now."""
    return (snapshot or {}).get("yes_bid" if side == "yes" else "no_bid")


def evaluate(trades, market, orderbook):
    """Return a signal dict. action is 'BUY YES', 'BUY NO', or 'NO TRADE'."""
    cfg = config.settings

    if not trades:
        return _no_trade("Waiting for live BTC-USD trades")

    spot_age = time.time() - trades[-1][0]
    if spot_age > cfg.stale_feed_seconds:
        return _no_trade(f"Coinbase BTC feed is stale ({spot_age:.0f}s since last trade)")

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

    remaining = seconds_to_close(market)
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
    exit_size = snapshot["yes_bid_size" if side == "yes" else "no_bid_size"]
    horizon = min(cfg.max_hold_seconds, max(0.0, remaining - cfg.settlement_guard_seconds))
    move = expected_move_cents(z, horizon, remaining)
    tiers = cfg.tiers()
    confidence = int(round(prob * 100))

    chart = indicators.analyze(trades)

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
        "spread_cents": spread,
        "edge_cents": round(edge, 2),
        "fair_prob": round(prob, 4),
        "strike": strike,
        "strike_source": strike_source,
        "spot": spot,
        "gap_bps": None if gap_bps is None else round(gap_bps, 1),
        "sigma_per_second": round(sigma, 8),
        "seconds_to_close": int(remaining),
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
        favorite["stop_cents"] = ask
        favorite["stop_price_cents"] = None

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
            favorite["stop_cents"] = ask
            favorite["stop_price_cents"] = None

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

    min_required_edge = max(cfg.min_edge_cents, fee_cents_per_contract + cfg.fee_safety_margin_cents)

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
            f"~{move:.1f}c expected travel, {stop_cents}c stop, scalping to "
            f"{'/'.join(str(tier.cents) for tier in tiers)}c"
        ),
    }
    payload.update(diagnostics)
    return payload
