# Autonomous trading setup

The system defaults to fully inert: `TRADING_MODE=off` and `KALSHI_ENV=demo`.
Deploying this branch changes no execution behaviour. The only visible change is
that the dashboard signal becomes real instead of a permanent 0%.

Work through the stages in order. Do not skip stage 3.

---

## Stage 1 — real signal, no credentials

Nothing to set. Deploy and confirm the dashboard now shows a live fair value,
book prices, and a specific reason when it says NO TRADE (for example "Best edge
2.1c is under the 6c minimum" rather than the old placeholder).

If it reports `Could not read a strike price from market ...`, the ticker format
differs from what `policy.extract_strike` expects. Send the ticker and it can be
fixed in one line. **This matters more than it looks** — a mis-parsed strike
produces a confident, inverted signal.

## Stage 2 — credentials, still no orders

Create an API key in your Kalshi account (Settings, API keys). You get a key ID
and download an RSA private key once.

In Railway, set:

| Variable | Value |
|---|---|
| `KALSHI_KEY_ID` | your key ID |
| `KALSHI_PRIVATE_KEY` | full PEM contents, `-----BEGIN...` through `-----END...` |
| `KALSHI_ENV` | `demo` |
| `ADMIN_TOKEN` | any long random string you choose |
| `DATA_DIR` | `/app/data` |

Attach a Railway volume mounted at `/app/data`. Without it, a restart resets the
daily loss counter and clears a latched halt.

Paste the private key directly into Railway's variable editor. Never commit it.
`\n`-escaped single-line PEMs are handled automatically.

Verify:

```
curl -H "x-admin-token: YOUR_ADMIN_TOKEN" \
  https://YOUR-APP.up.railway.app/api/trader/selftest
```

You want `signing_ok: true` and `balance_ok: true`. If signing fails, the key is
malformed. If signing succeeds but balance fails, the key lacks permissions or
is for the wrong environment.

## Stage 3 — dry run (do not skip)

Set `TRADING_MODE=dryrun`.

The loop now evaluates continuously and logs every order it *would* have placed
to `$DATA_DIR/decisions.jsonl`, through the identical code path live uses. No
orders are sent.

Let it run across a meaningful sample, then read the log and ask the question
that actually matters: **would you have approved each of these?** This is the
step that tells you whether the thresholds encode your judgment. Tune
`MIN_EDGE_CENTS`, `MIN_CONFIDENCE`, and the spread and size floors until the
answer is yes.

## Stage 4 — live on demo money

Set `TRADING_MODE=live`, keep `KALSHI_ENV=demo`. Real order placement, fake
money. Confirm orders appear in your Kalshi demo account, fills reconcile, and
the halt endpoint works:

```
curl -X POST -H "x-admin-token: YOUR_ADMIN_TOKEN" \
  https://YOUR-APP.up.railway.app/api/trader/halt
```

## Stage 5 — production, minimum size

Set `KALSHI_ENV=prod`. Leave every cap at its default (1 contract, $5/trade,
$20/day). Scale only after a real sample of live fills.

---

## Limits

All optional; defaults shown. These are what replace your approval step.

| Variable | Default | Meaning |
|---|---|---|
| `MAX_CONTRACTS_PER_TRADE` | `1` | Hard contract cap |
| `MAX_DOLLARS_PER_TRADE` | `5` | Dollar cap; sizing takes the tighter of the two |
| `DAILY_LOSS_LIMIT_DOLLARS` | `20` | Latches a halt when breached |
| `MAX_TRADES_PER_DAY` | `10` | Daily trade cap |
| `MAX_OPEN_POSITIONS` | `1` | Concurrency cap |
| `COOLDOWN_SECONDS` | `120` | Minimum gap between entries |
| `MIN_EDGE_CENTS` | `6` | Required edge vs fair value |
| `MIN_CONFIDENCE` | `60` | Confidence floor |
| `MAX_SPREAD_CENTS` | `6` | Skip wide books |
| `MIN_BOOK_SIZE` | `20` | Required resting size |
| `MIN_PRICE_CENTS` / `MAX_PRICE_CENTS` | `12` / `88` | Avoid the tails |
| `MIN_SECONDS_TO_CLOSE` | `90` | No entries near settlement |

The daily loss limit is measured against account balance, not a local tally, so
open exposure and fees are included. A breach latches and survives restart; it
clears at the next UTC day. A manual halt only clears via `/api/trader/resume`.

---

## Three decisions still yours

These need your judgment, not a default:

**1. Exit rule.** Positions currently hold to settlement — there is no early
exit on a confidence reversal, no stop, no take-profit. For 15-minute binaries
this is defensible, but it means every entry is committed. If you want early
exits, that is a real addition and needs its own rules.

**2. Sizing.** Fixed contract count, deliberately. Kelly sizing on an
uncalibrated model sizes up on its own errors, so it should wait until the
dry-run log shows the edge estimate is honest.

**3. Strike parsing.** Confirm stage 1 resolves a strike against a real
KXBTC15M market before trusting any signal.

---

## Order endpoint

`KALSHI_ORDER_PATH` defaults to `/portfolio/orders`. Kalshi also exposes a newer
`/portfolio/events/orders` surface; if your account is provisioned for that one,
set the variable rather than editing code.
