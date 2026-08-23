# Autonomous trading setup

The system ships inert. `TRADING_MODE=off` and `KALSHI_ENV=demo` are the
defaults, so deploying this branch places no orders. Work through the stages in
order; each one is verifiable before the next carries any risk.

## Environment variables

Set these in Railway (Variables tab). Never commit them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRADING_MODE` | `off` | `off` / `dryrun` / `live` |
| `KALSHI_ENV` | `demo` | `demo` (paper) or `prod` (real money) |
| `KALSHI_API_KEY_ID` | — | Key ID from Kalshi API settings |
| `KALSHI_PRIVATE_KEY` | — | Full RSA private key PEM |
| `ADMIN_TOKEN` | — | Secret for the selftest/halt/resume endpoints |
| `KALSHI_SERIES_TICKER` | `KXBTC15M` | Market series to trade |
| `RISK_STATE_PATH` | `data/risk_state.json` | Halt latch and daily counters |
| `DECISION_LOG_PATH` | `data/decisions.jsonl` | Append-only decision log |

### Signal thresholds

| Variable | Default | Meaning |
| --- | --- | --- |
| `MIN_EDGE_CENTS` | `3.0` | Required gap between fair value and the ask |
| `MIN_CONFIDENCE` | `58` | Probability floor for the chosen side |
| `MAX_SPREAD_CENTS` | `8` | Skip wider books |
| `MIN_PRICE_CENTS` / `MAX_PRICE_CENTS` | `8` / `92` | Tradable price band |
| `MIN_SECONDS_TO_CLOSE` | `75` | Do not enter near settlement |
| `MAX_SECONDS_TO_CLOSE` | `900` | Trading window length |

### Risk limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_CONTRACTS_PER_TRADE` | `5` | Hard size cap |
| `MAX_COST_PER_TRADE_CENTS` | `500` | $5.00 max per entry |
| `MAX_OPEN_POSITIONS` | `1` | Concurrency cap |
| `MAX_TRADES_PER_DAY` | `12` | Daily activity cap |
| `DAILY_LOSS_LIMIT_CENTS` | `2000` | $20.00 drawdown then latched halt |
| `COOLDOWN_SECONDS` | `60` | Minimum gap between entries |

All of these are read per-evaluation, so changing one in Railway takes effect on
the next loop iteration without a code change.

## Persist the state directory

The halt latch and the decision log are files. Railway's container filesystem is
ephemeral — without a volume, a redeploy erases the log and, more importantly,
clears a latched loss-limit halt. Attach a Railway volume mounted at `/app/data`
before going live.

## Stage 1 — confirm the signal is real

Deploy with `TRADING_MODE=off`. No credentials needed; the book and market data
are public.

Check `/api/state` and confirm:

- `kalshi.market_ticker` resolves to an active market
- `kalshi.orderbook_at` is recent
- `signal.confidence` moves off 0 and `signal.reason` reports real edge numbers
- `signal.strike_source` reads `market.floor_strike`, not `ticker`

That last one matters most. If the strike is being parsed out of the ticker
string rather than read from a field, verify the number against the market title
before continuing — an inverted strike produces a confident wrong signal.

## Stage 2 — verify credentials

Create an API key in Kalshi, set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY`,
keep `KALSHI_ENV=demo`, then:

```
curl -X POST https://<your-app>/admin/selftest -H "X-Admin-Token: <ADMIN_TOKEN>"
```

`ok: true` with a balance means signing works. An `auth` error type means the
key, the PEM formatting, or the environment is wrong. If the order endpoint
path differs for your account, override it with `KALSHI_ORDER_PATH`.

## Stage 3 — dry run, and actually read it

Set `TRADING_MODE=dryrun`. The full path runs — signal, risk check, sizing — and
logs the order it would have placed without sending anything.

Let it run across a meaningful sample, then read `/api/decisions` and ask of
each entry: *would I have approved this?*

This is the stage that gets skipped, and it is the one that matters. The
thresholds in this branch are placeholders chosen to be conservative, not
calibrated to your judgment. Dry-run output is how you find out whether the
numbers encode what you would have done — while disagreeing is still free.

Adjust the thresholds and re-run until the log reads like your own decisions.

## Stage 4 — live on demo

Keep `KALSHI_ENV=demo`, set `TRADING_MODE=live`. Real order placement, paper
money. Confirm orders appear in your Kalshi demo account, fills reconcile, and
the daily counters increment. Trip the loss limit deliberately if you can — a
halt you have never seen fire is not a halt you can rely on.

## Stage 5 — live on production

Set `KALSHI_ENV=prod` with production credentials, at minimum size. Leave
`MAX_CONTRACTS_PER_TRADE` and `MAX_COST_PER_TRADE_CENTS` low until you have a
real sample of live fills. Scale on evidence, not on the absence of a problem.

## Kill switch

```
curl -X POST https://<your-app>/admin/halt   -H "X-Admin-Token: <ADMIN_TOKEN>"
curl -X POST https://<your-app>/admin/resume -H "X-Admin-Token: <ADMIN_TOKEN>"
```

A manual halt survives restarts and the UTC day boundary; it clears only via
`/admin/resume`. An automatic loss-limit halt clears at the next UTC day.
Setting `TRADING_MODE=off` in Railway also stops the loop on redeploy.

## What this does not do

- **No exits.** Positions are held to settlement. There is no stop-loss and no
  early exit on signal reversal. For 15-minute binaries that is defensible, but
  it means every entry is a committed decision.
- **No sizing model.** Fixed contract count, deliberately. Kelly sizing on an
  uncalibrated probability model scales up on its own estimation errors.
- **No calibration tracking.** The model's stated probabilities are not yet
  compared against realized settlement rates. Until they are, treat the
  confidence number as an ordering signal, not a true probability.
