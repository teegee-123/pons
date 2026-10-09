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

## 3. Bigger project: on-chain trade signals for pump.fun

pump.fun's API doesn't carry what predicts rugs: developer selling, unique buyers, sniper or bundled buys at launch,
top-holder share. Reading individual trades from Solana (like the pons tick feed) would add them. A multi-day job;
first check what a free source (Solana RPC logs for the pump program, or a public trade websocket) allows.

## Smaller follow-ups found along the way

- **Replay never evolves.** `replay` reports "0 evolution epochs": the engine starts its epoch clock at the current
  time, and recordings are older. Start the clock at the first recorded poll.
- **Mayhem coins** are left out of the pump.fun universe. To study them, sells would need capping at the curve's
  real SOL reserves (plus what our own buy would have added).
- **Pons** keeps its original rules. The new live-evolution and lab options are in its Settings, off. The sell-pricing
  fix is pump.fun only: pons' graduated fills come from the Uniswap v4 quoter, which would need the same correction.
- **Lab runs aren't repeatable** across processes even with `--seed`: crossover walks a set of strings. Set
  `PYTHONHASHSEED=0` when comparing two runs.
