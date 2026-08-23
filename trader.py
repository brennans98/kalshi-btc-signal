"""The autonomous loop.

Three modes, set by TRADING_MODE:
    off     signal only, no decision path exercised (default)
    dryrun  full decision path incl. simulated round trips, places nothing
    live    places real orders

Ordering inside a tick is deliberate: exits first, then entries. A tick that
entered before exiting could open a new scalp while an existing one sat past its
stop. Exits also bypass the entry gates entirely -- cooldowns and daily caps
must never be able to trap an open position.

dryrun exists because the honest test of an autonomous policy is not whether it
runs, but whether you would have approved what it chose. It simulates complete
round trips -- entry, each ladder rung, stops, timeouts -- against real book
prices, writing to a paper lot file separate from live. Read the log, check the
choices against your own judgment, then decide about live.

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
    "broker_positions": [],
    "reconciled_at": None,
}


def mode():
    return config.settings.trading_mode


def _order_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def snapshot():
    """Everything the dashboard needs about the trader."""
    current_mode = mode()
    state = risk.load_state()

    payload = dict(status)
    payload["mode"] = current_mode
    payload["enabled"] = config.settings.is_enabled
    payload["limits"] = risk.limits()
    payload["halted"] = bool(state.get("halted"))
    payload["halt_reason"] = state.get("halt_reason")
    payload["halt_is_manual"] = bool(state.get("halt_is_manual"))
    payload["trades_today"] = state.get("trades_today", 0)
    payload["day_start_balance_cents"] = state.get("day_start_balance_cents")
    payload["drawdown_cents"] = risk.drawdown_cents(status.get("balance_cents"))
    payload["scalp"] = scalp.view(current_mode if current_mode != "off" else "dryrun")
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

    status["broker_positions"] = open_positions
    status["reconciled_at"] = int(time.time())
    return open_positions


# ---------------------------------------------------------------- exits


async def _exit_lot(client, lot, intent, dry):
    """Execute one exit intent. Returns a log-shaped result dict."""
    current_mode = mode()
    ticker, side = lot["ticker"], lot["side"]
    count, price = intent["count"], intent["limit_price"]

    record = {
        "event": "exit",
        "mode": current_mode,
        "tier": intent["tier"],
        "kind": intent["kind"],
        "ticker": ticker,
        "side": side,
        "count": count,
        "limit_price_cents": price,
        "entry_price_cents": lot["entry_price"],
        "reason": intent["reason"],
    }

    if dry:
        realized = scalp.record_exit(
            current_mode, lot["key"], intent["tier"], intent["kind"], count, price
        )
        record["outcome"] = "simulated"
        record["realized_cents"] = realized
        risk.log_decision(record)
        status["last_exit"] = {
            "outcome": "simulated",
            "reason": f"{intent['reason']} - sold {count} {side} @ {price}c",
            "at": int(time.time()),
        }
        return record

    try:
        response = await client.sell(
            ticker=ticker,
            side=side,
            count=count,
            price_cents=price,
            client_order_id=_order_id(f"exit-{intent['tier']}"),
        )
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected while exiting {ticker}: {error}")
        record["outcome"] = "auth_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        risk.log_decision(record)
        raise
    except KalshiApiError as error:
        # Leave the lot open: the ladder re-offers on the next tick.
        record["outcome"] = "order_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        risk.log_decision(record)
        return record

    realized = scalp.record_exit(
        current_mode, lot["key"], intent["tier"], intent["kind"], count, price
    )
    record["outcome"] = "placed"
    record["realized_cents"] = realized
    record["response"] = response
    risk.log_decision(record)

    status["last_exit"] = {
        "outcome": "placed",
        "reason": f"{intent['reason']} - sold {count} {side} @ {price}c",
        "at": int(time.time()),
    }
    return record


async def manage_open_scalps(client, dry):
    """Mark every open lot and act on whatever the ladder says.

    Exits are not gated by risk.check(). A cooldown or daily cap that could
    block closing a position would turn a risk control into a risk.
    """
    current_mode = mode()
    acted = 0

    for lot in scalp.open_lots(current_mode):
        try:
            book = await client.get_orderbook(lot["ticker"])
        except KalshiApiError as error:
            status["last_error"] = f"Book unavailable for {lot['ticker']}: {error}"
            continue

        snapshot_book = policy.book_snapshot(book)
        bid = policy.bid_for_side(snapshot_book, lot["side"])

        if bid is None:
            # No bid to sell into. Nothing to do but wait for one; the
            # settlement guard will still fire once it can.
            continue

        marked = scalp.mark(current_mode, lot["key"], bid)
        if not marked:
            continue

        for intent in scalp.plan(marked, bid):
            await _exit_lot(client, marked, intent, dry)
            acted += 1
            marked = scalp.get(current_mode, lot["key"])
            if not marked or (marked.get("count_open") or 0) <= 0:
                break

    if acted:
        scalp.forget_closed(current_mode)

    return acted


# --------------------------------------------------------------- entries


def _open_exposure(current_mode):
    """Open positions as the entry gate should see them.

    In dryrun the broker holds no paper position, so paper lots are the only
    truth. In live, lots and broker positions should agree; the larger of the
    two is used so a missed fill cannot understate exposure.
    """
    lots = scalp.open_lots(current_mode)
    lot_tickers = [lot["ticker"] for lot in lots]

    if current_mode != "live":
        return len(lots), lot_tickers

    broker = status.get("broker_positions") or []
    broker_tickers = [entry["ticker"] for entry in broker]
    tickers = list(dict.fromkeys(lot_tickers + broker_tickers))
    return max(len(lots), len(broker)), tickers


async def consider_entry(client, signal, market, dry):
    current_mode = mode()

    if not str(signal.get("action", "")).startswith("BUY"):
        status["last_decision"] = {"outcome": "no_signal", "reason": signal.get("reason")}
        return

    open_count, open_tickers = _open_exposure(current_mode)

    approved, reason, sizing = risk.check(
        signal, status.get("balance_cents"), open_count, open_tickers
    )

    record = {
        "event": "entry",
        "mode": current_mode,
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
    close_at = policy.close_epoch(market)

    ladder = {
        tier.name: {"target_cents": price + tier.cents, "exit_pct": tier.pct}
        for tier in config.settings.tiers()
    }
    record["ladder"] = ladder

    if dry:
        scalp.record_entry(current_mode, ticker, side, count, price, close_at)
        risk.record_trade(ticker, count, sizing["cost_cents"])
        record["outcome"] = "simulated"
        status["last_entry"] = {
            "outcome": "simulated",
            "reason": f"Would buy {count} {side} @ {price}c on {ticker}",
            "at": int(time.time()),
        }
        status["last_decision"] = status["last_entry"]
        risk.log_decision(record)
        return

    try:
        response = await client.create_order(
            ticker=ticker,
            side=side,
            count=count,
            price_cents=price,
            client_order_id=_order_id("entry"),
            action="buy",
        )
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected during entry: {error}")
        record["outcome"] = "auth_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        status["last_decision"] = {"outcome": "auth_error", "reason": str(error)}
        risk.log_decision(record)
        raise
    except KalshiApiError as error:
        record["outcome"] = "order_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        status["last_decision"] = {"outcome": "order_error", "reason": str(error)}
        risk.log_decision(record)
        return

    # Fill-or-kill: a response without a resting/filled order means no position.
    scalp.record_entry(current_mode, ticker, side, count, price, close_at)
    risk.record_trade(ticker, count, sizing["cost_cents"])

    record["outcome"] = "placed"
    record["response"] = response
    status["last_entry"] = {
        "outcome": "placed",
        "reason": f"Bought {count} {side} @ {price}c on {ticker}",
        "at": int(time.time()),
    }
    status["last_decision"] = status["last_entry"]
    risk.log_decision(record)

    try:
        await reconcile(client)
    except (KalshiApiError, KalshiAuthError):
        pass


# ------------------------------------------------------------------ loop


async def tick(client, context, dry):
    status["last_evaluated_at"] = int(time.time())

    # Exits before entries, always.
    await manage_open_scalps(client, dry)

    if risk.is_halted():
        status["last_decision"] = {
            "outcome": "halted",
            "reason": risk.load_state().get("halt_reason"),
        }
        return

    await consider_entry(client, context.get("signal") or {}, context.get("market"), dry)


async def loop(client, context_provider):
    """Run the decision loop. context_provider() returns {signal, market}."""
    cfg = config.settings

    if not cfg.is_enabled:
        status["running"] = False
        return

    if not client.has_credentials:
        status["running"] = False
        status["last_error"] = (
            f"Trading mode is '{cfg.trading_mode}' but credentials are unusable: "
            f"{client.credential_error}"
        )
        return

    problems = cfg.problems()
    if problems:
        status["running"] = False
        status["last_error"] = "Configuration invalid: " + "; ".join(problems)
        return

    status["running"] = True
    last_reconcile = 0.0

    while True:
        dry = not config.settings.is_live

        try:
            if time.time() - last_reconcile > config.settings.reconcile_seconds:
                await reconcile(client)
                last_reconcile = time.time()

            await tick(client, context_provider(), dry)
            status["last_error"] = None

        except KalshiAuthError as error:
            risk.halt(f"Authentication rejected: {error}")
            status["last_error"] = str(error)
            status["running"] = False
            return

        except Exception as error:
            status["last_error"] = str(error)[:200]

        await asyncio.sleep(config.settings.loop_seconds)
