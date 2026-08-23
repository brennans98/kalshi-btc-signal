"""The limits that replace the approval click.

Removing a human approval step does not make a system autonomous; it makes it
unbounded. What made approval a control was that a person could refuse. These
limits are the refusal, expressed in code and checked before every order.

Design notes:
  - The daily loss limit is measured against the Kalshi account balance, not a
    locally accumulated tally. A local tally that misses a fill drifts toward
    understating losses, which is the direction that hurts.
  - A breached loss limit latches a halt to disk. A restart is not a reset;
    Railway restarting the container must not resume trading.
  - Every decision is logged, including the ones that were blocked. Reviewing
    only the trades that happened hides the near-misses.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(os.getenv("RISK_STATE_PATH", "data/risk_state.json"))
LOG_PATH = Path(os.getenv("DECISION_LOG_PATH", "data/decisions.jsonl"))


def _env_int(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def limits():
    return {
        "max_contracts_per_trade": _env_int("MAX_CONTRACTS_PER_TRADE", 5),
        "max_cost_per_trade_cents": _env_int("MAX_COST_PER_TRADE_CENTS", 500),
        "max_open_positions": _env_int("MAX_OPEN_POSITIONS", 1),
        "max_trades_per_day": _env_int("MAX_TRADES_PER_DAY", 12),
        "daily_loss_limit_cents": _env_int("DAILY_LOSS_LIMIT_CENTS", 2000),
        "cooldown_seconds": _env_int("COOLDOWN_SECONDS", 60),
    }


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _blank_state():
    return {
        "day": _today(),
        "day_start_balance_cents": None,
        "trades_today": 0,
        "last_trade_at": 0,
        "halted": False,
        "halt_reason": None,
        "halt_is_manual": False,
        "tickers_traded": [],
    }


def load_state():
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception:
        state = _blank_state()

    # A new UTC day resets counters and clears an automatic halt. A manual halt
    # persists until it is explicitly resumed.
    if state.get("day") != _today():
        manual = state.get("halted") and state.get("halt_is_manual")
        reason = state.get("halt_reason") if manual else None
        state = _blank_state()
        state["halted"] = bool(manual)
        state["halt_reason"] = reason
        state["halt_is_manual"] = bool(manual)
        save_state(state)

    return state


def save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def log_decision(record):
    record = dict(record)
    record["logged_at"] = datetime.now(timezone.utc).isoformat()

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass


def read_decisions(limit=50):
    try:
        lines = LOG_PATH.read_text().strip().splitlines()
    except Exception:
        return []

    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue

    return list(reversed(records))


def halt(reason, manual=False):
    state = load_state()
    state["halted"] = True
    state["halt_reason"] = reason
    state["halt_is_manual"] = manual
    save_state(state)
    log_decision({"event": "halt", "reason": reason, "manual": manual})
    return state


def resume():
    state = load_state()
    state["halted"] = False
    state["halt_reason"] = None
    state["halt_is_manual"] = False
    save_state(state)
    log_decision({"event": "resume"})
    return state


def note_balance(balance_cents):
    """Record the day's opening balance the first time a balance is observed."""
    if balance_cents is None:
        return load_state()

    state = load_state()
    if state.get("day_start_balance_cents") is None:
        state["day_start_balance_cents"] = int(balance_cents)
        save_state(state)

    return state


def drawdown_cents(balance_cents):
    state = load_state()
    opening = state.get("day_start_balance_cents")

    if opening is None or balance_cents is None:
        return None

    return max(0, opening - int(balance_cents))


def record_trade(ticker, count, cost_cents):
    state = load_state()
    state["trades_today"] = int(state.get("trades_today", 0)) + 1
    state["last_trade_at"] = int(time.time())

    tickers = list(state.get("tickers_traded", []))
    if ticker not in tickers:
        tickers.append(ticker)
    state["tickers_traded"] = tickers[-50:]

    save_state(state)
    return state


def check(signal, balance_cents, open_position_count, open_tickers):
    """Decide whether the signal may be executed.

    Returns (approved: bool, reason: str, sizing: dict|None).
    """
    config = limits()
    state = load_state()

    if state.get("halted"):
        return False, f"Trading halted: {state.get('halt_reason')}", None

    drawdown = drawdown_cents(balance_cents)
    if drawdown is not None and drawdown >= config["daily_loss_limit_cents"]:
        halt(
            f"Daily loss limit reached (down {drawdown}c of "
            f"{config['daily_loss_limit_cents']}c allowed)"
        )
        return False, "Daily loss limit reached", None

    if state.get("trades_today", 0) >= config["max_trades_per_day"]:
        return False, f"Daily trade cap reached ({config['max_trades_per_day']})", None

    if open_position_count >= config["max_open_positions"]:
        return False, f"Open position cap reached ({config['max_open_positions']})", None

    ticker = signal.get("ticker")
    if ticker and ticker in (open_tickers or []):
        return False, f"Already holding a position in {ticker}", None

    elapsed = time.time() - float(state.get("last_trade_at") or 0)
    if elapsed < config["cooldown_seconds"]:
        wait = int(config["cooldown_seconds"] - elapsed)
        return False, f"Cooldown active ({wait}s remaining)", None

    price = int(signal.get("price_cents") or 0)
    if price <= 0:
        return False, "Signal carries no executable price", None

    count = config["max_contracts_per_trade"]
    affordable = config["max_cost_per_trade_cents"] // price
    count = min(count, affordable)

    if balance_cents is not None:
        count = min(count, int(balance_cents) // price)

    if count < 1:
        return False, "Per-trade cost cap or balance allows fewer than 1 contract", None

    return True, "Within limits", {"count": count, "cost_cents": count * price}
