"""Consolidated BTC spot feed: several exchanges, one median price.

Why more than one feed. Fair value in policy.py is a function of spot. A
single exchange's print is a single point of failure in three different ways:
a bad tick skews the standard score for a second, a stalled socket freezes
fair value while the market moves, and a venue-specific wick creates edge
that does not exist anywhere else. Every one of those produces a confident
order at a price nobody else agrees with.

Two uses of the extra feeds, both deliberately cheap:

  1. Cross-feed median. The tape the model reads is the median of whatever
     sources are currently fresh -- one liar cannot move it.
  2. Divergence veto. When the fresh sources disagree by more than
     SPOT_DIVERGENCE_BPS, that is treated exactly like a stale feed: stand
     down. It is a no-trade reason, never a blocking call.

Latency contract. Nothing here is ever awaited on the order path. Each
worker is an independent reconnecting WebSocket task that writes into memory;
the trading loop only ever reads the last consolidated value. The only feed
an order blocks on is the Kalshi book itself.

All sources are US-hosted or US-legal venues, which matters from a US AWS
region: Coinbase, Binance.US and Kraken. Enable/disable with SPOT_FEEDS.
"""

import asyncio
import json
import statistics
import time
from collections import deque

import websockets

import config

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
BINANCEUS_WS = "wss://stream.binance.us:9443/ws/btcusdt@trade"
KRAKEN_WS = "wss://ws.kraken.com/v2"

SOURCES = ("coinbase", "binanceus", "kraken")


class SpotHub:
    """Owns the spot tape and the per-source health picture.

    trades is the same [(timestamp, price)] shape the rest of the codebase
    already consumes, so policy.py and indicators.py need no changes to read
    a consolidated price instead of a single venue's.
    """

    def __init__(self, maxlen=20000):
        self.trades = deque(maxlen=maxlen)
        self.last = {}          # source -> (timestamp, price)
        self.errors = {}        # source -> last error string
        self.connected = {}     # source -> bool
        self.ticks = {}         # source -> count since boot

    # ---- enabled sources -------------------------------------------------

    def enabled(self):
        wanted = [
            name.strip().lower()
            for name in (config.settings.spot_feeds or "").split(",")
            if name.strip()
        ]
        return [name for name in wanted if name in SOURCES] or ["coinbase"]

    # ---- ingestion -------------------------------------------------------

    def note(self, source, price):
        """Record one print and republish the consolidated median.

        Called from the socket workers, once per trade message. The median is
        recomputed here rather than at read time so the trading loop never
        pays for it.
        """
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return

        now = time.time()
        self.last[source] = (now, price)
        self.ticks[source] = self.ticks.get(source, 0) + 1

        fresh = self._fresh_prices(now)
        if not fresh:
            return

        # Median, not mean: with three sources one bad print is discarded
        # outright instead of being averaged into the number we trade on.
        consolidated = statistics.median(fresh)
        self.trades.append((now, consolidated))

    def _fresh_prices(self, now=None):
        now = now or time.time()
        window = config.settings.stale_feed_seconds
        return [
            price
            for timestamp, price in self.last.values()
            if now - timestamp <= window
        ]

    # ---- health ----------------------------------------------------------

    def divergence_bps(self, now=None):
        """Spread between the highest and lowest fresh source, in bps.

        None when fewer than two sources are fresh -- there is nothing to
        disagree about, which is a coverage problem, not a divergence one.
        """
        fresh = self._fresh_prices(now)
        if len(fresh) < 2:
            return None
        low, high = min(fresh), max(fresh)
        if low <= 0:
            return None
        return (high - low) / low * 10000.0

    def status(self):
        now = time.time()
        window = config.settings.stale_feed_seconds
        sources = {}

        for name in self.enabled():
            entry = self.last.get(name)
            age = None if entry is None else round(now - entry[0], 2)
            sources[name] = {
                "connected": bool(self.connected.get(name)),
                "price": None if entry is None else entry[1],
                "age_seconds": age,
                "fresh": age is not None and age <= window,
                "ticks": self.ticks.get(name, 0),
                "error": self.errors.get(name),
            }

        fresh_count = sum(1 for entry in sources.values() if entry["fresh"])
        divergence = self.divergence_bps(now)

        return {
            "sources": sources,
            "fresh_sources": fresh_count,
            "enabled_sources": len(sources),
            "divergence_bps": None if divergence is None else round(divergence, 2),
            "divergence_limit_bps": config.settings.spot_divergence_bps,
            "consolidated": self.trades[-1][1] if self.trades else None,
            "connected": fresh_count > 0,
            # Aliases for the dashboard, which asks two blunter questions:
            # is ANY venue giving us prices, and what is currently wrong.
            "connected_any": fresh_count > 0,
            "error": "; ".join(
                f"{name}: {message}"
                for name, message in sorted(self.errors.items())
                if message and not self.connected.get(name)
            )
            or None,
        }

    # ---- workers ---------------------------------------------------------

    def tasks(self):
        """One reconnecting task per enabled source."""
        workers = {
            "coinbase": self._coinbase,
            "binanceus": self._binanceus,
            "kraken": self._kraken,
        }
        return [workers[name]() for name in self.enabled() if name in workers]

    async def _run(self, source, connect, subscribe, handle):
        """Shared reconnect/backoff shell so each venue only writes its parser."""
        backoff = 1
        while True:
            try:
                async with connect() as websocket:
                    if subscribe is not None:
                        await websocket.send(json.dumps(subscribe))
                    self.connected[source] = True
                    self.errors[source] = None
                    backoff = 1
                    async for raw_message in websocket:
                        try:
                            handle(json.loads(raw_message))
                        except (ValueError, TypeError, KeyError):
                            continue
            except Exception as error:
                self.connected[source] = False
                self.errors[source] = str(error)[:160]
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)

    def _coinbase(self):
        def handle(message):
            if message.get("channel") != "market_trades":
                return
            for event in message.get("events", []):
                for trade in event.get("trades", []):
                    self.note("coinbase", trade.get("price"))

        return self._run(
            "coinbase",
            lambda: websockets.connect(COINBASE_WS, ping_interval=20, ping_timeout=20),
            {
                "type": "subscribe",
                "channel": "market_trades",
                "product_ids": ["BTC-USD"],
            },
            handle,
        )

    def _binanceus(self):
        def handle(message):
            # Raw trade stream: {"e":"trade","p":"64123.45",...}
            if message.get("e") == "trade":
                self.note("binanceus", message.get("p"))

        return self._run(
            "binanceus",
            lambda: websockets.connect(BINANCEUS_WS, ping_interval=20, ping_timeout=20),
            None,  # the stream is selected in the URL path
            handle,
        )

    def _kraken(self):
        def handle(message):
            # v2: {"channel":"trade","type":"update","data":[{"price":64123.4,...}]}
            if message.get("channel") != "trade":
                return
            for trade in message.get("data") or []:
                self.note("kraken", trade.get("price"))

        return self._run(
            "kraken",
            lambda: websockets.connect(KRAKEN_WS, ping_interval=20, ping_timeout=20),
            {
                "method": "subscribe",
                "params": {"channel": "trade", "symbol": ["BTC/USD"]},
            },
            handle,
        )


hub = SpotHub()
