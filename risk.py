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
import policy
import store


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
        "conviction_sizing": bool(cfg.conviction_sizing),
        "min_cost_per_trade_cents": cfg.min_cost_per_trade_cents,
        "max_entries_per_market": cfg.max_entries_per_market,
        "dip_max_entries_per_market": cfg.dip_max_entries_per_market,
        "dip_cooldown_seconds": cfg.dip_cooldown_seconds,
        "dip_stop_cooldown_seconds": cfg.dip_stop_cooldown_seconds,
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
    state = store.read(config.settings.risk_state_path, _blank_state)

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
    """Persist risk state atomically.

    This file holds the halt latch. The previous version truncated the file
    before writing it and swallowed every failure, so a process killed mid-write
    left an unparseable file -- and the reader treated that as "no halt". A
    breached daily loss limit could therefore un-latch itself across a crash or
    redeploy, which defeats the entire purpose of latching it to disk.
    """
    store.write(config.settings.risk_state_path, state)


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


def record_cost(cost_cents):
    """Add deployed cost to the day WITHOUT counting a new trade.

    For subsequent partial fills on a resting order: the same order, so the same
    trade for pacing purposes, but real additional money committed.
    """
    state = load_state()
    state["cost_today_cents"] = int(state.get("cost_today_cents", 0)) + int(cost_cents or 0)
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
    # Note this blocks ADDING to a live position, not re-entering after one
    # closes. Dip re-entry is sequential by design: exit, then look for the
    # next dislocation. Averaging into an open losing dip lot is how a bad
    # read becomes a bad day.
    if ticker and ticker in (open_tickers or []):
        return False, f"Already holding {ticker}", None

    # ---- lane-specific pacing -------------------------------------------
    # The trend lane's caps exist to stop momentum re-chasing: after a stop,
    # the same wrong read tends to fire again within seconds. A dip lane is
    # the opposite case -- several genuine dislocations inside one 15-minute
    # window is normal, and it is precisely the re-entry the manual strategy
    # depends on. So the dip lane gets its own, looser, but still bounded
    # allowance rather than an exemption.
    is_dip = bool(signal.get("dip"))
    lane = signal.get("lane") or ("dip" if is_dip else "trend")
    entry_cap = cfg.dip_max_entries_per_market if is_dip else cfg.max_entries_per_market
    cooldown = cfg.dip_cooldown_seconds if is_dip else cfg.cooldown_seconds
    stop_cooldown = cfg.dip_stop_cooldown_seconds if is_dip else cfg.stop_cooldown_seconds

    attempts = int((state.get("ticker_attempts") or {}).get(ticker) or 0)
    if ticker and attempts >= entry_cap:
        return False, (
            f"Market attempt cap: already entered {ticker} {attempts}x "
            f"(max {entry_cap} per market on the {lane} lane)"
        ), None

    elapsed = time.time() - float(state.get("last_trade_at") or 0)
    if elapsed < cooldown:
        return False, f"Cooldown active ({int(cooldown - elapsed)}s remaining)", None

    since_stop = time.time() - float(state.get("last_stop_at") or 0)
    if since_stop < stop_cooldown:
        return False, (
            f"Post-stop cooldown ({int(stop_cooldown - since_stop)}s "
            f"remaining before {lane} entries resume)"
        ), None

    price = int(signal.get("price_cents") or 0)
    if price <= 0:
        return False, "Signal carries no executable price", None

    # ---- conviction sizing ----------------------------------------------
    # The budget for THIS trade, before any of the hard caps below. A
    # marginal-but-valid signal buys the small version; a fully-confirmed one
    # buys the large version. This only ever narrows the range between MIN and
    # MAX cost -- it cannot exceed MAX_COST_PER_TRADE_CENTS, and every risk
    # cap after it still applies.
    conviction = signal.get("conviction")
    budget_cents = cfg.max_cost_per_trade_cents
    if cfg.conviction_sizing and isinstance(conviction, (int, float)):
        span = cfg.max_cost_per_trade_cents - cfg.min_cost_per_trade_cents
        scaled = cfg.min_cost_per_trade_cents + span * max(0.0, min(100.0, float(conviction))) / 100.0
        budget_cents = int(min(cfg.max_cost_per_trade_cents, max(cfg.min_cost_per_trade_cents, scaled)))

    count = min(
        cfg.max_contracts_per_trade,
        budget_cents // price,
    )

    # ---- what the book can actually supply -------------------------------
    # Entries are fill-or-kill: the whole size must be available at or inside
    # the limit price, or nothing happens at all. Sizing purely from the budget
    # asked for contracts that were not there, so the order was killed and the
    # bot logged "unfilled" and moved on. Cap the request at the depth we are
    # willing to reach, and price it at the worst level we would touch so the
    # order can actually complete.
    levels = [
        (int(level[0]), int(level[1]))
        for level in (signal.get("entry_levels") or [])
        if isinstance(level, (list, tuple)) and len(level) >= 2
    ]
    reachable = policy.depth_within(levels, cfg.max_entry_slippage_cents)

    if cfg.depth_aware_sizing and levels:
        if reachable < 1:
            return False, "No resting offers within the entry slippage cap", None
        count = min(count, reachable)

    if balance_cents is not None:
        count = min(count, int(balance_cents) // price)

    # Bound the worst case of THIS trade, not just its cost: contracts times
    # the stop distance is what a stop-out actually takes from the account.
    # The signal carries the volatility-scaled stop this entry will trade
    # with; a wider stop means fewer contracts for the same risk budget.
    stop_cents = int(signal.get("stop_cents") or cfg.stop_cents)
    if balance_cents is not None and stop_cents > 0:
        # The strategy split: favorites risk their full premium (stop == ask)
        # against their own budget slice; scalps risk their stop distance
        # against theirs. Each strategy is sized independently.
        risk_pct = cfg.fav_risk_pct if signal.get("favorite") else cfg.per_trade_risk_pct
        risk_budget = int(balance_cents) * risk_pct // 100
        count = min(count, max(0, risk_budget // stop_cents))

    # Never size beyond what the exit side can absorb. Entering 6 contracts
    # against a 2-lot bid means the ladder cannot sell what it just bought.
    # The exit-liquidity cap protects round trips; a late settlement snipe
    # never exits, so the resting bid's depth is irrelevant to it.
    exit_size = signal.get("exit_bid_size")
    holds_to_settlement = signal.get("late_settlement") or signal.get("favorite")
    if not holds_to_settlement and isinstance(exit_size, int) and exit_size > 0:
        count = min(count, exit_size)

    if count < 1:
        return False, (
            f"Cost cap, balance, or exit liquidity allows fewer than 1 contract "
            f"(budget {budget_cents}c at {price}c/contract)"
        ), None

    # ---- price the order against the ladder, not the top of book ----------
    # The edge was measured against the best offer. Filling more contracts than
    # rest there means paying an AVERAGE price worse than that, which spends
    # part of the edge before the position even exists. Shrink until the edge
    # at the average fill price still clears the fee floor. Average price is
    # non-decreasing in size, so the largest size that clears is found by
    # walking down; if nothing clears, the trade is refused rather than taken
    # at a price the model never approved.
    fill = None
    if cfg.depth_aware_sizing and levels:
        fair_prob = signal.get("fair_prob")
        min_edge = signal.get("min_required_edge_cents")
        holds_to_settlement = signal.get("late_settlement") or signal.get("favorite")

        while count >= 1:
            candidate = policy.sweep(levels, count)
            if candidate["filled"] < count:
                # Should not happen after the depth cap, but never send an
                # order the book cannot fill.
                count = candidate["filled"]
                continue
            if candidate["cost_cents"] > budget_cents:
                count -= 1
                continue
            if (
                isinstance(fair_prob, (int, float))
                and isinstance(min_edge, (int, float))
                and not holds_to_settlement
            ):
                edge_at_fill = float(fair_prob) * 100 - candidate["avg_price_cents"]
                if edge_at_fill < float(min_edge):
                    count -= 1
                    continue
            fill = candidate
            break

        if count < 1 or fill is None:
            return False, (
                "Edge does not survive the average fill price at any size "
                "(the book is too thin to enter without giving the edge back)"
            ), None

    cost_cents = fill["cost_cents"] if fill else count * price
    limit_price = fill["worst_price_cents"] if fill else price
    avg_price = fill["avg_price_cents"] if fill else float(price)

    return True, "Within limits", {
        "count": count,
        "cost_cents": cost_cents,
        "budget_cents": budget_cents,
        "conviction": conviction,
        "lane": lane,
        # The limit the order must carry for a fill-or-kill of this size to
        # complete: the worst level it will touch, not the best.
        "limit_price_cents": limit_price,
        "avg_price_cents": avg_price,
        "slippage_cents": round(avg_price - price, 3),
        "est_entry_fee_cents": round(fill["fee_cents"], 2) if fill else None,
        "book_depth_reachable": reachable,
    }