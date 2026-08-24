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


def mode():
    value = config.settings.trading_mode
    return value if value in config.VALID_MODES else "off"


def _order_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def snapshot():
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
    payload["drawdown_cents"] = risk.drawdown_cents(status.get("balance_cents"), state=state)
    payload["scalp"] = scalp.view(active if active != "off" else "dryrun")
    return payload


async def _book(client, ticker):
    """Orderbook with a short TTL, so several lots on one ticker share a fetch."""
    cached = _book_cache.get(ticker)
    if cached and time.time() - cached[0] < _BOOK_TTL:
        return cached[1]

    book = await client.get_orderbook(ticker)
    _book_cache[ticker] = (time.time(), book)
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


async def run_exits(client):
    """Walk every open lot through the ladder, stops and guards."""
    active = mode()
    if active == "off":
        return

    for lot in scalp.open_lots(active):
        ticker, side = lot["ticker"], lot["side"]

        try:
            snap = policy.book_snapshot(await _book(client, ticker))
        except KalshiAuthError:
            raise
        except KalshiApiError as error:
            status["last_error"] = f"Book unavailable for {ticker}: {error}"
            continue

        bid = policy.bid_for_side(snap, side)
        if bid is None:
            # No bid means no exit is possible this tick. The settlement guard
            # will still fire once the clock runs down, but there is nothing to
            # sell into right now.
            continue

        marked = scalp.mark(active, lot["key"], bid)
        if not marked:
            continue

        for intent in scalp.plan(marked, bid):
            await _execute_exit(client, active, marked, intent)


async def _execute_exit(client, active, lot, intent):
    ticker, side = lot["ticker"], lot["side"]
    count, price = intent["count"], intent["limit_price"]

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

    realized = scalp.record_exit(
        active, lot["key"], intent["tier"], intent["kind"], count, price
    )

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

    open_lots = scalp.open_lots(active)
    open_tickers = [lot["ticker"] for lot in open_lots]

    approved, reason, sizing = risk.check(
        signal, status.get("balance_cents"), len(open_lots), open_tickers
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
    price = int(signal["price_cents"])
    count = sizing["count"]

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
            record["outcome"] = "order_error"
            record["error"] = str(error)
            status["last_error"] = str(error)
            status["last_decision"] = {"outcome": "order_error", "reason": str(error)}
            risk.log_decision(record)
            return

        # V2 returns 201 even when a fill-or-kill order is killed unfilled.
        # An unfilled entry is not a trade: no lot, no cooldown, no daily count.
        filled = client.filled_count(response)
        if filled < count:
            record["outcome"] = "unfilled"
            record["filled"] = filled
            status["last_decision"] = {
                "outcome": "unfilled",
                "reason": (
                    f"FOK entry killed unfilled ({filled}/{count} "
                    f"{side} @ {price}c on {ticker})"
                ),
            }
            risk.log_decision(record)
            return

    # Recorded in both live and dryrun. Cooldown and the daily trade cap are
    # pacing limits, not capital controls -- they need to be exercised in
    # dryrun too, or a dry run can fire trades back-to-back at a rate live
    # trading never could, making the simulation misleading.
    risk.record_trade(ticker, count, sizing["cost_cents"])

    scalp.record_entry(active, ticker, side, count, price, policy.close_epoch(market))

    targets = signal.get("scalp_targets") or {}
    record["outcome"] = "opened" if active == "live" else "simulated"
    record["ladder"] = targets
    risk.log_decision(record)

    status["last_entry"] = {
        "ticker": ticker,
        "side": side,
        "count": count,
        "price_cents": price,
        "targets": targets,
        "stop_cents": signal.get("stop_price_cents"),
        "simulated": active != "live",
        "at": int(time.time()),
    }
    status["last_decision"] = {
        "outcome": record["outcome"],
        "reason": (
            f"{'Bought' if active == 'live' else 'Simulated'} {count} {side} "
            f"@ {price}c on {ticker}"
        ),
    }


async def tick(client, signal_provider, market_provider):
    status["last_tick_at"] = int(time.time())

    # Exits first, always. See the module docstring.
    await run_exits(client)

    # Evaluate the daily loss limit every tick, independent of whether a BUY
    # signal exists this cycle. See module docstring.
    risk.check_halt(status.get("balance_cents"))

    if risk.is_halted():
        state = risk.load_state()
        status["last_decision"] = {
            "outcome": "halted",
            "reason": f"Entries halted: {state.get('halt_reason')}",
        }
        return

    await run_entry(client, signal_provider(), market_provider())


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

        await asyncio.sleep(config.settings.loop_seconds)