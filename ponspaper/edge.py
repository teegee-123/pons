"""Edge map: strategy-independent forward returns, bucketed by entry features.

Every token in the trading universe is sampled periodically as a hypothetical buy of `sizeUsd` (model fill with
fees + impact + gas). At each horizon we value a hypothetical sell the same way. Averaging those net returns per
feature bucket shows which conditions have had positive expectancy after costs.
"""
import json
import os

from .sim import liquidation_usd, model_venue

BUCKETS = {
    "ageMin":      [1, 5, 15, 60, 360, 1440],
    "mcapUsd":     [5000, 7500, 10000, 15000, 25000, 40000, 60000],
    "progressPct": [10, 25, 50, 75, 90, 100],
    "chg1m":       [-10, -2, 2, 10, 25],
    "chg5m":       [-20, -5, 5, 20, 50],
    "chg15m":      [-30, -10, 10, 30, 80],
    "tpm1":        [1, 3, 6, 12, 25],
    "vol1m":       [100, 300, 1000, 3000],
    "ddPeak":      [5, 15, 30, 50],
    "tradeCount":  [20, 50, 100, 300, 1000],
    "idleSec":     [10, 30, 90, 300],
    "taxBps":      [1, 101, 201, 301],
    "socials":     [1, 2, 3],
}
GRID = ("ageMin", "mcapUsd")


def bucket(key, v):
    if v is None:
        return None
    edges = BUCKETS[key]
    for i, e in enumerate(edges):
        if v < e:
            return i
    return len(edges)


def labels(key):
    e = BUCKETS[key]
    out = [f"<{_fmt(e[0])}"]
    out += [f"{_fmt(a)}–{_fmt(b)}" for a, b in zip(e, e[1:])]
    out.append(f"≥{_fmt(e[-1])}")
    return out


def _fmt(x):
    if abs(x) >= 1000:
        return f"{x / 1000:g}k"
    return f"{x:g}"


def _agg_new():
    return [0, 0.0, 0.0, 0]  # n, sum, sumsq, wins


def _agg_add(a, r):
    a[0] += 1
    a[1] += r
    a[2] += r * r
    a[3] += 1 if r > 0 else 0


class EdgeMap:
    def __init__(self, cfg, data_dir=None):
        self.cfg = cfg
        self.path = os.path.join(data_dir, "edge_samples.jsonl") if data_dir else None
        self.pending = []
        self.last_sample = {}
        self.reset_aggs()

    def reset_aggs(self):
        H = len(self.cfg["horizonsMin"])
        self.agg = {k: [[_agg_new() for _ in range(H)] for _ in range(len(v) + 1)] for k, v in BUCKETS.items()}
        self.grid = {}
        self.base = [_agg_new() for _ in range(H)]
        self.samples = 0

    def maybe_sample(self, tok, feats, now, ex):
        if not self.cfg.get("enabled", True):
            return
        if now - self.last_sample.get(tok.addr, 0) < self.cfg["sampleEverySec"]:
            return
        d = tok.d
        v, qu = model_venue(d, ex), d.get("quoteUsd")
        if v is None or not qu:
            return
        size = self.cfg["sizeUsd"]
        tokens, used = v.buy((size - ex["gasUsd"]) / qu)
        if tokens <= 0:
            return
        self.last_sample[tok.addr] = now
        b = {k: bucket(k, feats.get(k)) for k in BUCKETS}
        raw = {k: (round(feats[k], 4) if isinstance(feats.get(k), float) else feats.get(k)) for k in BUCKETS}
        self.pending.append({"addr": tok.addr, "sym": d.get("symbol"), "t": now, "tokens": tokens,
                             "cost": used * qu + ex["gasUsd"], "b": b, "raw": raw,
                             "rets": [None] * len(self.cfg["horizonsMin"])})

    def due_addrs(self, now, market):
        """Tokens whose next horizon is due but whose data predates it (so the engine can refresh them)."""
        out = set()
        hs = self.cfg["horizonsMin"]
        for s in self.pending:
            for i, h in enumerate(hs):
                if s["rets"][i] is None:
                    due = s["t"] + h * 60
                    tok = market.get(s["addr"])
                    if now >= due and tok and tok.updated < due:
                        out.add(s["addr"])
                    break
        return out

    def resolve(self, now, market, ex):
        hs = self.cfg["horizonsMin"]
        keep = []
        done = []
        for s in self.pending:
            tok = market.get(s["addr"])
            for i, h in enumerate(hs):
                if s["rets"][i] is not None:
                    continue
                due = s["t"] + h * 60
                if now < due:
                    break
                if tok is None:
                    s["rets"][i] = float("nan")
                    continue
                if tok.updated < due and now - due < 45:
                    break  # wait briefly for a fresher quote
                val = liquidation_usd(tok.d, s["tokens"], ex)
                s["rets"][i] = val / s["cost"] - 1.0
                self._add(s, i)
            if all(r is not None for r in s["rets"]):
                done.append(s)
            else:
                keep.append(s)
        self.pending = keep
        self.samples += len(done)
        if done and self.path:
            try:
                with open(self.path, "a", encoding="utf8") as fh:
                    for s in done:
                        fh.write(json.dumps({"t": round(s["t"]), "addr": s["addr"], "sym": s["sym"], "f": s["raw"],
                                             "rets": [None if r != r else round(r, 5) for r in s["rets"]]}) + "\n")
            except OSError:
                pass
        cut = now - 3600
        if len(self.last_sample) > 5000:
            self.last_sample = {a: t for a, t in self.last_sample.items() if t > cut}

    def _add(self, s, i):
        """Fold horizon i of sample s into the aggregates as soon as it resolves."""
        r = s["rets"][i]
        _agg_add(self.base[i], r)
        for k, bi in s["b"].items():
            if bi is not None:
                _agg_add(self.agg[k][bi][i], r)
        ga, gm = s["b"].get(GRID[0]), s["b"].get(GRID[1])
        if ga is not None and gm is not None:
            cell = self.grid.setdefault(f"{ga},{gm}", [_agg_new() for _ in self.cfg["horizonsMin"]])
            _agg_add(cell[i], r)

    # --- views ---
    @staticmethod
    def _view(a):
        n, s, ss, w = a
        if not n:
            return {"n": 0}
        mean = s / n
        sd = max(0.0, ss / n - mean * mean) ** 0.5
        return {"n": n, "mean": mean, "win": w / n, "se": sd / n ** 0.5 if n > 1 else None}

    def view(self, h_idx):
        feats = {}
        for k in BUCKETS:
            feats[k] = [{"label": lab, **self._view(self.agg[k][i][h_idx])} for i, lab in enumerate(labels(k))]
        grid = {key: self._view(cells[h_idx]) for key, cells in self.grid.items()}
        return {"horizons": self.cfg["horizonsMin"], "h": h_idx, "base": self._view(self.base[h_idx]),
                "features": feats, "grid": grid, "gridAxes": {GRID[0]: labels(GRID[0]), GRID[1]: labels(GRID[1])},
                "samples": self.samples, "pending": len(self.pending)}

    def to_dict(self):
        return {"agg": self.agg, "grid": self.grid, "base": self.base, "samples": self.samples,
                "horizons": self.cfg["horizonsMin"]}

    def load(self, d):
        if not d or d.get("horizons") != self.cfg["horizonsMin"]:
            return
        try:
            for k in BUCKETS:
                if k in d["agg"] and len(d["agg"][k]) == len(BUCKETS[k]) + 1:
                    self.agg[k] = d["agg"][k]
            self.grid = d.get("grid", {})
            self.base = d.get("base", self.base)
            self.samples = d.get("samples", 0)
        except (KeyError, TypeError):
            self.reset_aggs()
