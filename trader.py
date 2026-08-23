"""The autonomous scalping loop.

Order of operations on every tick, and the order matters:

    1. exits   -- manage what is already open
    2. entries -- only then consider opening something new

Exits run first and unconditionally. A cooldown, a daily trade cap, or a
latched halt blocks new entries; none of them may block closing a position the
system already holds. A system that can open but not close is worse than one
that does nothing.

Three modes, set by TRADING_MODE:
    off     evaluate nothing, place nothing (default)
    dryrun  full decision path, including simulated round trips through the
            ladder, placing nothing
    live    places real orders

dryrun exists because the honest test of an autonomous scalper is not whether
it runs, but whether the round trips it produces are ones you would have taken.
It writes to a separate lot file, so paper scalps never mix with live ones.

Balance and positions are reconciled against Kalshi rather than tracked purely
locally, so a restart or a missed fill cannot leave the loop believing it is
flat when it is not.
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
    "running": False,
    "last_evaluated_at": None,
    "last_decision": None,
    "last_entry": None,
    "last_exit": None,
    "last_error": None,
    "balance_cents": None,
    "open_positions": [],
    "reconciled_at": None,
}


def mode():
    return config.settings.trading_mode


def _order_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:14]}"


def snapshot():
    state = risk.load_state()
    current = mode()

    payload = dict(status)
    payload["mode"] = current
    payload["limits"] = risk.limits()
    payload["halted"] = bool(state.get("halted"))
    payload["halt_reason"] = state.get("halt_reason")
    payload["halt_is_manual"] = bool(state.get("halt_is_manual"))
    payload["trades_today"] = state.get("trades_today", 0)
    payload["day_start_balance_cents"] = state.get("day_start_balance_cents")
    payload["drawdown_cents"] = risk.drawdown_cents(status.get("balance_cents"))
    payload["scalp"] = scalp.view(current if current != "off" else "dryrun")
    return payload


async def reconcile(client):
    """Refresh balance and open positions from Kalshi."""
    balance = await client.get_balance()
    positions = await client.get_positions()

    status["balance_cents"] = balance.get("balance")
    risk.note_balance(balance.get("balance"))

    open_positions = []
    for entry in positions.get("market_positions", []):
        quantity = entry.get("position") or 0
        if quantity:
            open_positions.append({"ticker": entry.get("ticker"), "position": quantity})

    status["open_positions"] = open_positions
    status["reconciled_at"] = int(time.time())
    return open_positions


# ---------------------------------------------------------------- exits


async def manage_exits(client, book_provider):
    """Walk every open lot and act on whatever the ladder says.

    book_provider(ticker) returns the current orderbook payload, or None.
    """
    current = mode()
    exits = []

    for lot in scalp.open_lots(current):
        book = book_provider(lot["ticker"])
        snap = policy.book_snapshot(book)
        bid = policy.bid_for_side(snap, lot["side"])

        if bid is None:
            continue

        marked = scalp.mark(current, lot["key"], bid)
        if not marked:
            continue

        for intent in scalp.plan(marked, bid):
            record = {
                "event": "exit",
                "mode": current,
                "ticker": marked["ticker"],
                "side": marked["side"],
                "tier": intent["tier"],
                "kind": intent["kind"],
                "count": intent["count"],
                "limit_price_cents": intent["limit_price"],
                "entry_price_cents": marked["entry_price"],
                "reason": intent["reason"],
            }

            if current == "dryrun":
                realized = scalp.record_exit(
                    current,
                    marked["key"],
                    intent["tier"],
                    intent["kind"],
                    intent["count"],
                    intent["limit_price"],
                )
                record["outcome"] = "dryrun"
                record["realized_cents"] = realized
                risk.log_decision(record)
                exits.append(record)
                status["last_exit"] = record
                continue

            try:
                response = await client.sell(
                    ticker=marked["ticker"],
                    side=marked["side"],
                    count=intent["count"],
                    price_cents=intent["limit_price"],
                    client_order_id=_order_id(f"exit-{intent['tier']}"),
                )
            except KalshiAuthError as error:
                risk.halt(f"Authentication rejected while exiting: {error}")
                record["outcome"] = "auth_error"
                record["error"] = str(error)
                status["last_error"] = str(error)
                risk.log_decision(record)
                raise
            except KalshiApiError as error:
                # Leave the lot open and retry on the next tick; the ladder is
                # re-evaluated from scratch every time.
                record["outcome"] = "exit_error"
                record["error"] = str(error)
                status["last_error"] = str(error)
                risk.log_decision(record)
                continue

            realized = scalp.record_exit(
                current,
                marked["key"],
                intent["tier"],
                intent["kind"],
                intent["count"],
                intent["limit_price"],
            )
            record["outcome"] = "placed"
            record["realized_cents"] = realized
            record["response"] = response
            risk.log_decision(record)
            exits.append(record)
            status["last_exit"] = record

    if exits:
        scalp.forget_closed(current)

    return exits


# --------------------------------------------------------------- entries


async def consider_entry(client, signal, market):
    current = mode()

    if not signal.get("action", "").startswith("BUY"):
        status["last_decision"] = {"outcome": "no_signal", "reason": signal.get("reason")}
        return

    # Local lots are the authority on what this system is scalping; the
    # reconciled account view catches anything opened outside it.
    local_lots = scalp.open_lots(current)
    local_tickers = [lot["ticker"] for lot in local_lots]
    account_tickers = [entry["ticker"] for entry in (status.get("open_positions") or [])]
    open_tickers = list({*local_tickers, *account_tickers})
    open_count = max(len(local_lots), len(status.get("open_positions") or []))

    approved, reason, sizing = risk.check(
        signal, status.get("balance_cents"), open_count, open_tickers
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
        scalp.record_entry(current, ticker, side, count, price, close_epoch)
        record["outcome"] = "dryrun"
        record["would_place"] = {
            "ticker": ticker,
            "side": side,
            "count": count,
            "limit_price_cents": price,
            "cost_cents": sizing["cost_cents"],
            "targets": signal.get("scalp_targets"),
            "stop_cents": signal.get("stop_price_cents"),
        }
        status["last_decision"] = {
            "outcome": "dryrun",
            "reason": f"Would scalp {count} {side} @ {price}c on {ticker}",
        }
        risk.log_decision(record)
        return

    client_order_id = _order_id("entry")

    try:
        response = await client.create_order(
            ticker=ticker,
            side=side,
            count=count,
            price_cents=price,
            client_order_id=client_order_id,
            action="buy",
        )
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected during entry: {error}")
        record["outcome"] = "auth_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        risk.log_decision(record)
        raise
    except KalshiApiError as error:
        record["outcome"] = "order_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        status["last_decision"] = {"outcome": "order_error", "reason": str(error)}
        risk.log_decision(record)
        return

    # Trust the fill count the venue reports over the count requested.
    order = (response or {}).get("order") or {}
    filled = order.get("filled_count")
    if not isinstance(filled, int) or filled <= 0:
        filled = count

    risk.record_trade(ticker, filled, filled * price)
    scalp.record_entry(current, ticker, side, filled, price, close_epoch)

    record["outcome"] = "placed"
    record["client_order_id"] = client_order_id
    record["filled_count"] = filled
    record["response"] = response
    status["last_entry"] = {
        "ticker": ticker,
        "side": side,
        "count": filled,
        "limit_price_cents": price,
        "targets": signal.get("scalp_targets"),
        "placed_at": int(time.time()),
    }
    status["last_decision"] = {
        "outcome": "placed",
        "reason": f"Scalping {filled} {side} @ {price}c on {ticker}",
    }
    risk.log_decision(record)

    try:
        await reconcile(client)
    except Exception:
        pass


# ------------------------------------------------------------------ loop


async def tick(client, signal_provider, market_provider, book_provider):
    status["last_evaluated_at"] = int(time.time())

    # Exits first, always.
    await manage_exits(client, book_provider)
    await consider_entry(client, signal_provider(), market_provider())


async def loop(client, signal_provider, market_provider, book_provider):
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

    status["running"] = True
    last_reconcile = 0.0

    while True:
        cfg = config.settings

        try:
            if time.time() - last_reconcile > cfg.reconcile_seconds:
                await reconcile(client)
                last_reconcile = time.time()

            await tick(client, signal_provider, market_provider, book_provider)
            status["last_error"] = None

        except KalshiAuthError as error:
            risk.halt(f"Authentication rejected: {error}")
            status["last_error"] = str(error)
            status["running"] = False
            return

        except Exception as error:
            status["last_error"] = str(error)[:200]

        await asyncio.sleep(cfg.loop_seconds)
