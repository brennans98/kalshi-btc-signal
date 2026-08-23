"""
Authenticated client for the Kalshi trade-api v2.

Authentication is RSA-PSS request signing. For every authenticated request the
string  <timestamp_ms><HTTP_METHOD><path>  is signed, where <path> includes the
"/trade-api/v2" prefix and excludes any query string. Three headers carry the
result:

    KALSHI-ACCESS-KEY        API key id
    KALSHI-ACCESS-TIMESTAMP  the same millisecond timestamp that was signed
    KALSHI-ACCESS-SIGNATURE  base64 RSA-PSS(SHA256, salt = digest length)

Market-data endpoints are public; everything under /portfolio requires the
headers.
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiError(RuntimeError):
    """Any non-success response from Kalshi."""


class KalshiAuthError(KalshiError):
    """Credentials missing, unparseable, or rejected."""


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        key_id: str = "",
        private_key_pem: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key_id = (key_id or "").strip()
        self._timeout = timeout
        self._key_error: Optional[str] = None
        self._private_key = self._load_key(private_key_pem)

    def _load_key(self, pem: str):
        pem = (pem or "").strip()
        if not pem:
            return None
        # Environment variable panels commonly turn newlines into literal \n.
        pem = pem.replace("\\n", "\n")
        try:
            return serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception as error:  # noqa: BLE001 - surfaced as status, not raised at import
            self._key_error = f"private key could not be parsed: {error}"
            return None

    @property
    def can_trade(self) -> bool:
        return bool(self.key_id and self._private_key)

    @property
    def key_error(self) -> Optional[str]:
        return self._key_error

    # ---- signing -------------------------------------------------------

    def _auth_headers(self, method: str, path: str) -> dict:
        if not self.can_trade:
            raise KalshiAuthError(
                self._key_error
                or "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY are required for portfolio calls"
            )
        timestamp = str(int(time.time() * 1000))
        signed_path = urlparse(self.base_url + path).path
        message = f"{timestamp}{method.upper()}{signed_path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        authenticated: bool = False,
    ) -> Any:
        headers = (
            self._auth_headers(method, path)
            if authenticated
            else {"Accept": "application/json"}
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method.upper(),
                f"{self.base_url}{path}",
                params=params,
                json=json_body,
                headers=headers,
            )

        if response.status_code in (401, 403):
            raise KalshiAuthError(
                f"{response.status_code} from {path}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise KalshiError(f"{response.status_code} from {path}: {response.text[:300]}")
        if not response.content:
            return {}
        return response.json()

    # ---- market data (public) ------------------------------------------

    async def markets(self, series_ticker: str, status: str = "open", limit: int = 100) -> list[dict]:
        payload = await self.request(
            "GET",
            "/markets",
            params={"series_ticker": series_ticker, "status": status, "limit": limit},
        )
        return payload.get("markets", [])

    async def orderbook(self, ticker: str, depth: int = 10) -> dict:
        payload = await self.request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        return payload.get("orderbook", {}) or {}

    # ---- portfolio (authenticated) -------------------------------------

    async def balance_cents(self) -> int:
        payload = await self.request("GET", "/portfolio/balance", authenticated=True)
        return int(payload.get("balance", 0))

    async def positions(self) -> list[dict]:
        payload = await self.request(
            "GET",
            "/portfolio/positions",
            params={"settlement_status": "unsettled", "limit": 200},
            authenticated=True,
        )
        return payload.get("market_positions", []) or []

    async def fills(self, limit: int = 100) -> list[dict]:
        payload = await self.request(
            "GET", "/portfolio/fills", params={"limit": limit}, authenticated=True
        )
        return payload.get("fills", []) or []

    async def create_order(
        self,
        *,
        order_path: str,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: int,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Place a limit order. `side` is "yes"/"no", `action` is "buy"/"sell"."""
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": int(count),
            "type": "limit",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if side == "yes":
            body["yes_price"] = int(price_cents)
        else:
            body["no_price"] = int(price_cents)
        return await self.request("POST", order_path, json_body=body, authenticated=True)
