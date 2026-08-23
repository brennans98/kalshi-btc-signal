import asyncio
import json
import time
from collections import deque
from pathlib import Path

import websockets
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
import policy
import risk
import scalp
import trader
from kalshi_client import KalshiClient

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
COINBASE_PRODUCT = "BTC-USD"

trades = deque(maxlen=20000)
books = {}

state = {
    "spot_connected": False,
    "spot_error": None,
    "kalshi_status": "Starting market discovery",
    "kalshi_error": None,
    "market": None,
    "kalshi_checked_at": None,
    "book_error": None,
    "book_updated_at": None,
}

app = FastAPI(title="BTC 15m Scalper")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
client = KalshiClient()


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


def current_market():
    return state.get("market")


def current_book(ticker=None):
    market = current_market()
    ticker = ticker or (market or {}).get("ticker")
    return books.get(ticker) if ticker else None


def signal():
    return policy.evaluate(trades, current_market(), current_book())


def api_payload():
    market = current_market() or {}
    book = policy.book_snapshot(current_book())

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
            "series_ticker": config.settings.series_ticker,
            "market_ticker": market.get("ticker"),
            "market_title": market.get("title"),
            "close_time": market.get("close_time"),
            "checked_at": state["kalshi_checked_at"],
            "book": book,
            "book_error": state["book_error"],
            "book_updated_at": state["book_updated_at"],
        },
        "trader": trader.snapshot(),
        "config": config.settings.public_view(),
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
    """The soonest-closing market that still has enough runway to scalp.

    Picking the very soonest close would keep selecting markets inside the
    settlement guard, which the policy then rejects every tick.
    """
    cfg = config.settings
    candidates = []

    for market in markets:
        if market.get("status") not in ("active", "open") or not market.get("ticker"):
            continue

        remaining = policy.seconds_to_close(market)
        if remaining is None or remaining < cfg.min_seconds_to_close:
            continue

        candidates.append((remaining, market))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


async def kalshi_market_discovery_worker():
    while True:
        try:
            payload = await client.get_markets(config.settings.series_ticker)
            market = choose_market(payload.get("markets", []))
            state["kalshi_checked_at"] = int(time.time())

            if market is None:
                state["kalshi_status"] = "No scalpable BTC-15m market in the window"
                state["kalshi_error"] = None
                state["market"] = None
            else:
                state["kalshi_status"] = "Current market discovered"
                state["kalshi_error"] = None
                state["market"] = market

        except Exception as error:
            state["kalshi_status"] = "Market discovery error"
            state["kalshi_error"] = str(error)[:180]

        await asyncio.sleep(20)


async def orderbook_worker():
    """Poll the book for the active market and for anything still held.

    Open lots are included because the exit ladder needs a live bid for every
    position, including one whose market is no longer the entry candidate.
    """
    while True:
        try:
            tickers = []
            market = current_market()
            if market and market.get("ticker"):
                tickers.append(market["ticker"])

            mode = trader.mode()
            for lot in scalp.open_lots(mode if mode != "off" else "dryrun"):
                if lot["ticker"] not in tickers:
                    tickers.append(lot["ticker"])

            for ticker in tickers:
                books[ticker] = await client.get_orderbook(ticker)

            for stale in [key for key in books if key not in tickers]:
                books.pop(stale, None)

            state["book_error"] = None
            state["book_updated_at"] = int(time.time())

        except Exception as error:
            state["book_error"] = str(error)[:180]

        await asyncio.sleep(config.settings.book_poll_seconds)


@app.on_event("startup")
async def startup():
    Path(config.settings.data_dir).mkdir(parents=True, exist_ok=True)

    asyncio.create_task(coinbase_worker())
    asyncio.create_task(kalshi_market_discovery_worker())
    asyncio.create_task(orderbook_worker())

    if config.settings.is_enabled:
        asyncio.create_task(
            trader.loop(client, signal, current_market, current_book)
        )


def require_admin(token):
    expected = config.settings.admin_token
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
    return {"decisions": risk.read_decisions(min(limit, 200))}


@app.get("/api/scalp")
async def api_scalp():
    mode = trader.mode()
    return scalp.view(mode if mode != "off" else "dryrun")


@app.get("/api/trader/selftest")
async def api_selftest(x_admin_token: str = Header(None)):
    require_admin(x_admin_token)
    return await client.selftest()


@app.post("/api/trader/halt")
async def api_halt(x_admin_token: str = Header(None)):
    require_admin(x_admin_token)
    return risk.halt("Manual halt via admin endpoint", manual=True)


@app.post("/api/trader/resume")
async def api_resume(x_admin_token: str = Header(None)):
    require_admin(x_admin_token)
    return risk.resume()


@app.post("/api/trader/flatten")
async def api_flatten(x_admin_token: str = Header(None)):
    """Halt entries and exit every open lot at the current bid."""
    require_admin(x_admin_token)
    risk.halt("Manual flatten via admin endpoint", manual=True)

    mode = trader.mode()
    for lot in scalp.open_lots(mode if mode != "off" else "dryrun"):
        snapshot = policy.book_snapshot(current_book(lot["ticker"]))
        bid = policy.bid_for_side(snapshot, lot["side"])
        if bid is None:
            continue

        if mode == "live":
            try:
                await client.sell(
                    ticker=lot["ticker"],
                    side=lot["side"],
                    count=lot["count_open"],
                    price_cents=max(1, int(bid)),
                    client_order_id=f"flatten-{int(time.time())}",
                )
            except Exception as error:
                risk.log_decision(
                    {"event": "flatten", "ticker": lot["ticker"], "error": str(error)}
                )
                continue

        scalp.record_exit(
            mode if mode != "off" else "dryrun",
            lot["key"],
            "manual",
            "time",
            lot["count_open"],
            int(bid),
        )

    return {"flattened": True, "scalp": scalp.view(mode if mode != "off" else "dryrun")}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "spot_connected": state["spot_connected"],
        "kalshi_status": state["kalshi_status"],
        "trading_mode": config.settings.trading_mode,
        "trader_running": trader.status["running"],
        "halted": risk.is_halted(),
    }


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")
