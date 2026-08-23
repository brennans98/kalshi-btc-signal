import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import policy
from kalshi_client import KalshiClient
from trader import Trader

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
COINBASE_PRODUCT = "BTC-USD"

KALSHI_PUBLIC_API = os.getenv("KALSHI_PUBLIC_API", "https://external-api.kalshi.com/trade-api/v2")
KALSHI_SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "KXBTC15M")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

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
    "orderbook": None,
    "orderbook_error": None,
    "orderbook_at": None,
}

app = FastAPI(title="BTC Signal Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def current_market():
    if not state["kalshi_market_ticker"]:
        return None
    return {
        "ticker": state["kalshi_market_ticker"],
        "title": state["kalshi_market_title"],
        "close_time": state["kalshi_close_time"],
    }


def trader_context():
    spot = trades[-1][1] if trades else None
    return spot, list(trades), current_market(), state["orderbook"]


trader = Trader(trader_context)


def require_admin(token):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")


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
        return policy.no_trade("Waiting for live BTC-USD trades")

    if time.time() - trades[-1][0] > 5:
        return policy.no_trade("Coinbase BTC feed is stale")

    spot, tape, market, orderbook = trader_context()
    return policy.evaluate(spot, tape, market, orderbook)


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
            "book": policy.best_levels(state["orderbook"]) if state["orderbook"] else None,
            "book_error": state["orderbook_error"],
            "book_at": state["orderbook_at"],
        },
        "trader": trader.snapshot(),
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
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{KALSHI_PUBLIC_API}/markets",
                    params={
                        "series_ticker": KALSHI_SERIES_TICKER,
                        "status": "open",
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
                previous = state["kalshi_market_ticker"]
                state["kalshi_status"] = "Current market discovered"
                state["kalshi_error"] = None
                state["kalshi_market_ticker"] = market.get("ticker")
                state["kalshi_market_title"] = market.get("title")
                state["kalshi_close_time"] = market.get("close_time")

                if previous != state["kalshi_market_ticker"]:
                    state["orderbook"] = None
                    state["orderbook_at"] = None

        except Exception as error:
            state["kalshi_status"] = "Market discovery error"
            state["kalshi_error"] = str(error)[:180]

        await asyncio.sleep(20)


async def orderbook_worker():
    """Poll the book for the active market. This is what was missing before."""
    public_client = KalshiClient()

    while True:
        ticker = state["kalshi_market_ticker"]

        if not ticker:
            await asyncio.sleep(2)
            continue

        try:
            if public_client.configured:
                book = await public_client.get_orderbook(ticker)
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(
                        f"{KALSHI_PUBLIC_API}/markets/{ticker}/orderbook",
                        params={"depth": 8},
                    )
                    response.raise_for_status()
                    book = response.json()

            state["orderbook"] = book
            state["orderbook_error"] = None
            state["orderbook_at"] = int(time.time())
        except Exception as error:
            state["orderbook_error"] = str(error)[:180]

        await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    asyncio.create_task(coinbase_worker())
    asyncio.create_task(kalshi_market_discovery_worker())
    asyncio.create_task(orderbook_worker())
    asyncio.create_task(trader.run())


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state():
    return api_payload()


@app.get("/api/trader")
async def api_trader():
    return trader.snapshot()


@app.get("/api/trader/selftest")
async def api_selftest(x_admin_token: str = Header(default="")):
    """Verify credentials and signing without placing an order."""
    require_admin(x_admin_token)
    return await trader.client.selftest()


@app.post("/api/trader/halt")
async def api_halt(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    trader.risk.halt("manual halt via admin endpoint")
    return trader.risk.snapshot()


@app.post("/api/trader/resume")
async def api_resume(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    trader.risk.resume()
    return trader.risk.snapshot()


@app.get("/health")
async def health():
    return {
        "ok": True,
        "spot_connected": state["spot_connected"],
        "kalshi_status": state["kalshi_status"],
        "trading_mode": trader.mode,
        "halted": trader.risk.state.get("halted", False),
    }


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")
