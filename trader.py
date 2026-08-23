"""The autonomous loop.

Three modes, set by TRADING_MODE:

    off     evaluate nothing; the dashboard stays informational (default)
    dryrun  run the full decision path and log every intended order, place none
    live    place orders

dryrun exercises the identical code path as live, so the decision log is a
faithful preview of what live would have done.
"""

import asyncio
import os
import time
import uuid

import policy
from kalshi_client import KalshiAuthError, KalshiClient, KalshiRequestError
from risk import RiskManager

POLL_SECONDS = 3
RECONCILE_SECONDS = 30


def mode():
    value = os.getenv("TRADING_MODE", "off").strip().lower()
    return value if value in ("off", "dryrun", "live") else "off"


class Trader:
    def __init__(self, get_context):
        """get_context() must return (spot, trades, market, orderbook)."""
        self.get_context = get_context
        self.client = KalshiClient()
        self.risk = RiskManager()
        self.mode = mode()
        self.status = "idle"
        self.last_error = None
        self.last_evaluation = None
        self.last_evaluated_at = None
        self.last_order = None
        self._reconciled_at = 0

    # ---------- reconciliation ----------

    async def reconcile(self):
        """Pull the broker's truth into local state. Never trust local counters alone."""
        if not self.client.configured:
            return

        try:
            balance = await self.client.get_balance()
            self.risk.observe_balance(balance.get("balance"))

            positions = await self.client.get_positions()
            self.risk.observe_positions(positions.get("market_positions") or positions.get("positions"))

            self._reconciled_at = time.time()
            self.last_error = None
        except KalshiAuthError as error:
            self.last_error = f"auth: {error}"
            self.risk.halt(f"authentication rejected: {str(error)[:120]}")
        except KalshiRequestError as error:
            self.last_error = f"reconcile: {str(error)[:160]}"

    # ---------- execution ----------

    async def place(self, signal, contracts):
        client_order_id = f"btc15m-{uuid.uuid4().hex[:16]}"
        ticker = signal["ticker"]
        side = signal["side"]
        price = int(round(signal["price_cents"]))

        if self.mode == "dryrun":
            record = {
                "event": "dryrun_order",
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "price_cents": price,
                "client_order_id": client_order_id,
                "signal": signal,
            }
            self.risk.log_decision(record)
            self.risk.record_trade(ticker, contracts)
            self.last_order = record
            return record

        try:
            response = await self.client.create_order(
                ticker=ticker,
                action="buy",
                side=side,
                count=contracts,
                limit_price_cents=price,
                client_order_id=client_order_id,
            )
        except KalshiAuthError as error:
            self.risk.halt(f"authentication rejected on order: {str(error)[:120]}")
            self.risk.log_decision({"event": "order_auth_error", "error": str(error)[:300]})
            self.last_error = f"auth: {error}"
            return None
        except KalshiRequestError as error:
            self.risk.log_decision({
                "event": "order_error",
                "ticker": ticker,
                "error": str(error)[:300],
                "signal": signal,
            })
            self.last_error = f"order: {str(error)[:160]}"
            return None

        order = (response or {}).get("order") or {}
        record = {
            "event": "live_order",
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "price_cents": price,
            "client_order_id": client_order_id,
            "order_id": order.get("order_id"),
            "order_status": order.get("status"),
            "signal": signal,
        }
        self.risk.log_decision(record)
        self.risk.record_trade(ticker, contracts)
        self.last_order = record

        # Fold the fill into balance and position state immediately.
        await self.reconcile()
        return record

    # ---------- loop ----------

    async def step(self):
        spot, trades, market, orderbook = self.get_context()
        signal = policy.evaluate(spot, trades, market, orderbook)

        self.last_evaluation = signal
        self.last_evaluated_at = int(time.time())

        if signal.get("action") == "NO TRADE":
            self.status = "watching"
            return

        allowed, reason, contracts = self.risk.check(signal)

        if not allowed:
            self.status = f"blocked: {reason}"
            self.risk.log_decision({
                "event": "blocked",
                "reason": reason,
                "signal": signal,
            })
            return

        self.status = f"placing {contracts} {signal['side']} @ {signal['price_cents']:.0f}c"
        await self.place(signal, contracts)
        self.status = "watching"

    async def run(self):
        if self.mode == "off":
            self.status = "disabled (TRADING_MODE=off)"
            return

        if not self.client.configured:
            self.status = "disabled (Kalshi credentials not set)"
            self.last_error = "KALSHI_KEY_ID or KALSHI_PRIVATE_KEY is missing"
            return

        await self.reconcile()
        self.status = "watching"

        while True:
            try:
                if time.time() - self._reconciled_at > RECONCILE_SECONDS:
                    await self.reconcile()

                if not self.risk.state.get("halted"):
                    await self.step()
                else:
                    self.status = f"halted: {self.risk.state.get('halt_reason')}"
            except Exception as error:
                self.last_error = f"loop: {str(error)[:200]}"
                self.status = "error"

            await asyncio.sleep(POLL_SECONDS)

    # ---------- reporting ----------

    def snapshot(self):
        return {
            "mode": self.mode,
            "env": self.client.env,
            "credentials_configured": self.client.configured,
            "status": self.status,
            "last_error": self.last_error,
            "last_evaluated_at": self.last_evaluated_at,
            "last_order": self.last_order,
            "risk": self.risk.snapshot(),
            "policy_limits": policy.limits(),
        }
