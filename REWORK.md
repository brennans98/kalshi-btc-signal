# Rework: what changed, why, and what it does not fix

This is the change record for the strategy rework. It is written to be read
before deploying, because two of these changes alter how much money the bot
puts at risk per position.

## The four things that were actually wrong

These were found by reading the code, not by guessing at strategy. They are
listed in order of how much they were costing.

### 1. The bot could not see the contract's price

This was the big one. The bot read BTC's price from Coinbase and computed a
fair value from it. It never kept any record of the *contract's* own price
history — only the current top of book. So a Kalshi contract that had sold off
15 cents in the last twenty seconds looked identical, to the bot, to one that
had been sitting flat at that price all along.

That is precisely the pattern being traded manually: watch the contract dip,
buy the dip, ride the recovery. The bot was structurally incapable of seeing
it. Not badly tuned — blind.

`booktape.py` is new and fixes this. Every book update is recorded as a
per-contract time series, and `booktape.analyze()` reports the drop from the
window high, the rise from the low, how long ago each happened, fast and slow
velocity, whether the fall is decelerating, and resting-size imbalance. This is
the contract's chart.

### 2. Exits ran on a book that was up to 1.5 seconds stale

`app.py` already maintained a live WebSocket orderbook, applying every delta
Kalshi pushed. But `trader.py` ignored it and fetched the book over REST with a
1.5-second cache for every exit decision.

So every stop, trail, and guard was evaluated against a price that could be a
second and a half old, on a market that reprices several times a second. On a
co-located server this is a self-inflicted wound: the network round trip was
never the bottleneck — the cache was. A 3ms link is worth nothing behind a
1500ms cache.

`trader.py` now reads the in-memory book directly out of the same process. REST
remains as a fallback for when the socket is reconnecting, gated on
`BOOK_MAX_AGE_SECONDS`.

### 3. The loop ran every 2 seconds regardless of what the market did

The trading loop slept a fixed 2 seconds. Combined with the stale book, a dip
could open and close entirely between two ticks.

The loop is now event-driven: `app.py` sets an event on every book publish, and
the loop wakes on it, falling back to `TRADE_LOOP_SECONDS` (now 0.25) as a
heartbeat when the book is quiet. Exits are evaluated on the tick that moved
the price. Order-status polling is throttled separately, because that is a
rate-limited REST call and does not benefit from running faster.

### 4. The exit ladder was designed to cut winners short

The old profile sold 50% of a position at +3 cents, 30% at +7, 20% at +14, and
had a chart-flip rule and a 300-second max-hold that could close a position
that was winning.

This is a coherent design — for cent-scalping. It is the opposite of riding a
move. A position about to run 30 cents had already sold half of itself at +3.

`EXIT_PROFILE=runner` is now the default. It holds, and exits on a trailing
stop that widens with the peak (`RUNNER_TRAIL_FRAC` of the peak gain, bounded
by `RUNNER_TRAIL_MIN_CENTS`/`RUNNER_TRAIL_MAX_CENTS`), tightens when the chart
turns against the position, does not apply the max-hold timer to a winner, and
rides a deep-ITM position to settlement. `EXIT_PROFILE=ladder` restores the old
behaviour unchanged.

## What was added

### Consolidated spot feed with a divergence veto (`feeds.py`)

Coinbase, Binance.US and Kraken now run simultaneously and are merged into a
median print. A single venue cannot distinguish a real move from its own
glitch, and trading a glitch is worse than missing a move.

When fewer than `SPOT_MIN_SOURCES` venues are alive, or they disagree by more
than `SPOT_DIVERGENCE_BPS`, entries stop. Not "pick the majority" — stop. One
of the feeds is wrong and nothing in this process can tell which.

Nothing in `feeds.py` is ever awaited on the order path. The only feed an order
may block on is the Kalshi L2 book.

### Dip and reversion entry lanes (`policy.py`)

Two new lanes, evaluated *before* the momentum gates — which are hostile to dip
buying by design, since a dip is by definition a move against the recent trend:

- **`dip_dislocation`** — the contract dropped while spot did not. This is the
  discount case, and `DIP_MAX_SPOT_MOVE_BPS` is the load-bearing setting: if
  spot really moved, the contract is *repriced*, not discounted, and buying it
  is taking the wrong side of a real move.
- **`dip_reversion`** — a deeper drop with the contract back near fair.

The lanes do **not** bypass the price band, spread cap, exit-liquidity check,
or fee floors. They are additional lanes, not an override, and they require the
fall to have decelerated (`DIP_REQUIRE_DECEL`) before buying — otherwise this
is a knife-catching machine.

### Conviction scoring and sizing

Every signal now carries a 0–100 conviction score blended from drop size, edge
over the fee floor, book imbalance, deceleration, and time remaining. With
`CONVICTION_SIZING=1` the per-trade budget scales linearly from
`MIN_COST_PER_TRADE_CENTS` ($5) to `MAX_COST_PER_TRADE_CENTS` ($20). A marginal
signal buys the small version; a fully confirmed one buys the large version.

Every hard risk cap still applies on top and can only ever reduce the result.

### Lane-specific pacing

The trend lane's 2-entries-per-market cap exists to stop momentum re-chasing
after a stop. Applied to dips it would forbid exactly the re-entry behaviour
that worked manually, so the dip lane has its own looser bounds
(`DIP_MAX_ENTRIES_PER_MARKET=4`, `DIP_COOLDOWN_SECONDS=8`).

Adding to an *open* position is still refused. Re-entry means after an exit;
averaging into a losing dip lot is how a bad read becomes a bad day.

## Size change — read this before deploying

`MAX_COST_PER_TRADE_CENTS` changed from **500 to 2000**, and
`MAX_CONTRACTS_PER_TRADE` from **6 to 60**.

The contract-count cap was the binding constraint before, and it was binding
much lower than intended: at a 40c ask, six contracts is $2.40, not $20. So the
bot was trading roughly a tenth of the intended size. Fixing the cost cap
without fixing the count cap would have changed nothing; fixing both is what
makes a $20 position actually cost $20.

This means **each position now risks up to 4x what it did before.** The daily
loss limit (`DAILY_LOSS_LIMIT_CENTS` / `DAILY_LOSS_LIMIT_PCT`) has not changed
and still bounds the day, but it will now be reached in fewer trades. Set the
caps to what you actually intend before enabling live trading.

## Deploy checklist

1. **Set the new variables.** All defaults are in `.env.example`. The defaults
   are usable, but `MAX_COST_PER_TRADE_CENTS` and `DAILY_LOSS_LIMIT_CENTS` are
   money decisions and should be set deliberately rather than inherited.
2. **`DATA_DIR` must be a named volume.** Risk state, the halt latch, and open
   lots live there. If it is container-local storage, a redeploy silently
   forgets open positions and clears a latched halt — a restart must not be a
   reset.
3. **Run `TRADING_MODE=dryrun` first, for at least a few full market cycles.**
   Then read the decision log rather than the summary. The specific thing to
   check: dip entries firing on contract drops with a quiet spot, and *not*
   firing when spot moved with the contract.
4. **Then demo money** (`KALSHI_ENV=demo`), then live at the smallest size
   limits, and only then the size you want.
5. **Watch `spot_feeds` on `/api/state`.** If venues are frequently diverging,
   the veto will be blocking entries and that is the feed's problem to fix, not
   a threshold to raise.

## Tests

`tests/test_strategy.py` covers the tape's drop/rise measurement and YES/NO
mirroring, the dip lane firing on a dislocation and refusing both a still-falling
knife and a drop matched by the underlying, the divergence and
insufficient-sources vetoes, the trail's monotonicity and bounds, conviction
sizing staying inside $5–$20, and the lane-specific re-entry allowance.

These prove the code computes what it claims to compute. They prove nothing
about profitability. A backtest against recorded book data is the next honest
step, and even that would not settle it.

## What this does not fix

The uploaded plan document put it correctly, and it is worth repeating here
rather than burying: no set of edits makes a live-money bot "perfect," and
nobody can guarantee profit on a binary market. Anyone promising that is
selling confidence, not engineering.

Specifically:

- **The dip lane is a hypothesis, not a fact.** It encodes the belief that
  short-term contract dips with a steady underlying tend to revert. That belief
  is plausible and it is what the manual trading appears to have exploited, but
  it has not been measured on this market. It could be a real inefficiency, or
  it could be that the dips were selling off for a reason the tape does not
  contain — a large informed order, for instance.
- **A run of profitable $5–$20 manual trades is not yet distinguishable from a
  good streak in a favourable regime.** That is not scepticism about the
  trading; it is what a small sample cannot tell you. The bot's advantage over
  manual trading is that it will produce a large enough sample to find out.
- **Latency is now genuinely exploited, but latency is not edge.** It removes a
  handicap. Reacting instantly to a signal that is wrong just loses money
  faster, which is why the divergence veto and the deceleration requirement
  matter more than the loop cadence.
- **The market makers on KXBTC15M are pricing these contracts efficiently and
  are also co-located.** The realistic goal is a small, real edge in specific
  conditions, not a structural advantage over everyone.
