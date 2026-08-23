"""Risk limits and persistent state.

This module is what replaces the manual approval step. Removing a human check
without enforcing these caps in code would leave the system unbounded.

State persists to DATA_DIR so a Railway restart cannot reset a daily loss
counter or clear a latched halt.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
STATE_FILE = DATA_DIR / "risk_state.json"
DECISION_LOG = DATA_DIR / "decisions.jsonl"


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def caps():
    return {
        "max_contracts_per_trade": _env_int("MAX_CONTRACTS_PER_TRADE", 1),
        "max_dollars_per_trade": _env_float("MAX_DOLLARS_PER_TRADE", 5.0),
        "daily_loss_limit_dollars": _env_float("DAILY_LOSS_LIMIT_DOLLARS", 20.0),
        "max_trades_per_day": _env_int("MAX_TRADES_PER_DAY", 10),
        "max_open_positions": _env_int("MAX_OPEN_POSITIONS", 1),
        "cooldown_seconds": _env_int("COOLDOWN_SECONDS", 120),
    }


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


DEFAULT_STATE = {
    "day": None,
    "trades_today": 0,
    "day_open_balance_cents": None,
    "last_trade_at": 0,
    "halted": False,
    "halt_reason": None,
    "open_positions": {},
    "last_balance_cents": None,
}


class RiskManager:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state = dict(DEFAULT_STATE)
        self._load()
        self._roll_day()

    # ---------- persistence ----------

    def _load(self):
        if not STATE_FILE.exists():
            return
        try:
            stored = json.loads(STATE_FILE.read_text())
            if isinstance(stored, dict):
                self.state.update(stored)
        except Exception:
            pass

    def _save(self):
        try:
            STATE_FILE.write_text(json.dumps(self.state, indent=2))
        except Exception:
            pass

    def log_decision(self, record):
        entry = dict(record)
        entry["at"] = int(time.time())
        entry["at_iso"] = datetime.now(timezone.utc).isoformat()
        try:
            with DECISION_LOG.open("a") as handle:
                handle.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ---------- day boundary ----------

    def _roll_day(self):
        today = _today()
        if self.state.get("day") == today:
            return

        self.state["day"] = today
        self.state["trades_today"] = 0
        self.state["day_open_balance_cents"] = self.state.get("last_balance_cents")

        # A loss-driven halt is released by the new trading day; a manual halt is not.
        if self.state.get("halted") and self.state.get("halt_reason", "").startswith("daily loss"):
            self.state["halted"] = False
            self.state["halt_reason"] = None

        self._save()

    # ---------- broker reconciliation ----------

    def observe_balance(self, balance_cents):
        if balance_cents is None:
            return
        self.state["last_balance_cents"] = balance_cents
        if self.state.get("day_open_balance_cents") is None:
            self.state["day_open_balance_cents"] = balance_cents
        self._save()
        self._check_daily_loss()

    def observe_positions(self, positions):
        """Replace local position state with the broker's view."""
        mapped = {}
        for position in positions or []:
            ticker = position.get("ticker")
            count = position.get("position") or position.get("count") or 0
            if ticker and count:
                mapped[ticker] = count
        self.state["open_positions"] = mapped
        self._save()

    def daily_pnl_dollars(self):
        opening = self.state.get("day_open_balance_cents")
        current = self.state.get("last_balance_cents")
        if opening is None or current is None:
            return None
        return (current - opening) / 100.0

    def _check_daily_loss(self):
        pnl = self.daily_pnl_dollars()
        limit = caps()["daily_loss_limit_dollars"]
        if pnl is not None and pnl <= -abs(limit) and not self.state.get("halted"):
            self.halt(f"daily loss limit reached ({pnl:.2f} vs -{abs(limit):.2f})")

    # ---------- halt control ----------

    def halt(self, reason):
        self.state["halted"] = True
        self.state["halt_reason"] = reason
        self._save()
        self.log_decision({"event": "halt", "reason": reason})

    def resume(self):
        self.state["halted"] = False
        self.state["halt_reason"] = None
        self._save()
        self.log_decision({"event": "resume"})

    # ---------- the gate ----------

    def check(self, signal):
        """Return (allowed, reason, contracts)."""
        self._roll_day()
        config = caps()

        if self.state.get("halted"):
            return False, f"halted: {self.state.get('halt_reason')}", 0

        ticker = signal.get("ticker")
        price = signal.get("price_cents")

        if not ticker or not price:
            return False, "signal is missing a ticker or price", 0

        if self.state.get("trades_today", 0) >= config["max_trades_per_day"]:
            return False, f"daily trade cap reached ({config['max_trades_per_day']})", 0

        open_positions = self.state.get("open_positions") or {}

        if ticker in open_positions:
            return False, f"already holding a position in {ticker}", 0

        if len(open_positions) >= config["max_open_positions"]:
            return False, f"open position cap reached ({config['max_open_positions']})", 0

        elapsed = time.time() - (self.state.get("last_trade_at") or 0)
        if elapsed < config["cooldown_seconds"]:
            return False, f"cooldown active ({int(config['cooldown_seconds'] - elapsed)}s remaining)", 0

        pnl = self.daily_pnl_dollars()
        if pnl is not None and pnl <= -abs(config["daily_loss_limit_dollars"]):
            return False, "daily loss limit reached", 0

        # Size on the tighter of the contract cap and the dollar cap.
        by_dollars = int((config["max_dollars_per_trade"] * 100) // price)
        contracts = min(config["max_contracts_per_trade"], max(by_dollars, 0))

        if contracts < 1:
            return False, (
                f"${config['max_dollars_per_trade']:.2f} per-trade cap will not cover "
                f"one contract at {price:.0f}c"
            ), 0

        return True, "within limits", contracts

    def record_trade(self, ticker, contracts):
        self.state["trades_today"] = self.state.get("trades_today", 0) + 1
        self.state["last_trade_at"] = time.time()
        positions = dict(self.state.get("open_positions") or {})
        positions[ticker] = positions.get(ticker, 0) + contracts
        self.state["open_positions"] = positions
        self._save()

    def snapshot(self):
        config = caps()
        return {
            "halted": self.state.get("halted", False),
            "halt_reason": self.state.get("halt_reason"),
            "day": self.state.get("day"),
            "trades_today": self.state.get("trades_today", 0),
            "max_trades_per_day": config["max_trades_per_day"],
            "open_positions": self.state.get("open_positions") or {},
            "daily_pnl_dollars": self.daily_pnl_dollars(),
            "daily_loss_limit_dollars": config["daily_loss_limit_dollars"],
            "balance_dollars": (
                self.state["last_balance_cents"] / 100.0
                if self.state.get("last_balance_cents") is not None
                else None
            ),
            "caps": config,
        }
