"""
The autonomous decision loop.

One pass of `step()` is the whole cycle: read the current decision, ask the
risk manager whether it is allowed, size it, and place it. Modes:

    off     nothing is evaluated and nothing is placed
    dryrun  everything is evaluated and logged, nothing is placed
    live    orders are placed

Dry-run is the important one. It exercises the identical code path an order
would take and writes what it would have done to the decision log, so the
policy can be compared against the judgement it is replacing before any money
is committed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from kalshi_client import KalshiAuthError, KalshiClient, KalshiError


class Trader:
    def __init__(
        self,
        *,
        settings: Any,
        client: KalshiClient,
        risk: Any,
        decide: Callable[[], dict],
    ) -> None:
        self.settings = settings
        self.client = client
        self.risk = risk
        self.decide = decide
        self.status: dict = {
            "running": False,
            "mode": settings.trading_mode,
            "last_pass_at": None,
            "last_action": None,
            "last_block": None,
            "last_error": None,
            "orders_placed": 0,
            "dry_run_intents": 0,
            "account_synced_at": None,
        }

    # ---- account reconciliation ---------------------------------------

    async def sync_account(self) -> None:
        """Align local state with the broker before acting on it."""
        if not self.client.can_trade:
            return
        try:
            balance = await self.client.balance_cents()
            self.risk.note_balance(balance)
            positions = await self.client.positions()
            self.risk.sync_positions(positions)
            self.status["account_synced_at"] = int(time.time())
            self.status["last_error"] = None
        except KalshiAuthError as error:
            self.status["last_error"] = f"auth: {error}"
            self.risk.halt(f"Kalshi rejected the credentials: {error}")
        except (KalshiError, Exception) as error:  # noqa: BLE001
            self.status["last_error"] = str(error)[:200]

    # ---- one decision cycle -------------------------------------------

    async def step(self) -> None:
        self.status["last_pass_at"] = int(time.time())

        if not self.settings.is_enabled:
            self.status["last_action"] = "disabled"
            return

        decision = self.decide()
        self.status["last_action"] = decision.get("action")

        if decision.get("action") == "NO TRADE":
            self.status["last_block"] = None
            return

        block = self.risk.veto(decision)
        if block:
            self.status["last_block"] = block
            self.risk.log_decision({"event": "blocked", "reason": block, "decision": decision})
            return

        count = self.risk.size_for(decision)
        if count < 1:
            self.status["last_block"] = "Size limits leave no contracts to buy"
            self.risk.log_decision({"event": "blocked", "reason": "size zero", "decision": decision})
            return

        self.status["last_block"] = None

        if not self.settings.is_live:
            self.status["dry_run_intents"] += 1
            self.risk.record_order(decision, count, live=False)
            self.risk.log_decision(
                {"event": "dry_run_intent", "count": count, "decision": decision}
            )
            return

        try:
            result = await self.client.create_order(
                order_path=self.settings.order_path,
                ticker=decision["ticker"],
                side=decision["side"],
                action="buy",
                count=count,
                price_cents=int(decision["price_cents"]),
            )
        except KalshiAuthError as error:
            self.status["last_error"] = f"auth: {error}"
            self.risk.halt(f"Kalshi rejected the credentials: {error}")
            self.risk.log_decision({"event": "order_auth_error", "error": str(error)[:300]})
            return
        except Exception as error:  # noqa: BLE001
            self.status["last_error"] = str(error)[:200]
            self.risk.log_decision({"event": "order_error", "error": str(error)[:300]})
            return

        self.status["orders_placed"] += 1
        self.status["last_error"] = None
        self.risk.record_order(decision, count, live=True)
        self.risk.log_decision(
            {
                "event": "order_placed",
                "count": count,
                "decision": decision,
                "order": result.get("order", result),
            }
        )
        await self.sync_account()

    # ---- loop ----------------------------------------------------------

    async def run(self) -> None:
        self.status["running"] = True
        await self.sync_account()
        last_sync = time.time()

        while True:
            try:
                await self.step()
                if time.time() - last_sync > 30:
                    await self.sync_account()
                    last_sync = time.time()
            except Exception as error:  # noqa: BLE001 - the loop must not die
                self.status["last_error"] = str(error)[:200]
            await asyncio.sleep(self.settings.loop_seconds)

    def public_view(self) -> dict:
        return dict(self.status, mode=self.settings.trading_mode)
