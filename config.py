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
    order_path: str = field(default_factory=lambda: _str("KALSHI_ORDER_PATH", "/portfolio/orders"))
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
    # Kalshi charges ceil(0.07 * C * P * (1-P) * 100) cents per fill. This adds
    # a cushion above the raw fee estimate before an edge is considered
    # tradable, so a fee-rounding quirk or a slightly-stale price doesn't turn
    # a marginal "profitable" trade into a guaranteed loss.
    fee_safety_margin_cents: float = field(
        default_factory=lambda: _float("FEE_SAFETY_MARGIN_CENTS", 0.5)
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

    # ---- the exits that are not profits --------------------------------
    stop_cents: int = field(default_factory=lambda: _int("SCALP_STOP_CENTS", 6))
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

    # ---- size and risk limits ------------------------------------------
    max_contracts_per_trade: int = field(default_factory=lambda: _int("MAX_CONTRACTS_PER_TRADE", 6))
    max_cost_per_trade_cents: int = field(
        default_factory=lambda: _int("MAX_COST_PER_TRADE_CENTS", 500)
    )
    daily_loss_limit_cents: int = field(default_factory=lambda: _int("DAILY_LOSS_LIMIT_CENTS", 2000))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 40))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 2))
    cooldown_seconds: int = field(default_factory=lambda: _int("COOLDOWN_SECONDS", 20))

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
            "trail_cents": self.trail_cents,
            "max_hold_seconds": self.max_hold_seconds,
            "settlement_guard_seconds": self.settlement_guard_seconds,
            "min_edge_cents": self.min_edge_cents,
            "min_confidence": self.min_confidence,
            "max_contracts_per_trade": self.max_contracts_per_trade,
            "max_cost_per_trade_cents": self.max_cost_per_trade_cents,
            "daily_loss_limit_cents": self.daily_loss_limit_cents,
            "max_trades_per_day": self.max_trades_per_day,
            "max_open_positions": self.max_open_positions,
            "cooldown_seconds": self.cooldown_seconds,
            "credentials_present": bool(self.key_id and self.private_key_pem),
            "config_problems": self.problems(),
        }


settings = Settings()


def reload() -> Settings:
    """Re-read the environment. Used by the admin endpoints."""
    global settings
    settings = Settings()
    return settings