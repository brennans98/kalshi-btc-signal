import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

import websockets
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import policy
import risk
import trader
from kalshi_client import KalshiClient

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
COINBASE_PRODUCT = "BTC-USD"

KALSHI_SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "KXBTC15M")
LOOP_INTERVAL = float(os.getenv("LOOP_INTERVAL_SECONDS", "5"))

trades = deque(maxlen=20000)
client = KalshiClient()

state = {
    "spot_connected": False,
    "spot_error": None,
    "kalshi_status": "Starting market discovery",
    "kalshi_error": None,
    "kalshi_market_ticker": None,
    "kalshi_market_title": None,
    "kalshi_close_time": None,
    "kalshi_checked_at": None,
    "market": None,
    "orderbook": None,
    "orderbook_at": None,
    "orderbook_error": None,
}

app = FastAPI(title="BTC Signal Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


def signal():
    return policy.evaluate(trades, state["market"], state["orderbook"])


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
            "series_ticker": KALSHI_SERIES_TICKER,
            "market_ticker": state["kalshi_market_ticker"],
            "market_title": state["kalshi_market_title"],
            "close_time": state["kalshi_close_time"],
            "checked_at": state["kalshi_checked_at"],
            "orderbook_at": state["orderbook_at"],
            "orderbook_error": state["orderbook_error"],
        },
        "execution": trader.snapshot(),
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
            payload = await client.get_markets(KALSHI_SERIES_TICKER)
            market = choose_market(payload.get("markets", []))

            state["kalshi_checked_at"] = int(time.time())

            if market is None:
                state["kalshi_status"] = "No open BTC-15m market found"
                state["kalshi_error"] = None
                state["market"] = None
                state["kalshi_market_ticker"] = None
                state["kalshi_market_title"] = None
                state["kalshi_close_time"] = None
            else:
                state["kalshi_status"] = "Current market discovered"
                state["kalshi_error"] = None
                state["market"] = market
                state["kalshi_market_ticker"] = market.get("ticker")
                state["kalshi_market_title"] = market.get("title")
                state["kalshi_close_time"] = market.get("close_time")

        except Exception as error:
            state["kalshi_status"] = "Market discovery error"
            state["kalshi_error"] = str(error)[:180]

        await asyncio.sleep(20)


async def orderbook_worker():
    """Poll the active market's book. Without this the model has no ask to price against."""
    while True:
        ticker = state.get("kalshi_market_ticker")

        if not ticker:
            state["orderbook"] = None
            await asyncio.sleep(2)
            continue

        try:
            state["orderbook"] = await client.get_orderbook(ticker)
            state["orderbook_at"] = int(time.time())
            state["orderbook_error"] = None
        except Exception as error:
            state["orderbook_error"] = str(error)[:180]

        await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    asyncio.create_task(coinbase_worker())
    asyncio.create_task(kalshi_market_discovery_worker())
    asyncio.create_task(orderbook_worker())
    asyncio.create_task(trader.loop(client, signal, interval=LOOP_INTERVAL))


def require_admin(token):
    expected = os.getenv("ADMIN_TOKEN", "").strip()

    if not expected:
        raise HTTPException(503, "ADMIN_TOKEN is not configured")

    if token != expected:
        raise HTTPException(401, "Invalid admin token")


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state():
    return api_payload()


@app.get("/api/decisions")
async def api_decisions(limit: int = 50):
    return {"decisions": risk.read_decisions(limit=limit)}


@app.get("/health")
async def health():
    execution = trader.snapshot()
    return {
        "ok": True,
        "spot_connected": state["spot_connected"],
        "kalshi_status": state["kalshi_status"],
        "trading_mode": execution["mode"],
        "loop_running": execution["running"],
        "halted": execution["halted"],
    }


@app.post("/admin/selftest")
async def admin_selftest(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return await client.selftest()


@app.post("/admin/halt")
async def admin_halt(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return risk.halt("Manual halt via admin endpoint", manual=True)


@app.post("/admin/resume")
async def admin_resume(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return risk.resume()


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")
