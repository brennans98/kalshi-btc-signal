"""The limits that replace the approval click.

Removing a human approval step does not make a system autonomous; it makes it
unbounded. What made approval a control was that a person could refuse. These
limits are the refusal, expressed in code and checked before every entry.

Design notes:
  - The daily loss limit is measured against the Kalshi account balance, not a
    locally accumulated tally. A local tally that misses a fill drifts toward
    understating losses, which is the direction that hurts.
  - A breached loss limit latches a halt to disk. A restart is not a reset;
    Railway restarting the container must not resume trading.
  - A halt stops ENTRIES. The trader keeps running exits, because abandoning an
    open position is not a safety measure.
  - Every decision is logged, including blocked ones. Reviewing only the trades
    that happened hides the near-misses.
  - Scalping changes the shape of these limits, not their purpose: the trade
    count per day is higher and the cooldown much shorter, because the whole
    point is many small round trips. The loss cap is what actually bounds the
    day, and it does not care how many trades produced the loss.
  - The loss-limit check runs on every tick via check_halt(), independent of
    whether a new entry signal exists. A quiet period with no BUY signals must
    not let the account drift past the limit unnoticed while exits keep firing.
"""

import json
import time
from datetime import datetime, timezone

import config


def limits():
    cfg = config.settings
    return {
        "max_contracts_per_trade": cfg.max_contracts_per_trade,
        "max_cost_per_trade_cents": cfg.max_cost_per_trade_cents,
        "max_open_positions": cfg.max_open_positions,
        "max_trades_per_day": cfg.max_trades_per_day,
        "daily_loss_limit_cents": cfg.daily_loss_limit_cents,
        "cooldown_seconds": cfg.cooldown_seconds,
    }


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _blank_state():
    return {
        "day": _today(),
        "day_start_balance_cents": None,
        "trades_today": 0,
        "cost_today_cents": 0,
        "last_trade_at": 0,
        "halted": False,
        "halt_reason": None,
        "halt_is_manual": False,
        "tickers_traded": [],
    }


def load_state():
    try:
        state = json.loads(config.settings.risk_state_path.read_text())
    except Exception:
        state = _blank_state()

    # A new UTC day resets counters and clear