"""
Every tunable in one place, driven by environment variables so limits can be
changed in Railway without a code change.

This module is the single source of truth. Earlier versions read os.getenv
directly from policy.py and risk.py as well, which produced two different names
for the same idea (MIN_EDGE as a probability in one file, MIN_EDGE_CENTS as
cents in another). Everything now reads config.settings.

Nothing here has a permissive default. TRADING_MODE defaults to "off",
KALSHI_ENV to "demo", and every size limit defaults small. Widening the
system's authority is always an explicit act.

Import as `import config` and read `config.settings.x` at call time, so that
reload() is visible to callers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _str(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(float(_str(name, str(default))))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

VALID_MODES = ("off", "dryrun", "live")
TIER_NAMES = ("small", "medium", "large")


@dataclass(frozen=True)
class Tier:
    """One rung of the profit ladder.

    cents: gain per contract, measured against the entry price, that arms this
           rung.
    pct:   share of the ORIGINAL position size to sell when it arms.
    """

    name: str
    cents: int
    pct: int


@dataclass
class Settings:
    # ---- environment / credentials -------------------------------------
    kalshi_env: str = field(default_factory=lambda: _str("KALSHI_ENV", "demo").lower())
    key_id: str = field(default_factory=lambda: _str("KALSHI_API_KEY_ID", ""))
    private_key_pem: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY", ""))
    series_ticker: str = field(default_factory=lambda: _str("KALSHI_SERIES_TICKER", "KXBTC15M"))
    order_path: str = field(default_factory=lambda: _str("KALSHI_ORDER_PATH", "/portfolio/events/orders"))
    admin_token: str = field(default_factory=lambda: _str("ADMIN_TOKEN", ""))
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "./data"))

    # ---- autonomy ------------------------------------------------------
    # off    - evaluate nothing, place nothing (default)
    # dryrun - full decision path incl. simulated round trips, places nothing
    # live   - places real orders
    trading_mode: str = field(default_factory=lambda: _str("TRADING_MODE", "off").lower())

    # ---- entry ---------------------------------------------------------
    # A scalper takes a smaller edge than a hold-to-settlement trader, because
    # it is not asking to be right at expiry -- only to be right by a few cents
    # for a few minutes.
    min_edge_cents: float = field(default_factory=lambda: _float("MIN_EDGE_CENTS", 2.0))
    min_confidence: int = field(default_factory=lambda: _int("MIN_CONFIDENCE", 55))
    max_spread_cents: int = field(default_factory=lambda: _int("MAX_SPREAD_CENTS", 4))
    min_exit_liquidity: int = field(default_factory=lambda: _int("MIN_EXIT_LIQUIDITY", 25))
    min_price_cents: int = field(default_factory=lambda: _int("MIN_PRICE_CENTS", 15))
    max_price_cents: int = field(default_factory=lambda: _int("MAX_PRICE_CENTS", 85))
    min_seconds_to_close: int = field(default_factory=lambda: _int("MIN_SECONDS_TO_CLOSE", 180))
    max_seconds_to_close: int = field(default_factory=lambda: _int("MAX_SECONDS_TO_CLOSE", 900))
    vol_window_seconds: int = field(default_factory=lambda: _int("VOL_WINDOW_SECONDS", 300))
    min_history_seconds: int = field(default_factory=lambda: _int("MIN_HISTORY_SECONDS", 120))
    stale_feed_seconds: float = field(default_factory=lambda: _float("STALE_FEED_SECONDS", 5.0))

    # ---- consolidated spot feeds ----------------------------------------
    # Fair value is a function of spot, so a single exchange feed is a single
    # point of failure: one bad tick, one stalled socket, or one venue-local
    # wick all manufacture edge that exists nowhere else. SPOT_FEEDS lists the
    # venues to consolidate (coinbase, binanceus, kraken -- all reachable with
    # low latency from a US region); the tape the model reads is the MEDIAN of
    # whichever of them are currently fresh.
    spot_feeds: str = field(
        default_factory=lambda: _str("SPOT_FEEDS", "coinbase,binanceus,kraken").lower()
    )
    # When the fresh sources disagree by more than this, stand down. Real
    # divergence between major venues at 15-minute-option scale is a data
    # problem, not an opportunity, and it is the exact condition under which
    # a fair value is least trustworthy.
    spot_divergence_bps: float = field(
        default_factory=lambda: _float("SPOT_DIVERGENCE_BPS", 8.0)
    )
    # Minimum number of fresh sources required to trade at all. 2 means a
    # lone surviving feed can no longer be believed unchallenged; 1 keeps the
    # bot trading on a single feed as before.
    spot_min_sources: int = field(default_factory=lambda: _int("SPOT_MIN_SOURCES", 2))

    # ---- fees ------------------------------------------------------------
    # Kalshi charges ceil(0.07 * C * P * (1-P) * 100) cents per TAKER fill.
    # Maker (resting) fills on this series (fee_type "quadratic", per the
    # published fee schedule) are charged the taker formula scaled by
    # MAKER_FEE_MULT, which is 0 for KXBTC15M -- resting orders trade free.
    # The safety margin adds a cushion above the raw fee estimate before an
    # edge is considered tradable, so a fee-rounding quirk or a slightly-stale
    # price doesn't turn a marginal "profitable" trade into a guaranteed loss.
    fee_safety_margin_cents: float = field(
        default_factory=lambda: _float("FEE_SAFETY_MARGIN_CENTS", 0.5)
    )
    maker_fee_multiplier: float = field(default_factory=lambda: _float("MAKER_FEE_MULT", 0.0))

    # ---- execution style -------------------------------------------------
    # maker: entries rest as post-only bids and ladder exits rest as post-only
    #        asks, paying zero fees on this series and earning the spread
    #        instead of paying it. Stops and guards always cross as takers --
    #        an exit that must happen now cannot wait in a queue.
    # taker: the previous behaviour (FOK entries, IOC exits) as a fallback.
    entry_style: str = field(default_factory=lambda: _str("ENTRY_STYLE", "maker").lower())
    exit_style: str = field(default_factory=lambda: _str("EXIT_STYLE", "maker").lower())
    # ---- which exit engine owns a winner --------------------------------
    # ladder: the original three fixed rungs (sell 50% at +3c, 30% at +7c,
    #         20% at +14c). Books profit early and often -- and caps every
    #         winner at the size of its smallest rung.
    # runner: no fixed rungs. A winner is held behind a volatility-scaled
    #         trailing stop that tightens as the peak grows, is not cut by
    #         max-hold while it is still working, and rides to fee-free
    #         settlement when it goes deep in the money. Bigger average
    #         winner, lower win rate, and strictly more variance -- that is
    #         the trade being made, not a free upgrade.
    exit_profile: str = field(default_factory=lambda: _str("EXIT_PROFILE", "runner").lower())
    # How far above the current bid a maker entry may improve to gain queue
    # priority. Clamped so it never locks or crosses the book.
    maker_improve_cents: int = field(default_factory=lambda: _int("MAKER_IMPROVE_CENTS", 1))
    # How long a maker entry rests before Kalshi auto-cancels it. Short on
    # purpose: a 15-minute market moves, and an entry that has not filled in
    # this window was priced for a book that no longer exists.
    entry_rest_seconds: int = field(default_factory=lambda: _int("ENTRY_REST_SECONDS", 20))
    # Cancel a resting maker entry when the model's fair value moves this
    # many cents against it while it rests. A resting bid that only fills
    # after the tape turns is not a fill, it is adverse selection -- several
    # live stops fired 3-6 seconds after the maker fill for exactly this
    # reason.
    entry_cancel_adverse_cents: int = field(
        default_factory=lambda: _int("ENTRY_CANCEL_ADVERSE_CENTS", 4)
    )

    # ---- the profit ladder ---------------------------------------------
    # Three targets, hit in order, each selling part of the position. The
    # small rung banks something quickly and takes the trade off risk; the
    # large rung is what pays for the stops.
    small_cents: int = field(default_factory=lambda: _int("SCALP_SMALL_CENTS", 3))
    small_pct: int = field(default_factory=lambda: _int("SCALP_SMALL_PCT", 50))
    medium_cents: int = field(default_factory=lambda: _int("SCALP_MEDIUM_CENTS", 7))
    medium_pct: int = field(default_factory=lambda: _int("SCALP_MEDIUM_PCT", 30))
    large_cents: int = field(default_factory=lambda: _int("SCALP_LARGE_CENTS", 14))
    large_pct: int = field(default_factory=lambda: _int("SCALP_LARGE_PCT", 20))

    # ---- chart analysis --------------------------------------------------
    # The chart read (indicators.py) that gates every entry. EMAs on bar
    # closes define the trend; RSI defines exhaustion. An entry must agree
    # with the trend and must not chase an exhausted move.
    bar_seconds: int = field(default_factory=lambda: _int("CHART_BAR_SECONDS", 5))
    ema_fast_seconds: int = field(default_factory=lambda: _int("EMA_FAST_SECONDS", 60))
    ema_slow_seconds: int = field(default_factory=lambda: _int("EMA_SLOW_SECONDS", 240))
    rsi_period: int = field(default_factory=lambda: _int("RSI_PERIOD", 14))
    rsi_overbought: float = field(default_factory=lambda: _float("RSI_OVERBOUGHT", 70.0))
    rsi_oversold: float = field(default_factory=lambda: _float("RSI_OVERSOLD", 30.0))
    # EMA separation, in basis points of price, below which the trend call is
    # 'flat'. Keeps a crossing EMA pair from flapping the call bar to bar.
    trend_deadzone_bps: float = field(default_factory=lambda: _float("TREND_DEADZONE_BPS", 2.0))
    # Support/resistance: entries are refused when spot sits just below the
    # recent high (BUY YES into a ceiling) or just above the recent low
    # (BUY NO into a floor). "Just" is measured in units of the tape's own
    # expected one-minute move: SR_BUFFER_SIGMA x sigma(60s) x spot. Spot AT
    # or BEYOND the level is a break, the opposite read, and is not blocked.
    sr_lookback_seconds: int = field(default_factory=lambda: _int("SR_LOOKBACK_SECONDS", 600))
    sr_buffer_sigma: float = field(default_factory=lambda: _float("SR_BUFFER_SIGMA", 1.0))
    # Chart-flip exit: when the EMA trend turns against an open lot that is
    # not in profit, cut it immediately instead of donating the rest of the
    # stop distance. CHART_EXIT=0 disables; the max-gain bound keeps this
    # away from winners (the trail and the ladder own those).
    chart_exit: int = field(default_factory=lambda: _int("CHART_EXIT", 1))
    chart_exit_max_gain_cents: int = field(
        default_factory=lambda: _int("CHART_EXIT_MAX_GAIN_CENTS", 0)
    )
    # ---- late settlement window -----------------------------------------
    # Inside MIN_SECONDS_TO_CLOSE a stop is fiction: there is no time or
    # book left to exit into, so a wrong entry loses its entire premium.
    # The only trade taken there is the settlement snipe: a near-certainty
    # (model prob >= LATE_MIN_FAIR_PROB, trend agreeing) bought at a
    # discount (ask <= LATE_MAX_PRICE_CENTS), entered as a taker, sized
    # against its FULL premium as the risk, and held to fee-free settlement
    # -- never exited early. LATE_ENTRY=0 disables; the final
    # LATE_MIN_SECONDS are always off-limits (orders need time to land).
    late_entry: int = field(default_factory=lambda: _int("LATE_ENTRY", 1))
    late_min_seconds: int = field(default_factory=lambda: _int("LATE_MIN_SECONDS", 45))
    late_min_fair_prob: float = field(default_factory=lambda: _float("LATE_MIN_FAIR_PROB", 0.93))
    late_max_price_cents: int = field(default_factory=lambda: _int("LATE_MAX_PRICE_CENTS", 96))

    # Favorite buying: when the scalp path declines a market, buy the strong
    # side at 75-92c if the model still says it is underpriced, rest a free
    # maker bid, and hold to fee-free settlement. High win rate, full premium
    # at risk on each loss -- the other half of the strategy split.
    favorite_entry: int = field(default_factory=lambda: _int("FAV_ENTRY", 1))
    fav_min_price_cents: int = field(default_factory=lambda: _int("FAV_MIN_PRICE_CENTS", 75))
    fav_max_price_cents: int = field(default_factory=lambda: _int("FAV_MAX_PRICE_CENTS", 92))
    fav_min_edge_cents: float = field(default_factory=lambda: _float("FAV_MIN_EDGE_CENTS", 1.5))
    fav_min_fair_prob: float = field(default_factory=lambda: _float("FAV_MIN_FAIR_PROB", 0.80))
    fav_risk_pct: int = field(default_factory=lambda: _int("FAV_RISK_PCT", 20))

    # ---- the exits that are not profits --------------------------------
    # stop_cents is the FALLBACK stop, used when a lot carries no stop of its
    # own (adopted positions, lots from before this feature). New entries get
    # a volatility-scaled stop: STOP_VOL_MULT x the expected one-minute
    # contract move, clamped to [STOP_MIN_CENTS, STOP_MAX_CENTS]. A fixed
    # stop is noise-clipped in a fast tape and dead weight in a slow one.
    stop_cents: int = field(default_factory=lambda: _int("SCALP_STOP_CENTS", 6))
    stop_min_cents: int = field(default_factory=lambda: _int("STOP_MIN_CENTS", 6))
    stop_max_cents: int = field(default_factory=lambda: _int("STOP_MAX_CENTS", 14))
    stop_vol_mult: float = field(default_factory=lambda: _float("STOP_VOL_MULT", 1.5))
    trail_cents: int = field(default_factory=lambda: _int("SCALP_TRAIL_CENTS", 3))
    max_hold_seconds: int = field(default_factory=lambda: _int("SCALP_MAX_HOLD_SECONDS", 300))
    settlement_guard_seconds: int = field(
        default_factory=lambda: _int("SCALP_SETTLEMENT_GUARD_SECONDS", 90)
    )
    exit_slippage_cents: int = field(default_factory=lambda: _int("SCALP_EXIT_SLIPPAGE_CENTS", 0))
    exit_tif: str = field(default_factory=lambda: _str("SCALP_EXIT_TIF", "immediate_or_cancel"))
    # A position too small to split three ways exits whole at this rung.
    small_lot_exit_tier: str = field(
        default_factory=lambda: _str("SCALP_SMALL_LOT_EXIT_TIER", "medium").lower()
    )
    # ---- riding winners to settlement ------------------------------------
    # Settlement pays the full 100c fee-free. A position that is deep in the
    # money when the settlement guard would normally flatten it (bid at or
    # above SETTLE_RIDE_MIN_BID_CENTS and in profit) is held to settlement
    # instead of sold: selling at 85c keeps 85c minus taker fees, settling
    # keeps 100c. The ride is re-checked every tick -- if the bid slips below
    # the floor, the position is flattened immediately with whatever book
    # remains. SETTLE_RIDE=0 disables.
    settle_ride: int = field(default_factory=lambda: _int("SETTLE_RIDE", 1))
    settle_ride_min_bid_cents: int = field(
        default_factory=lambda: _int("SETTLE_RIDE_MIN_BID_CENTS", 80)
    )

    # ---- size and risk limits ------------------------------------------
    # The BINDING size constraint is the cost cap below, not this contract
    # count. At a 40c ask a $20 position is 50 contracts, so a 6-contract cap
    # silently turned every "$20" trade into a $2.40 one. This stays as a
    # backstop against a fat-fingered cost cap at a 1c ask, nothing more.
    # ---- depth-aware execution -------------------------------------------
    # Entries are fill-or-kill, so the ENTIRE requested size must be available
    # at or inside the limit price or the order is killed and nothing happens.
    # Sizing from the budget alone ("$20 / 45c = 44 contracts") ignores whether
    # 44 contracts exist there. They usually do not on a 15-minute market, so
    # the order was killed, logged as unfilled, and the bot moved on believing
    # it had tried. With depth-aware sizing the order asks for what the book
    # can actually supply.
    depth_aware_sizing: int = field(default_factory=lambda: _int("DEPTH_AWARE_SIZING", 1))
    # How far up the ask ladder an entry may reach. Depth three cents above the
    # best offer is depth we do not want: it is what turns "size to the book"
    # into "sweep the book" and hands the edge back as slippage.
    max_entry_slippage_cents: int = field(
        default_factory=lambda: _int("MAX_ENTRY_SLIPPAGE_CENTS", 2)
    )
    # The same question for the exit: how deep into the bid ladder we are
    # willing to count as liquidity we can actually get out through. Wider than
    # the entry cap, because being unable to exit is worse than paying to.
    exit_depth_slippage_cents: int = field(
        default_factory=lambda: _int("EXIT_DEPTH_SLIPPAGE_CENTS", 3)
    )
    max_contracts_per_trade: int = field(default_factory=lambda: _int("MAX_CONTRACTS_PER_TRADE", 60))
    max_cost_per_trade_cents: int = field(
        default_factory=lambda: _int("MAX_COST_PER_TRADE_CENTS", 2000)
    )
    # ---- conviction sizing ----------------------------------------------
    # Every entry carries a 0-100 conviction score (policy.py). With
    # CONVICTION_SIZING on, budgeted cost scales linearly from MIN_ to
    # MAX_COST_PER_TRADE_CENTS across that range, so a marginal signal takes
    # the $5 version of the trade and only a fully-confirmed one takes the
    # $20 version. All the hard risk caps still apply on top -- this can only
    # ever spend LESS than they allow, never more.
    conviction_sizing: int = field(default_factory=lambda: _int("CONVICTION_SIZING", 1))
    min_cost_per_trade_cents: int = field(
        default_factory=lambda: _int("MIN_COST_PER_TRADE_CENTS", 500)
    )
    # $50. Deliberately 2.5x max position size ($20): a single full-size loss
    # must not be able to halt the whole day, or the halt stops being a
    # circuit breaker and becomes a one-strike rule. Still capped in practice
    # by DAILY_LOSS_LIMIT_PCT of the day's opening balance (see risk.py), so a
    # small account is protected by the percentage, not this number.
    daily_loss_limit_cents: int = field(default_factory=lambda: _int("DAILY_LOSS_LIMIT_CENTS", 5000))
    # The absolute daily loss limit above is meaningless when it exceeds the
    # account balance. The effective limit is the SMALLER of the absolute cap
    # and this percentage of the day's opening balance, so a small account is
    # bounded by a number it can actually feel.
    daily_loss_limit_pct: int = field(default_factory=lambda: _int("DAILY_LOSS_LIMIT_PCT", 25))
    # Cap the worst-case loss of a single trade (contracts x stop distance) to
    # a fraction of the balance, so one stop-out cannot take a large bite.
    per_trade_risk_pct: int = field(default_factory=lambda: _int("PER_TRADE_RISK_PCT", 15))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 40))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 2))
    cooldown_seconds: int = field(default_factory=lambda: _int("COOLDOWN_SECONDS", 20))
    # After a stop-out, no new entries for this long. The 15:48-15:50 pattern
    # this exists for: stop, re-enter the same direction 30 seconds later,
    # stop again, re-enter again -- three stops inside two minutes of chop.
    stop_cooldown_seconds: int = field(default_factory=lambda: _int("STOP_COOLDOWN_SECONDS", 120))
    # A market that keeps stopping us out is telling us the signal is wrong
    # there right now. Cap entries per individual market so one chopping
    # 15-minute window cannot be re-tried into the ground (the observed
    # pattern: four entries into the same market, four stops).
    max_entries_per_market: int = field(default_factory=lambda: _int("MAX_ENTRIES_PER_MARKET", 2))

    # ---- the contract's own chart ---------------------------------------
    # booktape.py records the Kalshi contract price itself, which nothing in
    # this bot used to do -- every read was computed from BTC spot. These
    # govern that tape and the dip reads derived from it.
    tape_seconds: int = field(default_factory=lambda: _int("TAPE_SECONDS", 900))
    # Below this many samples in the lookback the tape is still warming and
    # every read returns None. A warming tape must never look like a signal.
    tape_min_samples: int = field(default_factory=lambda: _int("TAPE_MIN_SAMPLES", 12))

    # ---- dip lanes -------------------------------------------------------
    # The manual trade this bot could not previously express: a sharp drop in
    # the CONTRACT price that spot does not justify, bought as it decelerates
    # and held for the recovery. Two variants, separately switchable.
    #
    # dislocation: the contract fell hard while spot barely moved. The edge is
    #              mechanical (someone dumped into a thin book) and the model's
    #              fair value is the reference. This is the higher-quality lane.
    # reversion:   spot itself moved and the contract overshot it -- oversold
    #              or overbought on the spot read, now stabilising. This is
    #              genuinely fading a move, so it demands a bigger drop and
    #              carries lower conviction by construction.
    dip_entry: int = field(default_factory=lambda: _int("DIP_ENTRY", 1))
    dip_dislocation: int = field(default_factory=lambda: _int("DIP_DISLOCATION", 1))
    dip_reversion: int = field(default_factory=lambda: _int("DIP_REVERSION", 1))
    # Window over which the "recent high" a dip is measured against is found.
    dip_lookback_seconds: int = field(default_factory=lambda: _int("DIP_LOOKBACK_SECONDS", 90))
    # How far the contract mid must sit below that rolling high to count.
    dip_min_drop_cents: float = field(default_factory=lambda: _float("DIP_MIN_DROP_CENTS", 6.0))
    # The reversion lane fades a real move, so it needs a deeper dip.
    dip_reversion_min_drop_cents: float = field(
        default_factory=lambda: _float("DIP_REVERSION_MIN_DROP_CENTS", 10.0)
    )
    # Model edge still required after fees. A dip with no edge is just a
    # correctly-repriced contract, and buying it is buying a falling knife
    # for its own sake.
    dip_min_edge_cents: float = field(default_factory=lambda: _float("DIP_MIN_EDGE_CENTS", 3.0))
    # Dislocation test: spot must have moved LESS than this over the lookback.
    # If spot really did fall, the contract price is right and there is no
    # dislocation to collect.
    dip_max_spot_move_bps: float = field(
        default_factory=lambda: _float("DIP_MAX_SPOT_MOVE_BPS", 12.0)
    )
    # Timing. Require the fall to have stopped before buying it: the fast
    # velocity window must be flat or turning up while the slower one is still
    # negative. DIP_DECEL_TOLERANCE is how much residual fall still counts as
    # "stopped", in cents/second.
    dip_require_decel: int = field(default_factory=lambda: _int("DIP_REQUIRE_DECEL", 1))
    dip_stabilize_seconds: float = field(
        default_factory=lambda: _float("DIP_STABILIZE_SECONDS", 3.0)
    )
    dip_decel_tolerance: float = field(default_factory=lambda: _float("DIP_DECEL_TOLERANCE", 0.15))
    # Resting size on our side minus theirs, normalised. Above zero means the
    # bid is rebuilding underneath the dip rather than evaporating.
    dip_min_imbalance: float = field(default_factory=lambda: _float("DIP_MIN_IMBALANCE", 0.0))
    # A dip needs room to recover; buying one with two minutes left is buying
    # a coin flip with a spread attached.
    dip_min_seconds_to_close: int = field(
        default_factory=lambda: _int("DIP_MIN_SECONDS_TO_CLOSE", 180)
    )
    # Dips repeat inside one 15-minute window, which is the whole point of
    # re-entry. These are the dip lane's OWN limits, replacing the trend
    # lane's MAX_ENTRIES_PER_MARKET / STOP_COOLDOWN_SECONDS, which exist to
    # stop momentum re-chasing and would otherwise block the second dip.
    dip_max_entries_per_market: int = field(
        default_factory=lambda: _int("DIP_MAX_ENTRIES_PER_MARKET", 4)
    )
    dip_cooldown_seconds: int = field(default_factory=lambda: _int("DIP_COOLDOWN_SECONDS", 8))
    dip_stop_cooldown_seconds: int = field(
        default_factory=lambda: _int("DIP_STOP_COOLDOWN_SECONDS", 30)
    )
    # Dips are volatile by definition; a dip lot gets a wider stop so normal
    # post-dip chop does not shake it out before the recovery.
    dip_stop_mult: float = field(default_factory=lambda: _float("DIP_STOP_MULT", 1.4))

    # ---- runner exits ----------------------------------------------------
    # Active when EXIT_PROFILE=runner. The trail arms once a lot is
    # RUNNER_ARM_CENTS in profit, then sits RUNNER_TRAIL_FRAC of the peak gain
    # behind the peak, clamped to [MIN, MAX]. Early on that is a loose leash;
    # as the peak grows the absolute give-back grows but the fraction
    # surrendered shrinks -- which is what "let it run, then protect it"
    # actually means numerically.
    runner_arm_cents: int = field(default_factory=lambda: _int("RUNNER_ARM_CENTS", 4))
    runner_trail_frac: float = field(default_factory=lambda: _float("RUNNER_TRAIL_FRAC", 0.35))
    runner_trail_min_cents: int = field(default_factory=lambda: _int("RUNNER_TRAIL_MIN_CENTS", 5))
    runner_trail_max_cents: int = field(default_factory=lambda: _int("RUNNER_TRAIL_MAX_CENTS", 12))
    # When the chart flips against a WINNING lot, tighten the trail by this
    # factor instead of dumping the position. A flip is a warning, not a
    # verdict, and cutting a winner on it is how the ladder capped winners.
    runner_flip_tighten: float = field(default_factory=lambda: _float("RUNNER_FLIP_TIGHTEN", 0.5))
    # Do not let SCALP_MAX_HOLD_SECONDS flatten a position that is in profit
    # with the trend still onside. Max-hold exists to kill stagnant lots, and
    # a lot that is winning is not stagnant.
    runner_hold_winners: int = field(default_factory=lambda: _int("RUNNER_HOLD_WINNERS", 1))
    # Optional single de-risk rung, off by default because it is exactly the
    # cent-scalping the runner profile exists to stop. Set the percentage
    # above zero to sell that slice once the gain reaches PARTIAL_CENTS.
    runner_partial_pct: int = field(default_factory=lambda: _int("RUNNER_PARTIAL_PCT", 0))
    runner_partial_cents: int = field(default_factory=lambda: _int("RUNNER_PARTIAL_CENTS", 10))

    # ---- chop filter -----------------------------------------------------
    # Kaufman efficiency ratio of the BTC tape: |net move| / sum of |one-
    # second moves| over the window. 1.0 is a straight line; a pure random
    # walk over N one-second samples scores about sqrt(pi / (2N)) -- roughly
    # 0.09 for a 180-second window. A momentum signal only has an edge when
    # the tape is actually going somewhere, so entries require clearly more
    # directionality than noise.
    chop_window_seconds: int = field(default_factory=lambda: _int("CHOP_WINDOW_SECONDS", 180))
    min_efficiency_ratio: float = field(default_factory=lambda: _float("MIN_EFFICIENCY_RATIO", 0.20))

    # ---- loop ----------------------------------------------------------
    # The loop no longer sleeps its way through a move. It waits on the book
    # event published by the WebSocket feed and wakes on the first change,
    # falling back to this interval when the book is quiet. At 2.0s the bot
    # could not see a dip that completes in four seconds; that was the single
    # largest cause of missed entries and late exits.
    loop_seconds: float = field(default_factory=lambda: _float("TRADE_LOOP_SECONDS", 0.25))
    # Minimum gap between ticks, regardless of how fast the book is updating.
    # An event-driven loop with no floor spins as fast as the busiest market
    # will let it: on a fast tape the wake event is already set again before the
    # tick finishes, so the loop never sleeps, burns a core, and adds scheduling
    # jitter to the exit path it was supposed to speed up. 20ms is far below
    # any timescale the strategy reasons about and still bounds the spin.
    loop_min_seconds: float = field(
        default_factory=lambda: _float("TRADE_LOOP_MIN_SECONDS", 0.02)
    )
    # Order-status polling is the one thing in the loop that costs API calls,
    # so it keeps its own floor independent of the loop cadence.
    pending_poll_seconds: float = field(default_factory=lambda: _float("PENDING_POLL_SECONDS", 0.5))
    book_poll_seconds: float = field(default_factory=lambda: _float("BOOK_POLL_SECONDS", 2.0))
    reconcile_seconds: float = field(default_factory=lambda: _float("RECONCILE_SECONDS", 30.0))
    # How stale the in-memory WebSocket book may be before the trader falls
    # back to a REST fetch. Exits used to read a 1.5s-cached REST book, which
    # meant every stop and trail decision was made on a price that could be
    # a second and a half old.
    book_max_age_seconds: float = field(
        default_factory=lambda: _float("BOOK_MAX_AGE_SECONDS", 2.0)
    )

    # ---- derived -------------------------------------------------------
    @property
    def base_url(self) -> str:
        return PROD_BASE_URL if self.kalshi_env == "prod" else DEMO_BASE_URL

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def is_enabled(self) -> bool:
        return self.trading_mode in ("dryrun", "live")

    def tiers(self) -> list[Tier]:
        return [
            Tier("small", self.small_cents, self.small_pct),
            Tier("medium", self.medium_cents, self.medium_pct),
            Tier("large", self.large_cents, self.large_pct),
        ]

    def tier(self, name: str) -> Tier:
        for candidate in self.tiers():
            if candidate.name == name:
                return candidate
        return self.tiers()[1]

    def path(self, filename: str) -> Path:
        return Path(self.data_dir) / filename

    @property
    def risk_state_path(self) -> Path:
        return self.path("risk_state.json")

    @property
    def decision_log_path(self) -> Path:
        return self.path("decisions.jsonl")

    def lots_path(self, mode: str) -> Path:
        # Paper lots never share a file with live lots.
        return self.path(f"scalp_lots_{mode}.json")

    def problems(self) -> list[str]:
        """Configuration errors worth surfacing on the dashboard."""
        issues: list[str] = []

        if self.trading_mode not in VALID_MODES:
            issues.append(f"TRADING_MODE must be one of {VALID_MODES}")
        if self.kalshi_env not in ("demo", "prod"):
            issues.append("KALSHI_ENV must be 'demo' or 'prod'")
        if self.is_enabled and not (self.key_id and self.private_key_pem):
            issues.append("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY are required to trade")
        if self.is_live and not self.admin_token:
            issues.append("ADMIN_TOKEN is required in live mode so the kill switch is reachable")
        if self.max_contracts_per_trade < 1:
            issues.append("MAX_CONTRACTS_PER_TRADE must be at least 1")

        small, medium, large = self.tiers()
        if not small.cents < medium.cents < large.cents:
            issues.append("Scalp targets must increase: small < medium < large")
        if small.pct + medium.pct + large.pct != 100:
            issues.append("SCALP_SMALL_PCT + SCALP_MEDIUM_PCT + SCALP_LARGE_PCT must equal 100")
        if self.stop_cents < 1:
            issues.append("SCALP_STOP_CENTS must be at least 1")
        if self.small_lot_exit_tier not in TIER_NAMES:
            issues.append(f"SCALP_SMALL_LOT_EXIT_TIER must be one of {TIER_NAMES}")
        if self.settlement_guard_seconds >= self.min_seconds_to_close:
            issues.append(
                "MIN_SECONDS_TO_CLOSE must exceed SCALP_SETTLEMENT_GUARD_SECONDS, "
                "or every entry is flattened immediately"
            )
        if self.stop_cents <= self.max_spread_cents:
            issues.append(
                "SCALP_STOP_CENTS is inside MAX_SPREAD_CENTS: the stop can trigger on "
                "spread alone, before price moves"
            )
        if self.entry_style not in ("maker", "taker"):
            issues.append("ENTRY_STYLE must be 'maker' or 'taker'")
        if self.exit_style not in ("maker", "taker"):
            issues.append("EXIT_STYLE must be 'maker' or 'taker'")
        if not 0 <= self.maker_fee_multiplier <= 1:
            issues.append("MAKER_FEE_MULT must be between 0 and 1")
        if not 1 <= self.daily_loss_limit_pct <= 100:
            issues.append("DAILY_LOSS_LIMIT_PCT must be between 1 and 100")
        if not 1 <= self.per_trade_risk_pct <= 100:
            issues.append("PER_TRADE_RISK_PCT must be between 1 and 100")
        if self.entry_rest_seconds < 2:
            issues.append("ENTRY_REST_SECONDS must be at least 2")
        if self.max_entries_per_market < 1:
            issues.append("MAX_ENTRIES_PER_MARKET must be at least 1")
        if not 0 <= self.min_efficiency_ratio <= 1:
            issues.append("MIN_EFFICIENCY_RATIO must be between 0 and 1")
        if self.chop_window_seconds < 30:
            issues.append("CHOP_WINDOW_SECONDS must be at least 30 to hold enough samples")
        if self.stop_min_cents < 1:
            issues.append("STOP_MIN_CENTS must be at least 1")
        if self.stop_max_cents < self.stop_min_cents:
            issues.append("STOP_MAX_CENTS must be >= STOP_MIN_CENTS")
        if self.stop_vol_mult <= 0:
            issues.append("STOP_VOL_MULT must be positive")
        if self.bar_seconds < 1:
            issues.append("CHART_BAR_SECONDS must be at least 1")
        if self.ema_fast_seconds >= self.ema_slow_seconds:
            issues.append("EMA_FAST_SECONDS must be less than EMA_SLOW_SECONDS")
        if self.ema_fast_seconds < 2 * self.bar_seconds:
            issues.append("EMA_FAST_SECONDS must cover at least 2 bars")
        if self.rsi_period < 2:
            issues.append("RSI_PERIOD must be at least 2")
        if not self.rsi_oversold < self.rsi_overbought:
            issues.append("RSI_OVERSOLD must be below RSI_OVERBOUGHT")
        if not 0 <= self.rsi_oversold <= 100 or not 0 <= self.rsi_overbought <= 100:
            issues.append("RSI thresholds must be between 0 and 100")
        if self.trend_deadzone_bps < 0:
            issues.append("TREND_DEADZONE_BPS must be >= 0")
        if not 50 <= self.settle_ride_min_bid_cents <= 99:
            issues.append("SETTLE_RIDE_MIN_BID_CENTS must be between 50 and 99")
        if self.entry_cancel_adverse_cents < 1:
            issues.append("ENTRY_CANCEL_ADVERSE_CENTS must be at least 1")
        if self.sr_lookback_seconds < 60:
            issues.append("SR_LOOKBACK_SECONDS must be at least 60")
        if self.sr_buffer_sigma < 0:
            issues.append("SR_BUFFER_SIGMA must be >= 0")
        if not 1 <= self.fav_risk_pct <= 100:
            issues.append("FAV_RISK_PCT must be between 1 and 100")
        if not 50 <= self.fav_min_price_cents <= self.fav_max_price_cents <= 99:
            issues.append(
                "FAV_MIN/MAX_PRICE_CENTS must satisfy 50 <= min <= max <= 99"
            )
        if not 0.5 <= self.fav_min_fair_prob <= 0.99:
            issues.append("FAV_MIN_FAIR_PROB must be between 0.5 and 0.99")
        if self.fav_min_edge_cents < 0:
            issues.append("FAV_MIN_EDGE_CENTS cannot be negative")
        if self.late_min_seconds < 15:
            issues.append("LATE_MIN_SECONDS must be at least 15 (orders need time to land)")
        if not 0.5 <= self.late_min_fair_prob <= 0.99:
            issues.append("LATE_MIN_FAIR_PROB must be between 0.5 and 0.99")
        if not 50 <= self.late_max_price_cents <= 99:
            issues.append("LATE_MAX_PRICE_CENTS must be between 50 and 99")

        # ---- consolidated spot feeds ----
        known_feeds = {"coinbase", "binanceus", "kraken"}
        requested = {name.strip() for name in self.spot_feeds.split(",") if name.strip()}
        unknown = requested - known_feeds
        if unknown:
            issues.append(f"SPOT_FEEDS contains unknown sources: {sorted(unknown)}")
        if not requested:
            issues.append("SPOT_FEEDS must list at least one source")
        if self.spot_min_sources < 1:
            issues.append("SPOT_MIN_SOURCES must be at least 1")
        if requested and self.spot_min_sources > len(requested):
            issues.append(
                "SPOT_MIN_SOURCES exceeds the number of feeds in SPOT_FEEDS, "
                "so no entry can ever pass the freshness check"
            )
        if self.spot_divergence_bps <= 0:
            issues.append("SPOT_DIVERGENCE_BPS must be positive")

        # ---- exit profile ----
        if self.exit_profile not in ("runner", "ladder"):
            issues.append("EXIT_PROFILE must be 'runner' or 'ladder'")
        if self.runner_arm_cents < 1:
            issues.append("RUNNER_ARM_CENTS must be at least 1")
        if not 0 < self.runner_trail_frac < 1:
            issues.append("RUNNER_TRAIL_FRAC must be between 0 and 1")
        if self.runner_trail_min_cents < 1:
            issues.append("RUNNER_TRAIL_MIN_CENTS must be at least 1")
        if self.runner_trail_max_cents < self.runner_trail_min_cents:
            issues.append("RUNNER_TRAIL_MAX_CENTS must be >= RUNNER_TRAIL_MIN_CENTS")
        if self.runner_trail_min_cents <= self.max_spread_cents:
            issues.append(
                "RUNNER_TRAIL_MIN_CENTS is inside MAX_SPREAD_CENTS: the trail can "
                "trigger on spread alone"
            )
        if not 0 < self.runner_flip_tighten <= 1:
            issues.append("RUNNER_FLIP_TIGHTEN must be between 0 and 1")
        if not 0 <= self.runner_partial_pct <= 100:
            issues.append("RUNNER_PARTIAL_PCT must be between 0 and 100")
        if self.runner_partial_pct and self.runner_partial_cents < 1:
            issues.append("RUNNER_PARTIAL_CENTS must be at least 1 when a partial is enabled")

        # ---- dip lanes ----
        if self.dip_lookback_seconds < 15:
            issues.append("DIP_LOOKBACK_SECONDS must be at least 15")
        if self.dip_min_drop_cents <= self.max_spread_cents:
            issues.append(
                "DIP_MIN_DROP_CENTS is inside MAX_SPREAD_CENTS: a spread widening "
                "would register as a dip"
            )
        if self.dip_reversion_min_drop_cents < self.dip_min_drop_cents:
            issues.append("DIP_REVERSION_MIN_DROP_CENTS must be >= DIP_MIN_DROP_CENTS")
        if self.dip_min_edge_cents < 0:
            issues.append("DIP_MIN_EDGE_CENTS cannot be negative")
        if self.dip_max_spot_move_bps <= 0:
            issues.append("DIP_MAX_SPOT_MOVE_BPS must be positive")
        if self.dip_stabilize_seconds < 0.5:
            issues.append("DIP_STABILIZE_SECONDS must be at least 0.5")
        if self.dip_decel_tolerance < 0:
            issues.append("DIP_DECEL_TOLERANCE cannot be negative")
        if not -1 <= self.dip_min_imbalance <= 1:
            issues.append("DIP_MIN_IMBALANCE must be between -1 and 1")
        if self.dip_min_seconds_to_close <= self.settlement_guard_seconds:
            issues.append(
                "DIP_MIN_SECONDS_TO_CLOSE must exceed SCALP_SETTLEMENT_GUARD_SECONDS, "
                "or a dip entry is flattened on arrival"
            )
        if self.dip_max_entries_per_market < 1:
            issues.append("DIP_MAX_ENTRIES_PER_MARKET must be at least 1")
        if self.dip_stop_mult <= 0:
            issues.append("DIP_STOP_MULT must be positive")
        if self.tape_min_samples < 4:
            issues.append("TAPE_MIN_SAMPLES must be at least 4 to estimate velocity")
        if self.tape_seconds < self.dip_lookback_seconds:
            issues.append("TAPE_SECONDS must be >= DIP_LOOKBACK_SECONDS")

        # ---- sizing ----
        if self.min_cost_per_trade_cents < 1:
            issues.append("MIN_COST_PER_TRADE_CENTS must be at least 1")
        if self.min_cost_per_trade_cents > self.max_cost_per_trade_cents:
            issues.append("MIN_COST_PER_TRADE_CENTS must be <= MAX_COST_PER_TRADE_CENTS")

        # ---- depth-aware execution ----
        if self.max_entry_slippage_cents < 0:
            issues.append("MAX_ENTRY_SLIPPAGE_CENTS cannot be negative")
        if self.exit_depth_slippage_cents < 0:
            issues.append("EXIT_DEPTH_SLIPPAGE_CENTS cannot be negative")
        if self.max_entry_slippage_cents > self.max_spread_cents:
            issues.append(
                "MAX_ENTRY_SLIPPAGE_CENTS must be <= MAX_SPREAD_CENTS, or an entry "
                "may pay more slippage than the spread the strategy refuses to trade"
            )

        # ---- loop ----
        if self.loop_seconds <= 0:
            issues.append("TRADE_LOOP_SECONDS must be positive")
        if self.loop_min_seconds < 0:
            issues.append("TRADE_LOOP_MIN_SECONDS cannot be negative")
        if self.loop_min_seconds > self.loop_seconds:
            issues.append(
                "TRADE_LOOP_MIN_SECONDS must be <= TRADE_LOOP_SECONDS, or the "
                "floor would outlast the heartbeat it is meant to bound"
            )
        if self.pending_poll_seconds < self.loop_seconds:
            issues.append(
                "PENDING_POLL_SECONDS must be >= TRADE_LOOP_SECONDS, or order-status "
                "polling will run every tick and burn the API rate limit"
            )
        if self.book_max_age_seconds <= 0:
            issues.append("BOOK_MAX_AGE_SECONDS must be positive")

        return issues

    def cautions(self) -> list[str]:
        """Settings that are legal but dangerous together.

        Distinct from problems(): none of these is wrong, so none of them should
        stop the bot from starting. They are combinations where the numbers
        interact in a way that is easy to set by accident and expensive to
        discover live. Surfaced on the dashboard so the choice is deliberate
        rather than implicit.
        """
        notes = []

        # After raising per-trade size, a single full-size losing position can
        # consume the entire day's loss budget and latch the halt. That may be
        # intended -- one strike and stop -- but it should not be a surprise.
        # The runtime limit can only be lower than this (risk.py also caps it at
        # a percentage of the day's opening balance), so comparing against the
        # absolute cap is the conservative check.
        effective_daily = self.daily_loss_limit_cents
        if effective_daily and self.max_cost_per_trade_cents >= effective_daily:
            notes.append(
                f"One full-size position (${self.max_cost_per_trade_cents / 100:.2f}) "
                f"can lose the entire daily budget (${effective_daily / 100:.2f}), so a "
                f"single bad trade can halt the day. Raise DAILY_LOSS_LIMIT_CENTS "
                f"to roughly 2-3x max position size if you want room to be wrong "
                f"more than once."
            )

        # Conviction sizing has no range to work with if the bounds coincide.
        if self.conviction_sizing and self.min_cost_per_trade_cents >= self.max_cost_per_trade_cents:
            notes.append(
                "CONVICTION_SIZING is on but MIN_COST_PER_TRADE_CENTS equals "
                "MAX_COST_PER_TRADE_CENTS, so every trade is the same size and "
                "conviction has no effect on sizing."
            )

        # The dip lane needs the contract tape to have something in it.
        if self.dip_entry and self.tape_seconds < self.dip_lookback_seconds:
            notes.append(
                "TAPE_SECONDS is shorter than DIP_LOOKBACK_SECONDS, so the dip "
                "lane will never see a full lookback window."
            )

        # A trail that cannot tighten below the arm threshold never protects
        # anything: the stop would sit above the point at which it armed.
        if self.exit_profile == "runner" and self.runner_trail_min_cents >= max(
            self.runner_arm_cents, 1
        ) * 3:
            notes.append(
                f"RUNNER_TRAIL_MIN_CENTS ({self.runner_trail_min_cents}) is wide "
                f"relative to RUNNER_ARM_CENTS ({self.runner_arm_cents}): the trail "
                f"arms early but sits far behind price, so small winners will give "
                f"most of the gain back before the trail triggers."
            )

        # Riding to settlement while the guard would force an exit first.
        if self.settle_ride and self.max_hold_seconds < self.settlement_guard_seconds:
            notes.append(
                "SCALP_MAX_HOLD_SECONDS is shorter than SETTLEMENT_GUARD, so the "
                "max-hold exit fires before a settlement ride can ever happen."
            )

        # Live trading with a size the user has not walked up to.
        if self.trading_mode == "live" and self.kalshi_env == "demo":
            notes.append(
                "TRADING_MODE is live but KALSHI_ENV is demo: orders go to the "
                "demo exchange, not the real one."
            )

        return notes

    def public_view(self) -> dict:
        """Non-secret settings, safe to expose on /api/state."""
        return {
            "trading_mode": self.trading_mode,
            "kalshi_env": self.kalshi_env,
            "series_ticker": self.series_ticker,
            "ladder": [
                {"name": tier.name, "target_cents": tier.cents, "exit_pct": tier.pct}
                for tier in self.tiers()
            ],
            "stop_cents": self.stop_cents,
            "stop_min_cents": self.stop_min_cents,
            "stop_max_cents": self.stop_max_cents,
            "stop_vol_mult": self.stop_vol_mult,
            "trail_cents": self.trail_cents,
            "max_hold_seconds": self.max_hold_seconds,
            "settlement_guard_seconds": self.settlement_guard_seconds,
            "min_edge_cents": self.min_edge_cents,
            "min_confidence": self.min_confidence,
            "max_contracts_per_trade": self.max_contracts_per_trade,
            "max_cost_per_trade_cents": self.max_cost_per_trade_cents,
            "daily_loss_limit_cents": self.daily_loss_limit_cents,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "per_trade_risk_pct": self.per_trade_risk_pct,
            "max_trades_per_day": self.max_trades_per_day,
            "max_open_positions": self.max_open_positions,
            "cooldown_seconds": self.cooldown_seconds,
            "stop_cooldown_seconds": self.stop_cooldown_seconds,
            "max_entries_per_market": self.max_entries_per_market,
            "chop_window_seconds": self.chop_window_seconds,
            "min_efficiency_ratio": self.min_efficiency_ratio,
            "bar_seconds": self.bar_seconds,
            "ema_fast_seconds": self.ema_fast_seconds,
            "ema_slow_seconds": self.ema_slow_seconds,
            "rsi_period": self.rsi_period,
            "rsi_overbought": self.rsi_overbought,
            "rsi_oversold": self.rsi_oversold,
            "trend_deadzone_bps": self.trend_deadzone_bps,
            "settle_ride": bool(self.settle_ride),
            "settle_ride_min_bid_cents": self.settle_ride_min_bid_cents,
            "entry_cancel_adverse_cents": self.entry_cancel_adverse_cents,
            "sr_lookback_seconds": self.sr_lookback_seconds,
            "sr_buffer_sigma": self.sr_buffer_sigma,
            "chart_exit": bool(self.chart_exit),
            "chart_exit_max_gain_cents": self.chart_exit_max_gain_cents,
            "late_entry": bool(self.late_entry),
            "late_min_seconds": self.late_min_seconds,
            "late_min_fair_prob": self.late_min_fair_prob,
            "late_max_price_cents": self.late_max_price_cents,
            "favorite_entry": bool(self.favorite_entry),
            "fav_min_price_cents": self.fav_min_price_cents,
            "fav_max_price_cents": self.fav_max_price_cents,
            "fav_min_edge_cents": self.fav_min_edge_cents,
            "fav_min_fair_prob": self.fav_min_fair_prob,
            "fav_risk_pct": self.fav_risk_pct,
            "entry_style": self.entry_style,
            "exit_style": self.exit_style,
            "exit_profile": self.exit_profile,
            "runner_arm_cents": self.runner_arm_cents,
            "runner_trail_frac": self.runner_trail_frac,
            "runner_trail_min_cents": self.runner_trail_min_cents,
            "runner_trail_max_cents": self.runner_trail_max_cents,
            "runner_flip_tighten": self.runner_flip_tighten,
            "runner_hold_winners": bool(self.runner_hold_winners),
            "runner_partial_pct": self.runner_partial_pct,
            "runner_partial_cents": self.runner_partial_cents,
            "spot_feeds": self.spot_feeds,
            "spot_divergence_bps": self.spot_divergence_bps,
            "spot_min_sources": self.spot_min_sources,
            "dip_entry": bool(self.dip_entry),
            "dip_dislocation": bool(self.dip_dislocation),
            "dip_reversion": bool(self.dip_reversion),
            "dip_lookback_seconds": self.dip_lookback_seconds,
            "dip_min_drop_cents": self.dip_min_drop_cents,
            "dip_reversion_min_drop_cents": self.dip_reversion_min_drop_cents,
            "dip_min_edge_cents": self.dip_min_edge_cents,
            "dip_max_spot_move_bps": self.dip_max_spot_move_bps,
            "dip_require_decel": bool(self.dip_require_decel),
            "dip_min_imbalance": self.dip_min_imbalance,
            "dip_min_seconds_to_close": self.dip_min_seconds_to_close,
            "dip_max_entries_per_market": self.dip_max_entries_per_market,
            "dip_cooldown_seconds": self.dip_cooldown_seconds,
            "dip_stop_cooldown_seconds": self.dip_stop_cooldown_seconds,
            "dip_stop_mult": self.dip_stop_mult,
            "conviction_sizing": bool(self.conviction_sizing),
            "min_cost_per_trade_cents": self.min_cost_per_trade_cents,
            "loop_seconds": self.loop_seconds,
            "loop_min_seconds": self.loop_min_seconds,
            "depth_aware_sizing": bool(self.depth_aware_sizing),
            "max_entry_slippage_cents": self.max_entry_slippage_cents,
            "exit_depth_slippage_cents": self.exit_depth_slippage_cents,
            "book_max_age_seconds": self.book_max_age_seconds,
            "maker_improve_cents": self.maker_improve_cents,
            "entry_rest_seconds": self.entry_rest_seconds,
            "maker_fee_multiplier": self.maker_fee_multiplier,
            "credentials_present": bool(self.key_id and self.private_key_pem),
            "config_problems": self.problems(),
            "config_cautions": self.cautions(),
        }


settings = Settings()


def reload() -> Settings:
    """Re-read the environment. Used by the admin endpoints."""
    global settings
    settings = Settings()
    return settings