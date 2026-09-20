"""BTC 15-minute dashboard and autonomous trader.

The web process owns the market-data feeds; the trader loop runs beside them in
the same event loop and reads the signal they produce. Everything the trader
does is visible on /api/state.

Three feeds run here, and the distinction between them matters:

  - feeds.hub: several spot exchanges at once, merged into a median tape. Used
    for the underlying read and for a divergence veto -- when the venues
    disagree, one of them is broken, and neither the bot nor anyone else knows
    which, so it stands down.
  - kalshi_orderbook_ws_worker: the authenticated Kalshi L2 book, applied delta
    by delta into an in-memory book. This is the ONLY feed an order is allowed
    to block on, and the trader reads it directly out of this process instead
    of re-fetching over REST.
  - booktape: every published book snapshot, kept as a per-contract time series.
    This is the contract's own chart -- the thing the strategy previously had no
    access to, and the reason it could not see a dip in the contract price that
    a human watching the screen sees instantly.
"""

import asyncio
import json
import time
from pathlib import Path

import websockets
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import booktape
import config
import feeds
import policy
import risk
import scalp
import trader
from kalshi_client import KalshiApiError, KalshiAuthError, KalshiClient

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

trades = feeds.hub.trades

state = {
    "kalshi_status": "Starting market discovery",
    "kalshi_error": None,
    "market": None,
    "orderbook": None,
    "book_error": None,
    "book_updated_at": None,
    "book_source": None,
    "book_published_at": None,
    "kalshi_checked_at": None,
}

app = FastAPI(title="BTC 15m Scalper")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

client = KalshiClient()

_local_book = {"ticker": None, "yes": {}, "no": {}}


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


def current_signal():
    return policy.evaluate(
        list(trades),
        state["market"],
        state["orderbook"],
        spot_status=feeds.hub.status(),
        clock_status=client.clock_status(),
    )


def current_market():
    return state["market"]


def public_trade_log(limit=30):
    events = []
    for record in risk.read_decisions(200):
        event = record.get("event")
        if event == "entry" and record.get("outcome") in ("opened", "simulated"):
            signal = record.get("signal") or {}
            sizing = record.get("sizing") or {}
            events.append({
                "at": record.get("logged_at"), "type": "entry",
                "mode": record.get("mode"), "ticker": signal.get("ticker"),
                "side": signal.get("side"), "count": sizing.get("count"),
                "price_cents": signal.get("price_cents"),
                "realized_cents": None, "detail": signal.get("reason"),
            })
        elif event == "exit" and record.get("outcome") in ("exited", "simulated"):
            events.append({
                "at": record.get("logged_at"), "type": "exit",
                "mode": record.get("mode"), "ticker": record.get("ticker"),
                "side": record.get("side"), "count": record.get("count"),
                "price_cents": record.get("limit_price_cents"),
                "realized_cents": record.get("realized_cents"),
                "detail": record.get("reason"),
            })
        if len(events) >= limit:
            break
    return events


def api_payload():
    market = state["market"] or {}
    return {
        "signal": current_signal(),
        "btc_usd": trades[-1][1] if trades else None,
        "move_15s": pct_move(15),
        "move_60s": pct_move(60),
        "spot_connected": feeds.hub.status()["connected_any"],
        "last_error": feeds.hub.status()["error"],
        "spot_feeds": feeds.hub.status(),
        "divergence_veto_count": policy.divergence_veto_count,
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
            "book_source": state["book_source"],
            "tape": booktape.view(market.get("ticker")),
        },
        "trader": trader.snapshot(client),
        "trade_log": public_trade_log(),
        "config": config.settings.public_view(),
        "updated_at": int(time.time()),
    }


def choose_market(markets):
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
                    state["book_published_at"] = None
                    if previous:
                        booktape.forget(previous)
        except Exception as error:
            state["kalshi_status"] = "Market discovery error"
            state["kalshi_error"] = str(error)[:180]
        await asyncio.sleep(20)


def _reset_local_book(ticker):
    _local_book["ticker"] = ticker
    _local_book["yes"] = {}
    _local_book["no"] = {}


def _price_cents(value):
    try:
        if isinstance(value, str):
            return int(round(float(value) * 100))
        return int(value)
    except (TypeError, ValueError):
        return None


def _count(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _apply_book_levels(side, levels):
    book_side = {}
    for level in levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price, size = _price_cents(level[0]), _count(level[1])
        if price is None or size is None:
            continue
        if size > 0:
            book_side[price] = size
    _local_book[side] = book_side


def _apply_book_delta(side, price, delta):
    price = _price_cents(price)
    delta = _count(delta)
    if price is None or delta is None:
        return
    current = _local_book[side].get(price, 0) + delta
    if current > 0:
        _local_book[side][price] = current
    else:
        _local_book[side].pop(price, None)


def _publish_local_book():
    state["orderbook"] = {
        "orderbook": {
            "yes": [[price, int(size)] for price, size in _local_book["yes"].items()],
            "no": [[price, int(size)] for price, size in _local_book["no"].items()],
        }
    }
    state["book_error"] = None
    state["book_updated_at"] = int(time.time())
    state["book_published_at"] = time.time()
    state["book_source"] = "websocket"
    ticker = _local_book["ticker"]
    if ticker:
        booktape.record(ticker, policy.book_snapshot(state["orderbook"]))
    trader.note_book_update(ticker)
    trader.notify_book()


def _handle_ws_message(raw_message, ticker):
    data = json.loads(raw_message)
    msg_type = data.get("type")
    body = data.get("msg") or {}

    if msg_type == "fill":
        trader.notify_fill(body)
        return

    if body.get("market_ticker") not in (None, ticker):
        return

    if msg_type == "orderbook_snapshot":
        _apply_book_levels("yes", body.get("yes_dollars_fp") or body.get("yes"))
        _apply_book_levels("no", body.get("no_dollars_fp") or body.get("no"))
        _publish_local_book()
    elif msg_type == "orderbook_delta":
        side = body.get("side")
        price = body.get("price_dollars", body.get("price"))
        delta = body.get("delta_fp", body.get("delta"))
        if side in ("yes", "no") and price is not None and delta is not None:
            _apply_book_delta(side, price, delta)
            _publish_local_book()
    elif msg_type == "error":
        code = (body or {}).get("code")
        message = (body or {}).get("msg")
        state["book_error"] = f"ws error {code}: {message}"[:180]


async def kalshi_orderbook_ws_worker():
    backoff = 1
    while True:
        ticker = (state["market"] or {}).get("ticker")
        if not ticker or not client.has_credentials:
            await asyncio.sleep(2)
            continue
        if _local_book["ticker"] != ticker:
            _reset_local_book(ticker)
        try:
            headers = client.sign_ws_handshake()
            async with websockets.connect(
                client.ws_url, additional_headers=headers,
                ping_interval=20, ping_timeout=20,
            ) as websocket:
                await websocket.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": [ticker],
                    },
                }))
                await websocket.send(json.dumps({
                    "id": 2, "cmd": "subscribe",
                    "params": {"channels": ["fill"]},
                }))
                backoff = 1
                subscribed_ticker = ticker
                async for raw_message in websocket:
                    if (state["market"] or {}).get("ticker") != subscribed_ticker:
                        break
                    _handle_ws_message(raw_message, subscribed_ticker)
        except Exception as error:
            state["book_error"] = f"ws: {str(error)[:160]}"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)


async def kalshi_keepalive_worker():
    while True:
        await asyncio.sleep(20)
        try:
            await client.warm()
        except Exception:
            pass


async def orderbook_rest_fallback_worker():
    while True:
        ticker = (state["market"] or {}).get("ticker")
        if not ticker:
            await asyncio.sleep(2)
            continue
        book_age = (
            time.time() - state["book_updated_at"] if state["book_updated_at"] else None
        )
        is_stale = book_age is None or book_age > (config.settings.book_poll_seconds * 3)
        if is_stale:
            try:
                fresh = await client.get_orderbook(ticker)
                if (state["market"] or {}).get("ticker") == ticker:
                    state["orderbook"] = fresh
                    state["book_error"] = None
                    state["book_updated_at"] = int(time.time())
                    state["book_source"] = "rest_fallback"
            except Exception as error:
                state["book_error"] = f"rest: {str(error)[:160]}"
        await asyncio.sleep(config.settings.book_poll_seconds)


def _live_book(ticker):
    if not ticker or (state["market"] or {}).get("ticker") != ticker:
        return None
    book = state["orderbook"]
    if not book:
        return None
    return book, state.get("book_published_at") or 0


@app.on_event("startup")
async def startup():
    for task in feeds.hub.tasks():
        asyncio.create_task(task)
    asyncio.create_task(kalshi_market_discovery_worker())
    asyncio.create_task(kalshi_orderbook_ws_worker())
    asyncio.create_task(orderbook_rest_fallback_worker())
    asyncio.create_task(kalshi_keepalive_worker())
    if config.settings.is_enabled:
        trader.set_book_provider(_live_book)
        asyncio.create_task(trader.loop(client, current_signal, current_market))


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state():
    return api_payload()


@app.get("/api/stream")
async def api_stream():
    async def event_stream():
        while True:
            yield f"data: {json.dumps(api_payload())}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "spot_connected": feeds.hub.status()["connected_any"],
        "kalshi_status": state["kalshi_status"],
        "trading_mode": config.settings.trading_mode,
        "trader_running": trader.status.get("running"),
        "halted": risk.is_halted(),
    }


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest")


def require_admin(token):
    expected = config.settings.admin_token
    if not expected:
        raise HTTPException(503, "ADMIN_TOKEN is not configured")
    if token != expected:
        raise HTTPException(401, "Invalid admin token")


@app.get("/api/trader/selftest")
async def api_selftest(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    result = await client.selftest()
    if isinstance(result, dict):
        result = dict(result)
        result["key_id_var"] = config.credential_source(config.KEY_ID_ALIASES) or None
        result["private_key_var"] = config.credential_source(config.PRIVATE_KEY_ALIASES) or None
    return result


@app.get("/api/trader/tier")
async def api_tier(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    try:
        return await client.get_api_limits()
    except (KalshiApiError, KalshiAuthError) as error:
        return {"ok": False, "error": str(error)[:200]}


@app.post("/api/trader/tier/upgrade")
async def api_tier_upgrade(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    try:
        return await client.upgrade_api_tier()
    except (KalshiApiError, KalshiAuthError) as error:
        return {"ok": False, "error": str(error)[:200]}


@app.post("/api/trader/halt")
async def api_halt(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return risk.halt("Halted manually via API", manual=True)


@app.post("/api/trader/resume")
async def api_resume(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return risk.resume(rebaseline_balance=trader.status.get("balance_cents"))


@app.post("/api/trader/flatten")
async def api_flatten(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    risk.halt("Flattened manually via API", manual=True)
    lot_mode = config.settings.trading_mode
    if lot_mode == "off":
        lot_mode = "live"
    closed = []
    if lot_mode == "live":
        for pending_key, pending in scalp.pending_all("live").items():
            try:
                await trader._cancel_pending_entry(client, pending_key, pending, "Manual flatten via API")
            except (KalshiApiError, KalshiAuthError) as error:
                closed.append({"ticker": pending.get("ticker"), "error": str(error)[:140]})
    for lot in scalp.open_lots(lot_mode):
        try:
            book = await client.get_orderbook(lot["ticker"])
        except (KalshiApiError, KalshiAuthError) as error:
            closed.append({"ticker": lot["ticker"], "error": str(error)[:140]})
            continue
        bid = policy.bid_for_side(policy.book_snapshot(book), lot["side"])
        if bid is None:
            closed.append({"ticker": lot["ticker"], "error": "no resting bid to sell into"})
            continue
        marked = scalp.mark(lot_mode, lot["key"], bid)
        if not marked:
            continue
        if lot_mode == "live":
            try:
                marked = await trader._cancel_exit_orders(client, marked)
            except (KalshiApiError, KalshiAuthError) as error:
                closed.append({"ticker": lot["ticker"], "error": str(error)[:140]})
                continue
            if (marked.get("count_open") or 0) <= 0:
                closed.append({"ticker": lot["ticker"], "side": lot["side"], "sold": 0,
                               "note": "fully closed by resting ladder fills", "still_open": 0})
                continue
        wanted = marked["count_open"]
        intent = {
            "tier": "manual", "kind": "time", "count": wanted,
            "limit_price": max(1, min(99, int(round(bid)))),
            "reason": "Manual flatten via API",
        }
        try:
            await trader._execute_exit(client, lot_mode, marked, intent)
        except (KalshiApiError, KalshiAuthError) as error:
            closed.append({"ticker": lot["ticker"], "error": str(error)[:140]})
            continue
        remaining = (scalp.get(lot_mode, lot["key"]) or {}).get("count_open") or 0
        closed.append({"ticker": lot["ticker"], "side": lot["side"],
                       "sold": wanted - remaining, "price_cents": intent["limit_price"],
                       "still_open": remaining})
    return {"halted": True, "mode": lot_mode, "closed": closed}


@app.get("/api/trader/fills")
async def api_fills(x_admin_token: str = Header(default=""), limit: int = 200):
    require_admin(x_admin_token)
    try:
        payload = await client.get_fills(limit=min(int(limit), 1000))
    except (KalshiApiError, KalshiAuthError) as error:
        return {"error": str(error)}
    return payload


@app.get("/api/trader/settlements")
async def api_settlements(x_admin_token: str = Header(default=""), limit: int = 200):
    require_admin(x_admin_token)
    try:
        payload = await client.request("GET", "/portfolio/settlements", params={"limit": min(int(limit), 1000)})
    except (KalshiApiError, KalshiAuthError) as error:
        return {"error": str(error)}
    return payload


@app.get("/api/trader/decisions")
async def api_decisions(limit: int = 50, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return {"decisions": risk.read_decisions(min(limit, 500))}


@app.get("/api/trader/scalps")
async def api_scalps(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return {"live": scalp.view("live"), "dryrun": scalp.view("dryrun"), "active_mode": config.settings.trading_mode}