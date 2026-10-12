# pons paper trader

Polls `https://ponsfamily.com/api/launches?sort=active`, paper-trades many strategies at once with realistic
fees and latency, and evolves them so winning filter/exit combinations rise to the top.

```
python -m ponspaper            # or double-click run.bat  ->  http://127.0.0.1:8787
python -m ponspaper replay     # backtest + evolve on recorded data (data/snapshots)
```

Needs Python 3.10+ only (no packages). The corporate proxy is picked up from `HTTPS_PROXY`, or from the
Windows PAC script. Set `PONS_PROXY=host:port` to override.

The same code can trade **pump.fun** instead: set `PONS_VENUE=pumpfun` (see [pump.fun](#pumpfun) below). Without
it, everything runs on pons exactly as before.

## How fills are simulated

| Cost | How it is modelled |
|---|---|
| Protocol fee | 1% on curve buys and sells (`feeBps()` read on-chain) |
| Creator tax | Each token's `creatorTaxBps` (0–6%), charged on buys and sells |
| Graduated tokens | Uniswap v4 pool: 1% hook fee + creator tax, quoted with the on-chain v4 quoter |
| Price impact | Exact constant-product curve math, the same formula the pons frontend uses |
| Latency | An order executes `latencyMs` after the signal (750 ms default) against **live on-chain state at that moment** |
| Slippage limit | A buy reverts if it would get >10% fewer tokens than quoted when the signal fired; sells revert beyond 40%, and after 3 failed sells the position is dumped |
| Gas | $0.02 per transaction, reverted ones included |

Exits (take profit, stop loss, trailing stop) are measured on **net** return: what selling right now would pay
after fees, impact and gas. A fresh position therefore starts around −4% to −8%.

## Tick data (on-chain trades)

Every poll also reads every individual pons bonding-curve buy and sell from Robinhood Chain event logs: one
extra request, trades arrive a few seconds after they happen. They power seven extra signals, which are
available as filters, GA genes, edge-map rows and Market-tab columns:

| Signal | Meaning |
|---|---|
| Buy share 1m | % of the last minute's volume that was buys |
| Net flow 1m | buy minus sell USD in the last minute |
| Buyers 5m / Sellers 5m | distinct wallets buying / selling in the last 5 minutes |
| Biggest buy 1m | largest single buy in the last minute |
| Volume spike | last minute's volume vs the average minute of the last 15 |
| Creator sold | USD the token's creator has sold since we started watching |

A signal stays empty until its whole window has been watched; after a feed gap or restart, the windows start
again. Trades are recorded with the snapshots, so the genetic lab and replays use the same signals.

Limitations:
- Graduated tokens (Uniswap v4 pools) aren't covered yet.
- "Creator sold" only counts sales made while the trader was watching.

## Finding winning strategies

- **Leaderboard:** 4 starter manual strategies plus 40 auto strategies, each with its own $1,000 bankroll.
  Promote an auto strategy to keep it permanently.
- **Score:** mark-to-market average net return per trade (open positions valued at what selling now would
  return), shrunk toward 0 when there are few trades. This keeps lucky 1–2 trade strategies from topping the board.
- **Live evolution (every 10 minutes):** auto strategies are retired when they are:
  - **losing:** bottom 30% of those judged, and negative. A strategy is judged after 6 closed trades *or* 60 minutes
    alive with any exposure.
  - **stuck:** losing overall while holding a position that has been under water for 45+ minutes.
  - **clones:** making exactly the same trades as an older strategy.
  - **idle:** no trades for 30 minutes.

  Replacements come from lab champions first, then mutations of the live winners, then random genomes.
- **Genetic lab (background):** a full genetic algorithm that evolves genomes on the *recorded* market data
  (last 12h by default). It uses tournament selection, crossover, per-gene mutation (the rate rises when progress
  stalls), elitism, random immigrants and clone removal.
  - **Fitness:** average net return per trade minus one standard error, minus penalties for each filter, for
    unprofitable training slices and for drawdown beyond 10%, plus a small bonus for trading often when profitable.
  - **Diversity:** genomes that trade mostly the same tokens as others are marked down for breeding, so different
    ideas stay alive. Elites are chosen so no two make mostly the same trades.
  - **Champions** must pass three exams:
    - profitable in most of the 4 training slices;
    - profitable on the held-back most recent 30% of the data, with 15+ trades;
    - still profitable under a stress test (+750 ms latency, +0.5% fees).

    They are re-checked on every data refresh and dropped if they stop passing.
  - **Live feedback:** champions are injected into live trading, the final out-of-sample test.
    - One that fails live is blacklisted, and its close relatives are marked down.
    - Live winners are fed back into the lab population.
  - **CPU:** limited to 30% of a CPU, and it pauses after 50 generations on the same data until the next refresh.
  - **Locally:** `python -m ponspaper ga [recording.jsonl.gz]`.
- **Time stop:** an exit gene the GA can evolve. It sells a position that has been under water for N minutes.
- **Long-run record (weeks, not hours):** the lab trains on a short recent window, so on its own it forgets. On
  every data refresh (~90 minutes) each tracked genome - the best of each round, champions and live winners, up to
  400 - is also scored on the new stretch of data, counting only trades it could not have been bred on (data that
  arrived after it started being tracked). Only running totals are kept (stretches won, trades, average, a compact
  cumulative curve), saved with the lab state, so the record outlives the raw recordings and keeps growing for as
  long as the service runs.
  - **Proven:** 12+ stretches with trades (~18h), 40+ trades, profitable in most stretches, and average net return
    per trade minus one standard error still above zero. Proven genomes breed with a bonus, are always put back in
    the population, go live first (`LR-...` strategies), and lose the status only if their record turns negative
    ("faded"), not after one bad window.
  - The Genetic lab tab shows the record; thresholds are under Settings → Genetic lab (*Long-run breeding weight*
    0 = ignore the record when breeding).
- **Edge map:** strategy-independent. Every universe token is sampled as a hypothetical trade and valued after
  1/5/15/30 minutes. It shows which age, mcap, momentum, etc. buckets have positive expectancy after costs.
- **Filters & settings:** the *trading universe* is the set of global filters (default: ETH-paired tokens, age
  ≤ 24h). Strategy filters only narrow inside it. Fees, latency, slippage and evolution are all editable.
- **Backtest:** `replay` runs a large population over the recorded snapshots in seconds. Adopt the best results
  back into live paper trading to test them forward on fresh data.

## pump.fun

`PONS_VENUE=pumpfun` (or double-click `run-pumpfun.bat` -> http://127.0.0.1:8788) runs a separate pump.fun paper
trader: its own data folder (`%LOCALAPPDATA%\ponspaper\data-pumpfun`), its own Postgres tables (`pump_*`), its
own starter strategies and search space. The dashboard, evolution, genetic lab, edge map and backtests all work the
same way.

**Data.** Every 2 seconds it polls three pump.fun lists in parallel: newest launches
(`frontend-api-v3.pump.fun/coins?limit=60`), bonding-curve coins by last trade, and graduated coins by last
trade. pump.fun launches about 50 coins a minute and a page covers only ~2 seconds of trading, so coins that
slip through are re-read with `/coins-v2/{mint}`.

**On-chain trade stream.** The trader follows every pump.fun bonding-curve trade, launch and graduation as it lands
on Solana (`pumpstream.py`), like a bot on a chain feed, instead of relying on the 2-second API polls alone:
- **Decisions on chain data.** Every 250 ms (*React every*), the coins that just traded have their exits and entries
  checked on the curve after their latest trade. With polling alone, strategies acted on API data that could be
  seconds old, and stop losses fired up to a poll late.
- **Launches are tradeable at once**, from the launch transaction itself, before the API lists the coin.
- **Fills and marks use the stream's curve state.** An order fills on the curve as it stands when it lands, with no
  RPC round trip (`stream` in the Fills table). Coins the stream hasn't seen trade yet are still read from Solana.
- **Each coin's own fee.** pump.fun charges 0.95% plus a creator fee that varies per coin: usually 0.30%, sometimes
  0%, and some creators charge 1%. Once a coin's trade has been seen, its fills, marks and recordings use that fee.
- **Per-trade signals** (the seven pons tick signals, Trades/min, Vol/min and four new ones, see Signals below).

| Source | Used when | Behind the chain | Cost |
|---|---|---|---|
| Yellowstone gRPC | `SOLANA_GRPC_URL` is set (plus `SOLANA_GRPC_TOKEN` if the provider gives an x-token) | typically a few hundred ms | PublicNode: free with a token (`solana-yellowstone-grpc.publicnode.com:443`); others paid (e.g. Chainstack from $49/month); needs `grpcio` (in requirements.txt) |
| Websocket `logsSubscribe` | otherwise: `SOLANA_WS_URL`, else the websocket of `SOLANA_RPC_URL`, else the public endpoint | about 0.8 s measured (median 1.3 s against block times rounded down to the second) | free |

Both carry the same events: pump.fun logs a `TradeEvent` per trade with the curve's reserves after it, plus
`CreateEvent` and `CompleteEvent` (layouts from pump.fun's IDL, checked against real transactions). Only events
logged by the pump program itself count: any program can log look-alike data, and fake pump events are a known trick
against trading bots. Measured on the websocket: about 38 trades a second, 0.2–0.3 MB/s received (roughly 15–25 GB a
day) and about 3% of one CPU core for the whole trader.

When the stream drops, goes quiet for 20 s or falls more than 5 s behind the chain, it reconnects. Meanwhile fills
read Solana as before, and the per-trade signals stay empty until their windows have been watched again (the
recording marks the outage, so the lab sees the same gaps). The pill next to *live* in the header shows the stream's
state. *On-chain trade stream* under Settings → Polling turns it off. The public websocket allows one subscription per
IP: a second one (say, a local trader next to a test) made the first fall a minute behind until it closed.

If gRPC fails 3 times in a row without delivering anything (wrong token, endpoint down, a network that blocks it),
the websocket takes over for 30 minutes, then gRPC is tried again; the pill says *gRPC unavailable* and why. The gRPC
client speaks the Yellowstone protocol directly (no generated code) and was tested against a local server replaying
real pump.fun transactions.

**Setting the gRPC token.** On Render: the pump.fun service's Environment tab, `SOLANA_GRPC_URL` and
`SOLANA_GRPC_TOKEN`. On this PC: a `.env` file at the repo root (git ignores it) with the same two lines, e.g.
`SOLANA_GRPC_URL=solana-yellowstone-grpc.publicnode.com:443`; it is read at start-up and never overrides variables
that are already set. Behind the Standard Bank proxy gRPC can't connect: the proxy re-signs TLS and only speaks
HTTP/1.1, and gRPC needs HTTP/2 end to end, so the office network falls back to the websocket. At home or on Render,
gRPC is used.

**Fills use the slot the order lands in.** An order lands `latencyMs` after the signal (give or take half of
*Latency jitter*: how long a transaction takes to land varies). It must meet the curve as it stood on chain at that
moment, but the stream only shows that moment later: it runs about 0.8 s behind the chain on the websocket. So the
order waits for the stream to catch up (its lag, measured from every trade's block time), takes the newest slot
the stream has then delivered as its landing slot, waits one more slot (Solana slots are about 0.21 s; the stream
measures them) so all of that slot's trades are in, and fills on the curve after every trade of earlier slots and a
random number of the trades in its own slot: where in a slot a transaction lands isn't up to the sender. Hovering a
fill in the Trades tab shows its slot and position. Coins the stream hasn't seen trade yet are read from Solana.

Without this, fills were priced on what the stream had shown when the order landed, i.e. the chain about 0.8 s
earlier. Measured on live fills, that was optimistic in fast markets: one 21-minute run's sells would have filled
1.5% worse on average (p90 5%), a later 6-minute run's on average about the same, but 1 in 10 sells 2.5% worse or
more. The cost is in the tail, where stop losses meet falling prices.

Pricing is constant product on the virtual reserves with the fees the program charges, which reproduces real
on-chain trades to the token. Open positions are marked on the stream's latest curve, so take profit / stop loss act
on what a bot on the same feed would see.

| Cost | How it is modelled on pump.fun |
|---|---|
| Curve fee | the coin's own: 0.95% protocol + its creator fee, from its on-chain trades; 1.25% until one has been seen |
| Graduated (PumpSwap) | 1.25% by default. The real fee is tiered by market cap (about 1.15% at $200k, 0.3% above $20M) |
| Solana fee | $0.05 per transaction (base + priority fee), reverted ones included |
| Latency | 750 ms ± 200 ms (*Latency jitter* 400) after the signal, then priced as the chain stood in that slot; the lab uses the average |
| Slippage limits | as on pons |

Set `SOLANA_RPC_URL` to a private RPC (a free Helius or QuickNode endpoint) for reliability; without it the
public `api.mainnet-beta.solana.com` is used, which rate-limits and may refuse some cloud servers. If Solana
can't be read, fills fall back to a fresh pump.fun API read (`api` in the Fills table), then to the last poll
(`model`). The RPC URL is never saved in the config or shown on the dashboard.

**Signals.** pump.fun's API has no lifetime volume or trade count, so *Lifetime volume/trades*, *Creator tax* and
*Buyback* are hidden. *Trades/min*, *Vol/min* and the seven tick signals (buy share, net flow, buyers, sellers,
biggest buy, volume spike, creator sold) come from the trade stream and stay empty without it. pump.fun adds:

| Signal | Meaning |
|---|---|
| Net inflow 1m / 5m | buys minus sells into the curve, USD (growth of its real SOL reserves) |
| Curve speed | curve progress per minute since launch |
| Below ATH | how far market cap is below pump.fun's all-time high for the coin |
| Livestream | the creator is streaming on pump.fun right now |
| Replies | comments on the coin's page |
| Creator's coins 24h | coins the same wallet launched in the last day (serial launchers), counted since start-up |
| Curve depth | quote in the curve's virtual reserve (the pool's once graduated), USD; a normal curve starts at 30 SOL |
| Holders | wallets holding the coin (trade stream) |
| Creator holds | share of supply the creator's wallet still holds (trade stream) |
| Top 10 hold | share of supply in the 10 biggest wallets, creator included, curve excluded (trade stream) |
| Launch buys | share of supply wallets other than the creator bought in the first 3 s: snipers and bundled launches (trade stream) |

The last four need every trade since launch, so they are only known for coins the stream has watched since launch.
They stop at graduation, like the other per-trade signals: PumpSwap trades aren't streamed.

**Coins it can't price are left out.** The default universe skips two kinds of coin, because neither paper fills
nor real trades make sense on them (both can be switched back on under Settings → Trading universe):
- **Curve depth under $1,500.** Some non-standard curves hold well under 1 SOL, so a $50 order is many times the
  curve. Before this rule, coins under $1k market cap made 66 of the first 995 trades and lost 52% each on average,
  mostly within seconds.
- **Mayhem mode.** The program moves these curves' virtual reserves without anyone trading (one lost 55% of its
  virtual SOL in 75 seconds while sellers took out 0.05 SOL), so constant-product pricing doesn't hold, and their
  real SOL reserves are a few dollars, far less than a position's paper value.
- **Non-standard curves.** A standard curve keeps the same constant (30 SOL × 1,073M tokens) all the way up: 47,026
  of 47,027 recorded states of normal coins did, against 2% of Mayhem coins' states. *Standard curve = yes* catches
  Mayhem-like coins whose flag is missing.

**Sell pricing.** A paper buy never reaches the chain, so the reserves read later don't contain it. Selling into
them as they are would charge the price impact twice. On pump.fun a sell is priced on the curve as it would stand
with our buy in it, so a round trip with nobody else trading returns what went in minus fees. For a $50 order
that is 3% better than the double-counted price at launch, 0.9% near graduation (pons is unchanged). This only
applies while a position is a small part of the curve, as it always is on a standard curve; otherwise the plain
price is used, which can never pay out more than the curve holds.

**Edge-map buys wait for the latency.** Some coins' curves complete in the launch transaction and open on PumpSwap
at the graduation price, then trade up to ×10,000 higher by the next poll: USDF went from $46k to $400M market cap
within 2 seconds. Buying at the price that triggered the sample, one such coin (+565,000%) made the whole edge map
look profitable, though nobody outside could have bought there. So on pump.fun an edge sample buys like a real
order, at the first quote after `latencyMs` (*Edge buys wait for latency* under Settings → Polling, edge map & recording; changing it
restarts the edge map; pons is unchanged). Replaying the first 5.8 hours, the 1-minute baseline went from +27% to
−3%, and the biggest sample left is a ×35 run over 30 minutes.

**Winners must not rest on one trade.** pump.fun returns are fat-tailed: rugs at −90%, the odd +300% pump. In the
first hour, the top 6 live strategies all owed their score to one +394% trade, and 31 of the 44 live strategies had
been bred from them. So on pump.fun (settings under Live evolution and Genetic lab; pons keeps its original rules):
- **Score without best trade:** each live strategy's score leaves out its best closed trade.
- **Breed after 6 trades:** only strategies with 6+ closed trades are parents of new ones.
- **Max 3 children:** one strategy has at most 3 live children at once.
- **Clone overlap 0.7:** a strategy whose recent trades are 70%+ the same as an older one's is retired.
- **Lab: leave out best trades = 1:** genomes are scored, and must pass every exam, without their best trade.

**Genetic lab.** Trains on the last 2 hours, with at most one entry candidate per coin every 6 seconds
(*Candidate spacing*), which keeps it near 120 MB of memory on a 512 MB server. The stream's lag is recorded, so
simulated fills wait for it exactly like live ones: the lab prices a fill on the recording as it stood `latencyMs` +
lag after the signal. Recordings made without the stream can't show how far a price moves between a signal and its
fill, but live fills do: with *Charge live slippage* on (the pump.fun default), the lab then charges every simulated
trade the per-side slippage measured on recent live fills. On stream recordings that would count the same move
twice, so it is skipped (the Genetic lab tab shows which: *Fills priced on*).

**Recording.** Only coins in the trading universe (plus anything held) are recorded, in a compact form (curve
reserves; price, market cap and progress are derived again on replay), and unchanged rows are skipped for 60 s.
With the stream, each recorded coin's trades are recorded too (time, side, USD, wallet, tokens), and the curve after
each burst of trades, so the lab trains on the same per-trade signals and price path that live trading saw. That
roughly doubles the recording, from about 3 to 6 MB an hour.

Limitations:
- Graduated coins are not read from Solana or streamed. PumpSwap swaps don't follow constant product on the pool's
  vault balances (checked against real swaps), so they fill on a fresh pump.fun API read with a modelled pool depth.
- Coins paired with tokens other than SOL are outside the default universe and are not read from Solana.
- Request load: about 3 requests/second to pump.fun plus about 1/second to Solana. If pump.fun starts refusing
  requests, raise the poll interval in Settings.
- Replay a pump.fun recording locally with `PONS_VENUE=pumpfun` set:
  `set PONS_VENUE=pumpfun` then `python -m ponspaper replay pumpfun_snapshots.jsonl.gz`.

## Running on Render (free plan)

`render.yaml` describes the service. On Render: **New → Blueprint**, pick this repo, and paste a Postgres
connection string into `DATABASE_URL` when asked.

- **Storage:** Render's free disk is wiped on every restart, so with `DATABASE_URL` set, state, trades and
  recordings go to Postgres. They're written every 15 minutes (`PONS_DB_SAVE_SEC`), plus on shutdown; a crash can
  lose up to 15 minutes.
- **Nothing is deleted automatically.** Settings → Database shows the database size, what recordings and trades take,
  how far back they go and how fast they grow, and has a button to delete recordings and/or stored trades older than
  a number of days (the lab's own data window is always kept). The tables are then rewritten so the database really
  shrinks. Keep an eye on the size: a full database refuses every write, including the saved state, so progress
  stops being saved (the unsaved backlog in memory is capped, so the service itself keeps running).
- **Staying awake:** free services sleep after 15 minutes without visitors. Point an uptime monitor (e.g.
  UptimeRobot, every 5 minutes) at `https://<your-service>.onrender.com/health`.
- **Downloads:** the Trades tab downloads every trade as CSV; the Backtest tab downloads the recording. Replay it
  locally with `python -m ponspaper replay pons_snapshots.jsonl.gz`.
- **No password:** anyone with the URL can view and change everything.

### A second service for pump.fun

Deploy the same repo again (e.g. from another free Render account) with `PONS_VENUE=pumpfun`.
`render-pumpfun.yaml` has the settings: **New → Blueprint**, pick this repo, and set the Blueprint path to
`render-pumpfun.yaml`. Or create a Web Service by hand with the same build/start commands and environment variables.
`render.yaml` is unchanged, so the existing pons service is not affected.

- **Database:** use a separate free database if you can. Sharing one with pons also works, because the tables
  are prefixed (`pons_*` / `pump_*`). pump.fun writes far more than pons (recordings of every coin in the universe
  plus ~1,000 paper trades an hour), so check Settings → Database regularly and delete old data from there.
- **Solana RPC:** add `SOLANA_RPC_URL` with your private RPC URL (recommended).

## Exporting strategies and trades

One command pulls the best strategies, their exact rules and their trading history out of a running trader, pons or
pump.fun, on Render or on this PC. It only reads, so nothing needs redeploying:

```
python -m ponspaper export https://pumpfun-paper-trader.onrender.com
python -m ponspaper export https://<your-pons-service>.onrender.com --recording
python -m ponspaper export http://127.0.0.1:8787
```

It writes `exports/<venue>_<date-time>/`:

| File | Contents |
|---|---|
| `strategies.csv` | every live strategy ranked by score: trades, win rate, average return, P&L, drawdown, profit factor, fees, and its full rules as JSON |
| `strategies.json` | everything: config, live strategies (rules, stats, equity curve, open positions, last 100 trades), hall of fame, lab champions (with their exam results) and the long-run record |
| `hall_of_fame.csv` | best auto strategies seen, including retired ones |
| `long_run.csv` | the lab's long-run record: forward-tested genomes, stretches won, trades, out-of-sample return and confidence |
| `trades.csv` | every stored closed trade, all strategies, with the conditions at entry (pons: all trades; pump.fun: last 7 days) |
| `recording.jsonl.gz` | with `--recording`: the recorded market data, for `replay` / `ga` on this PC |

`trades.csv` joins to the strategies on `strategy_id`. A free Render service that is asleep takes up to a minute
to answer the first request.

## Data

Locally, data lives in `%LOCALAPPDATA%\ponspaper\data` (outside OneDrive, so it doesn't sync constantly). Override it with
`--data <folder>` or the `PONS_DATA` environment variable. It contains `state.json` (resumes on restart),
`trades.csv` (every closed trade with entry features), `edge_samples.jsonl`, and `snapshots/` (hourly gz files,
about 1 MB/hour).
