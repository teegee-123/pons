# Notes: things to run or build later

## 1. Train the lab on more data (on your PC)

The server's lab only fits 2 hours of pump.fun data in Render's 512 MB, and 2 hours is too little to tell a pattern
from luck. Your PC has no such limit:

1. Download the recording: Backtest tab → *Download recording*, or
   `python -m ponspaper export https://pumpfun-paper-trader.onrender.com --recording`.
2. Run the lab on all of it (12–24 hours or more is the goal):
   ```
   set PONS_VENUE=pumpfun
   python -m ponspaper ga pumpfun_snapshots.jsonl.gz --generations 50
   ```
3. Champions land in the Backtest tab of a local trader (`run-pumpfun.bat`); *Adopt* sends one to live paper trading.

Recordings are no longer deleted automatically, so each download holds everything since your last clean-up.

## 2. Watch the database

- Settings → Database (both services) shows the size, what recordings and trades take, and growth per day. Delete old
  data from there before the database fills up. A full database refuses every write, including the saved state.
- `PONS_DB_KEEP_DAYS` is no longer read. You can remove it from each Render service's Environment tab.

## 3. On-chain trade stream for pump.fun: next steps

The stream (README → pump.fun → On-chain trade stream) is in: per-trade signals, holders, fills and marks on chain
state, 250 ms reactions, per-coin fees. Still open:

- **Turn on gRPC on Render.** PublicNode's free endpoint: set `SOLANA_GRPC_URL=solana-yellowstone-grpc.publicnode.com:443`
  and `SOLANA_GRPC_TOKEN` in the pump.fun service's Environment tab (the local copy is in the git-ignored `.env`).
  The office proxy blocks gRPC, so it has only been tested against a local server replaying real transactions.
  Check the header pill says "chain gRPC" without "(gRPC unavailable)", and that the lag drops below the
  websocket's ~1.3 s.
- **Check the free websocket on Render.** The public endpoint may refuse cloud servers. If the pill keeps saying
  "reconnecting", add a free Helius key as `SOLANA_RPC_URL` (its websocket is used automatically).
- **Stream PumpSwap too.** Graduated coins still fill on the API with a modelled pool, and their per-trade signals
  stop at graduation. PumpSwap logs `BuyEvent`/`SellEvent` with pool reserves (pump_amm IDL).
- **Priority fees in SOL.** `gasUsd` is a flat $0.05. Real bots pay a priority fee and often a Jito tip in SOL, more
  when competing for the same slot.

## Smaller follow-ups found along the way

- **Replay never evolves.** `replay` reports "0 evolution epochs": the engine starts its epoch clock at the current
  time, and recordings are older. Start the clock at the first recorded poll.
- **Mayhem coins** are left out of the pump.fun universe. To study them, sells would need capping at the curve's
  real SOL reserves (plus what our own buy would have added).
- **Pons** keeps its original rules. The new live-evolution and lab options are in its Settings, off. The sell-pricing
  fix is pump.fun only: pons' graduated fills come from the Uniswap v4 quoter, which would need the same correction.
- **Lab runs aren't repeatable** across processes even with `--seed`: crossover walks a set of strings. Set
  `PYTHONHASHSEED=0` when comparing two runs.
