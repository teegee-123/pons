"""pump.fun (Solana) adapter, used when PONS_VENUE=pumpfun.

Polls the pump.fun frontend API and converts every coin into the same token dict the pons API returns, so the
engine, strategies, lab and edge map run unchanged.

Lists (fetched in parallel every poll, see DEFAULT_LISTS):
  * newest launches                        /coins?limit=60
  * bonding-curve coins by last trade      ...&sort=last_trade_timestamp&order=DESC&complete=false
  * graduated coins by last trade          ...&complete=true
One coin is re-read with /coins-v2/{mint} (edge-map horizons, coins Solana can't answer for).

Prices: the bonding curve is constant product on virtual reserves. Open positions and fills on curve coins use the
curve account read from Solana (solana.py); everything else uses the reserves the API returns. Graduated coins
trade in a PumpSwap pool, modelled as a constant-product pool seeded the way pump.fun migrates (the curve's
closing price, with the 206.9M tokens kept back from the curve), at the current market-cap price.
"""
import urllib.parse

SOL_MINTS = {None, "", "11111111111111111111111111111111", "So11111111111111111111111111111111111111112"}
CURVE_TOKENS = 793_100_000          # tokens sold on the curve (real token reserves at launch)
LAUNCH_TOKENS = 1_073_000_000       # virtual token reserves at launch
POOL_TOKENS = 206_900_000           # tokens kept back and paired with the raised quote at graduation
SOL_CLOSE_PRICE = 115.005 / 279.9e6  # SOL per token when a SOL-paired curve completes
STD_CURVE_K = 30.0 * LAUNCH_TOKENS   # virtual SOL x virtual tokens, the same all the way up every standard SOL curve

_LIST = "/coins?offset=0&limit=60&sort=last_trade_timestamp&order=DESC&includeNsfw=true"
DEFAULT_LISTS = ["/coins?limit=60", _LIST + "&complete=false", _LIST + "&complete=true"]

# Overrides of the engine's DEFAULT_CONFIG for this venue.
CONFIG = {
    "apiBase": "https://frontend-api-v3.pump.fun",
    "rpcUrl": None,                # Solana RPC comes from SOLANA_RPC_URL (see solana.py), never from the config
    "poll": {"intervalSec": 2.0, "refreshCap": 8, "heldRefreshSec": 5.0, "lists": DEFAULT_LISTS},
    "execution": {
        "gasUsd": 0.05,            # Solana base fee + priority fee / tip, per transaction
        "protocolFeeBps": 125,     # bonding curve: 0.95% protocol + 0.30% creator
        "hookFeeBps": 125,         # PumpSwap after graduation: tiered by market cap, 1.25% for small coins
        "useChainQuotes": True,    # fill on the curve / pool read from Solana when the order lands
    },
    # Coins the simulator can't price are left out:
    # - curve depth: normal curves start at 30 SOL (~$3k); non-standard ones can hold well under 1 SOL, where a $50
    #   order is many times the curve and neither paper fills nor real trades make sense;
    # - Mayhem mode: the program moves these curves' virtual reserves without trades (one lost 55% of its virtual
    #   SOL in 75 s while sellers took out 0.05 SOL), so constant-product pricing doesn't hold, and their real SOL
    #   reserves are a few dollars, far less than a $50 position's paper value;
    # - non-standard curves: the same thing on coins without the Mayhem flag. A standard curve keeps the constant
    #   30 SOL x 1,073M tokens (47,026 of 47,027 recorded states of normal coins; 2% of Mayhem coins' states).
    "universe": {"quote": {"in": ["SOL"]}, "ageMin": {"min": None, "max": 1440}, "depthUsd": {"min": 1500, "max": None},
                 "mayhem": {"in": ["no"]}, "stdCurve": {"in": ["yes"]}},
    # Live evolution: pump.fun returns are fat-tailed (rugs at -90%, the odd +300% pump), so one trade must not make a
    # strategy a winner: scores leave out each strategy's best trade, only strategies with 6+ closed trades breed,
    # one parent has at most 3 live children, and strategies trading mostly the same coins as an older one retire.
    "evolution": {"scoreDropBest": True, "parentMinTrades": 6, "maxChildren": 3, "cloneOverlap": 0.7},
    # busy venue: a shorter window and at most one entry candidate per coin every 6 s keep the lab's dataset
    # around 120 MB, so it fits a 512 MB server next to the live trader. dropBest: genomes are scored and examined
    # without their best trade, for the same reason as above.
    "lab": {"windowHours": 2, "sampleEverySec": 30, "candGapSec": 6, "calibrateSlippage": True, "dropBest": 1},
    # thousands of coins an hour: sample each one less often. fillLatency: coins whose curve completes in the launch
    # transaction can open on PumpSwap at the graduation price and trade x10,000 higher by the next poll (USDF went
    # from $46k to $400M in 2 s), so a sample buys like a real order, at the first quote after latencyMs.
    "edge": {"sampleEverySec": 300, "fillLatency": True},
    # ~50 launches a minute: record only coins in the trading universe (or held), skipping unchanged repeats,
    # so a free Postgres database holds a day or two of recordings
    "record": {"dedupeSec": 60, "universeOnly": True},
    "ticks": {"enabled": False},
}

# Starter manual strategies, scaled to pump.fun (launch ~$3k market cap, graduation ~$45k at $110/SOL).
SEEDS = [
    {"name": "Fresh momentum", "filters": {"ageMin": {"max": 5}, "chg1m": {"min": 10}, "mcapUsd": {"max": 10000}},
     "exits": {"tpPct": 40, "slPct": 20, "trailPct": 15, "trailArmPct": 20, "maxHoldMin": 10, "staleMin": 2}},
    {"name": "Mid-curve breakout", "filters": {"progressPct": {"min": 25, "max": 70}, "chg5m": {"min": 15}, "idleSec": {"max": 20}},
     "exits": {"tpPct": 30, "slPct": 15, "trailPct": None, "maxHoldMin": 20, "staleMin": 3}},
    {"name": "Dip bounce", "filters": {"ageMin": {"max": 120}, "ddPeak": {"min": 30}, "chg1m": {"min": 2}, "progressPct": {"min": 15}},
     "exits": {"tpPct": 25, "slPct": 15, "trailPct": None, "maxHoldMin": 15, "staleMin": 5}},
    {"name": "Graduation run", "filters": {"progressPct": {"min": 75}, "stage": {"in": ["curve"]}, "chg5m": {"min": 0}},
     "exits": {"tpPct": 35, "slPct": 15, "trailPct": None, "maxHoldMin": 30, "staleMin": 5}},
    # the one edge-map group positive at a 15-minute hold on the first day (+11.9% +- 9.1, 108 samples; a recording of
    # the same day disagreed): around graduation, which is ~$45k at $110/SOL, so mostly coins that just graduated
    {"name": "Near graduation (edge map)", "filters": {"mcapUsd": {"min": 40000, "max": 60000}},
     "exits": {"tpPct": None, "slPct": None, "trailPct": None, "trailArmPct": None, "maxHoldMin": 15, "staleMin": None,
               "stuckMin": None}},
]

# Search-space changes for auto strategies: pump.fun market caps, plus curve-progress genes to make up for the
# signals this API doesn't have.
SPACE_VALUES = {
    "filters.mcapUsd.min": [3000, 4000, 5000, 7500, 10000, 15000, 25000],
    "filters.mcapUsd.max": [5000, 7500, 10000, 15000, 25000, 40000, 60000, 150000, 500000],
}
SPACE_EXTRA = [
    ("filters.progressPct.min", [5, 10, 20, 40, 60, 80], 0.3),
    ("filters.progressPct.max", [10, 20, 40, 60, 80, 95], 0.25),
    ("filters.chg15m.min",      [-30, -10, 0, 10, 30, 80], 0.2),
    ("filters.inflow1m.min",    [-200, 0, 100, 300, 1000, 3000], 0.3),
    ("filters.inflow5m.min",    [-500, 0, 250, 1000, 3000, 8000], 0.25),
    ("filters.fillRate.min",    [1, 2, 5, 10, 20, 40], 0.25),
    ("filters.athDdPct.min",    [10, 20, 40, 60, 80], 0.15),
    ("filters.athDdPct.max",    [5, 10, 20, 35, 50], 0.2),
    ("filters.replies.min",     [1, 3, 10, 30], 0.15),
    ("filters.creatorCoins.max", [1, 2, 3, 5], 0.25),
    ("filters.live.in",         [["yes"], ["no"]], 0.1),
    ("filters.mayhem.in",       [["no"], ["yes"]], 0.2),
]


def _num(x, key, scale=1.0):
    v = x.get(key)
    try:
        return float(v) / scale if v is not None else None
    except (TypeError, ValueError):
        return None


def normalize(x, partial=False):
    """pump.fun coin JSON -> token dict in the pons schema (plus `reserves`, `poolK`, `launchPriceQuote`, ...).
    partial: a /coins-v2 payload, which leaves out some fields (they stay as last seen)."""
    mint = x.get("mint")
    if not mint:
        return None
    qd, bd = int(x.get("quote_decimals") or 9), int(x.get("base_decimals") or 6)
    qs, ts = 10.0 ** qd, 10.0 ** bd
    vq = _num(x, "virtual_quote_reserves", qs)
    vq = vq if vq is not None else _num(x, "virtual_sol_reserves", qs)
    rq = _num(x, "real_quote_reserves", qs)
    rq = rq if rq is not None else _num(x, "real_sol_reserves", qs)
    vt, rt = _num(x, "virtual_token_reserves", ts), _num(x, "real_token_reserves", ts)
    supply = _num(x, "total_supply", ts) or 1e9
    qm = x.get("quote_mint")
    sol = qm in SOL_MINTS
    # market_cap is in SOL; market_cap_quote is in the pair's own quote token
    mc_q = _num(x, "market_cap_quote")
    if mc_q is None and sol:
        mc_q = _num(x, "market_cap")
    mc_usd = _num(x, "usd_market_cap") or _num(x, "market_cap_usd")
    qu = mc_usd / mc_q if mc_usd and mc_q else None
    complete = bool(x.get("complete"))
    on_curve = not complete and vq and vt
    price = vq / vt if on_curve else (mc_q / supply if mc_q else None)
    created = (_num(x, "created_timestamp") or 0) / 1000.0 or None
    last = (_num(x, "last_trade_timestamp") or 0) / 1000.0 or created
    d = {
        "address": mint, "symbol": x.get("symbol"), "name": x.get("name"),
        "createdAt": created, "lastTradeAt": last, "stage": "graduated" if complete else "curve",
        "priceQuote": price, "priceUsd": price * qu if price and qu else None, "marketCapUsd": mc_usd,
        "quoteUsd": qu, "raisedQuote": rq, "thresholdQuote": None, "graduatedAt": None,
        "progress": 1.0 if complete else (max(0.0, min(1.0, 1.0 - rt / CURVE_TOKENS)) if rt is not None else None),
        "volumeUsd": None, "tradeCount": None, "creatorTaxBps": 0, "buybackEnabled": False,
        "curve": x.get("bonding_curve"), "factory": None, "decimals": bd, "deployer": x.get("creator"), "supply": supply,
        "quote": {"symbol": "SOL" if sol else (qm[:4] + "…"), "decimals": qd, "address": qm},
        # only the keys the response carries, so a partial payload (coins-v2 has no telegram) doesn't erase one
        "socials": {k: x.get(k) for k in ("twitter", "telegram", "website") if k in x},
        "reserves": [vq, vt, rt] if on_curve and rt is not None else None,
        "poolK": None, "launchPriceQuote": None,
        "athMcapUsd": _num(x, "ath_market_cap"), "replies": x.get("reply_count"), "isLive": x.get("is_currently_live"),
        "mayhem": None if partial else bool(x.get("mayhem_state")),  # lists omit it for normal coins
    }
    if on_curve and rq is not None and rt is not None and vt - rt + CURVE_TOKENS > 0:
        d["launchPriceQuote"] = (vq - rq) / (vt - rt + CURVE_TOKENS)
    if complete:
        # after graduation the API keeps the curve's closing reserves: the pool opened at that price
        close = vq / vt if vq and vt else (SOL_CLOSE_PRICE if sol else None)
        d["poolK"] = close * POOL_TOKENS * POOL_TOKENS if close else None
    return d


def apply_state(d, st, now):
    """Token dict updated with its bonding curve as read from Solana (see solana.SolanaChain.states)."""
    n = dict(d)
    price = st["Q"] / st["T"]
    n.update(stage="curve", reserves=[st["Q"], st["T"], st["sellable"]], poolK=None, raisedQuote=st["raised"],
             progress=max(0.0, min(1.0, 1.0 - st["sellable"] / CURVE_TOKENS)))
    if price != d.get("priceQuote"):
        n["lastTradeAt"] = now  # the reserves moved, so somebody traded
    qu = d.get("quoteUsd")
    n.update(priceQuote=price, priceUsd=price * qu if qu else None,
             marketCapUsd=price * qu * (d.get("supply") or 1e9) if qu else d.get("marketCapUsd"))
    return n


META_KEYS = ("address", "symbol", "name", "createdAt", "decimals", "deployer", "supply", "launchPriceQuote", "quote",
             "socials", "mayhem")


def _sig(x, digits=7):
    return float(f"{x:.{digits}g}") if isinstance(x, float) else x


def record_meta(d):
    return {k: d[k] for k in META_KEYS if d.get(k) is not None}


def record_row(d):
    """Compact recording row; everything else is derived again on replay (see expand_row).
    curve:     [mint, "c", lastTradeAt, quoteUsd, athMcapUsd, replies, live, Q, T, sellable]
    graduated: [mint, "g", lastTradeAt, quoteUsd, athMcapUsd, replies, live, priceQuote, poolK]"""
    live = d.get("isLive")
    head = [d["address"], "g" if d.get("stage") == "graduated" else "c",
            round(d["lastTradeAt"], 1) if d.get("lastTradeAt") else None, _sig(d.get("quoteUsd")),
            _sig(d.get("athMcapUsd")), d.get("replies"), None if live is None else int(bool(live))]
    if head[1] == "c":
        return head + [_sig(x) for x in (d.get("reserves") or [None, None, None])]
    return head + [_sig(d.get("priceQuote"), 9), _sig(d.get("poolK"))]


def expand_row(row, meta):
    """Recording row + the coin's metadata -> token dict, as normalize() produced it live."""
    d = dict(meta)
    graduated = row[1] == "g"
    qu, lp = row[3], meta.get("launchPriceQuote")
    raised = None
    if graduated:
        price, d["poolK"], d["reserves"] = row[7], row[8], None
    else:
        d["reserves"], d["poolK"] = row[7:10], None
        price = row[7] / row[8] if row[7] and row[8] else None
        raised = row[7] - lp * LAUNCH_TOKENS if row[7] and lp else None  # virtual minus launch virtual quote
    sellable = None if graduated else row[9]
    usd = price * qu if price and qu else None
    d.update(stage="graduated" if graduated else "curve", lastTradeAt=row[2], quoteUsd=qu, priceQuote=price,
             athMcapUsd=row[4], replies=row[5], isLive=None if row[6] is None else bool(row[6]),
             priceUsd=usd, marketCapUsd=usd * (meta.get("supply") or 1e9) if usd else None, raisedQuote=raised,
             progress=1.0 if graduated else (max(0.0, min(1.0, 1.0 - sellable / CURVE_TOKENS)) if sellable is not None else None),
             volumeUsd=None, tradeCount=None, creatorTaxBps=0, buybackEnabled=False, thresholdQuote=None, graduatedAt=None)
    return d


def _url(cfg, path):
    return path if path.startswith("http") else cfg["apiBase"].rstrip("/") + path


def _is_by_last_trade(path):
    return "sort=last_trade_timestamp" in path


def _limit(path):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    try:
        return int((q.get("limit") or ["50"])[0])
    except ValueError:
        return 50


def fetch_lists(client, cfg, pool):
    """-> (items, cover, errors). cover: [(list, lastTradeAt times, full page)] for the lists sorted by last trade,
    so the engine can tell when trading outran a list. One failing list doesn't fail the poll."""
    paths = cfg["poll"].get("lists") or DEFAULT_LISTS
    futs = [pool.submit(client.get_json, _url(cfg, p)) for p in paths]
    items, cover, errors = {}, [], []
    for path, fut in zip(paths, futs):
        try:
            page = fut.result(timeout=25)
        except Exception as e:
            errors.append(f"{path}: {e!r}")
            continue
        rows = page if isinstance(page, list) else (page.get("coins") or page.get("items") or [])
        ds = [d for d in map(normalize, rows) if d]
        if _is_by_last_trade(path):
            # pages are capped (~70 coins), so a page at least 90% full may have cut off older trades
            cover.append((path, [d["lastTradeAt"] for d in ds if d.get("lastTradeAt")],
                          len(rows) >= 0.9 * min(_limit(path), 60)))
        for d in ds:
            old = items.get(d["address"])
            if old is None or (d.get("lastTradeAt") or 0) >= (old.get("lastTradeAt") or 0):
                items[d["address"]] = d
    if errors and not items:
        raise RuntimeError("; ".join(errors))
    return list(items.values()), cover, errors


def fetch_coin(client, cfg, mint):
    return normalize(client.get_json(_url(cfg, "/coins-v2/" + mint)), partial=True)
