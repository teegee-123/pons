"""Genetic lab: evolves strategy genomes on recorded market data, in seconds instead of live hours.

Pipeline
  1. Dataset: replay the recorded snapshots once, computing (exactly as live does) every token's features
     whenever it trades, plus a compact price path per token for valuing positions.
  2. Evaluator: simulates one genome over that dataset: entry filters, latency, the buy slippage limit,
     fees + impact + gas, every exit rule, maxOpen / cooldown / cash. Position outcomes are memoised.
  3. GA: tournament selection, uniform crossover, per-gene mutation with adaptive rate, elitism, random
     immigrants, and clone removal (genomes that make the same trades count once).
  4. Walk-forward check: the first part of the window trains, the most recent part validates. Only genomes
     that are also profitable on validation data enter the lab hall of fame and get deployed to live trading,
     which then acts as the true out-of-sample test.
"""
import bisect
import heapq
import math
import random
import statistics
import time

from . import strategy as S
from .market import Market
from .sim import model_venue

CLONE = -100.0  # fitness given to a genome that duplicates another's trades
RANGE_KEYS = [k for k, v in S.FILTERS.items() if v[2] == "range"]
ENUM_KEYS = [k for k, v in S.FILTERS.items() if v[2] == "enum"]
FEAT_KEYS = RANGE_KEYS + ENUM_KEYS
FI = {k: i for i, k in enumerate(FEAT_KEYS)}

DEFAULT_LAB = {"enabled": True, "windowHours": 12, "validateFrac": 0.3, "population": 60, "elite": 4,
               "tournament": 3, "crossover": 0.7, "mutation": 0.12, "immigrants": 0.1, "minTrades": 12,
               "minValTrades": 4, "rebuildMin": 90, "sampleEverySec": 10, "duty": 0.3,
               "promoteCount": 3, "maxGensPerData": 50}


class Dataset:
    """Entry candidates (features when a token trades) + per-token price paths."""

    def __init__(self, records, ex, universe, sample_every=10.0, yield_every=0.0):
        self.ex = dict(ex)
        self.size_usd = None
        self.tok_addr, self.tok_sym = [], []
        self.pt, self.pp = [], []   # per token: times, points (Q, T, sellable, fee_frac, quoteUsd, lastTradeAt)
        self.cands = []             # (t, tok, feats_tuple)
        self.t0 = self.t1 = None
        self.rows = 0
        self.covered = 0.0          # seconds of actual recording (gaps while the trader was off don't count)
        idx, last_c = {}, {}
        mk = Market()
        t_yield = time.time()
        for t, items in records:
            if self.t0 is None:
                self.t0 = t
            elif t - self.t1 < 120:
                self.covered += t - self.t1
            self.t1 = t
            for d in items:
                a = d["address"]
                self.rows += 1
                tok = mk.tokens.get(a)
                if tok is None:
                    tok = mk.ingest([d], t)[0]
                    changed = True
                else:
                    changed = tok.update(d, t)
                i = idx.get(a)
                if i is None:
                    i = idx[a] = len(self.tok_addr)
                    self.tok_addr.append(a)
                    self.tok_sym.append(d.get("symbol"))
                    self.pt.append([])
                    self.pp.append([])
                if changed or not self.pt[i]:
                    v = model_venue(d, ex)
                    qu = d.get("quoteUsd")
                    if v is not None and qu:
                        self.pt[i].append(t)
                        self.pp[i].append((v.Q, v.T, v.sellable, v.fee_frac, qu, d.get("lastTradeAt") or t))
                if not self.pt[i]:
                    continue
                if not changed and t - last_c.get(i, 0) < sample_every:
                    continue
                f = tok.features(t)
                if not S.passes(universe, f):
                    continue
                last_c[i] = t
                self.cands.append((t, i, tuple(f.get(k) for k in FEAT_KEYS)))
            if yield_every and time.time() - t_yield > 0.25:  # be gentle with a shared CPU
                time.sleep(yield_every)
                t_yield = time.time()
            if self.rows % 50000 == 0:
                mk.prune(t, set(), max_idle=7200)
        self.built_at = time.time()

    @property
    def hours(self):
        return self.covered / 3600.0

    def split_time(self, validate_frac):
        """Split by number of entry candidates, not clock time, so gaps in the recording (trader asleep or
        offline) don't leave the validation part nearly empty."""
        i = min(len(self.cands) - 1, max(0, int(len(self.cands) * (1.0 - validate_frac))))
        return self.cands[i][0]

    def state_at(self, i, t):
        """Index of the last price point of token i at or before t (or -1)."""
        return bisect.bisect_right(self.pt[i], t) - 1


def _buy(p, quote_in):
    Q, T, sellable, ff, qu, _ = p
    net = quote_in * (1.0 - ff)
    out = net * T / (Q + net)
    if out > sellable:
        out = max(0.0, sellable)
        need = out * Q / max(1e-30, T - out)
        return out, min(quote_in, need / (1.0 - ff))
    return out, quote_in


def _sell_usd(p, tokens):
    Q, T, sellable, ff, qu, _ = p
    return tokens * Q / (T + tokens) * (1.0 - ff) * qu


def compile_filters(filters):
    """-> list of (feature index, lo, hi) and (feature index, allowed set), cheapest to test first."""
    rng, enum = [], []
    for k, rule in (filters or {}).items():
        if k not in FI or not rule:
            continue
        if "in" in rule:
            if rule["in"]:
                enum.append((FI[k], frozenset(rule["in"])))
        elif rule.get("min") is not None or rule.get("max") is not None:
            rng.append((FI[k], rule.get("min"), rule.get("max")))
    return enum, rng


def _passes(feats, enum, rng):
    for j, allowed in enum:
        if feats[j] not in allowed:
            return False
    for j, lo, hi in rng:
        v = feats[j]
        if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
            return False
    return True


class Evaluator:
    def __init__(self, ds, ex, sizing):
        self.ds = ds
        self.ex = ex
        self.size = float(sizing["sizeUsd"])
        self.max_open = int(sizing["maxOpen"])
        self.bankroll = float(sizing["bankrollUsd"])
        self.cooldown = float(sizing.get("cooldownMin") or 0) * 60
        self.lat = ex["latencyMs"] / 1000.0
        self.gas = ex["gasUsd"]
        self.tol = ex["buySlippagePct"] / 100.0
        self.memo = {}

    def outcome(self, ci, xs, xkey):
        """Simulate one position from candidate ci with exit genes xs.
        -> None (buy reverted) or (exit_t, cost, proceeds, reason)."""
        mk = (ci, xkey)
        r = self.memo.get(mk)
        if r is not None or mk in self.memo:
            return r
        ds = self.ds
        t_sig, i, _ = ds.cands[ci]
        times, pts = ds.pt[i], ds.pp[i]
        si = bisect.bisect_right(times, t_sig) - 1
        fi = bisect.bisect_right(times, t_sig + self.lat) - 1
        if si < 0 or fi < 0:
            self.memo[mk] = None
            return None
        quote_in = (self.size - self.gas) / pts[si][4]
        exp, _ = _buy(pts[si], quote_in)
        quote_in_f = (self.size - self.gas) / pts[fi][4]
        tokens, used = _buy(pts[fi], quote_in_f)
        if tokens <= 0 or tokens < exp * (1.0 - self.tol):
            self.memo[mk] = None
            return None
        t_fill = t_sig + self.lat
        cost = used * pts[fi][4] + self.gas
        tp = xs.get("tpPct")
        sl = xs.get("slPct")
        tr = xs.get("trailPct")
        arm = (xs.get("trailArmPct") or 0) / 100.0
        mh = xs.get("maxHoldMin")
        st = xs.get("staleMin")
        stuck = xs.get("stuckMin")
        tp = tp / 100.0 if tp is not None else None
        sl = -sl / 100.0 if sl is not None else None
        tr = tr / 100.0 if tr is not None else None
        t_max = t_fill + mh * 60 if mh is not None else math.inf
        peak = -math.inf
        under_since = t_fill
        k, n = fi, len(times)
        reason, t_exit = None, None
        while True:
            p = pts[k]
            t_now = times[k] if k > fi else t_fill
            ret = (_sell_usd(p, tokens) - self.gas) / cost - 1.0
            if ret > peak:
                peak = ret
            if ret >= 0:
                under_since = None
            elif under_since is None:
                under_since = t_now
            if tp is not None and ret >= tp:
                reason, t_exit = "take profit", t_now
            elif sl is not None and ret <= sl:
                reason, t_exit = "stop loss", t_now
            elif tr is not None and peak >= arm and (1.0 + ret) <= (1.0 + peak) * (1.0 - tr):
                reason, t_exit = "trailing stop", t_now
            if reason:
                break
            # time-based exits can fire while nothing trades, i.e. before the next point
            t_next = times[k + 1] if k + 1 < n else math.inf
            cands_t = [(t_max, "max hold")]
            if st is not None:
                cands_t.append((max(t_now, p[5] + st * 60), "stale"))
            if stuck is not None and under_since is not None:
                cands_t.append((under_since + stuck * 60, "time stop"))
            te, why = min(cands_t)
            if te <= t_next and te != math.inf:
                reason, t_exit = why, max(te, t_now)
                break
            if k + 1 >= n:
                reason, t_exit = "end of data", t_now
                break
            k += 1
        ei = bisect.bisect_right(times, t_exit + self.lat) - 1 if reason != "end of data" else k
        proceeds = _sell_usd(pts[ei], tokens) - self.gas
        r = (t_exit + (self.lat if reason != "end of data" else 0.0), cost, proceeds, reason)
        if len(self.memo) > 120000:  # keep memory bounded on small servers
            self.memo.clear()
        self.memo[mk] = r
        return r

    def evaluate(self, g, t_from=-math.inf, t_to=math.inf, keep_trades=False):
        enum, rng = compile_filters(g.get("filters"))
        xs = g.get("exits", {})
        xkey = tuple(xs.get(k) for k in S.EXIT_FIELDS)
        cash = self.bankroll
        held, last_entry, open_h = set(), {}, []
        rets, holds, reasons, sig = [], [], {}, []
        pnl = peak_eq = dd = 0.0
        missed = 0
        trades = [] if keep_trades else None
        cands = self.ds.cands
        lo = bisect.bisect_left(cands, (t_from,)) if t_from != -math.inf else 0
        for ci in range(lo, len(cands)):
            t, i, f = cands[ci]
            if t >= t_to:
                break
            while open_h and open_h[0][0] <= t:
                et, ti, cost, proceeds, why, t_in = heapq.heappop(open_h)
                cash += proceeds
                held.discard(ti)
                pnl += proceeds - cost
                peak_eq = max(peak_eq, pnl)
                dd = max(dd, peak_eq - pnl)
                rets.append(proceeds / cost - 1.0)
                holds.append(et - t_in)
                reasons[why] = reasons.get(why, 0) + 1
                if keep_trades:
                    trades.append((t_in, et, self.ds.tok_sym[ti], proceeds / cost - 1.0, why))
            if i in held or len(open_h) >= self.max_open or cash < self.size:
                continue
            le = last_entry.get(i)
            if le is not None and t - le < self.cooldown:
                continue
            if not _passes(f, enum, rng):
                continue
            last_entry[i] = t
            out = self.outcome(ci, xs, xkey)
            if out is None:
                cash -= self.gas
                missed += 1
                continue
            et, cost, proceeds, why = out
            cash -= cost
            held.add(i)
            heapq.heappush(open_h, (et, i, cost, proceeds, why, t))
            if len(sig) < 64:
                sig.append(ci)
        while open_h:
            et, ti, cost, proceeds, why, t_in = heapq.heappop(open_h)
            pnl += proceeds - cost
            peak_eq = max(peak_eq, pnl)
            dd = max(dd, peak_eq - pnl)
            rets.append(proceeds / cost - 1.0)
            holds.append(et - t_in)
            reasons[why] = reasons.get(why, 0) + 1
            if keep_trades:
                trades.append((t_in, et, self.ds.tok_sym[ti], proceeds / cost - 1.0, why))
        n = len(rets)
        mean = sum(rets) / n if n else None
        sd = statistics.pstdev(rets) if n > 1 else None
        res = {"n": n, "mean": mean, "sd": sd, "lcb": (mean - sd / math.sqrt(n)) if n > 1 else None,
               "win": (sum(1 for r in rets if r > 0) / n) if n else None, "pnl": pnl, "maxDD": dd / self.bankroll,
               "avgHoldMin": (sum(holds) / n / 60.0) if n else None, "missed": missed, "reasons": reasons,
               "sig": hash(tuple(sig))}
        if keep_trades:
            res["trades"] = trades
        return res


def fitness(res, min_trades):
    """Lower 1-sigma confidence bound of the average net return per trade, in %. Below min_trades the genome
    is ranked by trade count only (always below any qualifying genome) so selection pushes toward trading."""
    n = res["n"]
    if n < max(2, min_trades):
        return -60.0 + 2.0 * n
    return 100.0 * res["lcb"]


class Lab:
    def __init__(self, cfg, rng=None):
        self.cfg = cfg  # full app config; lab settings in cfg["lab"]
        self.rng = rng or random.Random()
        self.ds = None
        self.ev = None
        self.pop = []           # genomes
        self.scored = []        # [(fitness, genome, train_res)] for the current generation
        self.gen = 0
        self.evals = 0
        self.history = []       # per generation stats
        self.hall = []          # validated champions
        self.mutation = None
        self.stall = 0
        self.best_seen = -math.inf
        self.phase = "idle"
        self.last_error = None
        self.gen_secs = None
        self.deployed = set()   # genome keys already sent to live
        self.gens_on_data = 0

    @property
    def lc(self):
        return self.cfg["lab"]

    # ---- data
    def build(self, records, yield_every=0.0):
        self.phase = "building dataset"
        ds = Dataset(records, self.cfg["execution"], self.cfg["universe"], self.lc["sampleEverySec"], yield_every)
        if not ds.cands or ds.hours < 0.25:
            self.phase = "waiting for data"
            raise ValueError(f"not enough recorded data yet ({ds.hours:.2f}h, {len(ds.cands)} candidates)")
        self.ds = ds
        self.gens_on_data = 0
        self.ev = Evaluator(ds, self.cfg["execution"], self.cfg["sizing"])
        self.split = ds.split_time(self.lc["validateFrac"])
        self.phase = "evolving"

    # ---- population
    def seed(self, specs):
        seen, pop = set(), []
        for sp in specs:
            g = S.genome(sp)
            k = S.genome_key(g)
            if k not in seen:
                seen.add(k)
                pop.append(g)
        while len(pop) < self.lc["population"]:
            g = S.random_spec(self.rng)
            k = S.genome_key(g)
            if k not in seen:
                seen.add(k)
                pop.append(g)
        self.pop = pop[: max(self.lc["population"], len(pop))]

    def _tournament(self, scored):
        k = max(2, int(self.lc["tournament"]))
        return max(self.rng.sample(scored, min(k, len(scored))), key=lambda x: x[0])[1]

    def step(self, throttle=None):
        """Evaluate the current population and breed the next one."""
        if self.ds is None:
            raise RuntimeError("no dataset")
        lc = self.lc
        t0 = time.time()
        if self.mutation is None:
            self.mutation = lc["mutation"]
        scored, by_sig = [], {}
        for g in self.pop:
            te = time.time()
            res = self.ev.evaluate(g, t_to=self.split)
            self.evals += 1
            entry = [fitness(res, lc["minTrades"]), g, res]
            scored.append(entry)
            # clone removal: genomes that make exactly the same trades are one strategy; only the best counts
            if res["n"] > 0:
                prev = by_sig.get(res["sig"])
                if prev is None:
                    by_sig[res["sig"]] = entry
                elif entry[0] > prev[0]:
                    prev[0] = CLONE
                    by_sig[res["sig"]] = entry
                else:
                    entry[0] = CLONE
            if throttle:
                throttle(time.time() - te)
        scored.sort(key=lambda x: x[0], reverse=True)
        self.scored = scored
        best = scored[0][0]
        fits = [s[0] for s in scored if s[0] > CLONE and s[2]["n"] >= lc["minTrades"]]
        uniq = len({s[2]["sig"] for s in scored if s[2]["n"] > 0})
        # adaptive mutation: raise it when progress stalls, relax it after an improvement
        if best > self.best_seen + 1e-9:
            self.best_seen = best
            self.stall = 0
            self.mutation = max(lc["mutation"], self.mutation * 0.8)
        else:
            self.stall += 1
            if self.stall >= 4:
                self.mutation = min(0.35, self.mutation * 1.3)
        self._validate(scored[:8])
        self.history.append({"gen": self.gen, "best": best, "median": statistics.median(fits) if fits else None,
                             "unique": uniq, "mutation": self.mutation, "validated": len(self.hall), "t": time.time()})
        self.history = self.history[-300:]
        # next generation
        nxt, keys = [], set()
        for s in scored:
            if len(nxt) >= lc["elite"]:
                break
            if s[0] > CLONE:
                k = S.genome_key(s[1])
                if k not in keys:
                    keys.add(k)
                    nxt.append(s[1])
        pool = [s for s in scored if s[0] > CLONE] or scored
        tries = 0
        while len(nxt) < lc["population"] and tries < lc["population"] * 20:
            tries += 1
            r = self.rng.random()
            if r < lc["immigrants"]:
                child = S.random_spec(self.rng)
            else:
                a = self._tournament(pool)
                child = S.crossover(a, self._tournament(pool), self.rng) if self.rng.random() < lc["crossover"] else a
                child = S.mutate_genes(child, self.mutation, self.rng)
            k = S.genome_key(child)
            if k in keys:
                continue
            keys.add(k)
            nxt.append(child)
        self.pop = nxt
        self.gen += 1
        self.gens_on_data = getattr(self, "gens_on_data", 0) + 1
        self.gen_secs = time.time() - t0

    def _validate(self, top):
        lc = self.lc
        for fit, g, res in top:
            if fit == CLONE or res["n"] < lc["minTrades"] or res["mean"] is None:
                continue
            k = S.genome_key(g)
            val = self.ev.evaluate(g, t_from=self.split)
            ok = (val["n"] >= lc["minValTrades"] and val["mean"] is not None and val["mean"] > 0 and res["mean"] > 0)
            entry = {"key": k, "genome": g, "fitness": fit, "train": _brief(res), "val": _brief(val),
                     "rank": min(fit, 100.0 * val["mean"]) if ok else None, "gen": self.gen, "validated": ok,
                     "at": time.time()}
            self.hall = [h for h in self.hall if h["key"] != k]
            if ok:
                self.hall.append(entry)
        self.hall.sort(key=lambda h: h["rank"], reverse=True)
        self.hall = self.hall[:20]

    def champions(self, n):
        """Best validated genomes not yet deployed to live."""
        out = []
        for h in self.hall:
            if h["key"] not in self.deployed:
                out.append(h)
            if len(out) >= n:
                break
        return out

    def view(self):
        ds = self.ds
        return {
            "phase": self.phase, "lastError": self.last_error, "gen": self.gen, "evals": self.evals,
            "genSecs": self.gen_secs, "mutation": self.mutation,
            "dataset": None if ds is None else {"hours": ds.hours, "rows": ds.rows, "cands": len(ds.cands),
                                                "tokens": len(ds.tok_addr), "t0": ds.t0, "t1": ds.t1,
                                                "split": self.split, "builtAt": ds.built_at},
            "history": self.history[-150:],
            "hall": [{k: h[k] for k in ("key", "fitness", "train", "val", "rank", "gen", "at")}
                     | {"desc": S.describe(h["genome"]), "deployed": h["key"] in self.deployed} for h in self.hall],
            "top": [{"fitness": f, "desc": S.describe(g), "train": _brief(r), "clone": f == CLONE}
                    for f, g, r in self.scored[:12]],
        }

    def to_dict(self):
        return {"pop": self.pop, "hall": self.hall, "gen": self.gen, "evals": self.evals, "history": self.history,
                "deployed": list(self.deployed), "mutation": self.mutation, "best_seen": self.best_seen}

    def load(self, d):
        if not d:
            return
        self.pop = [S.genome(g) for g in d.get("pop", [])]
        self.hall = d.get("hall", [])
        self.gen = d.get("gen", 0)
        self.evals = d.get("evals", 0)
        self.history = d.get("history", [])
        self.deployed = set(d.get("deployed", []))
        self.mutation = d.get("mutation")
        bs = d.get("best_seen")
        self.best_seen = bs if isinstance(bs, (int, float)) else -math.inf


def _brief(r):
    return {k: r.get(k) for k in ("n", "mean", "lcb", "win", "pnl", "maxDD", "avgHoldMin", "missed", "reasons")}
