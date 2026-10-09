"""Which launchpad this process paper-trades, chosen once at start-up by the PONS_VENUE environment variable:

  (unset) / pons    ponsfamily.com on Robinhood Chain (the original behaviour)
  pumpfun           pump.fun on Solana

One service trades one venue. Each venue keeps its own data folder and its own Postgres tables, so two services
(even two Render accounts sharing one database) never mix state.
"""
import os


def _pick(raw):
    n = (raw or "").strip().lower().replace(".", "").replace("-", "").replace("_", "")
    if n in ("", "pons", "ponsfamily"):
        return "pons"
    if n in ("pump", "pumpfun"):
        return "pumpfun"
    raise SystemExit(f"PONS_VENUE={raw!r} is not a known venue: use 'pons' (default) or 'pumpfun'")


NAME = _pick(os.environ.get("PONS_VENUE"))
PUMP = NAME == "pumpfun"

# signals built from pump.fun data (reserves between polls, all-time high, livestreams, replies, creator wallet)
PUMP_ONLY = frozenset({"inflow1m", "inflow5m", "fillRate", "athDdPct", "live", "replies", "creatorCoins", "mayhem",
                       "depthUsd", "stdCurve"})

if PUMP:
    LABEL = "pump.fun"
    QUOTE = "SOL"                                  # main quote asset; other pairs show up as "OTHER"
    TABLE_PREFIX = "pump"                          # Postgres tables pump_kv, pump_trades, pump_snapshots
    DATA_SUBDIR = "data-pumpfun"
    FILE_PREFIX = "pumpfun"
    TOKEN_URL = "https://pump.fun/coin/"
    FORGET_IDLE_SEC = 1800                         # pump.fun launches ~50 coins a minute: forget quiet ones sooner
    # entry conditions saved with every closed trade (trades.csv)
    TRADE_FEAT_COLS = ["ageMin", "mcapUsd", "progressPct", "chg1m", "chg5m", "chg15m", "ddPeak", "idleSec", "socials",
                       "inflow1m", "inflow5m", "fillRate", "athDdPct", "replies", "creatorCoins", "live", "mayhem", "depthUsd",
                       "stdCurve"]
    # signals the pump.fun API can't provide (no lifetime volume / trade count, no creator tax, no tick feed yet)
    UNAVAILABLE = frozenset({"volumeUsd", "tradeCount", "tpm1", "vol1m", "taxBps", "buyback", "buyRatio1m",
                             "netFlow1m", "buyers5m", "sellers5m", "whale1m", "volSpike", "devSoldUsd"})
else:
    LABEL = "pons"
    QUOTE = "ETH"
    TABLE_PREFIX = "pons"
    DATA_SUBDIR = "data"
    FILE_PREFIX = "pons"
    TOKEN_URL = "https://robin.etherscan.io/token/"
    FORGET_IDLE_SEC = 7200
    TRADE_FEAT_COLS = ["ageMin", "mcapUsd", "progressPct", "chg1m", "chg5m", "tpm1", "vol1m", "ddPeak", "taxBps", "socials"]
    UNAVAILABLE = PUMP_ONLY  # pons keeps exactly its original signal set


def addr_key(a):
    """Token key. EVM hex addresses are case-insensitive and stored lower-case; Solana base58 is case-sensitive."""
    a = a or ""
    return a.lower() if a.startswith("0x") else a
