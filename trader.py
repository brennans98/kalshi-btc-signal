"""The autonomous scalping loop.

Three modes, set by TRADING_MODE:
    off     signal only, no decision path exercised (default)
    dryrun  full decision path including simulated round trips; places nothing
    live    places real orders

Order of operations on every tick is deliberate: EXITS, then a loss-limit
check, then entries. An open scalp is capital already at risk on a contract
with minutes of life left; a new entry is optional. If the loop is slow, or an
error interrupts it, the work that must not be skipped is the ladder and the
stops.

Exits are also exempt from the risk gate. Cooldowns, daily trade caps and even a
latched halt block new entries only. Refusing to sell an open position is not a
safety measure -- it is the opposite, and a halt that stranded a position would
be worse than no halt at all.

The daily loss limit is checked every tick via risk.check_halt(), independent
of whether a BUY signal exists. Evaluating it only inside the entry path would
mean a quiet stretch with no new signals -- while exits keep realizing losses
via stops -- could sit past the limit indefinitely without ever halting.

dryrun is a real simulation, not a no-op: it opens paper lots, marks them
against the live book every tick, and walks them through the same ladder,
stops and guards, including the same cooldown and daily trade cap accounting
as live. What it produces is the thing worth reviewing before going live -- a
record of complete round trips, with the reason for every exit.
Paper lots are stored in a separate file from live ones.

Positions are reconciled against Kalshi rather than tracked locally, so a
restart or a missed fill cannot leave the loop believing it is flat when it is
not. A position found with no matching lot is adopted with a basis taken from
recent fills, so the ladder can still manage it.
"""

import asyncio
import time
import uuid

import config
import policy
import risk
import scalp
import store
from kalshi_client import KalshiApiError, KalshiAuthError

status = {
    "running": False,
    "last_tick_at": None,
    "last_decision": None,
    "last_entry": None,
    "last_exit": None,
    "last_error": None,
    "balance_cents": None,
    "open_positions": [],
    "reconciled_at": None,
    "adopted": [],
}

_book_cache = {}
_BOOK_TTL = 1.5

# ---- the live book, and why this matters more than anything else here ----
# app.py already maintains an authenticated WebSocket orderbook feed, applying
# every orderbook_delta Kalshi pushes into an in-memory book. This module,
# however, used to fetch the book over REST with a 1.5-second cache for every
# exit decision -- so stops, trails and guards were evaluated against a price
# that could be a second and a half stale, on a market that reprices several
# times a second. On a co-located server that is a self-inflicted wound: the
# network round trip was never the bottleneck, the cache was.
#
# book_provider is installed by app.py at startup and returns
# (book, published_at) from that live feed. REST remains the fallback for when
# the socket is reconnecting, which is the only time it should ever be hit.
_book_provider = None
_book_source = {"last": None, "ws_hits": 0, "rest_hits": 0}

# Set by app.py on every book publish so the loop can wake on a price change
# instead of sleeping through it. Assigned lazily because the event must be
# created on the running loop.
_book_event = None


def set_book_provider(provider):
    """Install the in-memory WebSocket book reader (called once, from app.py)."""
    global _book_provider
    _book_provider = provider


def book_event():
    """The asyncio.Event the loop waits on, created on first use."""
    global _book_event
    if _book_event is None:
        _book_event = asyncio.Event()
    return _book_event


def notify_book():
    """Wake the trading loop: the book just changed.

    Called from app.py's WebSocket publish path. This is the mechanism that
    turns a fixed 2-second poll into an event-driven reaction -- exits are
    evaluated on the tick that moved the price, not up to two seconds later.
    """
    try:
        book_event().set()
    except RuntimeError:
        # No running loop yet (import-time or shutdown); nothing to wake.
        pass


def mode():
    value = config.settings.trading_mode
    return value if value in config.VALID_MODES else "off"


def _order_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def snapshot(client=None):
    """Everything the dashboard needs about the trader."""
    active = mode()
    state = risk.load_state()

    payload = dict(status)
    payload["mode"] = active
    payload["limits"] = risk.limits()
    payload["halted"] = bool(state.get("halted"))
    payload["halt_reason"] = state.get("halt_reason")
    payload["halt_is_manual"] = bool(state.get("halt_is_manual"))
    payload["trades_today"] = state.get("trades_today", 0)
    payload["cost_today_cents"] = state.get("cost_today_cents", 0)
    payload["day_start_balance_cents"] = state.get("day_start_balance_cents")
    payload["drawdown_cents"] = risk.drawdown_cents(_equity_cents(), state=state)
    payload["scalp"] = scalp.view(active if active != "off" else "dryrun")
    # Persistence health. If this is unhealthy, state is not reaching disk and a
    # restart will forget open positions and any latched halt -- which matters
    # more than anything else on this payload.
    payload["storage"] = store.status()
    # Clock health. Time-to-close drives the settlement guard, the late-entry
    # window and every exit deadline, so a drifting clock is a trading fault --
    # visible here rather than discovered through an unexited expiry.
    if client is not None:
        payload["clock"] = client.clock_status()
    return payload


async def _book(client, ticker):
    """The freshest book available for this ticker.

    Order of preference:
      1. The in-memory WebSocket book, if it is younger than
         BOOK_MAX_AGE_SECONDS. Free, and as current as the exchange.
      2. A REST fetch with a short TTL, for when the socket is down or
         reconnecting.

    Every exit decision reads through here, so the WS path is what makes
    stops and trails act on the price that actually exists right now.
    """
    if _book_provider is not None:
        try:
            result = _book_provider(ticker)
        except Exception:
            result = None
        if result:
            book, published_at = result
            age = time.time() - (published_at or 0)
            if book and age <= config.settings.book_max_age_seconds:
                _book_source["last"] = "ws"
                _book_source["ws_hits"] += 1
                return book

    cached = _book_cache.get(ticker)
    if cached and time.time() - cached[0] < _BOOK_TTL:
        return cached[1]

    book = await client.get_orderbook(ticker)
    _book_cache[ticker] = (time.time(), book)
    _book_source["last"] = "rest"
    _book_source["rest_hits"] += 1
    return book


async def _basis_from_fills(client, ticker, side, count):
    """Average fill price for a position we do not have a lot for.

    Falls back to the current bid. A wrong basis makes the ladder measure gains
    from the wrong place, so this is worth a request.
    """
    try:
        payload = await client.get_fills(ticker=ticker, limit=50)
    except (KalshiApiError, KalshiAuthError):
        payload = {}

    total_count = 0
    total_cost = 0

    for fill in payload.get("fills", []):
        if fill.get("side") != side or fill.get("action") != "buy":
            continue
        quantity = int(fill.get("count") or 0)
        price = fill.get("yes_price" if side == "yes" else "no_price")
        if quantity <= 0 or price is None:
            continue
        total_count += quantity
        total_cost += quantity * int(price)
        if total_count >= count:
            break

    if total_count > 0:
        return round(total_cost / total_count, 2), "fills"

    try:
        snap = policy.book_snapshot(await _book(client, ticker))
        bid = policy.bid_for_side(snap, side)
        if bid:
            return float(bid), "current bid"
    except (KalshiApiError, KalshiAuthError):
        pass

    return None, "unavailable"


# ---- maker entries ---------------------------------------------------------
# A maker entry rests a post-only bid instead of crossing the spread. Nothing
# is recorded as a position until Kalshi reports fills; the resting order
# lives in scalp's "pending" store so a restart keeps polling it, and every
# order carries an expiration_time so nothing can be forgotten on the book.


def _record_pending_fills(pending_key, pending, filled_total):
    """Turn newly observed fills on a resting entry into lot contracts."""
    recorded = int(pending.get("filled_recorded") or 0)
    new_fill = filled_total - recorded
    if new_fill <= 0:
        return 0

    price = int(pending["price_cents"])
    scalp.record_entry(
        "live",
        pending["ticker"],
        pending["side"],
        new_fill,
        price,
        pending.get("close_epoch"),
        stop_cents=pending.get("stop_cents"),
        settle_only=bool(pending.get("settle_only")),
        lane=pending.get("lane"),
        conviction=pending.get("conviction"),
        # A resting order is a maker fill by construction, and fills at exactly
        # its limit price -- so the price is exact and the fee is the maker rate
        # (zero for this series today).
        entry_fee_cents=policy.maker_fee_cents(1, price),
    )
    pending["filled_recorded"] = filled_total

    if not pending.get("trade_counted"):
        # One resting order is one trade for pacing purposes (cooldown, daily
        # cap), counted when its first contract lands.
        risk.record_trade(pending["ticker"], filled_total, filled_total * price)
        pending["trade_counted"] = True
    else:
        # Later partial fills on the same resting order are not new trades, but
        # they ARE new money deployed. Previously only the contracts filled at
        # the moment the order first traded were added to the day's cost, so a
        # resting order that filled in pieces silently understated the day's
        # deployment for the rest of the session.
        risk.record_cost(new_fill * price)

    scalp.pending_put("live", pending_key, pending)

    risk.log_decision(
        {
            "event": "entry",
            "mode": "live",
            "outcome": "opened",
            "signal": {
                "ticker": pending["ticker"],
                "side": pending["side"],
                "price_cents": price,
                "reason": "Resting maker entry filled (zero fee)",
            },
            "sizing": {"count": new_fill},
            "filled_total": filled_total,
            "order_count": pending.get("count"),
        }
    )

    status["last_entry"] = {
        "ticker": pending["ticker"],
        "side": pending["side"],
        "count": filled_total,
        "price_cents": price,
        "maker": True,
        "simulated": False,
        "at": int(time.time()),
    }
    status["last_decision"] = {
        "outcome": "opened",
        "reason": (
            f"Maker entry filled {filled_total}/{pending.get('count')} "
            f"{pending['side']} @ {price}c on {pending['ticker']}"
        ),
    }
    return new_fill


def _fair_cents_for(signal, pending):
    """The model's CURRENT fair value, in cents, for a pending order's side.

    The signal's fair_prob is quoted for whichever side the signal chose this
    tick; flip it when the pending order sits on the other side. Returns None
    when the signal is for a different market or carries no fair value.
    """
    if not signal or signal.get("ticker") != pending.get("ticker"):
        return None
    prob = signal.get("fair_prob")
    if prob is None:
        return None
    fair = prob * 100.0
    if signal.get("side") != pending.get("side"):
        fair = 100.0 - fair
    return fair


async def _poll_pending_entries(client, signal=None):
    """Advance resting maker entries: record fills, retire finished orders.

    Also the adverse-selection guard: a resting bid whose fair value has
    moved ENTRY_CANCEL_ADVERSE_CENTS against it since placement is pulled
    rather than left to be filled by the very move that invalidated it.
    Several live stop-outs fired 3-6 seconds after the maker fill -- the bid
    was only hit because price was crashing through it.
    """
    if mode() != "live":
        return

    for pending_key, pending in scalp.pending_all("live").items():
        order_id = pending.get("order_id")
        expired = bool(
            pending.get("expire_epoch")
            and time.time() > float(pending["expire_epoch"]) + 10
        )

        try:
            payload = await client.get_order(order_id)
        except KalshiAuthError:
            raise
        except KalshiApiError as error:
            status["last_error"] = f"Entry order poll failed: {error}"
            if expired:
                # Kalshi already expired the order server-side; if the status
                # endpoint will not answer, there is nothing left to track.
                scalp.pending_del("live", pending_key)
            continue

        state_name, filled, remaining = client.order_progress(payload)
        _record_pending_fills(pending_key, pending, filled)

        still_resting = (
            state_name not in ("executed", "canceled") and remaining > 0 and not expired
        )
        if still_resting:
            fair_now = _fair_cents_for(signal, pending)
            fair_at_place = pending.get("fair_cents")
            slip = (
                None
                if fair_now is None or fair_at_place is None
                else fair_at_place - fair_now
            )
            if slip is not None and slip >= config.settings.entry_cancel_adverse_cents:
                await _cancel_pending_entry(
                    client,
                    pending_key,
                    pending,
                    f"Adverse selection guard: fair value moved {slip:.0f}c against "
                    f"the resting {pending.get('side')} bid since placement",
                )
                status["last_decision"] = {
                    "outcome": "rest_canceled",
                    "reason": (
                        f"Pulled resting entry on {pending.get('ticker')}: fair value "
                        f"moved {slip:.0f}c against it while it waited"
                    ),
                }
            continue

        if state_name in ("executed", "canceled") or remaining <= 0 or expired:
            scalp.pending_del("live", pending_key)
            if filled <= 0:
                risk.log_decision(
                    {
                        "event": "entry",
                        "mode": "live",
                        "outcome": "rest_expired",
                        "ticker": pending.get("ticker"),
                        "side": pending.get("side"),
                        "price_cents": pending.get("price_cents"),
                        "count": pending.get("count"),
                        "reason": "Resting entry expired unfilled -- no fee, no trade counted",
                    }
                )
                status["last_decision"] = {
                    "outcome": "rest_expired",
                    "reason": (
                        f"Resting entry on {pending.get('ticker')} expired "
                        f"unfilled after {config.settings.entry_rest_seconds}s"
                    ),
                }


async def _cancel_pending_entry(client, pending_key, pending, reason):
    """Cancel a resting entry order, absorbing any last-instant fills."""
    order_id = pending.get("order_id")

    try:
        await client.cancel_order(order_id)
    except KalshiAuthError:
        raise
    except KalshiApiError:
        pass  # already executed, expired or canceled -- the poll below settles it

    try:
        payload = await client.get_order(order_id)
        _, filled, _ = client.order_progress(payload)
        _record_pending_fills(pending_key, pending, filled)
    except KalshiAuthError:
        raise
    except KalshiApiError:
        pass

    scalp.pending_del("live", pending_key)
    risk.log_decision(
        {
            "event": "entry",
            "mode": "live",
            "outcome": "rest_canceled",
            "ticker": pending.get("ticker"),
            "side": pending.get("side"),
            "reason": reason,
        }
    )


# Markets Kalshi is not accepting orders on yet (each new 15-minute market
# rejects orders with market_not_found for its first minute or two even
# though discovery already lists it). ticker -> epoch until which entry
# attempts are deferred, so one rejection quiets the retry loop instead of
# spamming hundreds of doomed orders.
_unready_markets = {}


# Last settlement lookup per ticker, so a stale lot is checked on a human
# timescale rather than on every sub-second tick.
_settle_checks = {}
_SETTLE_CHECK_SECONDS = 5.0


def _defer_market(ticker, seconds=20):
    now = time.time()
    for key in [k for k, until in _unready_markets.items() if until <= now]:
        _unready_markets.pop(key, None)
    _unready_markets[ticker] = now + seconds


def _market_warming(ticker, record, error):
    """Handle Kalshi's market_not_found on a brand-new market: back off quietly."""
    _defer_market(ticker)
    record["outcome"] = "market_warming"
    record["error"] = str(error)
    record["risk_reason"] = "Kalshi is not accepting orders on this market yet"
    status["last_decision"] = {
        "outcome": "market_warming",
        "reason": f"{ticker} not accepting orders yet; retrying shortly",
    }
    risk.log_decision(record)


async def _settle_stale_lot(client, active, lot):
    """Resolve a lot whose market closed: credit the settlement and free the slot.

    A lot that rides to settlement (or that the guard could not flatten) never
    gets an exit fill, so without this it lingers as a ghost position forever,
    eating an open-position slot and hiding settlement wins from the stats.

    Throttled: settlement is published once, and the loop now runs several
    times a second. Without the throttle a single unsettled stale lot would
    issue a market fetch on every tick, spending rate limit on a value that
    changes at most once.
    """
    ticker = lot["ticker"]
    now = time.time()
    if now - float(_settle_checks.get(ticker) or 0) < _SETTLE_CHECK_SECONDS:
        return
    _settle_checks[ticker] = now

    try:
        market = await client.get_market(ticker)
    except KalshiAuthError:
        raise
    except KalshiApiError as error:
        status["last_error"] = f"Settlement check failed for {ticker}: {error}"
        return

    result = (market or {}).get("result") or ""
    if result not in ("yes", "no"):
        return  # Closed but not officially settled yet; try again next tick.

    won = result == lot["side"]
    count = lot.get("count_open") or 0
    # Settlement is fee-free on Kalshi: a contract held to expiry pays 100c or
    # 0c with no exit fee. Passing 0 explicitly stops the fallback fee model
    # from inventing an exit cost that was never charged.
    realized = scalp.record_exit(
        active,
        lot["key"],
        "settlement",
        "settlement",
        count,
        100 if won else 0,
        exit_fee_cents=0.0,
    )
    outcome = "settled_win" if won else "settled_loss"
    reason = (
        f"Market settled {result.upper()}: "
        + ("collected the full 100c" if won else "expired worthless")
        + f" ({realized:+d}c on {count})"
    )
    risk.log_decision(
        {
            "event": "exit",
            "mode": active,
            "ticker": ticker,
            "side": lot["side"],
            "tier": "settlement",
            "kind": "settlement",
            "count": count,
            "reason": reason,
            "outcome": outcome,
        }
    )
    status["last_decision"] = {"outcome": outcome, "reason": f"{ticker}: {reason}"}


async def _place_maker_entry(client, signal, market, record, sizing):
    """Rest a post-only bid instead of crossing the spread.

    No lot is opened here -- fills are observed by _poll_pending_entries as
    they land. A resting order that expires unfilled cost nothing: no fee, no
    cooldown, no daily trade count. That asymmetry is the entire trade-off of
    maker entries: zero fees and a better price, paid for with the risk of
    simply not getting filled.
    """
    cfg = config.settings
    ticker, side = signal["ticker"], signal["side"]
    count = sizing["count"]
    price = signal.get("maker_entry_price_cents")

    if not price or price <= 0:
        record["outcome"] = "blocked"
        record["risk_reason"] = "No maker entry price available (no bid on our side)"
        status["last_decision"] = {"outcome": "blocked", "reason": record["risk_reason"]}
        risk.log_decision(record)
        return

    now = time.time()
    close = policy.close_epoch(market)
    expire = int(now + cfg.entry_rest_seconds)
    if close:
        expire = min(expire, int(close - cfg.settlement_guard_seconds - 5))
    if expire <= now + 2:
        record["outcome"] = "blocked"
        record["risk_reason"] = "Too close to the settlement guard to rest an entry"
        status["last_decision"] = {"outcome": "blocked", "reason": record["risk_reason"]}
        risk.log_decision(record)
        return

    try:
        response = await client.place_resting(
            ticker=ticker,
            action="buy",
            side=side,
            count=count,
            price_cents=int(price),
            client_order_id=_order_id("entry"),
            expire_epoch=expire,
        )
        record["response"] = response
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected during entry: {error}")
        record["outcome"] = "auth_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        status["last_decision"] = {"outcome": "auth_error", "reason": str(error)}
        risk.log_decision(record)
        raise
    except KalshiApiError as error:
        if "market_not_found" in str(error):
            _market_warming(ticker, record, error)
            return
        record["outcome"] = "order_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        status["last_decision"] = {"outcome": "order_error", "reason": str(error)}
        risk.log_decision(record)
        return

    order = (response or {}).get("order") or response or {}
    order_id = order.get("order_id")
    state_name, filled, _ = client.order_progress(response)

    if not order_id or (state_name == "canceled" and filled <= 0):
        # post_only killed the order because it would have crossed a book
        # that moved between the signal and the placement.
        record["outcome"] = "rest_rejected"
        status["last_decision"] = {
            "outcome": "rest_rejected",
            "reason": f"Post-only entry at {price}c would have crossed; book moved",
        }
        risk.log_decision(record)
        return

    fair_prob = signal.get("fair_prob")
    pending = {
        "order_id": order_id,
        "ticker": ticker,
        "side": side,
        "count": count,
        "price_cents": int(price),
        "close_epoch": close,
        "placed_at": int(now),
        "expire_epoch": expire,
        "filled_recorded": 0,
        "trade_counted": False,
        "stop_cents": signal.get("stop_cents"),
        "settle_only": bool(signal.get("favorite") or signal.get("late_settlement")),
        # Carried onto the lot when the resting order fills, so the exit
        # engine and the dashboard know which lane opened it.
        "lane": signal.get("lane"),
        "conviction": signal.get("conviction"),
        "fair_cents": None if fair_prob is None else round(fair_prob * 100.0, 1),
    }
    scalp.pending_put("live", scalp.key(ticker, side), pending)

    if filled > 0:
        _record_pending_fills(scalp.key(ticker, side), pending, filled)

    record["outcome"] = "resting"
    record["maker_price_cents"] = int(price)
    record["expire_epoch"] = expire
    risk.log_decision(record)
    status["last_decision"] = {
        "outcome": "resting",
        "reason": (
            f"Resting {count} {side} @ {price}c on {ticker} "
            f"(maker, expires in {max(1, int(expire - now))}s)"
        ),
    }


# ---- maker exits -----------------------------------------------------------
# The profit ladder rests on the book as post-only reduce-only asks instead of
# being fired as taker IOC sells when the bid touches a target. Two effects:
# the fee on every profitable exit drops to zero, and the fill happens at our
# price the moment anyone crosses -- capturing the spread instead of paying
# it. Stops, trails and time exits still cross immediately as takers: when
# the trade is wrong, certainty of exit is worth the fee.


async def _ensure_exit_ladder(client, lot, bid):
    """Rest the profit ladder for a live lot that does not have one yet.

    The allocation is computed once and stored on the lot, so partial rung
    fills do not reshuffle contracts between tiers on later ticks. Rungs sit
    at entry + target, but never at or below the current bid (a post-only ask
    at the bid would cross and be rejected); if the bid has already run past
    a target, the rung rests one cent above the bid and keeps the extra.
    """
    plan_ = lot.get("ladder_plan")
    if plan_ is None:
        plan_ = [
            [tier.name, tier.cents, count]
            for tier, count in scalp.ladder_allocation(lot.get("count_open") or 0)
        ]
        lot = (
            scalp.update_lot("live", lot["key"], {"ladder_plan": plan_, "exit_orders": {}})
            or lot
        )

    exit_orders = dict(lot.get("exit_orders") or {})

    expire = None
    if lot.get("close_epoch"):
        expire = int(lot["close_epoch"] - config.settings.settlement_guard_seconds)
        if expire <= time.time() + 3:
            return lot  # the settlement guard takes it from here

    entry_price = int(round(lot.get("entry_price") or 0))
    changed = False

    for tier_name, tier_cents, count in plan_:
        if count <= 0 or tier_name in exit_orders:
            continue

        price = min(99, entry_price + int(tier_cents))
        if bid is not None:
            price = min(99, max(price, int(bid) + 1))

        try:
            response = await client.place_resting(
                ticker=lot["ticker"],
                action="sell",
                side=lot["side"],
                count=count,
                price_cents=price,
                client_order_id=_order_id("ladder"),
                expire_epoch=expire,
                reduce_only=True,
            )
        except KalshiAuthError:
            raise
        except KalshiApiError as error:
            status["last_error"] = f"Ladder rung failed on {lot['ticker']}: {error}"
            continue

        order = (response or {}).get("order") or response or {}
        order_id = order.get("order_id")
        if not order_id:
            continue

        exit_orders[tier_name] = {
            "order_id": order_id,
            "price_cents": price,
            "count": count,
            "filled_recorded": 0,
            "done": False,
        }
        changed = True

    if changed:
        lot = scalp.update_lot("live", lot["key"], {"exit_orders": exit_orders}) or lot
        risk.log_decision(
            {
                "event": "ladder",
                "mode": "live",
                "ticker": lot["ticker"],
                "side": lot["side"],
                "entry_price_cents": lot.get("entry_price"),
                "rungs": {
                    name: {"price_cents": rung["price_cents"], "count": rung["count"]}
                    for name, rung in exit_orders.items()
                },
            }
        )

    return lot


async def _poll_exit_orders(client, lot):
    """Absorb fills on the resting ladder into realized P&L."""
    exit_orders = dict(lot.get("exit_orders") or {})
    changed = False

    for tier_name, rung in exit_orders.items():
        if rung.get("done") or not rung.get("order_id"):
            continue

        try:
            payload = await client.get_order(rung["order_id"])
        except KalshiAuthError:
            raise
        except KalshiApiError as error:
            status["last_error"] = f"Exit order poll failed: {error}"
            continue

        state_name, filled, _ = client.order_progress(payload)
        new_fill = filled - int(rung.get("filled_recorded") or 0)

        if new_fill > 0:
            # A resting rung fills AT its limit price, so that price is the
            # true fill. The fee is the maker rate for this series.
            realized = scalp.record_exit(
                "live",
                lot["key"],
                tier_name,
                "profit",
                new_fill,
                rung["price_cents"],
                exit_fee_cents=policy.maker_fee_cents(1, rung["price_cents"]),
            )
            rung["filled_recorded"] = filled
            changed = True

            risk.log_decision(
                {
                    "event": "exit",
                    "mode": "live",
                    "ticker": lot["ticker"],
                    "side": lot["side"],
                    "tier": tier_name,
                    "kind": "profit",
                    "count": new_fill,
                    "limit_price_cents": rung["price_cents"],
                    "entry_price_cents": lot.get("entry_price"),
                    "outcome": "exited",
                    "realized_cents": realized,
                    "reason": "Resting ladder fill (maker, zero fee)",
                }
            )
            status["last_exit"] = {
                "ticker": lot["ticker"],
                "tier": tier_name,
                "kind": "profit",
                "count": new_fill,
                "price_cents": rung["price_cents"],
                "realized_cents": realized,
                "reason": "Resting ladder fill (maker, zero fee)",
                "at": int(time.time()),
            }

        if state_name in ("executed", "canceled"):
            rung["done"] = True
            changed = True

    if changed:
        scalp.update_lot("live", lot["key"], {"exit_orders": exit_orders})
        return scalp.get("live", lot["key"]) or lot

    return lot


async def _cancel_exit_orders(client, lot):
    """Pull every resting rung off the book before an urgent taker exit.

    Each rung gets a final status check after the cancel, because a fill can
    land in the instant between our decision and the cancel reaching the
    book. Selling those contracts again would open a short.
    """
    exit_orders = dict(lot.get("exit_orders") or {})

    for tier_name, rung in exit_orders.items():
        if rung.get("done") or not rung.get("order_id"):
            continue

        try:
            await client.cancel_order(rung["order_id"])
        except KalshiAuthError:
            raise
        except KalshiApiError:
            pass  # already executed or expired -- the poll below settles it

        try:
            payload = await client.get_order(rung["order_id"])
            _, filled, _ = client.order_progress(payload)
            new_fill = filled - int(rung.get("filled_recorded") or 0)
            if new_fill > 0:
                realized = scalp.record_exit(
                    "live",
                    lot["key"],
                    tier_name,
                    "profit",
                    new_fill,
                    rung["price_cents"],
                    exit_fee_cents=policy.maker_fee_cents(1, rung["price_cents"]),
                )
                rung["filled_recorded"] = filled
                risk.log_decision(
                    {
                        "event": "exit",
                        "mode": "live",
                        "ticker": lot["ticker"],
                        "side": lot["side"],
                        "tier": tier_name,
                        "kind": "profit",
                        "count": new_fill,
                        "limit_price_cents": rung["price_cents"],
                        "entry_price_cents": lot.get("entry_price"),
                        "outcome": "exited",
                        "realized_cents": realized,
                        "reason": "Ladder fill caught during cancel",
                    }
                )
        except KalshiAuthError:
            raise
        except KalshiApiError:
            pass

        rung["done"] = True

    scalp.update_lot("live", lot["key"], {"exit_orders": exit_orders})
    return scalp.get("live", lot["key"]) or lot


async def reconcile(client):
    """Refresh balance and positions from Kalshi, adopting anything untracked."""
    active = mode()
    balance = await client.get_balance()
    positions = await client.get_positions()

    status["balance_cents"] = balance.get("balance")
    risk.note_balance(balance.get("balance"))

    open_positions = []
    for entry in positions.get("market_positions", []):
        # V2 responses may express positions as fixed-point strings ("6.00").
        quantity = int(float(entry.get("position") or 0))
        if quantity == 0:
            continue

        ticker = entry.get("ticker")
        side = "yes" if quantity > 0 else "no"
        count = abs(quantity)
        open_positions.append({"ticker": ticker, "side": side, "count": count})

        # Live mode only: paper lots must never be reconciled against a real
        # account, or a dry run would start managing actual positions.
        if active != "live":
            continue

        lot = scalp.get("live", scalp.key(ticker, side))
        if lot and (lot.get("count_open") or 0) > 0:
            continue

        basis, source = await _basis_from_fills(client, ticker, side, count)
        if basis is None:
            continue

        market = {}
        try:
            market = await client.get_market(ticker)
        except (KalshiApiError, KalshiAuthError):
            pass

        scalp.record_entry(
            "live", ticker, side, count, basis, policy.close_epoch(market)
        )
        note = f"Adopted {count} {side} on {ticker} at {basis}c (basis from {source})"
        status["adopted"] = ([note] + status.get("adopted", []))[:10]
        risk.log_decision(
            {
                "event": "adopt",
                "ticker": ticker,
                "side": side,
                "count": count,
                "basis_cents": basis,
                "basis_source": source,
            }
        )

    status["open_positions"] = open_positions
    status["reconciled_at"] = int(time.time())
    return open_positions


async def run_exits(client, signal=None):
    """Walk every open lot through the ladder, stops, chart flips and guards.

    signal carries the live chart read; its trend feeds the chart-flip exit.
    The trend is read off the BTC tape, not any one market, so it applies to
    every KXBTC15M lot regardless of ticker.
    """
    active = mode()
    if active == "off":
        return

    trend = (signal or {}).get("trend")
    # A resting maker ladder and a trailing stop are incompatible ideas. The
    # ladder commits to selling at fixed prices decided when the lot opened,
    # which is exactly the behaviour that capped the winners: a lot that is
    # about to run 30c has already sold half of itself at +3c. The runner
    # profile takes the other side of that trade -- it holds, and exits on a
    # trail that moves with the peak -- so it must exit as a taker and must
    # not leave resting sells on the book.
    maker_exits = (
        active == "live"
        and config.settings.exit_style == "maker"
        and config.settings.exit_profile == "ladder"
    )
    pending = scalp.pending_all("live") if active == "live" else {}

    for lot in scalp.open_lots(active):
        ticker, side = lot["ticker"], lot["side"]

        # A market that closed over two minutes ago can only be settled: no
        # book, no exits, just the official result. Resolve and move on.
        close = lot.get("close_epoch")
        if close and time.time() - close > 120:
            await _settle_stale_lot(client, active, lot)
            continue

        try:
            snap = policy.book_snapshot(await _book(client, ticker))
        except KalshiAuthError:
            raise
        except KalshiApiError as error:
            status["last_error"] = f"Book unavailable for {ticker}: {error}"
            continue

        bid = policy.bid_for_side(snap, side)

        if maker_exits:
            lot = await _poll_exit_orders(client, lot)
            if (lot.get("count_open") or 0) <= 0:
                continue
            # While the entry order is still resting the lot may keep growing;
            # the ladder is placed once the entry order is settled.
            if lot["key"] not in pending:
                lot = await _ensure_exit_ladder(client, lot, bid)

        if bid is None:
            # No bid means no taker exit is possible this tick. The settlement
            # guard will still fire once the clock runs down, but there is
            # nothing to sell into right now.
            continue

        marked = scalp.mark(active, lot["key"], bid)
        if not marked:
            continue

        intents = scalp.plan(marked, bid, include_profit=not maker_exits, trend=trend)
        if not intents:
            continue

        if maker_exits:
            # Urgent exit (stop/trail/time). Pull our own resting orders first
            # so the taker sell cannot race them, absorb any last-instant
            # fills, then re-plan against what is actually still open.
            if marked["key"] in pending:
                await _cancel_pending_entry(
                    client,
                    marked["key"],
                    pending.pop(marked["key"]),
                    f"Urgent exit fired on {ticker}",
                )
            marked = await _cancel_exit_orders(client, marked)
            if (marked.get("count_open") or 0) <= 0:
                continue
            intents = scalp.plan(marked, bid, include_profit=False, trend=trend)

        for intent in intents:
            await _execute_exit(client, active, marked, intent)


async def _execute_exit(client, active, lot, intent):
    ticker, side = lot["ticker"], lot["side"]
    count, price = intent["count"], intent["limit_price"]

    # What we assume until the exchange tells us otherwise. In dryrun these
    # assumptions ARE the record, so they are deliberately pessimistic: fill at
    # the limit (never better) and pay the taker fee. A simulation that assumes
    # the good version of both is how a bot looks profitable on paper.
    exit_price = float(price)
    exit_fee = policy.taker_fee_cents(1, price)

    record = {
        "event": "exit",
        "mode": active,
        "ticker": ticker,
        "side": side,
        "tier": intent["tier"],
        "kind": intent["kind"],
        "count": count,
        "limit_price_cents": price,
        "entry_price_cents": lot["entry_price"],
        "reason": intent["reason"],
        "held_seconds": int(time.time() - (lot.get("opened_at") or time.time())),
    }

    if active == "live":
        try:
            response = await client.sell(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=price,
                client_order_id=_order_id("exit"),
            )
            record["response"] = response
        except KalshiAuthError as error:
            risk.halt(f"Authentication rejected while exiting {ticker}: {error}")
            record["outcome"] = "auth_error"
            record["error"] = str(error)
            status["last_error"] = str(error)
            risk.log_decision(record)
            raise
        except KalshiApiError as error:
            # Leave the lot open and retry next tick. Marking it exited here
            # would lose track of contracts we still hold.
            record["outcome"] = "exit_error"
            record["error"] = str(error)
            status["last_error"] = str(error)
            risk.log_decision(record)
            return

        # IOC can fill partially. Record only what actually sold; the rest of
        # the lot stays open and is retried next tick. Zero fill means the bid
        # vanished -- leave the whole lot open.
        filled = client.filled_count(response)
        if filled <= 0:
            record["outcome"] = "exit_unfilled"
            record["filled"] = 0
            risk.log_decision(record)
            return
        count = min(count, filled)
        record["filled"] = filled

        # A sell IOC at limit P fills against bids at or above P, so the
        # average fill is at least as good as the limit. Recording the limit
        # understated every profitable exit. Use the exchange's VWAP and its
        # real fee.
        actual_exit = client.average_fill_price_cents(response)
        actual_exit_fee = client.average_fee_paid_cents(response)
        if actual_exit is not None:
            record["actual_exit_price_cents"] = round(actual_exit, 3)
            exit_price = actual_exit
        if actual_exit_fee is not None:
            record["actual_exit_fee_cents"] = round(actual_exit_fee, 3)
            exit_fee = actual_exit_fee

    realized = scalp.record_exit(
        active, lot["key"], intent["tier"], intent["kind"], count, exit_price,
        exit_fee_cents=exit_fee,
    )

    if intent["kind"] == "stop":
        # Start the post-stop cooldown in dryrun too, so the simulation paces
        # entries the same way live trading would.
        risk.note_stop()

    record["outcome"] = "exited" if active == "live" else "simulated"
    record["realized_cents"] = realized
    risk.log_decision(record)

    status["last_exit"] = {
        "ticker": ticker,
        "tier": intent["tier"],
        "kind": intent["kind"],
        "count": count,
        "price_cents": price,
        "realized_cents": realized,
        "reason": intent["reason"],
        "at": int(time.time()),
    }


async def run_entry(client, signal, market):
    """Consider opening a new scalp."""
    active = mode()

    if not (signal.get("action") or "").startswith("BUY"):
        status["last_decision"] = {"outcome": "no_signal", "reason": signal.get("reason")}
        return

    # A market Kalshi just rejected with market_not_found is still warming
    # up; retrying every tick only piles up doomed orders in the log.
    if _unready_markets.get(signal.get("ticker"), 0) > time.time():
        status["last_decision"] = {
            "outcome": "market_warming",
            "reason": f"{signal.get('ticker')} not accepting orders yet; retrying shortly",
        }
        return

    open_lots = scalp.open_lots(active)
    pending = scalp.pending_all("live") if active == "live" else {}
    open_tickers = [lot["ticker"] for lot in open_lots] + [
        entry.get("ticker") for entry in pending.values()
    ]

    # A resting entry order counts as an open position for the risk gate: it
    # can become one at any moment, and stacking a second attempt on the same
    # market would double the intended size.
    approved, reason, sizing = risk.check(
        signal, status.get("balance_cents"), len(open_lots) + len(pending), open_tickers
    )

    record = {
        "event": "entry",
        "mode": active,
        "approved": approved,
        "risk_reason": reason,
        "signal": signal,
        "sizing": sizing,
    }

    if not approved:
        record["outcome"] = "blocked"
        status["last_decision"] = {"outcome": "blocked", "reason": reason}
        risk.log_decision(record)
        return

    ticker = signal["ticker"]
    side = signal["side"]
    count = sizing["count"]

    # The price the edge was measured at (best offer) and the price the order
    # must carry to actually complete. They differ whenever the size we want is
    # deeper than the best level. Sending the best offer as the limit on a
    # fill-or-kill order for more contracts than rest there guarantees a kill:
    # the exchange cannot fill the whole size at that price, so it fills none.
    quoted_price = int(signal["price_cents"])
    price = int(sizing.get("limit_price_cents") or quoted_price)
    # The modeled average, used as the basis only until the exchange tells us
    # the real one.
    expected_basis = float(sizing.get("avg_price_cents") or price)
    # Overwritten with the exchange's real VWAP once the order comes back. In
    # dryrun the modeled sweep average IS the basis -- which is the point of
    # modeling it, so a dry run's P&L reflects slippage instead of pretending
    # every order fills at the top of book.
    basis_cents = expected_basis
    # Pessimistic until the exchange reports the real figure, for the same
    # reason as the exit: a dry run must not assume the cheap version.
    entry_fee_cents = policy.taker_fee_cents(1, expected_basis)

    # A late settlement snipe always enters as a taker: a resting post-only
    # bid has no time to fill this close to settlement, and an unfilled
    # "near-certainty" is just a missed one. Everything else honors
    # entry_style as configured.
    if (
        active == "live"
        and config.settings.entry_style == "maker"
        and not signal.get("late_settlement")
    ):
        await _place_maker_entry(client, signal, market, record, sizing)
        return

    if active == "live":
        try:
            response = await client.create_order(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=price,
                client_order_id=_order_id("entry"),
            )
            record["response"] = response
        except KalshiAuthError as error:
            risk.halt(f"Authentication rejected during entry: {error}")
            record["outcome"] = "auth_error"
            record["error"] = str(error)
            status["last_error"] = str(error)
            status["last_decision"] = {"outcome": "auth_error", "reason": str(error)}
            risk.log_decision(record)
            raise
        except KalshiApiError as error:
            if "market_not_found" in str(error):
                _market_warming(ticker, record, error)
                return
            record["outcome"] = "order_error"
            record["error"] = str(error)
            status["last_error"] = str(error)
            status["last_decision"] = {"outcome": "order_error", "reason": str(error)}
            risk.log_decision(record)
            return

        # V2 returns 201 even when a fill-or-kill order is killed unfilled.
        # An unfilled entry is not a trade: no lot, no cooldown, no daily count.
        filled = client.filled_count(response)

        if filled <= 0:
            record["outcome"] = "unfilled"
            record["filled"] = 0
            status["last_decision"] = {
                "outcome": "unfilled",
                "reason": (
                    f"FOK entry killed unfilled (0/{count} "
                    f"{side} @ {price}c on {ticker})"
                ),
            }
            risk.log_decision(record)
            return

        if filled < count:
            # Fill-or-kill should make this impossible, so reaching it means an
            # assumption about the exchange is wrong. The old code returned here
            # without recording anything -- which would leave a REAL position
            # open on Kalshi that the bot does not know it owns: no stop, no
            # trail, no exit, discovered only at the next reconcile. Record what
            # actually filled and manage it.
            record["partial_fill"] = {"requested": count, "filled": filled}
            status["last_error"] = (
                f"Entry partially filled ({filled}/{count}) despite fill-or-kill; "
                f"managing the {filled} contracts that filled"
            )
            count = filled

        # The exchange's own volume-weighted fill price, which is the only
        # correct basis for the stop, the trail and P&L. Fall back to the
        # modeled sweep average only if the field is absent.
        actual_basis = client.average_fill_price_cents(response)
        actual_fee = client.average_fee_paid_cents(response)
        if actual_basis is not None:
            basis_cents = actual_basis
        if actual_fee is not None:
            entry_fee_cents = actual_fee
        record["fill"] = {
            "filled": filled,
            "quoted_price_cents": quoted_price,
            "limit_price_cents": price,
            "expected_basis_cents": round(expected_basis, 3),
            "actual_basis_cents": None if actual_basis is None else round(actual_basis, 3),
            "actual_fee_cents_per_contract": None if actual_fee is None else round(actual_fee, 3),
            "slippage_vs_quote_cents": round(basis_cents - quoted_price, 3),
        }

    # Recorded in both live and dryrun. Cooldown and the daily trade cap are
    # pacing limits, not capital controls -- they need to be exercised in
    # dryrun too, or a dry run can fire trades back-to-back at a rate live
    # trading never could, making the simulation misleading.
    # Cost recorded from the basis actually paid, not the quoted price. The
    # daily loss limit and pacing counters are only as accurate as this number.
    actual_cost_cents = int(round(count * basis_cents))
    risk.record_trade(ticker, count, actual_cost_cents)

    # The lot's entry price is the basis actually paid, rounded to the cent.
    # Every exit decision -- stop distance, trail arming, the runner's peak --
    # measures from this number, so recording the quoted price instead of the
    # filled price biased every one of them in the same direction.
    scalp.record_entry(
        active,
        ticker,
        side,
        count,
        int(round(basis_cents)),
        policy.close_epoch(market),
        stop_cents=signal.get("stop_cents"),
        settle_only=bool(signal.get("late_settlement") or signal.get("favorite")),
        lane=signal.get("lane"),
        conviction=signal.get("conviction"),
        entry_fee_cents=entry_fee_cents,
    )

    targets = signal.get("scalp_targets") or {}
    record["outcome"] = "opened" if active == "live" else "simulated"
    record["ladder"] = targets
    risk.log_decision(record)

    status["last_entry"] = {
        "ticker": ticker,
        "side": side,
        "count": count,
        "price_cents": int(round(basis_cents)),
        "quoted_price_cents": quoted_price,
        "limit_price_cents": price,
        "slippage_cents": round(basis_cents - quoted_price, 2),
        "targets": targets,
        "stop_cents": signal.get("stop_price_cents"),
        "simulated": active != "live",
        "at": int(time.time()),
    }
    status["last_decision"] = {
        "outcome": record["outcome"],
        "reason": (
            f"{'Bought' if active == 'live' else 'Simulated'} {count} {side} "
            f"@ {basis_cents:.2f}c on {ticker}"
            + (
                f" (quoted {quoted_price}c, limit {price}c)"
                if abs(basis_cents - quoted_price) >= 0.005
                else ""
            )
        ),
    }


def _equity_cents():
    """Cash plus what deployed money is still worth, at cost.

    The daily loss limit must measure losses, not deployment. Kalshi's cash
    balance drops by the full entry cost while a lot is open, and by the
    resting cost while a maker bid waits on the book. On a small account that
    dip alone can exceed the loss limit, which once halted the day seconds
    after a perfectly healthy first trade. Marking open lots and resting
    entries at cost keeps the check focused on realized losses; an adverse
    move is realized by the stop within seconds anyway, while a false halt
    latches until midnight.

    The cash snapshot refreshes on reconcile (every RECONCILE_SECONDS), so
    equity can briefly double-count a fresh fill until the next refresh.
    That slack only delays a legitimate halt by at most one reconcile
    interval; it cannot cause a false one.
    """
    cash = status.get("balance_cents")
    if cash is None:
        return None

    total = int(cash)
    for lot in scalp.open_lots("live"):
        total += int((lot.get("count_open") or 0) * (lot.get("entry_price") or 0))
    for pending in scalp.pending_all("live").values():
        resting = int(pending.get("count") or 0) - int(pending.get("filled_recorded") or 0)
        if resting > 0:
            total += resting * int(pending.get("price_cents") or 0)

    return total


_last_pending_poll = 0.0


async def tick(client, signal_provider, market_provider):
    global _last_pending_poll

    status["last_tick_at"] = int(time.time())
    status["book_source"] = _book_source["last"]
    signal = signal_provider()

    # Resting entries first: their fills create the lots the exit pass must
    # then protect (and the adverse-selection guard needs the fresh signal).
    # Then exits, always before entries. See module docstring.
    #
    # Order-status polling is throttled independently of the loop. The loop now
    # runs several times a second, but polling an order's status is a REST call
    # against a rate limit, and nothing about a resting order changes usefully
    # faster than PENDING_POLL_SECONDS. Exits, by contrast, are free -- they
    # read the in-memory book -- so they run every single tick.
    if time.time() - _last_pending_poll >= config.settings.pending_poll_seconds:
        _last_pending_poll = time.time()
        await _poll_pending_entries(client, signal)

    await run_exits(client, signal)

    # Evaluate the daily loss limit every tick, independent of whether a BUY
    # signal exists this cycle. Measured on equity, not cash: cash dips by
    # the entry cost of every open position, which is deployment, not loss.
    risk.check_halt(_equity_cents())

    if risk.is_halted():
        state = risk.load_state()
        status["last_decision"] = {
            "outcome": "halted",
            "reason": f"Entries halted: {state.get('halt_reason')}",
        }
        return

    await run_entry(client, signal, market_provider())

    # Commit anything the tick deferred. Last, so it is never in front of an
    # exit decision, and unconditional, so a quiet market cannot leave the most
    # recent mark unwritten indefinitely.
    store.flush(max_delay=1.0)


async def loop(client, signal_provider, market_provider):
    """Run until the process stops. Errors are logged, not fatal."""
    if mode() == "off":
        status["running"] = False
        return

    if not client.has_credentials:
        status["running"] = False
        status["last_error"] = (
            f"Trading mode is '{mode()}' but credentials are unusable: "
            f"{client.credential_error}"
        )
        return

    problems = config.settings.problems()
    if problems:
        status["running"] = False
        status["last_error"] = "Configuration rejected: " + "; ".join(problems)
        return

    status["running"] = True
    last_reconcile = 0.0
    last_sweep = time.time()

    while True:
        try:
            if time.time() - last_reconcile > config.settings.reconcile_seconds:
                await reconcile(client)
                last_reconcile = time.time()

            await tick(client, signal_provider, market_provider)
            status["last_error"] = None

            if time.time() - last_sweep > 600:
                scalp.forget_closed(mode())
                last_sweep = time.time()

        except KalshiAuthError as error:
            # Already halted by the raiser. Stop the loop rather than retry a
            # request that cannot succeed.
            status["last_error"] = str(error)
            status["running"] = False
            return

        except Exception as error:
            status["last_error"] = str(error)[:200]

        # Wait for the book to change, and fall back to the loop interval when
        # it is quiet. This is the difference between reacting to a dip on the
        # delta that created it and finding out about it up to two seconds
        # later -- the entire point of being co-located.
        #
        # The floor first: on a fast tape the wake event is set again before the
        # tick finishes, so without it the loop never yields at all.
        floor = config.settings.loop_min_seconds
        if floor > 0:
            await asyncio.sleep(floor)

        event = book_event()
        try:
            timeout = max(0.0, config.settings.loop_seconds - floor)
            if timeout > 0:
                await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            event.clear()