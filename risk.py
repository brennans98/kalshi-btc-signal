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
  - These gates apply to ENTRIES only. Exits are never blocked by a cooldown,
    a daily cap, or a halt -- a system that cannot close a position it already
    holds is more dangerous than one that cannot open a new one.
  - Every decision is logged, including blocked ones. Reviewing only the trades
    that happened hides the near-misses, which is where a scalper's real
    behaviour lives.
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
        path = config.settings.risk_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def log_decision(record):
    record = dict(record)
    record["logged_at"] = datetime.now(timezone.utc).isoformat()

    try:
        path = config.settings.decision_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def read_decisions(limit=50):
    try:
        lines = config.settings.decision_log_path.read_text().strip().splitlines()
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


def is_halted():
    return bool(load_state().get("halted"))


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
    """Decide whether an ENTRY may be executed.

    Returns (approved: bool, reason: str, sizing: dict|None).
    """
    cfg = config.settings
    config_limits = limits()
    state = load_state()

    if state.get("halted"):
        return False, f"Trading halted: {state.get('halt_reason')}", None

    drawdown = drawdown_cents(balance_cents)
    if drawdown is not None and drawdown >= config_limits["daily_loss_limit_cents"]:
        halt(
            f"Daily loss limit reached (down {drawdown}c of "
            f"{config_limits['daily_loss_limit_cents']}c allowed)"
        )
        return False, "Daily loss limit reached", None

    if state.get("trades_today", 0) >= config_limits["max_trades_per_day"]:
        return False, f"Daily trade cap reached ({config_limits['max_trades_per_day']})", None

    if open_position_count >= config_limits["max_open_positions"]:
        return False, f"Open position cap reached ({config_limits['max_open_positions']})", None

    ticker = signal.get("ticker")
    if ticker and ticker in (open_tickers or []):
        return False, f"Already scalping {ticker}", None

    elapsed = time.time() - float(state.get("last_trade_at") or 0)
    if elapsed < config_limits["cooldown_seconds"]:
        return False, f"Cooldown active ({int(config_limits['cooldown_seconds'] - elapsed)}s)", None

    price = int(signal.get("price_cents") or 0)
    if price <= 0:
        return False, "Signal carries no executable price", None

    count = min(
        config_limits["max_contracts_per_trade"],
        config_limits["max_cost_per_trade_cents"] // price,
    )

    # Never size past the resting bid we intend to scalp out into.
    exit_size = signal.get("exit_bid_size")
    if isinstance(exit_size, int) and exit_size > 0:
        count = min(count, exit_size)

    if balance_cents is not None:
        count = min(count, int(balance_cents) // price)

    if count < 1:
        return False, "Cost cap, balance, or exit liquidity allows fewer than 1 contract", None

    return True, "Within limits", {"count": count, "cost_cents": count * price}
