import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
COINBASE_PRODUCT = "BTC-USD"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "KXBTC15M")

trades = deque(maxlen=20000)

state = {
    "spot_connected": False,
    "spot_error": None,
    "kalshi_status": "Starting market discovery",
    "kalshi_error": None,
    "kalshi_market_ticker": None,
    "kalshi_market_title": None,
    "kalshi_close_time": None,
    "kalshi_checked_at": None,
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
    if not trades:
        return {
            "action": "NO TRADE",
            "confidence": 0,
            "reason": "Waiting for live BTC-USD trades",
        }

    if time.time() - trades[-1][0] > 5:
        return {
            "action": "NO TRADE",
            "confidence": 0,
            "reason": "Coinbase BTC feed is stale",
        }

    move_15 = pct_move(15)
    move_60 = pct_move(60)

    if move_15 is None or move_60 is None:
        return {
            "action": "NO TRADE",
            "confidence": 0,
            "reason": "Building 60 seconds of live BTC history",
        }

    return {
        "action": "NO TRADE",
        "confidence": 0,
        "reason": "Kalshi book model has not been enabled; no trade signal is issued",
    }


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
        },
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
    if market.get("status") == "open" and market.get("ticker")
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
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{KALSHI_API}/markets",
                    params={
    "series_ticker": KALSHI_SERIES_TICKER,
    "status": "active",
    "limit": 100,
},
                )
                response.raise_for_status()
                payload = response.json()

            markets = payload.get("markets", [])
            market = choose_market(markets)

            state["kalshi_checked_at"] = int(time.time())

            if market is None:
                state["kalshi_status"] = "No open BTC-15m market found"
                state["kalshi_error"] = None
                state["kalshi_market_ticker"] = None
                state["kalshi_market_title"] = None
                state["kalshi_close_time"] = None
            else:
                state["kalshi_status"] = "Current market discovered"
                state["kalshi_error"] = None
                state["kalshi_market_ticker"] = market.get("ticker")
                state["kalshi_market_title"] = market.get("title")
                state["kalshi_close_time"] = market.get("close_time")

        except Exception as error:
            state["kalshi_status"] = "Market discovery error"
            state["kalshi_error"] = str(error)[:180]

        await asyncio.sleep(20)


@app.on_event("startup")
async def startup():
    asyncio.create_task(coinbase_worker())
    asyncio.create_task(kalshi_market_discovery_worker())


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
    }


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")