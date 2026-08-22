import asyncio
import json
import time
from collections import deque
from pathlib import Path

import websockets
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
PRODUCT_ID = "BTC-USD"

trades = deque(maxlen=20000)
state = {
    "spot_connected": False,
    "last_error": None,
}

app = FastAPI(title="BTC Signal Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def price_change(seconds_ago: int):
    if len(trades) < 2:
        return None

    target = time.time() - seconds_ago
    old_price = None

    for timestamp, price in trades:
        if timestamp <= target:
            old_price = price
        else:
            break

    if old_price is None:
        return None

    latest_price = trades[-1][1]
    return ((latest_price - old_price) / old_price) * 100


def make_signal():
    if not trades:
        return {
            "action": "NO TRADE",
            "confidence": 0,
            "reason": "Waiting for live BTC data",
        }

    last_update_age = time.time() - trades[-1][0]
    if last_update_age > 5:
        return {
            "action": "NO TRADE",
            "confidence": 0,
            "reason": "BTC data feed is stale",
        }

    move_15 = price_change(15)
    move_60 = price_change(60)

    if move_15 is None or move_60 is None:
        return {
            "action": "NO TRADE",
            "confidence": 0,
            "reason": "Building 60-second live history",
        }

    if move_15 >= 0.035 and move_60 >= 0.070:
        return {
            "action": "UP CANDIDATE",
            "confidence": 55,
            "reason": "BTC is moving upward on both 15-second and 60-second windows",
        }

    if move_15 <= -0.035 and move_60 <= -0.070:
        return {
            "action": "DOWN CANDIDATE",
            "confidence": 55,
            "reason": "BTC is moving downward on both 15-second and 60-second windows",
        }

    return {
        "action": "NO TRADE",
        "confidence": 0,
        "reason": "Short-term BTC momentum does not align",
    }


def current_state():
    signal = make_signal()
    latest_price = trades[-1][1] if trades else None

    return {
        "signal": signal,
        "btc_usd": latest_price,
        "move_15s": price_change(15),
        "move_60s": price_change(60),
        "spot_connected": state["spot_connected"],
        "last_error": state["last_error"],
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
                    "product_ids": [PRODUCT_ID],
                }))

                state["spot_connected"] = True
                state["last_error"] = None

                async for raw_message in websocket:
                    message = json.loads(raw_message)

                    if message.get("channel") != "market_trades":
                        continue

                    for event in message.get("events", []):
                        for trade in event.get("trades", []):
                            price = float(trade["price"])
                            trades.append((time.time(), price))

        except Exception as error:
            state["spot_connected"] = False
            state["last_error"] = str(error)[:120]
            await asyncio.sleep(3)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(coinbase_worker())


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state():
    return current_state()


@app.get("/health")
async def health():
    return {
        "ok": True,
        "spot_connected": state["spot_connected"],
    }


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")