# Autonomous trading setup

The system now has a complete path from live data to a placed order. It ships
switched **off**: with no configuration it behaves exactly as before, except
the signal is real instead of a permanent `NO TRADE`.

Turning on autonomy is three environment settings and a deliberate sequence.

## 1. Credentials

Create an API key in your Kalshi account settings. You get a **key id** and
download a **private key** file once. Set both in Railway (Variables tab) -
never in the repository.

| Variable | Value |
| --- | --- |
| `KALSHI_API_KEY_ID` | the key id |
| `KALSHI_PRIVATE_KEY` | the full PEM contents, `-----BEGIN...` through `-----END...` |
| `KALSHI_ENV` | `demo` to start, `prod` for real money |
| `ADMIN_TOKEN` | any long random string - protects the kill switch |

Pasting the PEM as a multi-line value works; so does a single line with `\n`
escapes, which are converted on load.

## 2. Mode

`TRADING_MODE` controls autonomy and is the only switch that matters:

| Value | Behaviour |
| --- | --- |
| `off` | default - evaluates nothing, places nothing |
| `dryrun` | evaluates fully and logs every intended order, places nothing |
| `live` | places real orders |

## 3. Limits

All optional, all with conservative defaults. These are what stand in for
your approval, so set them deliberately.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MIN_EDGE` | `0.06` | required edge over the book, in probability (0.06 = 6c) |
| `MIN_CONFIDENCE` | `60` | model probability floor for the chosen side |
| `MAX_CONTRACTS_PER_TRADE` | `5` | hard contract cap |
| `MAX_TRADE_COST_DOLLARS` | `5` | hard dollar cap per order |
| `DAILY_LOSS_LIMIT_DOLLARS` | `20` | breaching this halts trading for the day |
| `MAX_TRADES_PER_DAY` | `10` | daily order cap |
| `MAX_OPEN_POSITIONS` | `1` | concurrent position cap |
| `COOLDOWN_SECONDS` | `120` | minimum gap between orders |
| `MAX_SPREAD_CENTS` | `6` | skip illiquid books |
| `MIN_BOOK_SIZE` | `20` | required resting size at the price |
| `MIN_SECONDS_TO_CLOSE` | `90` | no entries inside the last 90s |
| `MAX_SECONDS_TO_CLOSE` | `720` | no entries before the window is in range |
| `KALSHI_ORDER_PATH` | `/portfolio/orders` | set to `/portfolio/events/orders` if your account uses the V2 order surface |

## 4. Persistence

Daily counters, the loss-cap halt and the decision log live in `DATA_DIR`
(default `./data`). Railway's filesystem is ephemeral, so attach a **Volume**
mounted at `/app/data` and set `DATA_DIR=/app/data`. Without it a restart
resets the daily trade count and clears a latched halt.

## 5. Rollout sequence

1. Set credentials with `KALSHI_ENV=demo` and `TRADING_MODE=off`. Deploy.
2. `GET /api/trader/selftest` with header `X-Admin-Token: <ADMIN_TOKEN>`.
   It returns your balance if signing works.
3. Set `TRADING_MODE=dryrun`. Watch `data/decisions.jsonl` and the `trader`
   block on `/api/state`. Every `dry_run_intent` is an order the system would
   have placed - compare them against what you would have approved.
4. When the intents look right, `TRADING_MODE=live` with `KALSHI_ENV=demo`.
   Same code path, paper money.
5. Only then `KALSHI_ENV=prod`, at minimum size.

## Kill switch

```
curl -X POST https://<your-app>/api/trader/halt   -H "X-Admin-Token: <ADMIN_TOKEN>"
```

Halt latches and survives restarts when a volume is attached. Resume with
`/api/trader/resume`. Setting `TRADING_MODE=off` and redeploying is the
heavier equivalent.

## What still needs your judgement

- **Exit rule.** Positions are currently held to settlement. If you want an
  early exit on a confidence reversal, that is a deliberate addition.
- **Sizing.** Fixed contract count, not Kelly. Proportional sizing needs a
  calibrated win rate, which needs live history first.
- **Strike detection.** `policy.extract_strike` reads `floor_strike` and
  falls back to parsing the title. Confirm it resolves correctly against a
  real market before going live; a wrong strike silently inverts the model.
