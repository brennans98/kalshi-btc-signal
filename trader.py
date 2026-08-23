"""The autonomous scalping loop.

Three modes, set by TRADING_MODE:
    off     signal only, no decision path exercised (default)
    dryrun  full decision path including simulated round trips, places nothing
    live    places real orders

dryrun exists because the honest test of an autonomous policy is not whether it
runs, but whether you would have approved what it chose. It simulates entries
AND ladder exits at real book prices into a separate lot file, so the decision
log shows complete round trips with realized cents -- not just entry intents.
Run it across a real sample, read the log, then decide.

Order of operations on every tick, and the reason for it:

    1. exits   -- an open scalp is time-sensitive and already risked
    2. entries -- only ever optional

Exits are deliberately NOT gated by cooldown, the daily trade cap, or the open
position cap. Those limits exist to restrain how much risk the system takes on;
applying them to exits would strand a position the ladder wants to close, which
is the opposite of a control. A halt stops entries and leaves exits running for
the same reason.

State is reconciled against Kalshi rather than tracked locally, so a restart or
a missed fill cannot leave the loop believing it is flat when it is not.
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
    "broker_positions": [],
    "reconciled_at": None,
    "ticks": 0,
}


def mode():
    return config.settings.trading_mode


def _order_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def snapshot():
    current = mode()
    payload = dict(status)
    payload["mode"] = current
    payload["limits"] = risk.limits()
    payload["scalp"] = scalp.view(current if current != "off" else "dryrun")

    state = risk.load_state()
    payload["halted"] = bool(state.get("halted"))
    payload["halt_reason"] = state.get("halt_reason")
    payload["trades_today"] = state.get("trades_today", 0)
    payload["day_start_balance_cents"] = state.get("day_start_balance_cents")
    payload["drawdown_cents"] = risk.drawdown_cents(status.get("balance_cents"))
    return payload


async def reconcile(client):
    """Refresh balance and positions from Kalshi, and adopt anything unknown.

    A position the broker reports that we have no lot for is dangerous: nothing
    would ever exit it. That happens after a redeploy mid-trade, or if a fill
    landed as the container restarted. Adopt it with the current bid as its
    basis so the ladder and the settlement guard take it over. The basis is
    approximate, but a managed position with an approximate basis is far safer
    than an unmanaged one.
    """
    balance = await client.get_balance()
    positions = await client.get_positions()

    status["balance_cents"] = balance.get("balance")
    risk.note_balance(balance.get("balance"))

    broker = []
    for entry in positions.get("market_positions", []):
        quantity = entry.get("position") or 0
        if quantity:
            broker.append({"ticker": entry.get("ticker"), "position": quantity})

    status["broker_positions"] = broker
    status["reconciled_at"] = int(time.time())

    if mode() == "live":
        known = {(lot["ticker"], lot["side"]) for lot in scalp.open_lots("live")}

        for entry in broker:
            ticker = entry["ticker"]
            quantity = entry["position"]
            side = "yes" if quantity > 0 else "no"

            if (ticker, side) in known:
                continue

            try:
                book = policy.book_snapshot(await client.get_orderbook(ticker))
                market = await client.get_market(ticker)
            except (KalshiApiError, KalshiAuthError):
                continue

            bid = policy.bid_for_side(book, side)
            if bid is None:
                continue

            scalp.record_entry(
                "live",
                ticker,
                side,
                abs(quantity),
                bid,
                policy.close_epoch(market),
            )
            risk.log_decision(
                {
                    "event": "adopt",
                    "ticker": ticker,
                    "side": side,
                    "count": abs(quantity),
                    "assumed_basis_cents": bid,
                    "reason": "Broker position had no local lot; adopted so exits apply",
                }
            )

    return broker


async def manage_exits(client, book_provider):
    """Walk every open lot and act on whatever the ladder calls for.

    Runs before entries and is not subject to the entry limits.
    """
    current = mode()
    if current == "off":
        return

    for lot in scalp.open_lots(current):
        ticker, side = lot["ticker"], lot["side"]

        book = book_provider(ticker)
        if book is None:
            try:
                book = policy.book_snapshot(await client.get_orderbook(ticker))
            except (KalshiApiError, KalshiAuthError):
                continue

        bid = policy.bid_for_side(book, side)
        if bid is None:
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
        "held_seconds": int(time.time() - (lot.get("opened_at") or time.time())),
    }

    if current == "dryrun":
        realized = scalp.record_exit(
            "dryrun",
            lot["key"],
            intent["tier"],
            intent["kind"],
            intent["count"],
            intent["limit_price"],
        )
        record["outcome"] = "dryrun"
        record["realized_cents"] = realized
        status["last_exit"] = {**record, "at": int(time.time())}
        risk.log_decision(record)
        return

    try:
        response = await client.sell(
            ticker=lot["ticker"],
            side=lot["side"],
            count=intent["count"],
            price_cents=intent["limit_price"],
            client_order_id=_order_id("exit-" + intent["tier"]),
        )
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected during exit: {error}")
        record["outcome"] = "auth_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        risk.log_decision(record)
        return
    except KalshiApiError as error:
        # Not fatal: the ladder re-evaluates next tick at the new bid.
        record["outcome"] = "exit_error"
        record["error"] = str(error)
        status["last_error"] = str(error)
        risk.log_decision(record)
        return

    order = response.get("order") or {}
    filled = order.get("count_filled")
    filled = intent["count"] if filled is None else int(filled)

    if filled > 0:
        realized = scalp.record_exit(
            "live",
            lot["key"],
            intent["tier"],
            intent["kind"],
            filled,
            intent["limit_price"],
        )
        record["realized_cents"] = realized

    record["outcome"] = "filled" if filled else "unfilled"
    record["count_filled"] = filled
    status["last_exit"] = {**record, "at": int(time.time())}
    risk.log_decision(record)


async def consider_entry(client, signal, market):
    current = mode()
    status["last_evaluated_at"] = int(time.time())

    if not signal.get("action", "").startswith("BUY"):
        status["last_decision"] = {"outcome": "no_signal", "reason": signal.get("reason")}
        return

    lots = scalp.open_lots(current)
    open_tickers = [lot["ticker"] for lot in lots]

    approved, reason, sizing = risk.check(
        signal, status.get("balance_cents"), len(lots), open_tickers
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
    close_at = policy.close_epoch(market)

    if current == "dryrun":
        scalp.record_entry("dryrun", ticker, side, count, price, close_at)
        record["outcome"] = "dryrun"
        record["would_place"] = {
            "ticker": ticker,
            "side": side,
            "count": count,
            "limit_price_cents": price,
            "cost_cents": sizing["cost_cents"],
            "targets": signal.get("scalp_targets"),
            "stop_price_cents": signal.get("stop_price_cents"),
        }
        status["last_decision"] = {
            "outcome": "dryrun",
            "reason": f"Would buy {count} {side} @ {price}c on {ticker}",
        }
        status["last_entry"] = {**record["would_place"], "at": int(time.time())}
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

    order = response.get("order") or {}
    filled = order.get("count_filled")
    filled = count if filled is None else int(filled)

    record["outcome"] = "placed" if filled else "unfilled"
    record["count_filled"] = filled
    record["client_order_id"] = client_order_id

    if filled > 0:
        # Only a filled entry creates a lot, and only a fill counts against the
        # daily trade budget. A killed fill-or-kill order risked nothing.
        scalp.record_entry("live", ticker, side, filled, price, close_at)
        risk.record_trade(ticker, filled, filled * price)

        status["last_entry"] = {
            "ticker": ticker,
            "side": side,
            "count": filled,
            "limit_price_cents": price,
            "targets": signal.get("scalp_targets"),
            "client_order_id": client_order_id,
            "at": int(time.time()),
        }
        status["last_decision"] = {
            "outcome": "placed",
            "reason": f"Bought {filled} {side} @ {price}c on {ticker}",
        }
    else:
        status["last_decision"] = {
            "outcome": "unfilled",
            "reason": f"Entry did not fill at {price}c on {ticker}",
        }

    risk.log_decision(record)

    try:
        await reconcile(client)
    except (KalshiApiError, KalshiAuthError):
        pass


async def loop(client, signal_provider, market_provider, book_provider):
    """Run the decision loop.

    signal_provider() -> current signal dict
    market_provider() -> current market dict
    book_provider(ticker) -> cached book snapshot or None
    """
    cfg = config.settings

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

    problems = cfg.problems()
    if problems:
        status["running"] = False
        status["last_error"] = "Refusing to start: " + "; ".join(problems)
        return

    status["running"] = True
    last_reconcile = 0.0

    while True:
        try:
            if time.time() - last_reconcile > cfg.reconcile_seconds:
                await reconcile(client)
                last_reconcile = time.time()

            # Exits first, always, and regardless of halt state: a halt must
            # stop new risk, not abandon existing risk.
            await manage_exits(client, book_provider)

            if risk.is_halted():
                status["last_decision"] = {
                    "outcome": "halted",
                    "reason": risk.load_state().get("halt_reason"),
                }
            else:
                await consider_entry(client, signal_provider(), market_provider())

            status["ticks"] = status.get("ticks", 0) + 1
            status["last_error"] = None

        except KalshiAuthError as error:
            risk.halt(f"Authentication rejected: {error}")
            status["last_error"] = str(error)
            status["running"] = False
            return

        except Exception as error:
            status["last_error"] = str(error)[:200]

        await asyncio.sleep(cfg.loop_seconds)
