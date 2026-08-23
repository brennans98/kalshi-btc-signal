"""Authenticated Kalshi trade-api v2 client.

Signing contract:
    message   = f"{timestamp_ms}{METHOD}{path}"
    path      includes the "/trade-api/v2" prefix and EXCLUDES any query string
    signature = base64(RSA-PSS(SHA256, salt_length=DIGEST_LENGTH)) over message
    headers   KALSHI-ACCESS-KEY / KALSHI-ACCESS-SIGNATURE / KALSHI-ACCESS-TIMESTAMP

Authentication failures raise KalshiAuthError, deliberately a different type
from KalshiApiError, so the trading loop can halt on a bad key instead of
retrying a request that will never succeed.

Entries and exits use different time-in-force. An entry is fill-or-kill: a
partial fill at a price the edge was not calculated against is not the trade we
decided on. An exit is immediate-or-cancel: taking part of the way out is
strictly better than staying fully exposed, and the ladder will re-offer the
remainder on the next tick.
"""

import base64
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config


class KalshiAuthError(RuntimeError):
    """Credentials are missing, malformed, or rejected. Not retryable."""


class KalshiApiError(RuntimeError):
    """Transport or API error. May be transient."""


def _load_private_key(raw):
    if not raw:
        return None

    # Railway variables commonly arrive with literal backslash-n sequences.
    pem = raw.replace("\\n", "\n").strip()

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
        self._key_error = None
        # Set if the venue rejects our time_in_force field, so we stop sending it.
        self._tif_unsupported = False

        try:
            self.private_key = _load_private_key(cfg.private_key_pem)
        except KalshiAuthError as error:
            self.private_key = None
            self._key_error = str(error)

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

    def _sign(self, method, endpoint):
        timestamp = str(int(time.time() * 1000))
        path = urlparse(self.base_url).path + endpoint.split("?")[0]
        message = f"{timestamp}{method.upper()}{path}".encode("utf-8")

        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    async def request(self, method, endpoint, params=None, body=None, authenticate=True):
        headers = {"Content-Type": "application/json"}

        if authenticate:
            if not self.has_credentials:
                raise KalshiAuthError(self.credential_error)
            headers = self._sign(method, endpoint)

        try:
            async with httpx.AsyncClient(timeout=15) as http:
                response = await http.request(
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

        return response.json()

    # ---- market data (public) ----

    async def get_markets(self, series_ticker, status="open", limit=100):
        return await self.request(
            "GET",
            "/markets",
            params={"series_ticker": series_ticker, "status": status, "limit": limit},
            authenticate=False,
        )

    async def get_market(self, ticker):
        return await self.request("GET", f"/markets/{ticker}", authenticate=False)

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

    # ---- orders ----

    async def _place(self, body):
        """POST an order, retrying once without time_in_force if it is rejected.

        Kalshi has moved the order surface more than once. Rather than guess
        which time_in_force spelling this account's endpoint accepts, drop the
        field on the first rejection that names it and remember that for the
        rest of the process. An exit must not fail because of an optional field.
        """
        if self._tif_unsupported:
            body.pop("time_in_force", None)

        try:
            return await self.request("POST", self.order_path, body=body)
        except KalshiApiError as error:
            message = str(error).lower()
            if "time_in_force" in message and "time_in_force" in body:
                self._tif_unsupported = True
                body.pop("time_in_force", None)
                return await self.request("POST", self.order_path, body=body)
            raise

    def _order_body(self, ticker, action, side, count, price_cents, client_order_id, tif):
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": int(count),
            "type": "limit",
            "client_order_id": client_order_id,
        }

        if tif:
            body["time_in_force"] = tif

        # The price field is named for the side being traded, on both buys and
        # sells: selling yes at 62c is yes_price=62.
        if side == "yes":
            body["yes_price"] = int(price_cents)
        else:
            body["no_price"] = int(price_cents)

        return body

    async def create_order(
        self, ticker, side, count, price_cents, client_order_id, action="buy"
    ):
        """Place a limit order.

        Limit, not market: a market order on a thin 15-minute book can fill far
        from the price the edge was calculated against, which would silently
        invalidate the decision that produced it.
        """
        tif = "fill_or_kill" if action == "buy" else config.settings.exit_tif
        body = self._order_body(
            ticker, action, side, count, price_cents, client_order_id, tif
        )
        return await self._place(body)

    async def sell(self, ticker, side, count, price_cents, client_order_id):
        """Exit `count` contracts of `side` at a limit price.

        Uses immediate-or-cancel rather than fill-or-kill: a partial exit is
        better than no exit, and the ladder re-offers the remainder next tick.
        """
        return await self.create_order(
            ticker=ticker,
            side=side,
            count=count,
            price_cents=price_cents,
            client_order_id=client_order_id,
            action="sell",
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
