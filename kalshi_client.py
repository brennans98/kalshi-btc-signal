"""Authenticated Kalshi trade-api v2 client.

Signing contract (Kalshi trade-api v2):
    message   = timestamp_ms + HTTP_METHOD + path
    path      includes the /trade-api/v2 prefix and EXCLUDES any query string
    signature = RSA-PSS over SHA256, salt length = digest length, base64

Headers sent on every authenticated request:
    KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE
"""

import base64
import os
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiAuthError(Exception):
    """Credentials are missing, malformed, or rejected. Never retried blindly."""


class KalshiRequestError(Exception):
    """Transport or non-auth API error."""


class KalshiClient:
    def __init__(self, key_id=None, private_key_pem=None, env=None, timeout=10.0):
        self.key_id = (key_id if key_id is not None else os.getenv("KALSHI_KEY_ID", "")).strip()
        self.env = (env if env is not None else os.getenv("KALSHI_ENV", "demo")).strip().lower()
        self.base_url = PROD_BASE if self.env in ("prod", "production", "live") else DEMO_BASE
        self.order_path = os.getenv("KALSHI_ORDER_PATH", "/portfolio/orders")
        self.timeout = timeout
        self._pem = self._normalize_pem(
            private_key_pem if private_key_pem is not None else os.getenv("KALSHI_PRIVATE_KEY", "")
        )
        self._private_key = None

    # ---------- credentials ----------

    @staticmethod
    def _normalize_pem(raw):
        """Accept PEM pasted with real newlines or with literal backslash-n."""
        if not raw:
            return ""
        text = raw.strip().strip('"').strip("'")
        if "\\n" in text and "\n" not in text:
            text = text.replace("\\n", "\n")
        return text.strip()

    @property
    def configured(self):
        return bool(self.key_id) and bool(self._pem)

    @property
    def is_production(self):
        return self.base_url == PROD_BASE

    def _key(self):
        if self._private_key is None:
            if not self._pem:
                raise KalshiAuthError("KALSHI_PRIVATE_KEY is not set")
            if "PRIVATE KEY" not in self._pem:
                raise KalshiAuthError("KALSHI_PRIVATE_KEY does not look like a PEM private key")
            try:
                self._private_key = serialization.load_pem_private_key(
                    self._pem.encode("utf-8"), password=None
                )
            except Exception as error:
                raise KalshiAuthError(f"private key could not be parsed: {error}")
        return self._private_key

    def _auth_headers(self, method, path):
        if not self.key_id:
            raise KalshiAuthError("KALSHI_KEY_ID is not set")

        timestamp = str(int(time.time() * 1000))
        sign_path = urlparse(self.base_url + path).path
        message = f"{timestamp}{method.upper()}{sign_path}".encode("utf-8")

        signature = self._key().sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ---------- transport ----------

    async def _request(self, method, path, params=None, json_body=None, authenticated=True):
        headers = self._auth_headers(method, path) if authenticated else {"Accept": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method.upper(),
                    self.base_url + path,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
        except Exception as error:
            raise KalshiRequestError(f"{method.upper()} {path} transport failure: {error}")

        if response.status_code in (401, 403):
            raise KalshiAuthError(
                f"{method.upper()} {path} rejected ({response.status_code}): {response.text[:300]}"
            )

        if response.status_code >= 400:
            raise KalshiRequestError(
                f"{method.upper()} {path} failed ({response.status_code}): {response.text[:300]}"
            )

        if not response.content:
            return {}

        try:
            return response.json()
        except Exception as error:
            raise KalshiRequestError(f"{method.upper()} {path} returned non-JSON: {error}")

    # ---------- reads ----------

    async def get_orderbook(self, ticker, depth=8):
        return await self._request(
            "GET",
            f"/markets/{ticker}/orderbook",
            params={"depth": depth},
        )

    async def get_balance(self):
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self, ticker=None):
        params = {"limit": 200, "count_filter": "position"}
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/portfolio/positions", params=params)

    async def get_fills(self, ticker=None, limit=100):
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/portfolio/fills", params=params)

    # ---------- write ----------

    async def create_order(self, ticker, action, side, count, limit_price_cents, client_order_id,
                           order_type="limit", time_in_force=None):
        """Place an order. limit_price_cents is the price for `side` in cents (1-99)."""
        body = {
            "ticker": ticker,
            "action": action,          # "buy" | "sell"
            "side": side,              # "yes" | "no"
            "count": int(count),
            "type": order_type,
            "client_order_id": client_order_id,
        }

        if order_type == "limit":
            if side == "yes":
                body["yes_price"] = int(limit_price_cents)
            else:
                body["no_price"] = int(limit_price_cents)

        if time_in_force:
            body["time_in_force"] = time_in_force

        return await self._request("POST", self.order_path, json_body=body)

    # ---------- diagnostics ----------

    async def selftest(self):
        """Verify signing end to end without placing an order."""
        result = {
            "env": self.env,
            "base_url": self.base_url,
            "key_id_present": bool(self.key_id),
            "private_key_present": bool(self._pem),
            "signing_ok": False,
            "balance_ok": False,
            "balance_cents": None,
            "error": None,
        }

        try:
            self._auth_headers("GET", "/portfolio/balance")
            result["signing_ok"] = True
        except Exception as error:
            result["error"] = str(error)[:300]
            return result

        try:
            balance = await self.get_balance()
            result["balance_ok"] = True
            result["balance_cents"] = balance.get("balance")
        except Exception as error:
            result["error"] = str(error)[:300]

        return result
