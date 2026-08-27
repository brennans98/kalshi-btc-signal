# Running a dryrun, and where to watch it

Dryrun runs the entire decision path — feeds, book, lanes, sizing, exits, fees,
accounting — and simulates the round trips. It places nothing. It is the only
cheap way to find out whether the strategy does what you think before real money
is involved.

## About Dockge

Dockge cannot host the dashboard as a sub-page. It is a Docker Compose stack
manager: create, edit, start, stop, and a web terminal, with no plugin, iframe,
or custom-tab system ([Dockge](https://github.com/louislam/dockge)). Its own docs
point you at Portainer for anything outside Compose.

You do not need it to, though. **The bot already serves its own dashboard** —
`static/index.html` on port 8000, with a live SSE stream — so it is a web page in
its own right. Dockge manages the container; the bot's own page shows the price,
P&L, ladder, health and trade log. Two tabs, or one reverse proxy putting both on
one hostname.

## What the repo can and cannot change on the server

Worth being exact about, because it decides who does what.

Dockge is **not** GitOps. Its stacks live on the server itself, at
`/opt/stacks/<name>/compose.yaml`, and it never reads from GitHub. So a change to
`deploy/compose.dryrun.yaml` in this repo does not reach Dockge on its own. The
compose file and the environment variables have to be entered in Dockge once, by
hand.

After that one-time setup, **code changes do flow automatically**:

1. A commit lands on `main`.
2. `.github/workflows/ci.yml` runs the suite, the config check and a payload
   render. If any fail, nothing is published — the server keeps running the last
   good image.
3. If they pass, it builds and pushes `ghcr.io/brennans98/kalshi-btc-signal:latest`.
4. In Dockge you press **Update** on the stack. It pulls the new image and
   restarts. The named volume keeps positions and halt state across the restart.

So: compose and secrets are yours to enter once; the code that runs is whatever
passed CI. Nothing reaches your server without you pressing Update.

### One-time: let Dockge pull the private image

The GHCR package is private, which is correct — the image layers contain the
strategy source. Pulling it needs a credential, and *where* that credential lives
matters more than it looks.

Dockge itself runs in a container and shells out to the Docker CLI from inside it.
So `docker login` on the host writes `/root/.docker/config.json` on the host,
which the Dockge container cannot see. Dockge's own compose file carries a
commented-out line for exactly this reason
([Dockge](https://github.com/louislam/dockge)):

```yaml
# If you want to use private registries, you need to share the auth file with Dockge:
# - /root/.docker/:/root/.docker
```

First, get a token: GitHub → Settings → Developer settings → Personal access
tokens → **Tokens (classic)** → scope `read:packages`. It must be a classic token;
GHCR does not accept fine-grained ones for this. If the pull still returns
`denied` with `read:packages` set, add the `repo` scope — private-repo packages
can inherit permission from the repository.

**Quickest, no SSH.** In Dockge, open a shell on the `dockge` container itself and
log in there — that is the client that performs the pull
([Dockge discussion #98](https://github.com/louislam/dockge/discussions/98)):

```bash
docker login ghcr.io -u brennans98
# paste the classic PAT when prompted for a password
```

This works immediately. Its one weakness: the credential lives in the Dockge
container's own filesystem, so recreating or updating Dockge loses it and the
next pull fails with `denied`. Re-run the login if that happens.

**Permanent.** Log in on the host and share the file with Dockge:

```bash
sudo docker login ghcr.io -u brennans98      # writes /root/.docker/config.json
```

Then add this to the `volumes:` of **Dockge's own** stack and restart Dockge:

```yaml
      - /root/.docker/:/root/.docker
```

Either way, prove it before deploying the bot:

```bash
docker pull ghcr.io/brennans98/kalshi-btc-signal:latest
```

The remaining alternative is making the package public in its package settings.
It holds no secrets — credentials are injected at runtime, never baked into the
image — but it would publish your strategy code to anyone who looks, so prefer
the login.

## 1. Deploy the stack

In Dockge: **+ Compose** → name it `btc-bot-dryrun` → paste
`deploy/compose.dryrun.yaml` into the Compose tab → fill the Environment tab from
`.env.example` → **Deploy**.

The two settings that define the session:

```
TRADING_MODE=dryrun
KALSHI_ENV=prod
```

`prod` is deliberate. Dryrun never sends an order, and the demo environment's
books are thin enough that a dryrun against them measures demo liquidity rather
than your strategy. Real books, no orders.

Dryrun still needs `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY`, because it reads
real balance, positions, and order books. `ADMIN_TOKEN` is only strictly required
in live mode, but set it now so the halt and flatten controls work.

`DATA_DIR` must point at the named volume (`/app/data`). Without that, a redeploy
forgets open positions and any latched halt.

## 2. Reach the dashboard

The compose file binds the port to localhost, because the read-only endpoint needs
no auth and shows your balance and positions. From your machine:

```bash
ssh -N -L 8000:127.0.0.1:8000 ubuntu@<your-server>
```

Then open `http://localhost:8000`. The control endpoints (halt, resume, flatten)
require the `X-Admin-Token` header and return 503 when no token is configured, so
an exposed port cannot be used to trade — but it can be used to watch you.

If you would rather have a real URL for both Dockge and the dashboard, put a
reverse proxy in front (Caddy is two lines per site and gets you HTTPS and basic
auth automatically). Subdomains are less trouble than paths here: the dashboard
fetches `/api/state` and `/api/stream` from the root, so serving it under a path
prefix means rewriting those too.

## 3. How long to run

Markets are 15 minutes. "A few market runs" is therefore:

| Cycles | Wall clock | What it tells you |
|---|---|---|
| 4 | 1 hour | The plumbing works: feeds live, markets rolling, decisions logged |
| 8 | 2 hours | Whether entries actually fire, and at what rate |
| 24 | 6 hours | A first, still-unreliable read on the edge |
| 96+ | 1 day+ | Enough round trips for the win rate to mean much |

Two hours is the minimum I would deploy on. A day is the minimum I would believe
anything from. With 11 or so round trips you cannot separate a real edge from a
run of luck, in either direction.

## 4. What to watch, in priority order

**System Health card first.** If storage says FAILING, nothing else on the page is
trustworthy — state is not reaching disk. If the clock is drifting more than a
second or two against the exchange, every time-to-close decision is being made on
a bad number, and past two minutes the bot stops trading on purpose.

**Then the fee line under Session P&L**, which reads like:

```
Gross +$8.02 less $3.84 fees = net +$4.18 - fees took 48% of gross
```

That percentage is the single most informative number in a dryrun. Fees are
roughly 3–4c per contract round trip on this market. If they are eating half your
gross, the bot is trading too often for the edge it has, and the answer is fewer
and better entries, not more of them.

**Then the decision log.** In dryrun the interesting entries are the refusals.
`Edge does not survive the average fill price at any size` means depth-aware
sizing declined a trade the old code would have taken at a worse price than the
model approved. A steady stream of those is the system working.

## 5. What a dryrun cannot tell you

Dryrun assumes it gets filled at the price it asked for, minus modeled slippage
and fees. Live, some of those fills simply will not happen — a fill-or-kill entry
that the book moved away from is a trade the dryrun counted and reality would not
have. So treat dryrun P&L as an **upper bound**, not a forecast.

It also cannot tell you the strategy is profitable. A positive dryrun over 20
round trips is close to no evidence. What it can tell you, reliably, is whether
the bot is doing what it decided to do, and what trading costs.

## 6. Then, in order

1. **dryrun** on prod data, several full cycles, read the log rather than the P&L.
2. **demo** — exercises real order placement, rejections, and partial fills, which
   dryrun cannot. Expect worse behaviour here than in dryrun; that gap is the
   point.
3. **live at one contract.** Not your intended size. Confirm fills, fees and
   accounting match what the dashboard claims.
4. **live at intended size**, once the numbers reconcile.

Do not skip step 3. It is the only step that proves the fee and fill accounting
against real money, and that accounting is what every other number depends on.
