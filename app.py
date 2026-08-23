import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from kalshi_client import KalshiAuthError, KalshiClient, KalshiError
from policy import evaluate
from risk import RiskManager
from trader import Trader

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
COINBASE_PRODUCT = "BTC-USD"

trades = deque(maxlen=20000)

state = {
    "spot_connected": False,
    "spot_error": None,
    "kalshi_status": "Starting market discovery",
    "kalshi_error": None,
    "kalshi_market": None,
    "kalshi_market_ticker": None,
    "kalshi_market_title": None,
    "kalshi_close_time": None,
    "kalshi_checked_at": None,
    "orderbook": None,
    "orderbook_error": None,
    "orderbook_at": None,
}

app = FastAPI(title="BTC Signal Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

client = KalshiClient(
    base_url=settings.base_url,
    key_id=settings.key_id,
    private_key_pem=settings.private_key_pem,
)
risk = RiskManager(settings)


def pct_move(seconds_ago):
    if len(trades) < 2:
        return None

    cutoff = time.time() - seconds_ago
    earlier = None

    for timestamp, price in trades:
        if timestamp <= cutoff:
            earlier = price
        else:
            break

    if earlier is None:
        return None

    return ((trades[-1][1] - earlier) / earlier) * 100


def seconds_to_close() -> Optional[float]:
    raw = state.get("kalshi_close_time")
    if not raw:
        return None
    try:
        closes = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (closes - datetime.now(timezone.utc)).total_seconds()


def signal():
    """Current decision. Shared by the dashboard and the trading loop."""
    return evaluate(
        spot=trades[-1][1] if trades else None,
        trades=trades,
        market=state.get("kalshi_market"),
        orderbook=state.get("orderbook"),
        seconds_to_close=seconds_to_close(),
        settings=settings,
        now=time.time(),
    )


trader = Trader(settings=settings, client=client, risk=risk, decide=signal)


def api_payload():
    return {
        "signal": signal(),
        "btc_usd": trades[-1][1] if trades else None,
        "move_15s": pct_move(15),
        "move_60s": pct_move(60),
        "spot_connected": state["spot_connected"],
        "last_error": state["spot_error"],
        "kalshi": {
            "status": state["kalshi_status"],
            "error": state["kalshi_error"],
            "series_ticker": settings.series_ticker,
            "market_ticker": state["kalshi_market_ticker"],
            "market_title": state["kalshi_market_title"],
            "close_time": state["kalshi_close_time"],
            "checked_at": state["kalshi_checked_at"],
            "orderbook_at": state["orderbook_at"],
            "orderbook_error": state["orderbook_error"],
        },
        "trader": trader.public_view(),
        "risk": risk.public_view(),
        "config": settings.public_view(),
        "updated_at": int(time.time()),
    }


async def coinbase_worker():
    while True:
        try:
            async with websockets.connect(
                COINBASE_WS,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                await websocket.send(json.dumps({
                    "type": "subscribe",
                    "channel": "market_trades",
                    "product_ids": [COINBASE_PRODUCT],
                }))

                state["spot_connected"] = True
                state["spot_error"] = None

                async for raw_message in websocket:
                    message = json.loads(raw_message)

                    if message.get("channel") != "market_trades":
                        continue

                    for event in message.get("events", []):
                        for trade in event.get("trades", []):
                            trades.append((time.time(), float(trade["price"])))

        except Exception as error:
            state["spot_connected"] = False
            state["spot_error"] = str(error)[:160]
            await asyncio.sleep(3)


def choose_market(markets):
    open_markets = [
        market for market in markets
        if market.get("status") == "active" and market.get("ticker")
    ]

    if not open_markets:
        return None

    open_markets.sort(
        key=lambda market: market.get("close_time", "9999-12-31T23:59:59Z")
    )
    return open_markets[0]


async def kalshi_market_discovery_worker():
    while True:
        try:
            markets = await client.markets(settings.series_ticker)
            market = choose_market(markets)

            state["kalshi_checked_at"] = int(time.time())

            if market is None:
                state["kalshi_status"] = "No open BTC-15m market found"
                state["kalshi_error"] = None
                state["kalshi_market"] = None
                state["kalshi_market_ticker"] = None
                state["kalshi_market_title"] = None
                state["kalshi_close_time"] = None
            else:
                state["kalshi_status"] = "Current market discovered"
                state["kalshi_error"] = None
                state["kalshi_market"] = market
                state["kalshi_market_ticker"] = market.get("ticker")
                state["kalshi_market_title"] = market.get("title")
                state["kalshi_close_time"] = market.get("close_time")

        except Exception as error:
            state["kalshi_status"] = "Market discovery error"
            state["kalshi_error"] = str(error)[:180]

        await asyncio.sleep(20)


async def orderbook_worker():
    """Keep the book for the current market fresh - this is the input the
    signal was previously missing."""
    while True:
        ticker = state.get("kalshi_market_ticker")
        if not ticker:
            state["orderbook"] = None
            await asyncio.sleep(settings.book_poll_seconds)
            continue

        try:
            book = await client.orderbook(ticker)
            state["orderbook"] = book
            state["orderbook_at"] = int(time.time())
            state["orderbook_error"] = None
        except Exception as error:
            state["orderbook"] = None
            state["orderbook_error"] = str(error)[:180]

        await asyncio.sleep(settings.book_poll_seconds)


@app.on_event("startup")
async def startup():
    asyncio.create_task(coinbase_worker())
    asyncio.create_task(kalshi_market_discovery_worker())
    asyncio.create_task(orderbook_worker())
    if settings.is_enabled:
        asyncio.create_task(trader.run())


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state():
    return api_payload()


@app.get("/health")
async def health():
    return {
        "ok": True,
        "spot_connected": state["spot_connected"],
        "kalshi_status": state["kalshi_status"],
        "trading_mode": settings.trading_mode,
        "halted": bool(risk.state.get("halted")),
    }


def require_admin(token: Optional[str]) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured")
    if token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.post("/api/trader/halt")
async def halt(x_admin_token: Optional[str] = Header(default=None)):
    """Kill switch. Latches until explicitly resumed."""
    require_admin(x_admin_token)
    risk.halt("Manual halt")
    return {"halted": True}


@app.post("/api/trader/resume")
async def resume(x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    risk.resume()
    return {"halted": False}


@app.get("/api/trader/selftest")
async def selftest(x_admin_token: Optional[str] = Header(default=None)):
    """Verify the credentials and signing path without placing an order."""
    require_admin(x_admin_token)
    if not client.can_trade:
        return {
            "ok": False,
            "detail": client.key_error or "credentials are not configured",
        }
    try:
        balance = await client.balance_cents()
        positions = await client.positions()
    except KalshiAuthError as error:
        return {"ok": False, "detail": f"authentication rejected: {error}"}
    except KalshiError as error:
        return {"ok": False, "detail": str(error)[:300]}
    return {
        "ok": True,
        "environment": settings.kalshi_env,
        "balance_dollars": round(balance / 100, 2),
        "open_positions": len(positions),
    }


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")
