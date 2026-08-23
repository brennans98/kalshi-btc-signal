"""BTC 15-minute scalping dashboard and autonomous trader.

The web process owns the market-data feeds; the trader loop runs beside them in
the same event loop and reads the signal they produce. Everything the trader
does is visible on /api/state.
"""

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
from kalshi_client import KalshiApiError, KalshiAuthError, KalshiClient

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
    "market": None,
    "orderbook": None,
    "book_error": None,
    "book_updated_at": None,
    "kalshi_checked_at": None,
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


# The trader takes two providers rather than one combined object, so the signal
# and the market can each be read at the moment it is needed.
def current_signal():
    return policy.evaluate(list(trades), state["market"], state["orderbook"])


def current_market():
    return state["market"]


def api_payload():
    market = state["market"] or {}

    return {
        "signal": current_signal(),
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
            "book": policy.book_snapshot(state["orderbook"]),
            "book_error": state["book_error"],
            "book_updated_at": state["book_updated_at"],
        },
        "trader": trader.snapshot(),
        "config": config.settings.public_view(),
        "updated_at": int(time.time()),
    }


# ------------------------------------------------------------- feeds


async def coinbase_worker():
    while True:
        try:
            async with websockets.connect(
                COINBASE_WS, ping_interval=20, ping_timeout=20
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "channel": "market_trades",
                            "product_ids": [COINBASE_PRODUCT],
                        }
                    )
                )

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

    Picking the absolute soonest close would hand the trader a market it must
    immediately reject, and it would keep re-picking it while a scalpable
    market sat one slot behind.
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
                state["orderbook"] = None
            else:
                previous = (state["market"] or {}).get("ticker")
                state["kalshi_status"] = "Current market discovered"
                state["kalshi_error"] = None
                state["market"] = market
                if market.get("ticker") != previous:
                    state["orderbook"] = None

        except Exception as error:
            state["kalshi_status"] = "Market discovery error"
            state["kalshi_error"] = str(error)[:180]

        await asyncio.sleep(20)


async def orderbook_worker():
    """Poll the book for the market being traded.

    Without this the entry model has nothing to compare fair value against,
    which is exactly why the original dashboard could only ever say NO TRADE.
    """
    while True:
        ticker = (state["market"] or {}).get("ticker")

        if not ticker:
            state["orderbook"] = None
            await asyncio.sleep(2)
            continue

        try:
            state["orderbook"] = await client.get_orderbook(ticker)
            state["book_error"] = None
            state["book_updated_at"] = int(time.time())
        except Exception as error:
            state["book_error"] = str(error)[:180]

        await asyncio.sleep(config.settings.book_poll_seconds)


@app.on_event("startup")
async def startup():
    asyncio.create_task(coinbase_worker())
    asyncio.create_task(kalshi_market_discovery_worker())
    asyncio.create_task(orderbook_worker())

    if config.settings.is_enabled:
        asyncio.create_task(trader.loop(client, current_signal, current_market))


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()


# ------------------------------------------------------------- routes


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
        "trading_mode": config.settings.trading_mode,
        "trader_running": trader.status.get("running"),
        "halted": risk.is_halted(),
    }


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")


# ------------------------------------------------------------- admin


def require_admin(token):
    expected = config.settings.admin_token
    if not expected:
        raise HTTPException(503, "ADMIN_TOKEN is not configured")
    if token != expected:
        raise HTTPException(401, "Invalid admin token")


@app.get("/api/trader/selftest")
async def api_selftest(x_admin_token: str = Header(default="")):
    """Verify credentials and signing without placing an order."""
    require_admin(x_admin_token)
    return await client.selftest()


@app.post("/api/trader/halt")
async def api_halt(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return risk.halt("Halted manually via API", manual=True)


@app.post("/api/trader/resume")
async def api_resume(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return risk.resume()


@app.post("/api/trader/flatten")
async def api_flatten(x_admin_token: str = Header(default="")):
    """Halt entries, then sell every open scalp at the current bid.

    Deliberately works even when TRADING_MODE is "off": a position left over
    from an earlier live session is exactly what an emergency flatten needs to
    be able to reach.
    """
    require_admin(x_admin_token)
    risk.halt("Flattened manually via API", manual=True)

    lot_mode = config.settings.trading_mode
    if lot_mode == "off":
        lot_mode = "live"

    closed = []

    for lot in scalp.open_lots(lot_mode):
        try:
            book = await client.get_orderbook(lot["ticker"])
        except (KalshiApiError, KalshiAuthError) as error:
            closed.append({"ticker": lot["ticker"], "error": str(error)[:140]})
            continue

        bid = policy.bid_for_side(policy.book_snapshot(book), lot["side"])
        if bid is None:
            closed.append(
                {"ticker": lot["ticker"], "error": "no resting bid to sell into"}
            )
            continue

        marked = scalp.mark(lot_mode, lot["key"], bid)
        if not marked:
            continue

        wanted = marked["count_open"]
        intent = {
            "tier": "manual",
            "kind": "time",
            "count": wanted,
            "limit_price": max(1, min(99, int(round(bid)))),
            "reason": "Manual flatten via API",
        }

        try:
            await trader._execute_exit(client, lot_mode, marked, intent)
        except (KalshiApiError, KalshiAuthError) as error:
            closed.append({"ticker": lot["ticker"], "error": str(error)[:140]})
            continue

        remaining = (scalp.get(lot_mode, lot["key"]) or {}).get("count_open") or 0
        closed.append(
            {
                "ticker": lot["ticker"],
                "side": lot["side"],
                "sold": wanted - remaining,
                "price_cents": intent["limit_price"],
                "still_open": remaining,
            }
        )

    return {"halted": True, "mode": lot_mode, "closed": closed}


@app.get("/api/trader/decisions")
async def api_decisions(limit: int = 50, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return {"decisions": risk.read_decisions(min(limit, 500))}


@app.get("/api/trader/scalps")
async def api_scalps(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return {
        "live": scalp.view("live"),
        "dryrun": scalp.view("dryrun"),
        "active_mode": config.settings.trading_mode,
    }
