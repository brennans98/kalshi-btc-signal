"""The exit ladder. This is the part that makes it a scalper.

The previous version entered on edge and held to settlement, which means it had
no concept of taking profit -- every trade resolved at 0 or 100 and the only
question was whether the entry was right. Scalping inverts that: the entry only
needs to be slightly right, and the exit does the work.

Three profit rungs, hit in order, each selling a share of the ORIGINAL size:

    small   +3c   sell 50%   bank something, take the trade off risk
    medium  +7c   sell 30%   the expected case when the read is correct
    large  +14c   sell 20%   the tail that pays for the stops

And three exits that are not profits:

    stop              price moves against us by more than the lot's stop
                      (volatility-scaled at entry; falls back to SCALP_STOP_CENTS)
    trailing stop     armed only after the first rung; gives back at most
                      trail_cents from the peak instead of round-tripping
    time / guard      max hold reached, or close enough to settlement that
                      holding converts a scalp into a binary bet

The settlement guard is the important one. A 15-minute contract held to expiry
is not a scalp, it is a coin flip with a spread paid on entry. The guard exits
while there is still a book to sell into.

One deliberate exception: a position that is DEEP in the money when the guard
or the hold clock would flatten it rides to settlement instead. Selling at 85c
keeps 85c minus taker fees; settlement keeps the full 100c fee-free. The live
example that motivated this: the day's one winner was flattened at 85c by the
guard on a market that settled in its favor -- holding would have paid +156c
instead of +66c. The ride is re-checked every tick; if the bid slips below the
floor the position flattens immediately with whatever book remains.

Lots are persisted, keyed by ticker and side, so a container restart does not
lose track of an open scalp or its ladder progress. Paper and live lots live in
separate files.
"""

import json
import math
import time

import config
import store


def _blank_tier_hits():
    return {tier.name: 0 for tier in config.settings.tiers()}


def _blank():
    return {
        "lots": {},
        "pending": {},
        "stats": {
            "round_trips": 0,
            "tier_hits": _blank_tier_hits(),
            "stop_exits": 0,
            "trail_exits": 0,
            "time_exits": 0,
            "chart_exits": 0,
            "settlement_exits": 0,
            # Net of fees -- the number that says whether this works.
            "realized_cents": 0,
            # Exact fractional accumulators; the integers above are rounded
            # views of these, so partial exits do not accumulate rounding drift.
            "realized_cents_exact": 0.0,
            "gross_cents_exact": 0.0,
            "fees_cents_exact": 0.0,
            # Gross and fees kept separately so the cost of trading is visible.
            "realized_gross_cents": 0,
            "fees_cents": 0,
            "wins": 0,
            "losses": 0,
        },
    }


def _path(mode):
    return config.settings.lots_path(mode)


def _round_half_up(value):
    """Round to a whole cent, half away from zero.

    Python's built-in round() is banker's rounding: round(12.5) is 12, not 13.
    For money that is both surprising and biased in a way that shows up in
    aggregates, so money rounding is explicit here.
    """
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def _modeled_fee_per_contract(price_cents):
    """Published Kalshi taker fee for one contract at this price, in cents.

    0.07 * P * (1 - P) with P in dollars. Used only when the exchange has not
    told us the real number -- dryrun, adopted positions, or a response missing
    the field. Deliberately the TAKER rate: assuming the cheaper maker rate
    would understate costs, and this number exists to stop costs being
    understated.
    """
    if price_cents is None:
        return 0.0
    p = max(0.0, min(1.0, float(price_cents) / 100.0))
    return 0.07 * p * (1 - p) * 100.0


def load(mode):
    """Current lot state. Served from memory; see store.py for why.

    Note this returns the LIVE cached object, not a copy: callers mutate it and
    then call save(). That is deliberate -- copying the whole lot book on every
    read of a sub-second loop is exactly the kind of overhead this change was
    made to remove -- but it means a caller must not mutate state it does not
    intend to persist.
    """
    state = store.read(_path(mode), _blank)

    blank = _blank()
    state.setdefault("lots", {})
    state.setdefault("pending", {})
    stats = blank["stats"]
    stats.update(state.get("stats") or {})
    tier_hits = dict(blank["stats"]["tier_hits"])
    tier_hits.update(stats.get("tier_hits") or {})
    stats["tier_hits"] = tier_hits
    state["stats"] = stats
    return state


def save_lazy(mode, state):
    """Update lot state in memory and let store coalesce the disk write.

    Only for per-tick bookkeeping. Entries, exits and settlements use save().
    """
    store.write_lazy(_path(mode), state, max_delay=1.0)


def save(mode, state):
    """Persist lot state atomically.

    A failure here is NOT swallowed. Losing the lot book means losing the entry
    basis of live positions, so the trader needs to know rather than find out
    at the next restart.
    """
    store.write(_path(mode), state)


def key(ticker, side):
    return f"{ticker}|{side}"


def open_lots(mode):
    state = load(mode)
    lots = []
    for lot_key, lot in (state.get("lots") or {}).items():
        if (lot.get("count_open") or 0) > 0:
            entry = dict(lot)
            entry["key"] = lot_key
            lots.append(entry)
    return lots


def get(mode, lot_key):
    lot = (load(mode).get("lots") or {}).get(lot_key)
    if not lot:
        return None
    lot = dict(lot)
    lot["key"] = lot_key
    return lot


def update_lot(mode, lot_key, fields):
    """Merge fields into a lot and persist. Returns the updated lot or None."""
    state = load(mode)
    lot = (state.get("lots") or {}).get(lot_key)
    if not lot:
        return None
    lot.update(fields)
    save(mode, state)
    lot = dict(lot)
    lot["key"] = lot_key
    return lot


# ---- pending maker entries -------------------------------------------------
# A resting entry order is not a position yet. It is tracked here, keyed like
# a lot (ticker|side), so a restart can keep polling it and the risk gate can
# refuse to stack a second attempt on the same market. Every pending order
# carries an expiration_time on Kalshi's side, so an abandoned record can
# never leave a live order on the book indefinitely.


def pending_all(mode):
    return {key_: dict(value) for key_, value in (load(mode).get("pending") or {}).items()}


def pending_put(mode, pending_key, data):
    state = load(mode)
    state.setdefault("pending", {})[pending_key] = data
    save(mode, state)


def pending_del(mode, pending_key):
    state = load(mode)
    if pending_key in (state.get("pending") or {}):
        state["pending"].pop(pending_key)
        save(mode, state)


def ladder_allocation(count):
    """Split a position across the profit tiers: [(tier, contracts), ...].

    Same arithmetic the taker plan() uses: cumulative percentages of the
    original size, floor-rounded, with the last tier absorbing the remainder
    so every contract is assigned. A position too small to split three ways
    goes whole onto the small-lot exit tier.
    """
    cfg = config.settings
    if count <= 0:
        return []
    if count < 3:
        return [(cfg.tier(cfg.small_lot_exit_tier), count)]

    tiers = cfg.tiers()
    result = []
    allocated = 0
    cumulative = 0

    for index, tier in enumerate(tiers):
        cumulative += tier.pct
        if index == len(tiers) - 1:
            share = count - allocated
        else:
            share = max(0, math.floor(count * cumulative / 100) - allocated)
        share = min(share, count - allocated)
        if share > 0:
            result.append((tier, share))
        allocated += share

    return result


def record_entry(
    mode,
    ticker,
    side,
    count,
    entry_price,
    close_epoch=None,
    stop_cents=None,
    settle_only=False,
    lane=None,
    conviction=None,
    entry_fee_cents=None,
):
    """Open (or add to) a lot. Averages the basis if a lot already exists.

    lane and conviction are the signal's provenance: which entry path fired
    and how strong it read (0-100). Stored per lot so the trade record can be
    grouped by lane afterwards.

    entry_fee_cents is the fee PER CONTRACT actually charged on this entry
    (Kalshi reports it as average_fee_paid), stored so realized P&L can be
    reported net. Reporting gross P&L on a market where a round trip costs
    roughly 3-4c per contract in fees makes a losing strategy look breakeven,
    which is the single most misleading number a trading bot can produce.

    stop_cents is the volatility-scaled stop the signal computed at entry
    time; it is stored on the lot so plan() honors the stop the trade was
    sized against, not whatever the config says later. Lots without one
    (adopted positions, pre-feature lots) fall back to SCALP_STOP_CENTS.

    settle_only marks a late settlement snipe: the lot is never exited --
    no stop, no ladder, no guard -- it rides to settlement by design, and
    its premium was sized as the risk. Adding to a settle-only lot keeps
    the flag; a normal add to a normal lot never sets it.

    Averaging into an existing lot moves the basis, which invalidates the
    ladder's prior progress: a tier marked "done" against the old basis may
    not actually have been reached against the new, higher basis. tiers_done
    and peak_gain are reset here so the ladder re-evaluates from the new
    basis rather than skipping rungs it has not actually earned.
    """
    state = load(mode)
    lot_key = key(ticker, side)
    now = time.time()
    existing = (state["lots"] or {}).get(lot_key)

    if existing and (existing.get("count_open") or 0) > 0:
        held = existing["count_open"]
        basis = (held * existing["entry_price"] + count * entry_price) / (held + count)
        existing["entry_price"] = round(basis, 2)
        existing["count_open"] = held + count
        existing["count_original"] = existing.get("count_original", held) + count
        existing["close_epoch"] = close_epoch or existing.get("close_epoch")
        if stop_cents:
            existing["stop_cents"] = int(stop_cents)
        if settle_only:
            existing["settle_only"] = True
        if lane:
            existing["lane"] = lane
        if conviction is not None:
            existing["conviction"] = int(conviction)
        existing["tiers_done"] = []
        existing["peak_gain"] = 0
        # Fees follow the same weighted average as the basis, so a lot built
        # from two fills at different fee rates carries the blended cost.
        if entry_fee_cents is not None:
            prior_fee = float(existing.get("entry_fee_cents") or 0.0)
            existing["entry_fee_cents"] = round(
                (held * prior_fee + count * float(entry_fee_cents)) / (held + count), 4
            )
        state["lots"][lot_key] = existing
    else:
        state["lots"][lot_key] = {
            "ticker": ticker,
            "side": side,
            "count_open": count,
            "count_original": count,
            "entry_price": entry_price,
            "opened_at": now,
            "close_epoch": close_epoch,
            "stop_cents": int(stop_cents) if stop_cents else None,
            "settle_only": bool(settle_only),
            # Which entry lane opened this lot, and how convinced it was. Kept
            # on the lot so post-hoc analysis can compare lanes honestly --
            # "the dip lane is working" needs to be checkable, not assumed.
            "lane": lane,
            "conviction": None if conviction is None else int(conviction),
            "tiers_done": [],
            "peak_gain": 0,
            "last_bid": entry_price,
            # Fee per contract paid to open. None means unknown (an adopted
            # position, or an exchange response without the field).
            "entry_fee_cents": None if entry_fee_cents is None else round(float(entry_fee_cents), 4),
        }

    save(mode, state)
    return get(mode, lot_key)


def mark(mode, lot_key, bid):
    """Record the current bid and high-water gain. Called before plan().

    This runs on every tick of every open lot, which on an event-driven loop is
    many times a second. It therefore persists LAZILY: the in-memory state is
    updated immediately (so the trail and stop always read the true peak), but
    the disk write is coalesced. Synchronously fsyncing the lot book several
    times a second from inside the exit path would put filesystem latency in
    front of every stop evaluation -- the exact opposite of the point.

    The fields written here are also the cheapest ones to lose: after a crash,
    reconcile() re-adopts the real position from the exchange, and the peak
    rebuilds from the live book within a tick.
    """
    state = load(mode)
    lot = (state.get("lots") or {}).get(lot_key)
    if not lot:
        return None

    gain = bid - lot["entry_price"]
    lot["last_bid"] = bid
    lot["peak_gain"] = max(lot.get("peak_gain", 0), gain)
    lot["marked_at"] = time.time()
    save_lazy(mode, state)

    lot = dict(lot)
    lot["key"] = lot_key
    return lot


def _riding_to_settlement(cfg, bid, gain):
    """True when this lot should be HELD through the guard to settlement.

    Deep in the money (bid at or above the ride floor) and in profit:
    the market itself is pricing a high probability of settling our way,
    and settlement pays the full 100c with zero fees. Re-evaluated every
    tick -- one bad print below the floor and the ride is over.
    """
    return (
        bool(cfg.settle_ride)
        and bid is not None
        and gain > 0
        and bid >= cfg.settle_ride_min_bid_cents
    )


def runner_trail_cents(cfg, peak, opposed=False):
    """How far below the peak the trailing stop sits, in cents.

    None until the lot is RUNNER_ARM_CENTS in profit -- before that the hard
    stop is the only protection, because a trail on an underwater lot is just
    a tighter stop wearing a costume.

    Once armed the trail is RUNNER_TRAIL_FRAC of the peak gain, clamped to
    [MIN, MAX]. The shape matters: at a 6c peak the trail is the 5c floor, so
    the lot has room to breathe; at a 30c peak it is the 12c cap, which is
    only 40% of the move rather than the 100% a fixed 3c rung would have
    surrendered by exiting at +3c. That is the whole difference between
    scalping cents and riding a move.

    A trend flip against a winning lot tightens the trail by RUNNER_FLIP_TIGHTEN
    instead of dumping the position -- protecting the gain without paying a
    spread every time the EMAs cross.
    """
    if peak < cfg.runner_arm_cents:
        return None
    trail = cfg.runner_trail_frac * peak
    trail = max(float(cfg.runner_trail_min_cents), min(float(cfg.runner_trail_max_cents), trail))
    if opposed:
        trail = max(1.0, trail * cfg.runner_flip_tighten)
    return trail


def _plan_runner(cfg, lot, bid, now, gain, peak, stop_cents, remaining, limit_price, flatten, trend):
    """The runner exit engine: hold winners, protect them with a trail.

    The hard stop has already been checked by the caller and is identical in
    both profiles. What differs is everything about a WINNING lot:

      * no fixed profit rungs, so a winner is never capped at +3c
      * a trend flip tightens the trail instead of closing the position
      * max-hold does not evict a lot that is in profit and still onside
      * deep ITM still rides to fee-free settlement

    The cost of this is real and worth stating: every winner gives back part
    of its peak, and trades that would have banked +3c under the ladder will
    sometimes come back and stop out instead. It pays for that with the
    winners the ladder used to cut off at the knees.
    """
    opposed = trend in ("up", "down") and (
        (lot["side"] == "yes" and trend == "down") or (lot["side"] == "no" and trend == "up")
    )
    riding = _riding_to_settlement(cfg, bid, gain)

    # A losing lot with the chart now against it: the reason for holding is
    # gone, so cut rather than donate the rest of the stop distance.
    if cfg.chart_exit and opposed and gain <= cfg.chart_exit_max_gain_cents:
        return flatten(
            "chart",
            f"Chart flip: trend turned '{trend}' against this {lot['side'].upper()} "
            f"lot at {gain:+.0f}c -- cutting now instead of riding to the "
            f"{stop_cents}c stop",
        )

    # The trailing stop. Checked before the time-based exits so a winner that
    # has rolled over exits on price, with a reason that says why.
    trail = runner_trail_cents(cfg, peak, opposed)
    if trail is not None and (peak - gain) >= trail:
        return flatten(
            "trail",
            f"Trailing exit: gave back {peak - gain:.1f}c from a {peak:.1f}c peak "
            f"(trail {trail:.1f}c"
            + (", tightened on trend flip" if opposed else "")
            + f", banking {gain:+.1f}c)",
        )

    if lot.get("close_epoch"):
        seconds_left = lot["close_epoch"] - now
        if seconds_left <= cfg.settlement_guard_seconds:
            if riding:
                return []
            return flatten(
                "time",
                f"Settlement guard: {int(seconds_left)}s to close, flattening "
                f"rather than holding a binary",
            )

    held_for = now - (lot.get("opened_at") or now)
    if held_for >= cfg.max_hold_seconds and not riding:
        # Max-hold exists to evict lots that are going nowhere. A lot in
        # profit with the trend still onside is not going nowhere, and
        # evicting it is exactly the behaviour that capped winners.
        working = cfg.runner_hold_winners and gain > 0 and not opposed
        if not working:
            return flatten(
                "time",
                f"Max hold reached ({int(held_for)}s) at {gain:+.0f}c",
            )

    # Optional single de-risk slice. Off by default: it is the cent-scalping
    # this profile exists to avoid, and is here only for whoever wants it.
    if cfg.runner_partial_pct > 0 and "runner_partial" not in (lot.get("tiers_done") or []):
        original = lot.get("count_original") or remaining
        if gain >= cfg.runner_partial_cents and original >= 2:
            count = min(remaining, max(1, math.floor(original * cfg.runner_partial_pct / 100)))
            if count > 0:
                return [
                    {
                        "tier": "runner_partial",
                        "kind": "profit",
                        "count": count,
                        "limit_price": limit_price,
                        "reason": (
                            f"De-risk slice: +{gain:.0f}c reached the "
                            f"{cfg.runner_partial_cents}c partial, selling "
                            f"{cfg.runner_partial_pct}% and letting the rest run"
                        ),
                    }
                ]

    return []


def plan(lot, bid, now=None, include_profit=True, trend=None):
    """Return the exit intents this lot warrants at the current bid.

    Each intent: {tier, count, limit_price, reason, kind}. kind is 'profit' for
    a ladder rung and 'stop' / 'trail' / 'chart' / 'time' otherwise.

    trend is the live chart read ('up' / 'down' / 'flat' / None). When the
    trend has flipped against a lot that is not in profit, the lot is cut
    immediately (kind 'chart') instead of riding the full stop distance down:
    the reason for holding no longer exists, so neither does the position.

    include_profit=False is the maker-exit mode: profit rungs already rest on
    the book as post-only asks, so plan() only watches for the exits that must
    cross immediately -- stop, chart flip, trail, settlement guard, max hold.
    """
    cfg = config.settings
    now = now or time.time()

    remaining = lot.get("count_open") or 0
    if remaining <= 0 or bid is None:
        return []

    # A settlement snipe is never exited: its premium was the risk, its exit
    # is settlement. Every stop/guard/ladder below is deliberately skipped.
    if lot.get("settle_only"):
        return []

    entry = lot["entry_price"]
    gain = bid - entry
    peak = max(lot.get("peak_gain", 0), gain)
    done = list(lot.get("tiers_done") or [])
    stop_cents = int(lot.get("stop_cents") or cfg.stop_cents)
    limit_price = max(1, min(99, int(round(bid)) - cfg.exit_slippage_cents))

    def flatten(kind, reason, tier=None):
        return [
            {
                "tier": tier or kind,
                "kind": kind,
                "count": remaining,
                "limit_price": limit_price,
                "reason": reason,
            }
        ]

    # --- exits that override the ladder, in priority order ---------------
    # The hard stop is profile-independent: it is the one exit that is never
    # negotiable, and it is checked before anything else in both engines.
    if gain <= -stop_cents:
        return flatten("stop", f"Stop hit: {gain:.0f}c against a {stop_cents}c stop")

    if cfg.exit_profile == "runner":
        return _plan_runner(
            cfg, lot, bid, now, gain, peak, stop_cents, remaining, limit_price, flatten, trend
        )

    if cfg.chart_exit and trend in ("up", "down"):
        opposed = (lot["side"] == "yes" and trend == "down") or (
            lot["side"] == "no" and trend == "up"
        )
        if opposed and gain <= cfg.chart_exit_max_gain_cents:
            return flatten(
                "chart",
                f"Chart flip: trend turned '{trend}' against this "
                f"{lot['side'].upper()} lot at {gain:+.0f}c -- cutting now "
                f"instead of riding to the {stop_cents}c stop",
            )

    riding = _riding_to_settlement(cfg, bid, gain)

    seconds_left = None
    if lot.get("close_epoch"):
        seconds_left = lot["close_epoch"] - now
        if seconds_left <= cfg.settlement_guard_seconds:
            if riding:
                # Deep ITM: hold through settlement for the full fee-free
                # 100c instead of selling the guard. Checked every tick;
                # a bid below the floor flattens on the next pass.
                return []
            return flatten(
                "time",
                f"Settlement guard: {int(seconds_left)}s to close, flattening "
                f"rather than holding a binary",
            )

    held_for = now - (lot.get("opened_at") or now)
    if held_for >= cfg.max_hold_seconds and not riding:
        return flatten("time", f"Max hold reached ({int(held_for)}s)")

    if done and cfg.trail_cents > 0 and (peak - gain) >= cfg.trail_cents:
        return flatten(
            "trail",
            f"Trailing exit: gave back {peak - gain:.0f}c from a {peak:.0f}c peak",
        )

    if not include_profit:
        return []

    # --- the profit ladder ----------------------------------------------
    tiers = cfg.tiers()
    original = lot.get("count_original") or remaining

    # A position too small to split three ways scalps whole at one rung,
    # rather than rounding every tier down to zero contracts and never taking
    # profit at all.
    if original < 3:
        target = cfg.tier(cfg.small_lot_exit_tier)
        if gain >= target.cents:
            return flatten(
                "profit",
                f"+{gain:.0f}c reached the {target.name} target ({target.cents}c) "
                f"on a {original}-contract lot",
                tier=target.name,
            )
        return []

    intents = []
    allocated = 0
    exited = original - remaining
    cumulative = 0

    for index, tier in enumerate(tiers):
        cumulative += tier.pct

        if tier.name in done:
            continue
        if gain < tier.cents:
            break

        available = remaining - allocated
        if available <= 0:
            break

        if index == len(tiers) - 1:
            count = available
        else:
            count = max(0, math.floor(original * cumulative / 100) - exited - allocated)

        count = min(count, available)
        if count <= 0:
            continue

        intents.append(
            {
                "tier": tier.name,
                "kind": "profit",
                "count": count,
                "limit_price": limit_price,
                "reason": f"+{gain:.0f}c reached the {tier.name} target ({tier.cents}c)",
            }
        )
        allocated += count

    return intents


def record_exit(mode, lot_key, tier, kind, count, exit_price, exit_fee_cents=None):
    """Reduce the lot and update statistics. Returns realized cents, NET OF FEES.

    Previously this returned the gross price difference. On this market a taker
    round trip costs roughly 3-4c per contract, and a scalp aims at a handful of
    cents, so gross P&L does not merely flatter the record -- it can invert its
    sign. A strategy averaging +2c gross per contract is losing money, and the
    old number reported it as winning.

    exit_fee_cents is the fee per contract on this exit leg. When the exchange
    reports it we use that; otherwise we fall back to the published taker
    formula, which is the conservative assumption (maker fills are cheaper).
    """
    state = load(mode)
    lot = (state.get("lots") or {}).get(lot_key)
    if not lot:
        return 0

    count = min(int(count), lot.get("count_open") or 0)
    if count <= 0:
        return 0

    gross = (exit_price - lot["entry_price"]) * count

    # Entry fee is apportioned across the contracts being closed, so a lot
    # exited in pieces pays its entry fee once in total rather than once per
    # partial exit.
    entry_fee_per_contract = lot.get("entry_fee_cents")
    if entry_fee_per_contract is None:
        entry_fee_per_contract = _modeled_fee_per_contract(lot["entry_price"])
    if exit_fee_cents is None:
        exit_fee_cents = _modeled_fee_per_contract(exit_price)

    fees = (float(entry_fee_per_contract) + float(exit_fee_cents)) * count
    realized_exact = gross - fees
    realized = _round_half_up(realized_exact)
    lot["count_open"] = lot["count_open"] - count

    if kind == "profit" and tier not in (lot.get("tiers_done") or []):
        lot.setdefault("tiers_done", []).append(tier)

    # Totals accumulate in EXACT fractional cents and are only rounded for
    # display. Rounding each partial exit before adding it loses up to half a
    # cent every time, and a lot that scales out in five pieces drifts from its
    # own true P&L -- a bot that cannot add up its own results correctly cannot
    # be evaluated at all.
    stats = state["stats"]
    stats["realized_cents_exact"] = float(stats.get("realized_cents_exact", 0.0)) + realized_exact
    stats["gross_cents_exact"] = float(stats.get("gross_cents_exact", 0.0)) + gross
    stats["fees_cents_exact"] = float(stats.get("fees_cents_exact", 0.0)) + fees

    stats["realized_cents"] = _round_half_up(stats["realized_cents_exact"])
    # Kept alongside the net figure so the cost of trading is visible rather
    # than merely subtracted. If gross is healthy and net is not, the strategy
    # is trading too often for the edge it has.
    stats["realized_gross_cents"] = _round_half_up(stats["gross_cents_exact"])
    stats["fees_cents"] = _round_half_up(stats["fees_cents_exact"])

    if kind == "profit":
        stats["tier_hits"][tier] = stats["tier_hits"].get(tier, 0) + 1
    elif kind == "stop":
        stats["stop_exits"] = stats.get("stop_exits", 0) + 1
    elif kind == "trail":
        stats["trail_exits"] = stats.get("trail_exits", 0) + 1
    elif kind == "time":
        stats["time_exits"] = stats.get("time_exits", 0) + 1
    elif kind == "chart":
        stats["chart_exits"] = stats.get("chart_exits", 0) + 1
    elif kind == "settlement":
        stats["settlement_exits"] = stats.get("settlement_exits", 0) + 1

    if lot["count_open"] <= 0:
        stats["round_trips"] = stats.get("round_trips", 0) + 1
        if realized >= 0:
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1
        lot["closed_at"] = time.time()

    save(mode, state)
    return realized


def forget_closed(mode, keep=20):
    """Drop old fully-closed lots so the file does not grow without bound."""
    state = load(mode)
    closed = [
        (lot_key, lot)
        for lot_key, lot in (state.get("lots") or {}).items()
        if (lot.get("count_open") or 0) <= 0
    ]
    closed.sort(key=lambda item: item[1].get("closed_at") or 0)

    for lot_key, _ in closed[:-keep] if len(closed) > keep else []:
        state["lots"].pop(lot_key, None)

    save(mode, state)


def stats(mode):
    state = load(mode)
    payload = dict(state.get("stats") or {})
    payload["open_lots"] = len(open_lots(mode))
    return payload


def view(mode):
    """Dashboard-shaped summary of live scalps and the ladder's record."""
    cfg = config.settings
    lots = []

    for lot in open_lots(mode):
        bid = lot.get("last_bid")
        gain = None if bid is None else round(bid - lot["entry_price"], 1)
        lots.append(
            {
                "ticker": lot["ticker"],
                "side": lot["side"],
                "contracts": lot["count_open"],
                "of_original": lot.get("count_original"),
                "entry_cents": lot["entry_price"],
                "bid_cents": bid,
                "open_gain_cents": gain,
                "peak_gain_cents": round(lot.get("peak_gain", 0), 1),
                "stop_cents": None if lot.get("settle_only") else (lot.get("stop_cents") or cfg.stop_cents),
                "settle_only": bool(lot.get("settle_only")),
                "riding_to_settlement": bool(lot.get("settle_only"))
                or _riding_to_settlement(
                    cfg, bid, None if bid is None else bid - lot["entry_price"]
                ),
                "tiers_hit": lot.get("tiers_done") or [],
                "lane": lot.get("lane"),
                "conviction": lot.get("conviction"),
                # Where the trailing stop currently sits, so the dashboard
                # shows the live exit level rather than a static rung.
                "trail_cents": (
                    None
                    if cfg.exit_profile != "runner" or lot.get("settle_only")
                    else runner_trail_cents(cfg, max(lot.get("peak_gain", 0), gain or 0))
                ),
                "held_seconds": int(time.time() - (lot.get("opened_at") or time.time())),
                "seconds_to_close": (
                    int(lot["close_epoch"] - time.time()) if lot.get("close_epoch") else None
                ),
            }
        )

    pending = []
    for pending_key, entry in pending_all(mode).items():
        pending.append(
            {
                "key": pending_key,
                "ticker": entry.get("ticker"),
                "side": entry.get("side"),
                "count": entry.get("count"),
                "price_cents": entry.get("price_cents"),
                "filled": entry.get("filled_recorded") or 0,
                "expires_in_seconds": (
                    max(0, int(entry["expire_epoch"] - time.time()))
                    if entry.get("expire_epoch")
                    else None
                ),
            }
        )

    return {
        "ladder": [
            {"name": tier.name, "target_cents": tier.cents, "exit_pct": tier.pct}
            for tier in cfg.tiers()
        ],
        "stop_cents": cfg.stop_cents,
        "trail_cents": cfg.trail_cents,
        "entry_style": cfg.entry_style,
        "exit_style": cfg.exit_style,
        "open": lots,
        "pending_entries": pending,
        "stats": stats(mode),
    }