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
import binascii
import os
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

WS_PATH = "/trade-api/ws/v2"
# Kalshi's dedicated WebSocket hosts (recommended for API integrations).
# The shared api.elections.kalshi.com host remains supported; override with
# KALSHI_WS_URL if the dedicated host ever misbehaves.
PROD_WS_URL = os.environ.get("KALSHI_WS_URL") or "wss://external-api-ws.kalshi.com" + WS_PATH
DEMO_WS_URL = os.environ.get("KALSHI_DEMO_WS_URL") or "wss://external-api-ws.demo.kalshi.co" + WS_PATH


class KalshiAuthError(RuntimeError):
    """Credentials are missing, malformed, or rejected. Not retryable."""


class KalshiApiError(RuntimeError):
    """Transport or API error. May be transient."""


def _decode_if_base64(text):
    """Return the PEM inside a base64 blob, or "" when `text` isn't one.

    Some deployments store the key as KALSHI_PRIVATE_KEY_BASE64 — the whole PEM,
    header lines included, base64-encoded a second time. That is a legitimate
    way to move a multi-line secret through a single-line env var, so decode it
    rather than rejecting it. Only used when the raw value has no PEM header,
    so a real PEM is never round-tripped through this.
    """
    candidate = "".join(text.split())
    if not candidate:
        return ""
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return ""
    try:
        text_out = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text_out if "-----BEGIN" in text_out else ""


def _load_private_key(raw):
    if not raw:
        return None

    # Railway variables commonly arrive with literal backslash-n sequences.
    pem = raw.replace(chr(92) + "n", chr(10)).strip()

    # A PEM always carries a "-----BEGIN" header. Without one, the value may be
    # a base64-wrapped PEM; try that before failing.
    if "-----BEGIN" not in pem:
        unwrapped = _decode_if_base64(pem)
        if unwrapped:
            pem = unwrapped.replace(chr(92) + "n", chr(10)).strip()

    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as error:
        raise KalshiAuthError(
            "Kalshi private key could not be parsed as a PEM private key "
            f"(accepted vars: {', '.join(config.PRIVATE_KEY_ALIASES)}): {error}"
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

        # Clock drift tracking against the exchange (see _observe_clock).
        self.clock_skew_seconds = None
        self.clock_rtt_seconds = 0.0
        self.clock_samples = 0

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
                limits=httpx.Limits(
                    max_keepalive_connections=8,
                    max_connections=16,
                    # Kalshi's edge closes idle connections; without an expiry
                    # under the server's idle timeout, the first request after
                    # a quiet stretch pays a full TCP+TLS handshake -- exactly
                    # when the loop is trying to fire an urgent order.
                    keepalive_expiry=50.0,
                ),
            )
        return self._http

    async def warm(self):
        """Keep the pooled TLS connection hot with a cheap public request.

        Called on a timer by app.py. A connection that sat idle past the
        server's idle timeout is silently gone, and the next order pays the
        full handshake to find out. Pinging /exchange/status well inside the
        keepalive_expiry window means the pool always holds at least one
        established connection when an order needs one.
        """
        await self.request("GET", "/exchange/status", authenticate=False)

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
            return (
                "No Kalshi API key id found. Set one of: "
                + ", ".join(config.KEY_ID_ALIASES)
            )
        if not self.private_key:
            return (
                "No Kalshi private key found. Set one of: "
                + ", ".join(config.PRIVATE_KEY_ALIASES)
            )
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

        sent_at = time.time()
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

        self._observe_clock(response, sent_at, time.time())

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

    def _observe_clock(self, response, sent_at, received_at):
        """Estimate how far our clock is ahead of the exchange's.

        Every decision about time-to-close is made against the local clock, but
        the market closes on the exchange's clock. If ours drifts behind, we
        believe there is more time left than there is -- the settlement guard
        fires late and a position can reach expiry unexited, which is a total
        loss on that position rather than a small one. NTP normally keeps this
        within milliseconds; this exists so that when it does not, we find out
        from data instead of from a loss.

        The server Date header is second-granular, so the estimate is only good
        to about a second. That is precise enough for its purpose: it is a
        drift alarm, not a time source.
        """
        raw_date = None
        try:
            raw_date = response.headers.get("Date")
        except Exception:
            return
        if not raw_date:
            return

        try:
            server_epoch = parsedate_to_datetime(raw_date).timestamp()
        except (TypeError, ValueError, IndexError):
            return

        # Compare against the midpoint of the request, so one-way network
        # latency does not masquerade as clock drift.
        local_midpoint = (sent_at + received_at) / 2.0
        sample = local_midpoint - server_epoch

        self.clock_rtt_seconds = max(0.0, received_at - sent_at)
        # Exponential smoothing: a single sample is dominated by the header's
        # one-second granularity, so no single reading should move the estimate
        # far.
        if self.clock_skew_seconds is None:
            self.clock_skew_seconds = sample
        else:
            self.clock_skew_seconds = 0.8 * self.clock_skew_seconds + 0.2 * sample
        self.clock_samples += 1

    def clock_status(self):
        """Skew estimate for the dashboard, plus the guard adjustment it implies."""
        skew = self.clock_skew_seconds
        # Only trust the estimate once smoothing has had a few samples, and
        # never let it widen the trading window -- only shorten it.
        trusted = skew is not None and self.clock_samples >= 5
        # A huge reading means the local clock is broken, not that the market
        # closes nine days early. Shortening the window by that amount would
        # silently stop all trading and look like a strategy that found no
        # setups. Past this bound it is an alarm to act on, not a correction to
        # quietly apply.
        severe = bool(trusted and abs(skew) > 120.0)
        adjustment = 0.0
        if trusted and not severe:
            adjustment = max(-30.0, min(0.0, skew))

        return {
            "skew_seconds": None if skew is None else round(skew, 3),
            "rtt_seconds": round(self.clock_rtt_seconds, 4),
            "samples": self.clock_samples,
            "trusted": bool(trusted),
            # Second-granular header, so treat anything under ~1.5s as noise.
            "drifting": bool(trusted and abs(skew) > 1.5),
            "severe": severe,
            "guard_adjustment_seconds": round(adjustment, 3),
        }

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
        return await self.request("GET", "/account/limits")

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

    async def _place(
        self, ticker, action, side, count, price_cents, client_order_id, tif, **extra
    ):
        """Submit a limit order via the V2 endpoint (/portfolio/events/orders).

        Limit, never market: a market order on a thin 15-minute book can fill
        far from the price the edge was calculated against, which silently
        invalidates the decision that produced it. On a scalp where the whole
        target is a few cents, that is the difference between the trade working
        and not.

        V2 quotes everything on the YES leg of a single book:
          buy yes  -> bid at yes price
          buy no   -> ask at 100 - no price   (buying NO == selling YES)
          sell yes -> ask at yes price
          sell no  -> bid at 100 - no price   (selling NO == buying YES back)
        Prices are fixed-point dollar strings, counts fixed-point contract
        strings. time_in_force and self_trade_prevention_type are required.
        """
        price_cents = int(price_cents)
        if side == "yes":
            book_side = "bid" if action == "buy" else "ask"
            yes_cents = price_cents
        else:
            book_side = "ask" if action == "buy" else "bid"
            yes_cents = 100 - price_cents

        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": f"{int(count)}.00",
            "price": f"{yes_cents // 100}.{yes_cents % 100:02d}00",
            "time_in_force": tif or "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        body.update({key: value for key, value in extra.items() if value is not None})
        return await self.request("POST", self.order_path, body=body)

    @staticmethod
    def filled_count(response):
        """Contracts filled by a V2 order response, as a whole number.

        V2 returns 201 even for a fill-or-kill order that was killed unfilled,
        so callers must check this rather than treating success as a fill.
        fill_count is a fixed-point string like '6.00'.
        """
        try:
            return int(float((response or {}).get("fill_count") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def average_fill_price_cents(response):
        """The VWAP the order ACTUALLY filled at, in cents, or None.

        Kalshi returns `average_fill_price` as a fixed-point dollar string, and
        only when fill_count > 0. This matters because an order that sweeps more
        than one price level does not fill at the best offer, and recording the
        best offer as the entry basis makes every downstream number wrong: the
        stop sits in the wrong place, the trail arms early, and realized P&L is
        overstated by exactly the slippage. Using the exchange's own VWAP means
        the basis is not a model of the fill, it is the fill.
        """
        raw = (response or {}).get("average_fill_price")
        if raw is None:
            return None
        try:
            return float(raw) * 100.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def average_fee_paid_cents(response):
        """Fee actually charged per contract, in cents, or None.

        Also fixed-point dollars, also only present when fill_count > 0. The
        modeled fee is an estimate that rounds per fill; this is the real number.
        """
        raw = (response or {}).get("average_fee_paid")
        if raw is None:
            return None
        try:
            return float(raw) * 100.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _fp_int(value):
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def order_progress(cls, order_payload):
        """(status, filled, remaining) for a GET /portfolio/orders/{id} payload.

        status is 'resting', 'executed', or 'canceled'. Counts are fixed-point
        strings ('6.00') parsed to whole contracts. A canceled order can still
        carry fills that landed before the cancel, so callers must always
        reconcile fill_count, not just the status.
        """
        order = (order_payload or {}).get("order") or order_payload or {}
        status = order.get("status") or ""
        filled = cls._fp_int(order.get("fill_count_fp", order.get("fill_count")))
        remaining = cls._fp_int(order.get("remaining_count_fp", order.get("remaining_count")))
        return status, filled, remaining

    async def get_order(self, order_id):
        """Status and fill progress of one order."""
        return await self.request("GET", f"/portfolio/orders/{order_id}")

    async def cancel_order(self, order_id):
        """Cancel a resting order via the V2 endpoint.

        Returns the V2 cancel payload ({order_id, reduced_by, ts_ms}).
        Cancelling an order that already executed or expired raises a 404
        KalshiApiError, which callers treat as 'nothing left to cancel'.
        """
        return await self.request("DELETE", f"{self.order_path}/{order_id}")

    async def place_resting(
        self,
        ticker,
        action,
        side,
        count,
        price_cents,
        client_order_id,
        expire_epoch=None,
        reduce_only=False,
    ):
        """Rest a post-only limit order on the book (the maker leg).

        post_only guarantees the order never crosses: if the book moved and
        the price would match immediately, Kalshi rejects or cancels it rather
        than filling as a taker, so the zero-fee assumption cannot silently
        break. good_till_canceled plus expiration_time makes every resting
        order self-cleaning -- a crashed process leaves nothing on the book
        past its expiry.
        """
        return await self._place(
            ticker,
            action,
            side,
            count,
            price_cents,
            client_order_id,
            "good_till_canceled",
            post_only=True,
            expiration_time=int(expire_epoch) if expire_epoch else None,
            reduce_only=True if reduce_only else None,
        )

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