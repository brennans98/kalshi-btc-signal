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
    max_contracts_per_trade: int = field(default_factory=lambda: _int("MAX_CONTRACTS_PER_TRADE", 6))
    max_cost_per_trade_cents: int = field(
        default_factory=lambda: _int("MAX_COST_PER_TRADE_CENTS", 500)
    )
    daily_loss_limit_cents: int = field(default_factory=lambda: _int("DAILY_LOSS_LIMIT_CENTS", 2000))
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
    loop_seconds: float = field(default_factory=lambda: _float("TRADE_LOOP_SECONDS", 2.0))
    book_poll_seconds: float = field(default_factory=lambda: _float("BOOK_POLL_SECONDS", 2.0))
    reconcile_seconds: float = field(default_factory=lambda: _float("RECONCILE_SECONDS", 30.0))

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

        return issues

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
            "maker_improve_cents": self.maker_improve_cents,
            "entry_rest_seconds": self.entry_rest_seconds,
            "maker_fee_multiplier": self.maker_fee_multiplier,
            "credentials_present": bool(self.key_id and self.private_key_pem),
            "config_problems": self.problems(),
        }


settings = Settings()


def reload() -> Settings:
    """Re-read the environment. Used by the admin endpoints."""
    global settings
    settings = Settings()
    return settings