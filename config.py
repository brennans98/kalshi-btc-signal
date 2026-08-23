"""
All tunable behaviour for the autonomous trader, driven by environment
variables so that limits can be changed in Railway without a code change.

Nothing here has a permissive default. TRADING_MODE defaults to "off", every
size limit defaults small, and the loss cap defaults tight. Widening the
system's authority is always an explicit act.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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


def _bool(name: str, default: bool) -> bool:
    return _str(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

VALID_MODES = ("off", "dryrun", "live")


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
    # dryrun - evaluate fully, log the intended order, place nothing
    # live   - place real orders
    trading_mode: str = field(default_factory=lambda: _str("TRADING_MODE", "off").lower())

    # ---- entry thresholds ----------------------------------------------
    min_edge: float = field(default_factory=lambda: _float("MIN_EDGE", 0.06))
    min_confidence: int = field(default_factory=lambda: _int("MIN_CONFIDENCE", 60))
    max_spread_cents: int = field(default_factory=lambda: _int("MAX_SPREAD_CENTS", 6))
    min_book_size: int = field(default_factory=lambda: _int("MIN_BOOK_SIZE", 20))
    min_price_cents: int = field(default_factory=lambda: _int("MIN_PRICE_CENTS", 8))
    max_price_cents: int = field(default_factory=lambda: _int("MAX_PRICE_CENTS", 92))
    min_seconds_to_close: int = field(default_factory=lambda: _int("MIN_SECONDS_TO_CLOSE", 90))
    max_seconds_to_close: int = field(default_factory=lambda: _int("MAX_SECONDS_TO_CLOSE", 720))
    vol_window_seconds: int = field(default_factory=lambda: _int("VOL_WINDOW_SECONDS", 600))
    min_vol_samples: int = field(default_factory=lambda: _int("MIN_VOL_SAMPLES", 60))

    # ---- size and risk limits ------------------------------------------
    max_contracts_per_trade: int = field(default_factory=lambda: _int("MAX_CONTRACTS_PER_TRADE", 5))
    max_trade_cost_dollars: float = field(default_factory=lambda: _float("MAX_TRADE_COST_DOLLARS", 5.0))
    daily_loss_limit_dollars: float = field(default_factory=lambda: _float("DAILY_LOSS_LIMIT_DOLLARS", 20.0))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 10))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 1))
    cooldown_seconds: int = field(default_factory=lambda: _int("COOLDOWN_SECONDS", 120))

    # ---- loop ----------------------------------------------------------
    loop_seconds: float = field(default_factory=lambda: _float("TRADE_LOOP_SECONDS", 3.0))
    book_poll_seconds: float = field(default_factory=lambda: _float("BOOK_POLL_SECONDS", 2.0))

    @property
    def base_url(self) -> str:
        return PROD_BASE_URL if self.kalshi_env == "prod" else DEMO_BASE_URL

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def is_enabled(self) -> bool:
        return self.trading_mode in ("dryrun", "live")

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
        return issues

    def public_view(self) -> dict:
        """Non-secret settings, safe to expose on /api/state."""
        return {
            "trading_mode": self.trading_mode,
            "kalshi_env": self.kalshi_env,
            "series_ticker": self.series_ticker,
            "min_edge": self.min_edge,
            "min_confidence": self.min_confidence,
            "max_contracts_per_trade": self.max_contracts_per_trade,
            "max_trade_cost_dollars": self.max_trade_cost_dollars,
            "daily_loss_limit_dollars": self.daily_loss_limit_dollars,
            "max_trades_per_day": self.max_trades_per_day,
            "max_open_positions": self.max_open_positions,
            "credentials_present": bool(self.key_id and self.private_key_pem),
            "config_problems": self.problems(),
        }


settings = Settings()
