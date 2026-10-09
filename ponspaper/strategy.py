"""Filter definitions, strategy specs, and the random/mutation operators used by the evolver."""
import copy
import json
import random
import time
import uuid

from . import pumpfun
from . import venue as V

# key -> (label, unit, kind, help). kind: "range" ({"min","max"}) or "enum" ({"in": [...]}).
FILTERS = {
    "ageMin":      ("Age", "min", "range", "Minutes since the token was created"),
    "mcapUsd":     ("Market cap", "$", "range", "Fully diluted market cap in USD"),
    "progressPct": ("Curve progress", "%", "range", "Bonding-curve fill toward graduation (graduated = 100)"),
    "volumeUsd":   ("Lifetime volume", "$", "range", "Total traded volume in USD"),
    "tradeCount":  ("Lifetime trades", "#", "range", "Total number of trades"),
    "tpm1":        ("Trades / min", "#", "range", "Trades in the last minute"),
    "vol1m":       ("Volume / min", "$", "range", "USD volume in the last minute"),
    "chg1m":       ("Change 1m", "%", "range", "Spot price change over the last minute"),
    "chg5m":       ("Change 5m", "%", "range", "Spot price change over the last 5 minutes"),
    "chg15m":      ("Change 15m", "%", "range", "Spot price change over the last 15 minutes"),
    "ddPeak":      ("Below peak", "%", "range", "How far spot is below the highest price seen in the last 30 min"),
    "idleSec":     ("Idle", "s", "range", "Seconds since the last trade"),
    "taxBps":      ("Creator tax", "bps", "range", "Creator tax charged on every buy and sell (100 bps = 1%)"),
    "socials":     ("Socials", "#", "range", "Number of social links set (twitter, telegram, website, ...)"),
    "buyRatio1m":  ("Buy share 1m", "%", "range", "Share of the last minute's on-chain volume that was buys (tick data)"),
    "netFlow1m":   ("Net flow 1m", "$", "range", "Buy minus sell volume in the last minute, USD (tick data)"),
    "buyers5m":    ("Buyers 5m", "#", "range", "Distinct wallets that bought in the last 5 minutes (tick data)"),
    "sellers5m":   ("Sellers 5m", "#", "range", "Distinct wallets that sold in the last 5 minutes (tick data)"),
    "whale1m":     ("Biggest buy 1m", "$", "range", "Largest single buy in the last minute, USD (tick data)"),
    "volSpike":    ("Volume spike", "x", "range", "Last minute's volume vs the average minute over the last 15 (tick data)"),
    "devSoldUsd":  ("Creator sold", "$", "range", "USD the token's creator has sold since we started watching (tick data)"),
    "inflow1m":    ("Net inflow 1m", "$", "range", "Buys minus sells into the bonding curve over the last minute, USD (from curve reserves)"),
    "inflow5m":    ("Net inflow 5m", "$", "range", "Buys minus sells into the bonding curve over the last 5 minutes, USD (from curve reserves)"),
    "fillRate":    ("Curve speed", "%/min", "range", "Bonding-curve progress per minute since launch"),
    "athDdPct":    ("Below ATH", "%", "range", "How far market cap is below its all-time high (pump.fun's record, not just the last 30 min)"),
    "replies":     ("Replies", "#", "range", "Comments on the coin's pump.fun page"),
    "creatorCoins": ("Creator's coins 24h", "#", "range", "Coins the same wallet launched in the last 24h, this one included (launches seen since start-up)"),
    "depthUsd":    ("Curve depth", "$", "range", "Quote in the bonding curve's virtual reserve (the pool's, once graduated), USD. A normal pump.fun curve starts with 30 SOL; much less means a non-standard curve too thin to trade"),
    "live":        ("Livestream", "", "enum", "The creator is livestreaming on pump.fun right now"),
    "mayhem":      ("Mayhem mode", "", "enum", "Launched in pump.fun's Mayhem mode: an AI agent trades it and the curve is non-standard (often very thin)"),
    "stage":       ("Stage", "", "enum", "curve = still on the bonding curve, graduated = trading in the DEX pool"),
    "quote":       ("Quote asset", "", "enum", f"{V.QUOTE}-paired or paired with another token"),
    "buyback":     ("Buyback", "", "enum", "Buyback-and-burn enabled"),
}
# signals the venue can't provide are dropped, so they can't be filtered on, evolved or bucketed
FILTERS = {k: v for k, v in FILTERS.items() if k not in V.UNAVAILABLE}
ENUM_OPTIONS = {"stage": ["curve", "graduated"], "quote": [V.QUOTE, "OTHER"], "buyback": ["yes", "no"], "live": ["yes", "no"],
                "mayhem": ["yes", "no"]}

EXIT_FIELDS = {
    "tpPct":       ("Take profit", "%", "Sell when the net return (after all fees, impact and gas) reaches this"),
    "slPct":       ("Stop loss", "%", "Sell when the net return falls to minus this"),
    "trailPct":    ("Trailing stop", "%", "Once armed, sell when value drops this far from its peak"),
    "trailArmPct": ("Trail arms at", "%", "Net return at which the trailing stop becomes active"),
    "maxHoldMin":  ("Max hold", "min", "Sell after this many minutes regardless"),
    "staleMin":    ("Stale exit", "min", "Sell if nobody has traded the token for this long"),
    "stuckMin":    ("Time stop", "min", "Sell if the position has been under water (net return below 0) for this long"),
}
SIZING_FIELDS = {
    "sizeUsd":     ("Position size", "$", "USD spent per entry (fees come out of this)"),
    "maxOpen":     ("Max open", "#", "Maximum simultaneous positions"),
    "bankrollUsd": ("Bankroll", "$", "Starting paper capital"),
    "cooldownMin": ("Re-entry cooldown", "min", "Minimum minutes before re-entering the same token"),
}


def passes(filters, f):
    """True if feature dict f satisfies every active filter. A missing feature fails an active filter."""
    for key, rule in (filters or {}).items():
        if not rule:
            continue
        v = f.get(key)
        if "in" in rule:
            opts = rule.get("in")
            if opts and v not in opts:
                return False
            continue
        lo, hi = rule.get("min"), rule.get("max")
        if lo is None and hi is None:
            continue
        if v is None:
            return False
        if lo is not None and v < lo:
            return False
        if hi is not None and v > hi:
            return False
    return True


def clean_filters(filters):
    out = {}
    for key, rule in (filters or {}).items():
        if key not in FILTERS or not isinstance(rule, dict):
            continue
        if FILTERS[key][2] == "enum":
            opts = [o for o in (rule.get("in") or []) if o in ENUM_OPTIONS[key]]
            if opts and len(opts) < len(ENUM_OPTIONS[key]):
                out[key] = {"in": opts}
        else:
            lo, hi = _num(rule.get("min")), _num(rule.get("max"))
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            if lo is not None or hi is not None:
                out[key] = {"min": lo, "max": hi}
    return out


def _num(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


DEFAULT_EXITS = {"tpPct": 50.0, "slPct": 25.0, "trailPct": None, "trailArmPct": 15.0, "maxHoldMin": 30.0, "staleMin": 10.0,
                 "stuckMin": None}
DEFAULT_SIZING = {"sizeUsd": 50.0, "maxOpen": 5, "bankrollUsd": 1000.0, "cooldownMin": 60.0}


def new_id(prefix):
    return prefix + uuid.uuid4().hex[:8]


def normalize(spec, defaults=None):
    s = copy.deepcopy(spec)
    s.setdefault("id", new_id("m_" if s.get("kind") != "auto" else "a_"))
    s.setdefault("kind", "manual")
    s.setdefault("name", "Strategy")
    s.setdefault("enabled", True)
    s.setdefault("gen", 0)
    s.setdefault("parent", None)
    s.setdefault("created", time.time())
    s["filters"] = clean_filters(s.get("filters"))
    ex = dict(DEFAULT_EXITS)
    ex.update({k: _num(v) for k, v in (s.get("exits") or {}).items() if k in EXIT_FIELDS})
    s["exits"] = ex
    sz = dict(defaults or DEFAULT_SIZING)
    sz.update({k: _num(v) for k, v in (s.get("sizing") or {}).items() if k in SIZING_FIELDS and _num(v) is not None})
    sz["maxOpen"] = int(sz.get("maxOpen") or 1)
    s["sizing"] = sz
    return s


# ---------------------------------------------------------------------------------------------
# Search space for auto strategies. Values are ordered so mutation can step to neighbours.
# ---------------------------------------------------------------------------------------------
SPACE = [
    ("filters.ageMin.min",      [1, 2, 5, 10, 30, 60, 180], 0.25),
    ("filters.ageMin.max",      [3, 5, 10, 20, 30, 60, 120, 360, 1440], 0.5),
    ("filters.mcapUsd.min",     [4500, 5000, 6000, 8000, 10000, 15000, 20000, 30000], 0.4),
    ("filters.mcapUsd.max",     [6000, 8000, 10000, 15000, 20000, 30000, 45000, 80000, 200000], 0.4),
    ("filters.tradeCount.min",  [10, 25, 50, 100, 250, 500, 1000], 0.3),
    ("filters.tpm1.min",        [1, 2, 4, 8, 15, 30], 0.35),
    ("filters.vol1m.min",       [50, 150, 300, 600, 1200, 2500, 5000], 0.35),
    ("filters.chg1m.min",       [-20, -10, -5, 0, 2, 5, 10, 20], 0.35),
    ("filters.chg1m.max",       [-5, 0, 5, 10, 20, 40, 80], 0.25),
    ("filters.chg5m.min",       [-30, -15, -5, 0, 5, 10, 20, 40], 0.35),
    ("filters.chg5m.max",       [-10, 0, 10, 20, 40, 80, 150], 0.25),
    ("filters.ddPeak.min",      [5, 10, 20, 30, 40, 60], 0.2),
    ("filters.ddPeak.max",      [2, 5, 10, 20, 30], 0.2),
    ("filters.idleSec.max",     [10, 20, 45, 90, 180], 0.25),
    ("filters.taxBps.max",      [0, 100, 200, 300], 0.2),
    ("filters.socials.min",     [1, 2], 0.2),
    ("filters.stage.in",        [["curve"], ["graduated"]], 0.3),
    ("filters.buyRatio1m.min",  [40, 50, 60, 70, 80, 90], 0.3),
    ("filters.netFlow1m.min",   [-100, 0, 50, 150, 400, 1000], 0.3),
    ("filters.buyers5m.min",    [2, 3, 5, 8, 12, 20, 35], 0.3),
    ("filters.sellers5m.max",   [0, 1, 2, 4, 8, 15], 0.2),
    ("filters.whale1m.min",     [50, 100, 200, 400, 800], 0.2),
    ("filters.volSpike.min",    [1.5, 2, 3, 5, 8], 0.25),
    ("filters.devSoldUsd.max",  [0, 10, 50], 0.25),
    ("exits.tpPct",             [10, 15, 20, 30, 40, 50, 75, 100, 150, 250], 1.0),
    ("exits.slPct",             [8, 12, 15, 20, 25, 30, 40, 60], 0.9),
    ("exits.trailPct",          [8, 12, 15, 20, 30, 40], 0.5),
    ("exits.trailArmPct",       [0, 5, 10, 20, 40, 80], 1.0),
    ("exits.maxHoldMin",        [1, 2, 5, 10, 15, 30, 60, 120, 240], 1.0),
    ("exits.staleMin",          [1, 2, 5, 10, 20], 0.6),
    ("exits.stuckMin",          [2, 3, 5, 10, 20, 45, 90], 0.5),
]
if V.PUMP:
    SPACE = [(path, pumpfun.SPACE_VALUES.get(path, values), p_on) for path, values, p_on in SPACE
             if path.split(".")[1] not in V.UNAVAILABLE] + pumpfun.SPACE_EXTRA
GENE_PATHS = [g[0] for g in SPACE]


def _get(spec, path):
    cur = spec
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _set(spec, path, value):
    parts = path.split(".")
    cur = spec
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = cur[p] = {}
        cur = nxt
    if value is None:
        cur.pop(parts[-1], None)
    else:
        cur[parts[-1]] = value


def _blank():
    return {"filters": {}, "exits": {k: None for k in DEFAULT_EXITS}}


def random_spec(rng=random):
    s = _blank()
    for path, values, p_on in SPACE:
        if rng.random() < p_on:
            _set(s, path, copy.deepcopy(rng.choice(values)))
    return _finish(s)


def mutate(parent, rng=random):
    s = {"filters": copy.deepcopy(parent.get("filters", {})), "exits": copy.deepcopy(parent.get("exits", {}))}
    for _ in range(rng.choice([1, 1, 2, 2, 3])):
        path, values, p_on = rng.choice(SPACE)
        cur = _get(s, path)
        if cur is None:
            _set(s, path, copy.deepcopy(rng.choice(values)))
        elif rng.random() < 0.2 and not path.startswith("exits.tp") and path != "exits.maxHoldMin":
            _set(s, path, None)
        else:
            try:
                i = values.index(cur)
            except ValueError:
                i = rng.randrange(len(values))
            j = min(len(values) - 1, max(0, i + rng.choice([-1, 1]) if rng.random() < 0.8 else rng.randrange(len(values))))
            _set(s, path, copy.deepcopy(values[j]))
    return _finish(s)


def _finish(s):
    f = s.setdefault("filters", {})
    for key in list(f):
        rule = f[key]
        if not rule or all(v is None for v in rule.values()):
            del f[key]
            continue
        if "min" in rule or "max" in rule:
            lo, hi = rule.get("min"), rule.get("max")
            if lo is not None and hi is not None and lo >= hi:
                rule.pop("max")
            rule.setdefault("min", None)
            rule.setdefault("max", None)
    ex = s.setdefault("exits", {})
    if ex.get("tpPct") is None:
        ex["tpPct"] = 50
    if ex.get("maxHoldMin") is None:
        ex["maxHoldMin"] = 30
    return s


def describe(spec):
    """Short human label for a strategy's filters/exits."""
    f, ex = spec.get("filters", {}), spec.get("exits", {})
    parts = []

    def rng_txt(key, unit, scale=1, fmt="{:g}"):
        r = f.get(key)
        if not r:
            return None
        lo, hi = r.get("min"), r.get("max")
        a = fmt.format(lo / scale) if lo is not None else None
        b = fmt.format(hi / scale) if hi is not None else None
        if a and b:
            return f"{a}-{b}{unit}"
        return f">={a}{unit}" if a else f"<={b}{unit}"

    for key, label, unit, scale in (("ageMin", "age", "m", 1), ("mcapUsd", "mc", "k", 1000), ("progressPct", "prog", "%", 1),
                                    ("tpm1", "tpm", "", 1), ("vol1m", "v1m", "$", 1), ("chg1m", "1m", "%", 1),
                                    ("chg5m", "5m", "%", 1), ("chg15m", "15m", "%", 1), ("ddPeak", "dd", "%", 1),
                                    ("tradeCount", "trades", "", 1), ("idleSec", "idle", "s", 1), ("taxBps", "tax", "bps", 1),
                                    ("socials", "soc", "", 1), ("buyRatio1m", "buy%", "%", 1), ("netFlow1m", "flow", "$", 1),
                                    ("buyers5m", "buyers", "", 1), ("sellers5m", "sellers", "", 1), ("whale1m", "whale", "$", 1),
                                    ("volSpike", "spike", "x", 1), ("devSoldUsd", "devsold", "$", 1),
                                    ("inflow1m", "in1m", "$", 1), ("inflow5m", "in5m", "$", 1), ("fillRate", "speed", "%/m", 1),
                                    ("athDdPct", "ath-dd", "%", 1), ("replies", "replies", "", 1), ("creatorCoins", "devcoins", "", 1),
                                    ("depthUsd", "depth", "$", 1)):
        t = rng_txt(key, unit, scale)
        if t:
            parts.append(f"{label} {t}")
    if f.get("stage"):
        parts.append("/".join(f["stage"]["in"]))
    if f.get("live"):
        parts.append("live " + "/".join(f["live"]["in"]))
    if f.get("mayhem"):
        parts.append("mayhem " + "/".join(f["mayhem"]["in"]))
    xs = [f"tp{ex.get('tpPct'):g}" if ex.get("tpPct") is not None else None,
          f"sl{ex.get('slPct'):g}" if ex.get("slPct") is not None else None,
          f"tr{ex.get('trailPct'):g}@{(ex.get('trailArmPct') or 0):g}" if ex.get("trailPct") is not None else None,
          f"{ex.get('maxHoldMin'):g}m" if ex.get("maxHoldMin") is not None else None,
          f"ts{ex.get('stuckMin'):g}m" if ex.get("stuckMin") is not None else None]
    return (" | ".join(parts) or "any token") + "  ->  " + " ".join(x for x in xs if x)


# ---------------------------------------------------------------------------------------------
# Genetic-algorithm operators (used by the lab)
# ---------------------------------------------------------------------------------------------
def genome(spec):
    """The evolvable part of a spec."""
    return _finish({"filters": copy.deepcopy(spec.get("filters", {})), "exits": copy.deepcopy(spec.get("exits", {}))})


def _canon(v):
    """Numbers compare by value (40 == 40.0), so lab genomes and live specs get the same key."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        v = round(float(v), 6)
        return int(v) if v.is_integer() else v
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, dict):
        return {k: _canon(x) for k, x in v.items() if x is not None}
    return v


def genome_key(g):
    """Canonical text for a genome, so identical rule sets are recognised as duplicates."""
    f = {k: _canon(v) for k, v in sorted((g.get("filters") or {}).items()) if v and any(x is not None for x in v.values())}
    ex = {k: _canon(v) for k, v in sorted((g.get("exits") or {}).items()) if v is not None}
    return json.dumps({"f": {k: v for k, v in f.items() if v}, "x": ex}, sort_keys=True)


def crossover(a, b, rng=random):
    """Uniform crossover. A filter's min/max travel together so ranges stay coherent."""
    child = {"filters": {}, "exits": {}}
    for key in sorted(set(a.get("filters", {})) | set(b.get("filters", {}))):
        src = a if rng.random() < 0.5 else b
        if key in src.get("filters", {}):
            child["filters"][key] = copy.deepcopy(src["filters"][key])
    for key in set(a.get("exits", {})) | set(b.get("exits", {})):
        src = a if rng.random() < 0.5 else b
        child["exits"][key] = src.get("exits", {}).get(key)
    return _finish(child)


def mutate_genes(g, rate, rng=random):
    """Per-gene mutation: each gene independently, with probability `rate`, steps to a neighbouring value
    (80%), jumps anywhere (10%) or toggles on/off (10%). At least one gene always changes."""
    s = {"filters": copy.deepcopy(g.get("filters", {})), "exits": copy.deepcopy(g.get("exits", {}))}
    picks = [gene for gene in SPACE if rng.random() < rate] or [rng.choice(SPACE)]
    for path, values, p_on in picks:
        cur = _get(s, path)
        r = rng.random()
        if cur is None:
            _set(s, path, copy.deepcopy(rng.choice(values)))
        elif r < 0.1 and path not in ("exits.tpPct", "exits.maxHoldMin"):
            _set(s, path, None)
        else:
            try:
                i = values.index(cur)
            except ValueError:
                i = rng.randrange(len(values))
            j = rng.randrange(len(values)) if r > 0.9 else min(len(values) - 1, max(0, i + rng.choice([-1, 1])))
            _set(s, path, copy.deepcopy(values[j]))
    return _finish(s)
