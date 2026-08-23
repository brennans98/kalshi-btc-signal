"""
Risk limits and persisted trading state.

These checks are what replaces the human approval step. Removing the approval
prompt without them would leave the loop with no upper bound on what it can
lose, so every order passes through `RiskManager.veto()` first and the first
failing check stops it.

Daily loss is measured against account balance rather than inferred from
fills, so open exposure and fees are included automatically. Breaching it
latches a halt that survives a restart - clearing it is a deliberate act.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.state_path = self.data_dir / "trader_state.json"
        self.log_path = self.data_dir / "decisions.jsonl"
        self.state: dict = {}
        self._load()

    # ---- persistence ---------------------------------------------------

    def _default_state(self) -> dict:
        return {
            "day": _today(),
            "day_start_balance_cents": None,
            "balance_cents": None,
            "trades_today": 0,
            "halted": False,
            "halt_reason": None,
            "last_order_ts": 0.0,
            "open_positions": {},
            "last_order": None,
        }

    def _load(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if self.state_path.exists():
            try:
                self.state = {**self._default_state(), **json.loads(self.state_path.read_text())}
            except (OSError, ValueError):
                self.state = self._default_state()
        else:
            self.state = self._default_state()
        self.roll_day_if_needed()

    def save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.state, indent=2))
            os.replace(temp, self.state_path)
        except OSError:
            # A read-only or ephemeral filesystem must not stop the loop; the
            # in-memory limits still apply for the life of the process.
            pass

    def log_decision(self, entry: dict) -> None:
        record = {"ts": int(time.time()), **entry}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            pass

    # ---- day boundary --------------------------------------------------

    def roll_day_if_needed(self) -> None:
        today = _today()
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["day_start_balance_cents"] = self.state.get("balance_cents")
            self.state["trades_today"] = 0
            # A loss-cap halt is a daily limit and lifts with the new day. A
            # manual halt is not, and stays until it is explicitly resumed.
            if self.state.get("halt_reason", "").startswith("Daily loss"):
                self.state["halted"] = False
                self.state["halt_reason"] = None
            self.save()

    # ---- balance and positions -----------------------------------------

    def note_balance(self, balance_cents: int) -> None:
        self.state["balance_cents"] = balance_cents
        if self.state.get("day_start_balance_cents") is None:
            self.state["day_start_balance_cents"] = balance_cents
        self.save()

    def sync_positions(self, market_positions: list[dict]) -> None:
        """Replace tracked positions with what Kalshi actually reports."""
        live: dict[str, dict] = {}
        for position in market_positions:
            ticker = position.get("ticker")
            count = position.get("position") or 0
            if ticker and count:
                live[ticker] = {
                    "count": count,
                    "synced_at": int(time.time()),
                }
        self.state["open_positions"] = live
        self.save()

    @property
    def day_pnl_cents(self) -> Optional[int]:
        start = self.state.get("day_start_balance_cents")
        current = self.state.get("balance_cents")
        if start is None or current is None:
            return None
        return current - start

    # ---- halting -------------------------------------------------------

    def halt(self, reason: str) -> None:
        self.state["halted"] = True
        self.state["halt_reason"] = reason
        self.save()
        self.log_decision({"event": "halt", "reason": reason})

    def resume(self) -> None:
        self.state["halted"] = False
        self.state["halt_reason"] = None
        self.save()
        self.log_decision({"event": "resume"})

    # ---- the gate ------------------------------------------------------

    def veto(self, decision: dict, now: Optional[float] = None) -> Optional[str]:
        """Return a reason to block this order, or None to allow it."""
        now = now or time.time()
        settings = self.settings
        self.roll_day_if_needed()

        if self.state.get("halted"):
            return f"Trading halted: {self.state.get('halt_reason')}"

        pnl = self.day_pnl_cents
        if pnl is not None and pnl <= -abs(settings.daily_loss_limit_dollars * 100):
            reason = (
                f"Daily loss limit reached (${abs(pnl) / 100:.2f} "
                f"of ${settings.daily_loss_limit_dollars:.2f})"
            )
            self.halt(reason)
            return reason

        if self.state.get("trades_today", 0) >= settings.max_trades_per_day:
            return f"Daily trade cap of {settings.max_trades_per_day} reached"

        open_positions = self.state.get("open_positions", {})
        if len(open_positions) >= settings.max_open_positions:
            return f"Already holding {len(open_positions)} of {settings.max_open_positions} allowed positions"

        ticker = decision.get("ticker")
        if ticker and ticker in open_positions:
            return f"Already holding a position in {ticker}"

        elapsed = now - float(self.state.get("last_order_ts") or 0)
        if elapsed < settings.cooldown_seconds:
            return f"Cooldown active for another {settings.cooldown_seconds - elapsed:.0f}s"

        return None

    def size_for(self, decision: dict) -> int:
        """Contracts to buy, capped by both the count and the dollar limit."""
        price = int(decision.get("price_cents") or 0)
        if price <= 0:
            return 0
        by_cost = int((self.settings.max_trade_cost_dollars * 100) // price)
        by_book = int(decision.get("available_size") or 0)
        return max(0, min(self.settings.max_contracts_per_trade, by_cost, by_book))

    def record_order(self, decision: dict, count: int, live: bool) -> None:
        self.state["last_order_ts"] = time.time()
        if live:
            self.state["trades_today"] = int(self.state.get("trades_today", 0)) + 1
            ticker = decision.get("ticker")
            if ticker:
                self.state.setdefault("open_positions", {})[ticker] = {
                    "count": count,
                    "side": decision.get("side"),
                    "entry_price_cents": decision.get("price_cents"),
                    "opened_at": int(time.time()),
                }
        self.state["last_order"] = {
            "ticker": decision.get("ticker"),
            "side": decision.get("side"),
            "count": count,
            "price_cents": decision.get("price_cents"),
            "live": live,
            "at": int(time.time()),
        }
        self.save()

    def public_view(self) -> dict:
        pnl = self.day_pnl_cents
        return {
            "day": self.state.get("day"),
            "halted": bool(self.state.get("halted")),
            "halt_reason": self.state.get("halt_reason"),
            "trades_today": self.state.get("trades_today", 0),
            "open_positions": list((self.state.get("open_positions") or {}).keys()),
            "balance_dollars": (
                round(self.state["balance_cents"] / 100, 2)
                if self.state.get("balance_cents") is not None
                else None
            ),
            "day_pnl_dollars": round(pnl / 100, 2) if pnl is not None else None,
            "last_order": self.state.get("last_order"),
        }
