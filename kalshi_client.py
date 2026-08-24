"""Authenticated Kalshi trade-api v2 client.

Signing contract:
    message   = f"{timestamp_ms}{METHOD}{path}"
    path      includes the "/trade-api/v2" prefix and EXCLUDES any query string
    signature = base64(RSA-PSS(SHA256, salt_length=DIGEST_LENGTH)) over message
    headers   KALSHI-ACCESS-KEY / KALSHI-ACCESS-SIGNATURE / KALSHI-ACCESS-TIMESTAMP

The WebSocket endpoint (/trade-api/ws/v2) uses the identical RSA-PSS scheme but
a different path prefix than REST, so it is signed separately rather than
reusing _sign(), which is hardcoded to the REST base_url's path.

Authentication failures raise KalshiAuthError, deliberately a different type
from KalshiApiError, so the trading loop can halt on a bad key instead of
retrying a request that will never succeed.

One shared httpx.AsyncClient is reused for the life of the process. A scalper
polls the book every couple of seconds and fires paired entry/exit orders; a
fresh connection per request would add a TCP and TLS handshake to the latency
of every one of them.
"""

import base64
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

WS_PATH = "/trade-api/ws/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com" + WS_PATH
DEMO_WS_URL = "wss://demo-api.kalshi.co" + WS_PATH


class KalshiAuthError(RuntimeError):
    """Credentials are missing, malformed, or rejected. Not retryable."""


class KalshiApiError(RuntimeError):
    """Transport or API error. May be transient."""


def _load_private_key(raw):
    if not raw:
        return None

    # Railway variables commonly arrive with literal backslash-n sequences.
    pem = raw.replace(chr(92) + "n", chr(10)).strip()

    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as error:
        raise KalshiAuthError(
            f"KALSHI_PRIVATE_KEY could not be parsed as a PEM private key: {error}"
        )


class KalshiClient:
    def __init__(self):
        cfg = config.settings
        self.base_url = cfg.base_url
        self.environment = cfg.kalshi_env
        self.key_id = cfg.key_id or None
        self.order_path = cfg.order_path
        self.ws_url = PROD_WS_URL if cfg.kalshi_env == "prod" else DEMO_WS_URL
        self._key_error = None
        self._http = None
        # Set if the venue rejects time_in_force, so we stop sending it.
        self._tif_unsupported = False

        try:
            self.private_key = _load_private_key(cfg.private_key_pem)
        except KalshiAuthError as error:
            self.private_key = None
            self._key_error = str(error)

    # ---- lifecycle ----

    def _client(self):
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            )
        return self._http

    async def aclose(self):
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    # ---- credentials ----

    @property
    def has_credentials(self):
        return bool(self.key_id and self.private_key)

    @property
    def credential_error(self):
        if self._key_error:
            return self._key_error
        if not self.key_id:
            return "KALSHI_API_KEY_ID is not set"
        if not self.private_key:
            return "KALSHI_PRIVATE_KEY is not set"
        return None

    def _sign_message(self, message_text):
        signature = self.private_key.sign(
            message_text.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _sign(self, method, endpoint):
        timestamp = str(int(time.time() * 1000))
        path = urlparse(self.base_url).path + endpoint.split("?")[0]
        message = f"{timestamp}{method.upper()}{path}"

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign_message(message),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def sign_ws_handshake(self):
        """KALSHI-ACCESS-* headers for the /trade-api/ws/v2 WebSocket handshake.

        Same RSA-PSS scheme as REST, but signed over the WS path directly since
        it does not sit under the /trade-api/v2 REST prefix that _sign() assumes.
        """
        if not self.has_credentials:
            raise KalshiAuthError(self.credential_error)

        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}GET{WS_PATH}"

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign_message(message),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    async def request(self, method, endpoint, params=None, body=None, authenticate=True):
        headers = {"Content-Type": "application/json"}

        if authenticate:
            if not self.has_credentials:
                raise KalshiAuthError(self.credential_error)
            headers = self._sign(method, endpoint)

        try:
            response = await self._client().request(
                method.upper(),
                f"{self.base_url}{endpoint}",
                params=params,
                json=body,
                headers=headers,
            )
        except Exception as error:
            raise KalshiApiError(f"{method.upper()} {endpoint} transport error: {error}")

        if response.status_code in (401, 403):
            raise KalshiAuthError(
                f"{method.upper()} {endpoint} rejected ({response.status_code}): "
                f"{response.text[:200]}"
            )

        if response.status_code >= 400:
            raise KalshiApiError(
                f"{method.upper()} {endpoint} failed ({response.status_code}): "
                f"{response.text[:200]}"
            )

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError:
            return {}

    # ---- market data (public) ----

    async def get_markets(self, series_ticker=None, status="open", limit=100):
        return await self.request(
            "GET",
            "/markets",
            params={
                "series_ticker": series_ticker or config.settings.series_ticker,
                "status": status,
                "limit": limit,
            },
            authenticate=False,
        )

    async def get_market(self, ticker):
        payload = await self.request("GET", f"/markets/{ticker}", authenticate=False)
        return payload.get("market") or payload

    async def get_orderbook(self, ticker, depth=10):
        return await self.request(
            "GET",
            f"/markets/{ticker}/orderbook",
            params={"depth": depth},
            authenticate=False,
        )

    # ---- portfolio (authenticated) ----

    async def get_balance(self):
        return await self.request("GET", "/portfolio/balance")

    async def get_positions(self):
        return await self.request(
            "GET",
            "/portfolio/positions",
            params={"settlement_status": "unsettled", "limit": 200},
        )

    async def get_fills(self, ticker=None, limit=50):
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self.request("GET", "/portfolio/fills", params=params)

    # ---- account tier ----

    async def get_api_limits(self):
        """Current usage tier and token-bucket limits."""
        return await self.request("GET", "/account/api_limits")

    async def upgrade_api_tier(self):
        """Request the Advanced usage tier.

        Kalshi's own criterion: at least 1 of the account's last 100
        Predictions orders must have been created via the API. Dryrun mode
        never satisfies this, since it never calls create_order/sell -- only
        a real (live) order counts. Safe to call speculatively; Kalshi
        returns 403 with a clear message if the criterion is not met yet.
        """
        return await self.request("POST", "/account/api_usage_level/upgrade", body={})

    # ---- orders ----

    async def _place(self, ticker, action, side, count, price_cents, client_order_id, tif):
        """Submit a limit order.

        Limit, never market: a market order on a thin 15-minute book can fill
        far from the price the edge was calculated against, which silently
        invalidates the decision that produced it. On a scalp where the whole
        target is a few cents, that is the difference between the trade working
        and not.
        """
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": int(count),
            "type": "limit",
            "client_order_id": client_order_id,
        }

        if side == "yes":
            body["yes_price"] = int(price_cents)
        else:
            body["no_price"] = int(price_cents)

        if tif and not self._tif_unsupported:
            body["time_in_force"] = tif

        try:
            return await self.request("POST", self.order_path, body=body)
        except KalshiApiError as error:
            # Some environments reject time_in_force outright. Retry once
            # without it rather than failing every order from here on.
            if tif and not self._tif_unsupported and "time_in_force" in str(error):
                self._tif_unsupported = True
                body.pop("time_in_force", None)
                return await self.request("POST", self.order_path, body=body)
            raise

    async def create_order(self, ticker, side, count, price_cents, client_order_id):
        """Buy to open. Fill-or-kill: a partial entry at a stale price is worse
        than no entry, because the ladder is sized off the original count."""
        return await self._place(
            ticker, "buy", side, count, price_cents, client_order_id, "fill_or_kill"
        )

    async def sell(self, ticker, side, count, price_cents, client_order_id):
        """Sell to close a ladder rung or a stop.

        Immediate-or-cancel rather than fill-or-kill: a partial exit is
        genuinely useful here. Banking half of a rung and retrying the rest next
        tick beats refusing the whole exit because the bid could not absorb it.
        """
        return await self._place(
            ticker,
            "sell",
            side,
            count,
            price_cents,
            client_order_id,
            config.settings.exit_tif,
        )

    async def selftest(self):
        """Verify signing and read-only portfolio access without placing an order."""
        result = {
            "environment": self.environment,
            "base_url": self.base_url,
            "order_path": self.order_path,
            "has_credentials": self.has_credentials,
        }

        if not self.has_credentials:
            result["ok"] = False
            result["error"] = self.credential_error
            return result

        try:
            balance = await self.get_balance()
            positions = await self.get_positions()
        except KalshiAuthError as error:
            result["ok"] = False
            result["error_type"] = "auth"
            result["error"] = str(error)
            return result
        except KalshiApiError as error:
            result["ok"] = False
            result["error_type"] = "api"
            result["error"] = str(error)
            return result

        result["ok"] = True
        result["balance_cents"] = balance.get("balance")
        result["open_position_count"] = len(positions.get("market_positions", []))
        return result