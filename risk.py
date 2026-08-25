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
        "daily_loss_limit_pct": cfg.daily_loss_limit_pct,
        "effective_daily_loss_limit_cents": effective_loss_limit_cents(),
        "per_trade_risk_pct": cfg.per_trade_risk_pct,
        "cooldown_seconds": cfg.cooldown_seconds,
        "stop_cooldown_seconds": cfg.stop_cooldown_seconds,
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
        "last_stop_at": 0,
        "halted": False,
        "halt_reason": None,
        "halt_is_manual": False,
        "tickers_traded": [],
        "ticker_attempts": {},
    }


def load_state():
    try:
        state = json.loads(config.settings.risk_state_path.read_text())
    except Exception:
        state = _blank_state()

    if state.get("day") != _today():
        manual = state.get("halted") and state.get("halt_is_manual")
        reason = state.get("halt_reason") if manual else None
        state = _blank_state()
        state["halted"] = bool(manual)
        state["halt_reason"] = reason
        state["halt_is_manual"] = bool(manual)
        save_state(state)

    state.setdefault("cost_today_cents", 0)

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
        newline = chr(10)
        with path.open("a") as handle:
            handle.write(json.dumps(record) + newline)
    except Exception:
        pass


def read_decisions(limit=50, event=None):
    try:
        lines = config.settings.decision_log_path.read_text().strip().splitlines()
    except Exception:
        return []

    records = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if event and record.get("event") != event:
            continue
        records.append(record)
        if len(records) >= limit:
            break

    return records


def is_halted():
    return bool(load_state().get("halted"))


def halt(reason, manual=False):
    state = load_state()
    state["halted"] = True
    state["halt_reason"] = reason
    state["halt_is_manual"] = manual
    save_state(state)
    log_decision({"event": "halt", "reason": reason, "manual": manual})
    return state


def resume(rebaseline_balance=None):
    """Clear a halt and, when a balance is supplied, restart the day from it.

    A manual resume means a person has looked at the account and decided
    trading should continue. Re-baselining the day's opening balance to what
    the account holds NOW keeps the loss limit meaningful after a deposit:
    without it, a top-up leaves the limit measured from a stale, smaller
    opening balance -- either far too loose (drawdown measured from a number
    the account has left far behind) or far too tight (a percentage of
    yesterday's small balance).
    """
    state = load_state()
    state["halted"] = False
    state["halt_reason"] = None
    state["halt_is_manual"] = False
    if rebaseline_balance is not None:
        state["day_start_balance_cents"] = int(rebaseline_balance)
    save_state(state)
    log_decision({"event": "resume", "rebaselined_to": rebaseline_balance})
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


def drawdown_cents(balance_cents, state=None):
    state = state or load_state()
    opening = state.get("day_start_balance_cents")

    if opening is None or balance_cents is None:
        return None

    return max(0, opening - int(balance_cents))


def effective_loss_limit_cents(state=None):
    """The daily loss limit that actually applies today.

    The absolute cap (DAILY_LOSS_LIMIT_CENTS) is only meaningful while the
    account is larger than it. A 2000c limit on a 500c account is no limit at
    all -- the account would be empty long before the halt fired. The
    effective limit is the smaller of the absolute cap and
    DAILY_LOSS_LIMIT_PCT of the day's opening balance.
    """
    cfg = config.settings
    state = state or load_state()
    limit = cfg.daily_loss_limit_cents

    opening = state.get("day_start_balance_cents")
    if opening:
        pct_cap = max(1, int(opening) * cfg.daily_loss_limit_pct // 100)
        limit = min(limit, pct_cap)

    return limit


def note_stop():
    """Record that a stop-out just happened, starting the post-stop cooldown.

    A stop means the model was wrong about this market a moment ago. The
    minutes after a stop are exactly when the same wrong read is most likely
    to fire again -- the 15:48-15:50 sequence in the trade log was three
    stop-outs in two minutes of chop, each re-entering seconds after the
    last. Entries wait out STOP_COOLDOWN_SECONDS instead.
    """
    state = load_state()
    state["last_stop_at"] = int(time.time())
    save_state(state)
    return state


def check_halt(balance_cents):
    """Evaluate the daily loss limit and latch a halt if it has been breached.

    This must run every tick regardless of whether an entry signal exists.
    Exits can realize losses with no BUY signal anywhere nearby; if this check
    only ran inside the entry path, a quiet stretch after a losing run could
    sit past the loss limit indefinitely without ever halting.

    Returns the (possibly updated) state dict.
    """
    cfg = config.settings
    state = load_state()

    if state.get("halted"):
        return state

    drawdown = drawdown_cents(balance_cents, state=state)
    limit = effective_loss_limit_cents(state=state)
    if drawdown is not None and drawdown >= limit:
        return halt(
            f"Daily loss limit reached (down {drawdown}c of {limit}c allowed "
            f"-- the lesser of {cfg.daily_loss_limit_cents}c absolute and "
            f"{cfg.daily_loss_limit_pct}% of the day's opening balance)"
        )

    return state


def record_trade(ticker, count, cost_cents):
    state = load_state()
    state["trades_today"] = int(state.get("trades_today", 0)) + 1
    state["cost_today_cents"] = int(state.get("cost_today_cents", 0)) + int(cost_cents or 0)
    state["last_trade_at"] = int(time.time())

    tickers = list(state.get("tickers_traded", []))
    if ticker not in tickers:
        tickers.append(ticker)
    state["tickers_traded"] = tickers[-50:]

    attempts = dict(state.get("ticker_attempts") or {})
    attempts[ticker] = int(attempts.get(ticker, 0)) + 1
    # Each 15-minute market has a unique ticker, so this map only grows;
    # keep the most recent entries. Sorting by ticker works because the
    # tickers embed their timestamp.
    if len(attempts) > 100:
        attempts = dict(sorted(attempts.items())[-100:])
    state["ticker_attempts"] = attempts

    save_state(state)
    return state


def check(signal, balance_cents, open_position_count, open_tickers):
    """Decide whether an ENTRY may be executed.

    Exits are never routed through here; see the module docstring. The daily
    loss limit itself is evaluated in check_halt(), called once per tick from
    trader.tick() -- this function only reads the resulting halted flag, it
    does not re-derive the breach.

    Returns (approved: bool, reason: str, sizing: dict|None).
    """
    cfg = config.settings
    state = load_state()

    if state.get("halted"):
        return False, f"Trading halted: {state.get('halt_reason')}", None

    if state.get("trades_today", 0) >= cfg.max_trades_per_day:
        return False, f"Daily trade cap reached ({cfg.max_trades_per_day})", None

    if open_position_count >= cfg.max_open_positions:
        return False, f"Open position cap reached ({cfg.max_open_positions})", None

    ticker = signal.get("ticker")
    if ticker and ticker in (open_tickers or []):
        return False, f"Already scalping {ticker}", None

    # A market that already stopped us out is hostile to this signal right
    # now; re-trying it over and over is how a chop session drains a day.
    attempts = int((state.get("ticker_attempts") or {}).get(ticker) or 0)
    if ticker and attempts >= cfg.max_entries_per_market:
        return False, (
            f"Market attempt cap: already entered {ticker} {attempts}x "
            f"(max {cfg.max_entries_per_market} per market)"
        ), None

    elapsed = time.time() - float(state.get("last_trade_at") or 0)
    if elapsed < cfg.cooldown_seconds:
        return False, f"Cooldown active ({int(cfg.cooldown_seconds - elapsed)}s remaining)", None

    since_stop = time.time() - float(state.get("last_stop_at") or 0)
    if since_stop < cfg.stop_cooldown_seconds:
        return False, (
            f"Post-stop cooldown ({int(cfg.stop_cooldown_seconds - since_stop)}s "
            f"remaining before entries resume)"
        ), None

    price = int(signal.get("price_cents") or 0)
    if price <= 0:
        return False, "Signal carries no executable price", None

    count = min(
        cfg.max_contracts_per_trade,
        cfg.max_cost_per_trade_cents // price,
    )

    if balance_cents is not None:
        count = min(count, int(balance_cents) // price)

    # Bound the worst case of THIS trade, not just its cost: contracts times
    # the stop distance is what a stop-out actually takes from the account.
    if balance_cents is not None and cfg.stop_cents > 0:
        risk_budget = int(balance_cents) * cfg.per_trade_risk_pct // 100
        count = min(count, max(0, risk_budget // cfg.stop_cents))

    # Never size beyond what the exit side can absorb. Entering 6 contracts
    # against a 2-lot bid means the ladder cannot sell what it just bought.
    exit_size = signal.get("exit_bid_size")
    if isinstance(exit_size, int) and exit_size > 0:
        count = min(count, exit_size)

    if count < 1:
        return False, "Cost cap, balance, or exit liquidity allows fewer than 1 contract", None

    return True, "Within limits", {"count": count, "cost_cents": count * price}