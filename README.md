# pons paper trader

Polls `https://ponsfamily.com/api/launches?sort=active`, paper-trades many strategies at once with realistic
fees and latency, and evolves them so winning filter/exit combinations rise to the top.

```
python -m ponspaper            # or double-click run.bat  ->  http://127.0.0.1:8787
python -m ponspaper replay     # backtest + evolve on recorded data (data/snapshots)
```

Needs Python 3.10+ only (no packages). The corporate proxy is picked up from `HTTPS_PROXY`, or from the
Windows PAC script. Set `PONS_PROXY=host:port` to override.

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
  - **Fitness:** average net return per trade minus one standard error.
  - **Validation:** the most recent 30% of the data is held back, and only genomes profitable on both parts become
    champions. Champions are injected into live trading, which is the final out-of-sample test.
  - **CPU:** limited to 30% of a CPU, and it pauses after 50 generations on the same data until the next refresh.
  - **Locally:** `python -m ponspaper ga [recording.jsonl.gz]`.
- **Time stop:** an exit gene the GA can evolve. It sells a position that has been under water for N minutes.
- **Edge map:** strategy-independent. Every universe token is sampled as a hypothetical trade and valued after
  1/5/15/30 minutes. It shows which age, mcap, momentum, etc. buckets have positive expectancy after costs.
- **Filters & settings:** the *trading universe* is the set of global filters (default: ETH-paired tokens, age
  ≤ 24h). Strategy filters only narrow inside it. Fees, latency, slippage and evolution are all editable.
- **Backtest:** `replay` runs a large population over the recorded snapshots in seconds. Adopt the best results
  back into live paper trading to test them forward on fresh data.

## Running on Render (free plan)

`render.yaml` describes the service. On Render: **New → Blueprint**, pick this repo, and paste a Postgres
connection string into `DATABASE_URL` when asked.

- **Storage:** Render's free disk is wiped on every restart, so with `DATABASE_URL` set, state, trades and
  recordings go to Postgres. They're written every 15 minutes (`PONS_DB_SAVE_SEC`), plus on shutdown; a crash can
  lose up to 15 minutes. Recordings older than 10 days are deleted (`PONS_DB_KEEP_DAYS`).
- **Staying awake:** free services sleep after 15 minutes without visitors. Point an uptime monitor (e.g.
  UptimeRobot, every 5 minutes) at `https://<your-service>.onrender.com/health`.
- **Downloads:** the Trades tab downloads every trade as CSV; the Backtest tab downloads the recording. Replay it
  locally with `python -m ponspaper replay pons_snapshots.jsonl.gz`.
- **No password:** anyone with the URL can view and change everything.

## Data

Locally, data lives in `%LOCALAPPDATA%\ponspaper\data` (outside OneDrive, so it doesn't sync constantly). Override it with
`--data <folder>` or the `PONS_DATA` environment variable. It contains `state.json` (resumes on restart),
`trades.csv` (every closed trade with entry features), `edge_samples.jsonl`, and `snapshots/` (hourly gz files,
about 1 MB/hour).
