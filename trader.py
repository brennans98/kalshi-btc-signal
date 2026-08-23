"""The autonomous scalping loop.

Three modes, set by TRADING_MODE:
    off     signal only, no decision path exercised (default)
    dryrun  full decision path including simulated round trips, places nothing
    live    places real orders

Order of operations on every tick, and the order matters:

    1. reconcile   periodically re-read balance and positions from Kalshi
    2. exits       manage every open lot through the ladder
    3. entries     only then consider opening something new

Exits run first and are never gated by cooldown, daily caps, or the halt flag.
A risk limit exists to stop the system taking on NEW exposure; if it could also
block an exit it would trap an open position in a contract heading for
settlement, which is the opposite of protection. A halt stops entries and
leaves the exit engine running until flat.

dryrun exists because the honest test of an autonomous policy is not whether it
runs, but whether you would have approved what it chose. It simulates entries
and walks them through the same ladder against real book prices, so the decision
log shows complete round trips with realistic outcomes before any money moves.
Paper lots are stored separately from live lots.
"""

import asyncio
import time
import uuid

import config
import policy
import risk
import scalp
from kalshi_client import KalshiApiError, KalshiAuthError

status = {
    "mode": "off",
    "running": False,
    "last_evaluated_at": None,
    "last_decision": None,
    "last_entry": None,
    "last_exit": None,
    "last_error": None,
    "balance_cents": None,
    "kalshi_positions": [],
    "reconciled_at": None,
    "adopted": [],
}


def mode():
    return config.settings.trading_mode if config.settings.trading_mode in config.VALID_MODES else "off"


def snapshot():
    current = mode()
    state = risk.load_state()
    payload = dict(status)
    payload["mode"] = current
    payload["limits"] = risk.limits()
    payload["halted"] = bool(state.get("halted"))
    payload["halt_reason"] = state.get("halt_reason")
    payload["trades_today"] = state.get("trades_today", 0)
    payload["day_start_balance_cents"] = state.get("day_start_balance_cents")
    payload["drawdown_cents"] = risk.drawdown_cents(status.get("balance_cents"))
    payload["scalp"] = scalp.view(current if current != "off" else "dryrun")
    return payload


# ---------------------------------------------------------------- reconcile

async def reconcile(client):
    """Refresh balance and positions from Kalshi, and adopt any orphans.

    An orphan is a real position with no local lot -- left by a restart mid-trade,
    a manual order, or a fill that arrived after a crash. Adopting it hands it to
    the exit engine. The alternative is a live position that nothing is watching.
    """
    balance = await client.get_balance()
    positions = await client.get_positions()

    status["balance_cents"] = balance.get("balance")
    risk.note_balance(balance.get("balance"))

    held = []
    for entry in positions.get("market_positions", []):
        quantity = entry.get("position") or 0
        if quantity:
            held.append(
                {
                    "ticker": entry.get("ticker"),
                    "position": quantity,
                    "side": "yes" if quantity > 0 else "no",
                    "count": abs(quantity),
                }
            )

    status["kalshi_positions"] = held
    status["reconciled_at"] = int(time.time())

    if mode() != "live":
        return held

    known = {lot["key"] for lot in scalp.open_lots("live")}

    for position in held:
        lot_key = scalp.key(position["ticker"], position["side"])
        if lot_key in known:
            continue

        entry_price = await _infer_entry_price(client, position)
        market = await _safe_market(client, position["ticker"])

        scalp.record_entry(
            "live",
            position["ticker"],
            position["side"],
            position["count"],
            entry_price,
            policy.close_epoch(market),
        )

        note = {
            "ticker": position["ticker"],
            "side": position["side"],
            "count": position["count"],
            "assumed_entry_cents": entry_price,
            "at": int(time.time()),
        }
        status["adopted"] = ([note] + status.get("adopted", []))[:10]
        risk.log_decision({"event": "adopted_orphan_position", **note})

    return held


async def _infer_entry_price(client, position):
    """Best available basis for an adopted position: its most recent fill."""
    try:
        fills = await client.get_fills(ticker=position["ticker"], limit=20)
        for fill in fills.get("fills", []):
            if (fill.get("side") or "").lower() != position["side"]:
                continue
            price = fill.get("yes_price") if position["side"] == "yes" else fill.get("no_price")
            if isinstance(price, int) and price > 0:
                return price
    except (KalshiApiError, KalshiAuthError, AttributeError, TypeError):
        pass

    # No usable fill history. Mark at the current bid so the ladder measures
    # from something real; the stop then protects from here rather than from an
    # invented basis.
    book = await _safe_book(client, position["ticker"])
    bid = policy.bid_for_side(policy.book_snapshot(book), position["side"])
    return bid or 50


async def _safe_book(client, ticker):
    try:
        return await client.get_orderbook(ticker)
    except (KalshiApiError, KalshiAuthError):
        return None


async def _safe_market(client, ticker):
    try:
        return await client.get_market(ticker)
    except (KalshiApiError, KalshiAuthError):
        return None


# -------------------------------------------------------------------- exits

async def manage_exits(client):
    """Walk every open lot through the ladder. Runs before any entry logic."""
    current = mode()
    if current == "off":
        return

    for lot in scalp.open_lots(current):
        book = await _safe_book(client, lot["ticker"])
        bid = policy.bid_for_side(policy.book_snapshot(book), lot["side"])

        if bid is None:
            # No bid means no exit is possible this tick. Keep the lot and
            # retry; the settlement guard still applies on a later tick.
            continue

        marked = scalp.mark(current, lot["key"], bid)
        if not marked:
            continue

        for intent in scalp.plan(marked, bid):
            await _execute_exit(client, current, marked, intent)


async def _execute_exit(client, current, lot, intent):
    record = {
        "event": "exit",
        "mode": current,
        "ticker": lot["ticker"],
        "side": lot["side"],
        "tier": intent["tier"],
        "kind": intent["kind"],
        "count": intent["count"],
        "limit_price_cents": intent["limit_price"],
        "entry_price_cents": lot["entry_price"],
        "reason": intent["reason"],
    }

    if current == "dryrun":
        realized = scalp.record_exit(
            current, lot["key"], intent["tier"], intent["kind"],
            intent["count"], intent["limit_price"],
        )
        record["outcome"] = "simulated"
        record["realized_cents"] = realized
        risk.log_decision(record)
        status["last_exit"] = {
            "outcome": "simulated",
            "reason": f"{intent['reason']} ({intent['count']} @ {intent['limit_price']}c)",
            "at": int(time.time()),
        }
        return

    client_order_id = f"exit-{intent['kind']}-{uuid.uuid4().hex[:12]}"

    try:
        response = await client.sell(
            ticker=lot["ticker"],
            side=lot["side"],
            count=intent["count"],
            price_cents=intent["limit_price"],
            client_order_id=client_order_id,
        )
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected while exiting {lot['ticker']}: {error}")
        record["outcome"] = "auth_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        risk.log_decision(record)
        return
    except KalshiApiError as error:
        # The lot is untouched, so the next tick retries at the new bid. This is
        # why exits are immediate-or-cancel rather than resting orders.
        record["outcome"] = "exit_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        risk.log_decision(record)
        return

    filled = _filled_count(response, intent["count"])
    realized = scalp.record_exit(
        current, lot["key"], intent["tier"], intent["kind"], filled, intent["limit_price"]
    )

    record["outcome"] = "filled" if filled else "unfilled"
    record["filled_count"] = filled
    record["realized_cents"] = realized
    record["client_order_id"] = client_order_id
    risk.log_decision(record)

    status["last_exit"] = {
        "outcome": record["outcome"],
        "reason": f"{intent['reason']} ({filled} @ {intent['limit_price']}c)",
        "at": int(time.time()),
    }


def _filled_count(response, requested):
    """How many contracts an immediate-or-cancel sell actually took.

    Kalshi's order response shape has varied; when the count is not reported,
    assume the request filled. Assuming a fill on an exit is the safer error:
    the next reconcile corrects the lot, whereas assuming no fill would re-sell
    contracts that are already gone.
    """
    order = (response or {}).get("order") or response or {}

    for field in ("taker_fill_count", "filled_count", "fill_count"):
        value = order.get(field)
        if isinstance(value, int):
            return min(value, requested)

    remaining = order.get("remaining_count")
    if isinstance(remaining, int):
        return max(0, requested - remaining)

    return requested


# ------------------------------------------------------------------ entries

async def consider_entry(client, signal, market):
    current = mode()
    status["last_evaluated_at"] = int(time.time())

    if not (signal.get("action") or "").startswith("BUY"):
        status["last_decision"] = {"outcome": "no_signal", "reason": signal.get("reason")}
        return

    lots = scalp.open_lots(current)
    approved, reason, sizing = risk.check(
        signal,
        status.get("balance_cents"),
        len(lots),
        [lot["ticker"] for lot in lots],
    )

    record = {
        "event": "entry",
        "mode": current,
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
    price = int(signal["price_cents"])
    count = sizing["count"]
    close_epoch = policy.close_epoch(market)

    if current == "dryrun":
        scalp.record_entry("dryrun", ticker, side, count, price, close_epoch)
        risk.record_trade(ticker, count, sizing["cost_cents"])

        record["outcome"] = "simulated"
        record["would_place"] = {
            "ticker": ticker,
            "side": side,
            "count": count,
            "limit_price_cents": price,
            "cost_cents": sizing["cost_cents"],
            "targets": signal.get("scalp_targets"),
            "stop": signal.get("stop_price_cents"),
        }
        status["last_decision"] = {
            "outcome": "simulated",
            "reason": f"Would scalp {count} {side} @ {price}c on {ticker}",
        }
        status["last_entry"] = dict(record["would_place"], at=int(time.time()), simulated=True)
        risk.log_decision(record)
        return

    client_order_id = f"entry-{uuid.uuid4().hex[:14]}"

    try:
        response = await client.create_order(
            ticker=ticker,
            side=side,
            count=count,
            price_cents=price,
            client_order_id=client_order_id,
        )
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected during entry: {error}")
        record["outcome"] = "auth_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        status["last_decision"] = {"outcome": "auth_error", "reason": str(error)}
        risk.log_decision(record)
        return
    except KalshiApiError as error:
        record["outcome"] = "order_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        status["last_decision"] = {"outcome": "order_error", "reason": str(error)}
        risk.log_decision(record)
        return

    # Fill-or-kill: either the whole entry filled or none of it did.
    filled = _filled_count(response, count)

    if filled <= 0:
        record["outcome"] = "unfilled"
        status["last_decision"] = {
            "outcome": "unfilled",
            "reason": f"Entry did not fill at {price}c on {ticker}",
        }
        risk.log_decision(record)
        return

    scalp.record_entry("live", ticker, side, filled, price, close_epoch)
    risk.record_trade(ticker, filled, filled * price)

    record["outcome"] = "placed"
    record["filled_count"] = filled
    record["client_order_id"] = client_order_id
    record["response"] = response
    risk.log_decision(record)

    status["last_entry"] = {
        "ticker": ticker,
        "side": side,
        "count": filled,
        "limit_price_cents": price,
        "targets": signal.get("scalp_targets"),
        "stop": signal.get("stop_price_cents"),
        "client_order_id": client_order_id,
        "at": int(time.time()),
    }
    status["last_decision"] = {
        "outcome": "placed",
        "reason": f"Scalping {filled} {side} @ {price}c on {ticker}",
    }

    try:
        await reconcile(client)
    except (KalshiApiError, KalshiAuthError):
        pass


# --------------------------------------------------------------------- loop

async def loop(client, signal_provider, market_provider):
    """Run the scalping loop until the process stops."""
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
    last_tidy = time.time()

    while True:
        try:
            if time.time() - last_reconcile > config.settings.reconcile_seconds:
                await reconcile(client)
                last_reconcile = time.time()

            # Exits first, always, and regardless of the halt flag.
            await manage_exits(client)

            if not risk.is_halted():
                await consider_entry(client, signal_provider(), market_provider())

            if time.time() - last_tidy > 900:
                scalp.forget_closed(mode())
                last_tidy = time.time()

            status["last_error"] = None

        except KalshiAuthError as error:
            risk.halt(f"Authentication rejected: {error}")
            status["last_error"] = str(error)
            status["running"] = False
            return

        except Exception as error:
            status["last_error"] = str(error)[:200]

        await asyncio.sleep(config.settings.loop_seconds)
