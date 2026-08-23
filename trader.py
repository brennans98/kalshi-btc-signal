"""The autonomous loop.

Three modes, set by TRADING_MODE:
    off     signal only, no decision path exercised (default)
    dryrun  full decision and risk path, logs the order it would place
    live    places real orders

dryrun exists because the honest test of an autonomous policy is not whether it
runs, but whether you would have approved what it chose. Run dryrun across a
real sample, read the log, and check that against your own judgment before
letting it place anything.

State is reconciled against Kalshi rather than tracked locally, so a restart or
a missed fill cannot leave the loop believing it is flat when it is not.
"""

import asyncio
import os
import time
import uuid

import policy
import risk
from kalshi_client import KalshiAuthError, KalshiApiError

RECONCILE_INTERVAL = 30

status = {
    "mode": "off",
    "running": False,
    "last_evaluated_at": None,
    "last_decision": None,
    "last_order": None,
    "last_error": None,
    "balance_cents": None,
    "open_positions": [],
    "reconciled_at": None,
}


def mode():
    value = os.getenv("TRADING_MODE", "off").strip().lower()
    return value if value in ("off", "dryrun", "live") else "off"


def snapshot():
    state = risk.load_state()
    payload = dict(status)
    payload["mode"] = mode()
    payload["limits"] = risk.limits()
    payload["thresholds"] = policy.settings()
    payload["halted"] = bool(state.get("halted"))
    payload["halt_reason"] = state.get("halt_reason")
    payload["trades_today"] = state.get("trades_today", 0)
    payload["day_start_balance_cents"] = state.get("day_start_balance_cents")
    payload["drawdown_cents"] = risk.drawdown_cents(status.get("balance_cents"))
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
            open_positions.append(
                {"ticker": entry.get("ticker"), "position": quantity}
            )

    status["open_positions"] = open_positions
    status["reconciled_at"] = int(time.time())
    return open_positions


async def evaluate_and_maybe_trade(client, signal):
    status["last_evaluated_at"] = int(time.time())

    if not signal.get("action", "").startswith("BUY"):
        status["last_decision"] = {
            "outcome": "no_signal",
            "reason": signal.get("reason"),
        }
        return

    open_positions = status.get("open_positions") or []
    open_tickers = [entry["ticker"] for entry in open_positions]

    approved, reason, sizing = risk.check(
        signal,
        status.get("balance_cents"),
        len(open_positions),
        open_tickers,
    )

    record = {
        "event": "decision",
        "mode": mode(),
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

    if mode() == "dryrun":
        record["outcome"] = "dryrun"
        record["would_place"] = {
            "ticker": ticker,
            "side": side,
            "count": count,
            "limit_price_cents": price,
            "cost_cents": sizing["cost_cents"],
        }
        status["last_decision"] = {
            "outcome": "dryrun",
            "reason": f"Would buy {count} {side} @ {price}c on {ticker}",
        }
        risk.log_decision(record)
        return

    client_order_id = f"btc15m-{uuid.uuid4().hex[:16]}"

    try:
        response = await client.create_order(
            ticker=ticker,
            side=side,
            count=count,
            price_cents=price,
            client_order_id=client_order_id,
        )
    except KalshiAuthError as error:
        risk.halt(f"Authentication rejected during order placement: {error}")
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

    risk.record_trade(ticker, count, sizing["cost_cents"])

    record["outcome"] = "placed"
    record["client_order_id"] = client_order_id
    record["response"] = response
    status["last_order"] = {
        "ticker": ticker,
        "side": side,
        "count": count,
        "limit_price_cents": price,
        "client_order_id": client_order_id,
        "placed_at": int(time.time()),
    }
    status["last_decision"] = {
        "outcome": "placed",
        "reason": f"Bought {count} {side} @ {price}c on {ticker}",
    }
    risk.log_decision(record)

    # A fill changes both balance and exposure; re-read rather than assume.
    try:
        await reconcile(client)
    except Exception:
        pass


async def loop(client, signal_provider, interval=5):
    """Run the decision loop. signal_provider() returns the current signal dict."""
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
    last_reconcile = 0

    while True:
        try:
            if time.time() - last_reconcile > RECONCILE_INTERVAL:
                await reconcile(client)
                last_reconcile = time.time()

            await evaluate_and_maybe_trade(client, signal_provider())
            status["last_error"] = None

        except KalshiAuthError as error:
            risk.halt(f"Authentication rejected: {error}")
            status["last_error"] = str(error)
            status["running"] = False
            return

        except Exception as error:
            status["last_error"] = str(error)[:200]

        await asyncio.sleep(interval)
