"""
Every tunable in one place, driven by environment variables so limits can be
changed in Railway without a code change.

This module is the single source of truth. Everything now reads config.settings.

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


KEY_ID_ALIASES = (
    "KALSHI_API_KEY_ID",
    "KALSHI_ACCESS_KEY",
    "KALSHI_KEY_ID",
    "ACCESS_KEY",
)

PRIVATE_KEY_ALIASES = (
    "KALSHI_PRIVATE_KEY",
    "KALSHI_PRIVATE_KEY_BASE64",
    "KALSHI_PEM",
)


def _first_env(names, default: str = "") -> str:
    """First non-blank value among `names`, else `default`.

    Order matters: `names[0]` is canonical, later entries are legacy aliases.
    """
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def credential_source(names) -> str:
    """Which alias supplied a credential, for diagnostics. Never the value."""
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return name
    return ""


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


PROD_BASE_URL = _str("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
DEMO_BASE_URL = _str("KALSHI_DEMO_BASE_URL", "https://external-api.demo.kalshi.co/trade-api/v2")

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
    kalshi_env: str = field(default_factory=lambda: _str("KALSHI_ENV", "demo").lower())
    key_id: str = field(default_factory=lambda: _first_env(KEY_ID_ALIASES, ""))
    private_key_pem: str = field(default_factory=lambda: _first_env(PRIVATE_KEY_ALIASES, ""))
    series_ticker: str = field(default_factory=lambda: _str("KALSHI_SERIES_TICKER", "KXBTC15M"))
    order_path: str = field(default_factory=lambda: _str("KALSHI_ORDER_PATH", "/portfolio/events/orders"))
    admin_token: str = field(default_factory=lambda: _str("ADMIN_TOKEN", ""))
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "./data"))

    trading_mode: str = field(default_factory=lambda: _str("TRADING_MODE", "off").lower())

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

    spot_feeds: str = field(default_factory=lambda: _str("SPOT_FEEDS", "coinbase,binanceus,kraken").lower())
    spot_divergence_bps: float = field(default_factory=lambda: _float("SPOT_DIVERGENCE_BPS", 8.0))
    spot_min_sources: int = field(default_factory=lambda: _int("SPOT_MIN_SOURCES", 2))

    fee_safety_margin_cents: float = field(default_factory=lambda: _float("FEE_SAFETY_MARGIN_CENTS", 0.5))
    maker_fee_multiplier: float = field(default_factory=lambda: _float("MAKER_FEE_MULT", 0.0))

    entry_style: str = field(default_factory=lambda: _str("ENTRY_STYLE", "maker").lower())
    exit_style: str = field(default_factory=lambda: _str("EXIT_STYLE", "maker").lower())
    exit_profile: str = field(default_factory=lambda: _str("EXIT_PROFILE", "runner").lower())
    maker_improve_cents: int = field(default_factory=lambda: _int("MAKER_IMPROVE_CENTS", 1))
    entry_rest_seconds: int = field(default_factory=lambda: _int("ENTRY_REST_SECONDS", 20))
    entry_cancel_adverse_cents: int = field(default_factory=lambda: _int("ENTRY_CANCEL_ADVERSE_CENTS", 4))

    small_cents: int = field(default_factory=lambda: _int("SCALP_SMALL_CENTS", 3))
    small_pct: int = field(default_factory=lambda: _int("SCALP_SMALL_PCT", 50))
    medium_cents: int = field(default_factory=lambda: _int("SCALP_MEDIUM_CENTS", 7))
    medium_pct: int = field(default_factory=lambda: _int("SCALP_MEDIUM_PCT", 30))
    large_cents: int = field(default_factory=lambda: _int("SCALP_LARGE_CENTS", 14))
    large_pct: int = field(default_factory=lambda: _int("SCALP_LARGE_PCT", 20))

    bar_seconds: int = field(default_factory=lambda: _int("CHART_BAR_SECONDS", 5))
    ema_fast_seconds: int = field(default_factory=lambda: _int("EMA_FAST_SECONDS", 60))
    ema_slow_seconds: int = field(default_factory=lambda: _int("EMA_SLOW_SECONDS", 240))
    rsi_period: int = field(default_factory=lambda: _int("RSI_PERIOD", 14))
    rsi_overbought: float = field(default_factory=lambda: _float("RSI_OVERBOUGHT", 70.0))
    rsi_oversold: float = field(default_factory=lambda: _float("RSI_OVERSOLD", 30.0))
    trend_deadzone_bps: float = field(default_factory=lambda: _float("TREND_DEADZONE_BPS", 2.0))
    sr_lookback_seconds: int = field(default_factory=lambda: _int("SR_LOOKBACK_SECONDS", 600))
    sr_buffer_sigma: float = field(default_factory=lambda: _float("SR_BUFFER_SIGMA", 1.0))
    chart_exit: int = field(default_factory=lambda: _int("CHART_EXIT", 1))
    chart_exit_max_gain_cents: int = field(default_factory=lambda: _int("CHART_EXIT_MAX_GAIN_CENTS", 0))

    late_entry: int = field(default_factory=lambda: _int("LATE_ENTRY", 1))
    late_min_seconds: int = field(default_factory=lambda: _int("LATE_MIN_SECONDS", 45))
    late_min_fair_prob: float = field(default_factory=lambda: _float("LATE_MIN_FAIR_PROB", 0.93))
    late_max_price_cents: int = field(default_factory=lambda: _int("LATE_MAX_PRICE_CENTS", 96))

    favorite_entry: int = field(default_factory=lambda: _int("FAV_ENTRY", 1))
    fav_min_price_cents: int = field(default_factory=lambda: _int("FAV_MIN_PRICE_CENTS", 75))
    fav_max_price_cents: int = field(default_factory=lambda: _int("FAV_MAX_PRICE_CENTS", 92))
    fav_min_edge_cents: float = field(default_factory=lambda: _float("FAV_MIN_EDGE_CENTS", 1.5))
    fav_min_fair_prob: float = field(default_factory=lambda: _float("FAV_MIN_FAIR_PROB", 0.80))
    fav_risk_pct: int = field(default_factory=lambda: _int("FAV_RISK_PCT", 20))

    stop_cents: int = field(default_factory=lambda: _int("SCALP_STOP_CENTS", 6))
    stop_min_cents: int = field(default_factory=lambda: _int("STOP_MIN_CENTS", 6))
    stop_max_cents: int = field(default_factory=lambda: _int("STOP_MAX_CENTS", 14))
    stop_vol_mult: float = field(default_factory=lambda: _float("STOP_VOL_MULT", 1.5))
    trail_cents: int = field(default_factory=lambda: _int("SCALP_TRAIL_CENTS", 3))
    max_hold_seconds: int = field(default_factory=lambda: _int("SCALP_MAX_HOLD_SECONDS", 300))
    settlement_guard_seconds: int = field(default_factory=lambda: _int("SCALP_SETTLEMENT_GUARD_SECONDS", 90))
    exit_slippage_cents: int = field(default_factory=lambda: _int("SCALP_EXIT_SLIPPAGE_CENTS", 0))
    exit_tif: str = field(default_factory=lambda: _str("SCALP_EXIT_TIF", "immediate_or_cancel"))
    small_lot_exit_tier: str = field(default_factory=lambda: _str("SCALP_SMALL_LOT_EXIT_TIER", "medium").lower())

    settle_ride: int = field(default_factory=lambda: _int("SETTLE_RIDE", 1))
    settle_ride_min_bid_cents: int = field(default_factory=lambda: _int("SETTLE_RIDE_MIN_BID_CENTS", 80))

    depth_aware_sizing: int = field(default_factory=lambda: _int("DEPTH_AWARE_SIZING", 1))
    max_entry_slippage_cents: int = field(default_factory=lambda: _int("MAX_ENTRY_SLIPPAGE_CENTS", 2))
    exit_depth_slippage_cents: int = field(default_factory=lambda: _int("EXIT_DEPTH_SLIPPAGE_CENTS", 3))
    max_contracts_per_trade: int = field(default_factory=lambda: _int("MAX_CONTRACTS_PER_TRADE", 60))
    max_cost_per_trade_cents: int = field(default_factory=lambda: _int("MAX_COST_PER_TRADE_CENTS", 2000))

    conviction_sizing: int = field(default_factory=lambda: _int("CONVICTION_SIZING", 1))

    dryrun_balance_cents: int = field(default_factory=lambda: _int("DRYRUN_BALANCE_CENTS", 0))
    min_cost_per_trade_cents: int = field(default_factory=lambda: _int("MIN_COST_PER_TRADE_CENTS", 500))

    daily_loss_limit_cents: int = field(default_factory=lambda: _int("DAILY_LOSS_LIMIT_CENTS", 5000))
    daily_loss_limit_pct: int = field(default_factory=lambda: _int("DAILY_LOSS_LIMIT_PCT", 25))
    per_trade_risk_pct: int = field(default_factory=lambda: _int("PER_TRADE_RISK_PCT", 15))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 40))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 2))
    cooldown_seconds: int = field(default_factory=lambda: _int("COOLDOWN_SECONDS", 20))
    stop_cooldown_seconds: int = field(default_factory=lambda: _int("STOP_COOLDOWN_SECONDS", 120))
    max_entries_per_market: int = field(default_factory=lambda: _int("MAX_ENTRIES_PER_MARKET", 2))

    tape_seconds: int = field(default_factory=lambda: _int("TAPE_SECONDS", 900))
    tape_min_samples: int = field(default_factory=lambda: _int("TAPE_MIN_SAMPLES", 12))

    dip_entry: int = field(default_factory=lambda: _int("DIP_ENTRY", 1))
    dip_dislocation: int = field(default_factory=lambda: _int("DIP_DISLOCATION", 1))
    dip_reversion: int = field(default_factory=lambda: _int("DIP_REVERSION", 1))
    dip_lookback_seconds: int = field(default_factory=lambda: _int("DIP_LOOKBACK_SECONDS", 90))
    dip_min_drop_cents: float = field(default_factory=lambda: _float("DIP_MIN_DROP_CENTS", 6.0))
    dip_reversion_min_drop_cents: float = field(default_factory=lambda: _float("DIP_REVERSION_MIN_DROP_CENTS", 10.0))
    dip_min_edge_cents: float = field(default_factory=lambda: _float("DIP_MIN_EDGE_CENTS", 3.0))
    dip_max_spot_move_bps: float = field(default_factory=lambda: _float("DIP_MAX_SPOT_MOVE_BPS", 12.0))
    dip_require_decel: int = field(default_factory=lambda: _int("DIP_REQUIRE_DECEL", 1))
    dip_stabilize_seconds: float = field(default_factory=lambda: _float("DIP_STABILIZE_SECONDS", 3.0))
    dip_decel_tolerance: float = field(default_factory=lambda: _float("DIP_DECEL_TOLERANCE", 0.15))
    dip_min_imbalance: float = field(default_factory=lambda: _float("DIP_MIN_IMBALANCE", 0.0))
    dip_min_seconds_to_close: int = field(default_factory=lambda: _int("DIP_MIN_SECONDS_TO_CLOSE", 180))
    dip_max_entries_per_market: int = field(default_factory=lambda: _int("DIP_MAX_ENTRIES_PER_MARKET", 4))
    dip_cooldown_seconds: int = field(default_factory=lambda: _int("DIP_COOLDOWN_SECONDS", 8))
    dip_stop_cooldown_seconds: int = field(default_factory=lambda: _int("DIP_STOP_COOLDOWN_SECONDS", 30))
    dip_stop_mult: float = field(default_factory=lambda: _float("DIP_STOP_MULT", 1.4))

    runner_arm_cents: int = field(default_factory=lambda: _int("RUNNER_ARM_CENTS", 4))
    runner_trail_frac: float = field(default_factory=lambda: _float("RUNNER_TRAIL_FRAC", 0.35))
    runner_trail_min_cents: int = field(default_factory=lambda: _int("RUNNER_TRAIL_MIN_CENTS", 5))
    runner_trail_max_cents: int = field(default_factory=lambda: _int("RUNNER_TRAIL_MAX_CENTS", 12))
    runner_flip_tighten: float = field(default_factory=lambda: _float("RUNNER_FLIP_TIGHTEN", 0.5))
    runner_hold_winners: int = field(default_factory=lambda: _int("RUNNER_HOLD_WINNERS", 1))
    runner_partial_pct: int = field(default_factory=lambda: _int("RUNNER_PARTIAL_PCT", 0))
    runner_partial_cents: int = field(default_factory=lambda: _int("RUNNER_PARTIAL_CENTS", 10))

    halt_webhook_url: str = field(default_factory=lambda: _str("HALT_WEBHOOK_URL", ""))
    book_timeout_seconds: float = field(default_factory=lambda: _float("BOOK_TIMEOUT_SECONDS", 10.0))
    imbalance_veto_threshold: float = field(default_factory=lambda: _float("IMBALANCE_VETO_THRESHOLD", -0.5))

    chop_window_seconds: int = field(default_factory=lambda: _int("CHOP_WINDOW_SECONDS", 180))
    min_efficiency_ratio: float = field(default_factory=lambda: _float("MIN_EFFICIENCY_RATIO", 0.20))

    loop_seconds: float = field(default_factory=lambda: _float("TRADE_LOOP_SECONDS", 0.25))
    loop_min_seconds: float = field(default_factory=lambda: _float("TRADE_LOOP_MIN_SECONDS", 0.02))
    pending_poll_seconds: float = field(default_factory=lambda: _float("PENDING_POLL_SECONDS", 0.5))
    book_poll_seconds: float = field(default_factory=lambda: _float("BOOK_POLL_SECONDS", 2.0))
    reconcile_seconds: float = field(default_factory=lambda: _float("RECONCILE_SECONDS", 30.0))
    book_max_age_seconds: float = field(default_factory=lambda: _float("BOOK_MAX_AGE_SECONDS", 2.0))

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
        return self.path(f"scalp_lots_{mode}.json")

    def problems(self) -> list[str]:
        issues: list[str] = []
        if self.trading_mode not in VALID_MODES:
            issues.append(f"TRADING_MODE must be one of {VALID_MODES}")
        if self.kalshi_env not in ("demo", "prod"):
            issues.append("KALSHI_ENV must be 'demo' or 'prod'")
        if self.is_enabled and not (self.key_id and self.private_key_pem):
            issues.append("A Kalshi API key id and private key are required to trade.")
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
            issues.append("MIN_SECONDS_TO_CLOSE must exceed SCALP_SETTLEMENT_GUARD_SECONDS")
        if self.stop_cents <= self.max_spread_cents:
            issues.append("SCALP_STOP_CENTS is inside MAX_SPREAD_CENTS")
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
            issues.append("FAV_MIN/MAX_PRICE_CENTS must satisfy 50 <= min <= max <= 99")
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
            issues.append("SPOT_MIN_SOURCES exceeds the number of feeds in SPOT_FEEDS")
        if self.spot_divergence_bps <= 0:
            issues.append("SPOT_DIVERGENCE_BPS must be positive")
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
            issues.append("RUNNER_TRAIL_MIN_CENTS is inside MAX_SPREAD_CENTS")
        if not 0 < self.runner_flip_tighten <= 1:
            issues.append("RUNNER_FLIP_TIGHTEN must be between 0 and 1")
        if not 0 <= self.runner_partial_pct <= 100:
            issues.append("RUNNER_PARTIAL_PCT must be between 0 and 100")
        if self.runner_partial_pct and self.runner_partial_cents < 1:
            issues.append("RUNNER_PARTIAL_CENTS must be at least 1 when a partial is enabled")
        if self.dip_lookback_seconds < 15:
            issues.append("DIP_LOOKBACK_SECONDS must be at least 15")
        if self.dip_min_drop_cents <= self.max_spread_cents:
            issues.append("DIP_MIN_DROP_CENTS is inside MAX_SPREAD_CENTS")
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
            issues.append("DIP_MIN_SECONDS_TO_CLOSE must exceed SCALP_SETTLEMENT_GUARD_SECONDS")
        if self.dip_max_entries_per_market < 1:
            issues.append("DIP_MAX_ENTRIES_PER_MARKET must be at least 1")
        if self.dip_stop_mult <= 0:
            issues.append("DIP_STOP_MULT must be positive")
        if self.tape_min_samples < 4:
            issues.append("TAPE_MIN_SAMPLES must be at least 4 to estimate velocity")
        if self.tape_seconds < self.dip_lookback_seconds:
            issues.append("TAPE_SECONDS must be >= DIP_LOOKBACK_SECONDS")
        if self.min_cost_per_trade_cents < 1:
            issues.append("MIN_COST_PER_TRADE_CENTS must be at least 1")
        if self.min_cost_per_trade_cents > self.max_cost_per_trade_cents:
            issues.append("MIN_COST_PER_TRADE_CENTS must be <= MAX_COST_PER_TRADE_CENTS")
        if self.max_entry_slippage_cents < 0:
            issues.append("MAX_ENTRY_SLIPPAGE_CENTS cannot be negative")
        if self.exit_depth_slippage_cents < 0:
            issues.append("EXIT_DEPTH_SLIPPAGE_CENTS cannot be negative")
        if self.max_entry_slippage_cents > self.max_spread_cents:
            issues.append("MAX_ENTRY_SLIPPAGE_CENTS must be <= MAX_SPREAD_CENTS")
        if self.loop_seconds <= 0:
            issues.append("TRADE_LOOP_SECONDS must be positive")
        if self.loop_min_seconds < 0:
            issues.append("TRADE_LOOP_MIN_SECONDS cannot be negative")
        if self.book_timeout_seconds < 5:
            issues.append(f"BOOK_TIMEOUT_SECONDS={self.book_timeout_seconds}s is very low -- normal WS blips may trigger emergency exits")
        if self.loop_min_seconds > self.loop_seconds:
            issues.append("TRADE_LOOP_MIN_SECONDS must be <= TRADE_LOOP_SECONDS")
        if self.pending_poll_seconds < self.loop_seconds:
            issues.append("PENDING_POLL_SECONDS must be >= TRADE_LOOP_SECONDS")
        if self.book_max_age_seconds <= 0:
            issues.append("BOOK_MAX_AGE_SECONDS must be positive")
        if self.dryrun_balance_cents < 0:
            issues.append("DRYRUN_BALANCE_CENTS cannot be negative")
        if 0 < self.dryrun_balance_cents < self.min_cost_per_trade_cents:
            issues.append(f"DRYRUN_BALANCE_CENTS below MIN_COST_PER_TRADE_CENTS")
        return issues

    def cautions(self) -> list[str]:
        notes = []
        if self.trading_mode == "dryrun" and self.dryrun_balance_cents > 0:
            notes.append(f"Sizing against a SIMULATED ${self.dryrun_balance_cents / 100:.2f} balance (DRYRUN_BALANCE_CENTS)")
        if self.trading_mode == "live" and self.dryrun_balance_cents > 0:
            notes.append("DRYRUN_BALANCE_CENTS is set but has no effect in live mode")
        effective_daily = self.daily_loss_limit_cents
        if effective_daily and self.max_cost_per_trade_cents >= effective_daily:
            notes.append(f"One full-size position (${self.max_cost_per_trade_cents / 100:.2f}) can lose the entire daily budget (${effective_daily / 100:.2f})")
        if self.conviction_sizing and self.min_cost_per_trade_cents >= self.max_cost_per_trade_cents:
            notes.append("CONVICTION_SIZING is on but MIN/MAX cost are equal")
        if self.dip_entry and self.tape_seconds < self.dip_lookback_seconds:
            notes.append("TAPE_SECONDS is shorter than DIP_LOOKBACK_SECONDS")
        if self.exit_profile == "runner" and self.runner_trail_min_cents >= max(self.runner_arm_cents, 1) * 3:
            notes.append(f"RUNNER_TRAIL_MIN_CENTS is wide relative to RUNNER_ARM_CENTS")
        if self.settle_ride and self.max_hold_seconds < self.settlement_guard_seconds:
            notes.append("SCALP_MAX_HOLD_SECONDS is shorter than SETTLEMENT_GUARD")
        if self.trading_mode == "live" and self.kalshi_env == "demo":
            notes.append("TRADING_MODE is live but KALSHI_ENV is demo")
        return notes

    def public_view(self) -> dict:
        return {
            "trading_mode": self.trading_mode,
            "kalshi_env": self.kalshi_env,
            "series_ticker": self.series_ticker,
            "ladder": [{"name": tier.name, "target_cents": tier.cents, "exit_pct": tier.pct} for tier in self.tiers()],
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
            "halt_webhook_url": ("configured" if self.halt_webhook_url else ""),
            "book_timeout_seconds": self.book_timeout_seconds,
            "imbalance_veto_threshold": self.imbalance_veto_threshold,
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
    global settings
    settings = Settings()
    return settings