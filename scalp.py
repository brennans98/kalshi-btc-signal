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

    stop              price moves against us by more than the stop
    trailing stop     armed only after the first rung; gives back at most
                      trail_cents from the peak instead of round-tripping
    time / guard      max hold reached, or close enough to settlement that
                      holding converts a scalp into a binary bet

The settlement guard is the important one. A 15-minute contract held to expiry
is not a scalp, it is a coin flip with a spread paid on entry. The guard exits
while there is still a book to sell into.

Lots are persisted, keyed by ticker and side, so a container restart does not
lose track of an open scalp or its ladder progress. Paper and live lots live in
separate files.
"""

import json
import math
import time

import config


def _blank_tier_hits():
    return {tier.name: 0 for tier in config.settings.tiers()}


def _blank():
    return {
        "lots": {},
        "stats": {
            "round_trips": 0,
            "tier_hits": _blank_tier_hits(),
            "stop_exits": 0,
            "trail_exits": 0,
            "time_exits": 0,
            "realized_cents": 0,
            "wins": 0,
            "losses": 0,
        },
    }


def _path(mode):
    return config.settings.lots_path(mode)


def load(mode):
    try:
        state = json.loads(_path(mode).read_text())
    except Exception:
        return _blank()

    blank = _blank()
    state.setdefault("lots", {})
    stats = blank["stats"]
    stats.update(state.get("stats") or {})
    tier_hits = dict(blank["stats"]["tier_hits"])
    tier_hits.update(stats.get("tier_hits") or {})
    stats["tier_hits"] = tier_hits
    state["stats"] = stats
    return state


def save(mode, state):
    try:
        path = _path(mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


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


def record_entry(mode, ticker, side, count, entry_price, close_epoch=None):
    """Open (or add to) a lot. Averages the basis if a lot already exists.

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
        existing["tiers_done"] = []
        existing["peak_gain"] = 0
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
            "tiers_done": [],
            "peak_gain": 0,
            "last_bid": entry_price,
        }

    save(mode, state)
    return get(mode, lot_key)


def mark(mode, lot_key, bid):
    """Record the current bid and high-water gain. Called before plan()."""
    state = load(mode)
    lot = (state.get("lots") or {}).get(lot_key)
    if not lot:
        return None

    gain = bid - lot["entry_price"]
    lot["last_bid"] = bid
    lot["peak_gain"] = max(lot.get("peak_gain", 0), gain)
    lot["marked_at"] = time.time()
    save(mode, state)

    lot = dict(lot)
    lot["key"] = lot_key
    return lot


def plan(lot, bid, now=None):
    """Return the exit intents this lot warrants at the current bid.

    Each intent: {tier, count, limit_price, reason, kind}. kind is 'profit' for
    a ladder rung and 'stop' / 'trail' / 'time' otherwise.
    """
    cfg = config.settings
    now = now or time.time()

    remaining = lot.get("count_open") or 0
    if remaining <= 0 or bid is None:
        return []

    entry = lot["entry_price"]
    gain = bid - entry
    peak = max(lot.get("peak_gain", 0), gain)
    done = list(lot.get("tiers_done") or [])
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
    if gain <= -cfg.stop_cents:
        return flatten("stop", f"Stop hit: {gain:.0f}c against a {cfg.stop_cents}c stop")

    seconds_left = None
    if lot.get("close_epoch"):
        seconds_left = lot["close_epoch"] - now
        if seconds_left <= cfg.settlement_guard_seconds:
            return flatten(
                "time",
                f"Settlement guard: {int(seconds_left)}s to close, flattening "
                f"rather than holding a binary",
            )

    held_for = now - (lot.get("opened_at") or now)
    if held_for >= cfg.max_hold_seconds:
        return flatten("time", f"Max hold reached ({int(held_for)}s)")

    if done and cfg.trail_cents > 0 and (peak - gain) >= cfg.trail_cents:
        return flatten(
            "trail",
            f"Trailing exit: gave back {peak - gain:.0f}c from a {peak:.0f}c peak",
        )

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


def record_exit(mode, lot_key, tier, kind, count, exit_price):
    """Reduce the lot and update statistics. Returns realized cents."""
    state = load(mode)
    lot = (state.get("lots") or {}).get(lot_key)
    if not lot:
        return 0

    count = min(int(count), lot.get("count_open") or 0)
    if count <= 0:
        return 0

    realized = int(round((exit_price - lot["entry_price"]) * count))
    lot["count_open"] = lot["count_open"] - count

    if kind == "profit" and tier not in (lot.get("tiers_done") or []):
        lot.setdefault("tiers_done", []).append(tier)

    stats = state["stats"]
    stats["realized_cents"] = stats.get("realized_cents", 0) + realized

    if kind == "profit":
        stats["tier_hits"][tier] = stats["tier_hits"].get(tier, 0) + 1
    elif kind == "stop":
        stats["stop_exits"] = stats.get("stop_exits", 0) + 1
    elif kind == "trail":
        stats["trail_exits"] = stats.get("trail_exits", 0) + 1
    elif kind == "time":
        stats["time_exits"] = stats.get("time_exits", 0) + 1

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
                "tiers_hit": lot.get("tiers_done") or [],
                "held_seconds": int(time.time() - (lot.get("opened_at") or time.time())),
                "seconds_to_close": (
                    int(lot["close_epoch"] - time.time()) if lot.get("close_epoch") else None
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
        "open": lots,
        "stats": stats(mode),
    }