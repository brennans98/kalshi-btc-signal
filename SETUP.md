# Kalshi BTC 15m Scalper — setup

This is the operator's guide. Follow it in order; each stage exists to catch a
specific class of mistake before money is involved.

Safe by default: with no variables set beyond credentials, `TRADING_MODE` is
`off` and `KALSHI_ENV` is `demo`. Deploying changes nothing about execution.

---

## What the system does

It scalps Kalshi's 15-minute BTC markets. It estimates a fair probability from
live Coinbase trades, compares that against the Kalshi book, and enters when the
ask is enough below fair value — then manages the exit on a three-rung ladder.

| Rung | Default target | Sells |
|---|---|---|
| small | +3¢ | 50% of the position |
| medium | +7¢ | 30% |
| large | +14¢ | 20% |

And exits that are not profits: a **6¢ hard stop**, a **3¢ trailing stop** armed
only after the small rung hits, a **5-minute max hold**, and a **90-second
settlement guard** that flattens before expiry rather than accepting a coin flip.

The guard is the important one. A 15-minute contract held to settlement is not a
scalp — it resolves at 0 or 100. The guard sells while there is still a book.

---

## Stage 1 — Railway variables

Set these in your Railway service under Variables. **Never commit them.**

### Required

| Variable | Value |
|---|---|
| `KALSHI_API_KEY_ID` | The key ID from Kalshi → Settings → API Keys |
| `KALSHI_PRIVATE_KEY` | The full RSA private key, `-----BEGIN…` through `-----END…` |
| `ADMIN_TOKEN` | A long random string you invent. Gates the admin endpoints. |
| `DATA_DIR` | `/app/data` |

Paste the private key with real newlines if Railway's editor allows it; escaped
`\n` sequences are handled too.

### Attach a volume

Railway → your service → **Volumes** → mount at `/app/data`.

Without it, every restart wipes the daily loss counter, the trade count, a
latched halt, and all open lot tracking. A restart mid-scalp would leave a real
position with nothing managing its exit. This is not optional for live trading.

---

## Stage 2 — verify signing

Deploy, then:

```
curl -H "x-admin-token: YOUR_TOKEN" \
  https://YOUR-APP.up.railway.app/api/trader/selftest
```

`"ok": true` with a balance means credentials and request signing work. An
`auth` error means the key or key ID is wrong. Nothing is placed either way.

Also check the dashboard: the signal should now show real confidence and
specific reasons (`Edge 1.4¢ below the 2.0¢ minimum`) instead of a permanent
`NO TRADE`.

---

## Stage 3 — dry run, and actually read it

```
TRADING_MODE=dryrun
```

This is a real simulation, not a logging stub. It opens paper lots, marks them
against the live book every couple of seconds, and walks them through the same
ladder, stops and guards — producing complete round trips with realized cents.
Paper lots are kept in a separate file and never touch your account.

Let it run across a few hours of real market conditions, then:

```
curl -H "x-admin-token: YOUR_TOKEN" \
  "https://YOUR-APP.up.railway.app/api/trader/decisions?limit=200"

curl -H "x-admin-token: YOUR_TOKEN" \
  https://YOUR-APP.up.railway.app/api/trader/scalps
```

What you are looking for, in order of importance:

1. **Is the strike right?** Every entry logs `strike` and `strike_source`. If
   `strike_source` is `ticker` rather than `market.floor_strike`, verify the
   parsed number against the market title yourself. A wrong strike produces a
   confident, inverted signal — the worst failure this system has.
2. **Which rung is actually paying?** If `tier_hits` is all `small` and
   `stop_exits` is high, the stop is too tight for the volatility, or the small
   target is too close to the spread.
3. **How often does the settlement guard fire?** Frequent guard exits mean
   entries are happening too late in the contract's life — raise
   `MIN_SECONDS_TO_CLOSE`.
4. **Would you have approved these?** This is the real question. The limits
   below are what replaced your approval click; the dry run is how you check
   they encode your judgment.

---

## Stage 4 — live on demo money

```
TRADING_MODE=live
KALSHI_ENV=demo
```

Same code path, real order placement, fake money. This catches what dry run
cannot: actual fill behaviour, partial exits, and whether your limit prices get
hit at all. Confirm real fills appear before continuing.

---

## Stage 5 — production, minimum size

```
KALSHI_ENV=prod
MAX_CONTRACTS_PER_TRADE=1
MAX_COST_PER_TRADE_CENTS=100
DAILY_LOSS_LIMIT_CENTS=500
```

Stay here for a meaningful sample of round trips — dozens, not three. Scale only
after the live numbers match what the dry run predicted.

---

## The kill switch

```
# stop new entries; open positions keep being managed
curl -X POST -H "x-admin-token: YOUR_TOKEN" \
  https://YOUR-APP.up.railway.app/api/trader/halt

# stop entries AND sell everything at the current bid
curl -X POST -H "x-admin-token: YOUR_TOKEN" \
  https://YOUR-APP.up.railway.app/api/trader/flatten

curl -X POST -H "x-admin-token: YOUR_TOKEN" \
  https://YOUR-APP.up.railway.app/api/trader/resume
```

A halt stops **entries only** — exits keep running, because abandoning an open
position is not a safety measure. Use `flatten` when you want out entirely; it
works even with `TRADING_MODE=off`, which is the situation an emergency flatten
exists for.

A halt latches to disk. Restarting the container does not clear it. An automatic
halt (loss limit, auth failure) clears at the next UTC day; a manual halt stays
until you resume it.

---

## The strategy rework

`REWORK.md` documents the dip lanes, the runner exit profile, the consolidated
spot feed, conviction sizing, and the latency changes -- including a size-cap
change that raises per-position risk. Read it before enabling live trading.

## Every variable

### Entry

| Variable | Default | Meaning |
|---|---|---|
| `MIN_EDGE_CENTS` | `2.0` | Minimum ask-vs-fair gap to enter |
| `MIN_CONFIDENCE` | `55` | Confidence floor, percent |
| `MAX_SPREAD_CENTS` | `4` | Skip wider books — the spread eats a scalp |
| `MIN_EXIT_LIQUIDITY` | `25` | Contracts that must rest on the side you'll sell |
| `MIN_PRICE_CENTS` | `15` | Avoid cheap tails |
| `MAX_PRICE_CENTS` | `85` | Avoid expensive near-certainties |
| `MIN_SECONDS_TO_CLOSE` | `180` | Minimum runway to bother entering |
| `MAX_SECONDS_TO_CLOSE` | `900` | Don't enter before this much life remains |
| `VOL_WINDOW_SECONDS` | `300` | Volatility estimation window |
| `MIN_HISTORY_SECONDS` | `120` | Warm-up before any signal is issued |

### The ladder

| Variable | Default | Meaning |
|---|---|---|
| `SCALP_SMALL_CENTS` | `3` | Small target |
| `SCALP_SMALL_PCT` | `50` | Percent of original size sold there |
| `SCALP_MEDIUM_CENTS` | `7` | Medium target |
| `SCALP_MEDIUM_PCT` | `30` | |
| `SCALP_LARGE_CENTS` | `14` | Large target |
| `SCALP_LARGE_PCT` | `20` | |
| `SCALP_STOP_CENTS` | `6` | Hard stop, cents against entry |
| `SCALP_TRAIL_CENTS` | `3` | Trailing give-back, armed after the first rung |
| `SCALP_MAX_HOLD_SECONDS` | `300` | Time exit |
| `SCALP_SETTLEMENT_GUARD_SECONDS` | `90` | Flatten this long before close |
| `SCALP_SMALL_LOT_EXIT_TIER` | `medium` | Where a 1–2 contract lot exits whole |

The three percentages must total 100, and the three targets must increase. The
app refuses to start the trader if they don't.

### Risk

| Variable | Default | Meaning |
|---|---|---|
| `MAX_CONTRACTS_PER_TRADE` | `6` | Hard size ceiling |
| `MAX_COST_PER_TRADE_CENTS` | `500` | Dollar ceiling per entry |
| `DAILY_LOSS_LIMIT_CENTS` | `2000` | Breaching this latches a halt |
| `MAX_TRADES_PER_DAY` | `40` | |
| `MAX_OPEN_POSITIONS` | `2` | Concurrent scalps |
| `COOLDOWN_SECONDS` | `20` | Gap between entries |

The loss limit is measured against your actual Kalshi balance, not a local
tally, so it accounts for open exposure and fees. Position size is also capped
by resting exit liquidity — entering 6 against a 2-lot bid means the ladder
cannot sell what it just bought.

### Timing

| Variable | Default |
|---|---|
| `TRADE_LOOP_SECONDS` | `2.0` |
| `BOOK_POLL_SECONDS` | `2.0` |
| `RECONCILE_SECONDS` | `30.0` |

---

## Decisions still worth making

These are yours, and the defaults are guesses:

1. **Ladder spacing.** `3/7/14¢` against a `6¢` stop is roughly break-even at a
   50% hit rate on the small rung alone. Tighten or widen based on what the dry
   run shows about actual travel.
2. **Stop vs. spread.** The stop must exceed `MAX_SPREAD_CENTS`, or it triggers
   on the spread before price moves. The app rejects a config where it doesn't.
3. **Concurrency.** `MAX_OPEN_POSITIONS=2` on 15-minute markets means two
   correlated bets on the same underlying. Treat it as a single risk.

---

## A caution worth stating plainly

High-frequency scalping of short-dated binaries is unforgiving: fees and spread
are paid on every round trip, and a 3¢ target against a 4¢ spread cap is a thin
margin by construction. Nothing here has been validated against live fills —
the dry run and demo stages exist precisely because the model's assumptions are
unproven. Read Kalshi's API terms on automated trading before running in
production, and size as though the system will be wrong more often than it is
right.
