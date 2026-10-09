"""Genetic lab: evolves strategy genomes on recorded market data, in seconds instead of live hours.

Pipeline
  1. Dataset: replay the recorded snapshots once, computing (exactly as live does) every token's features
     whenever it trades, plus a compact price path per token for valuing positions.
  2. Evaluator: simulates one genome over that dataset: entry filters, latency, the buy slippage limit,
     fees + impact + gas, every exit rule, maxOpen / cooldown / cash. Position outcomes are memoised.
  3. GA: tournament selection, uniform crossover, per-gene mutation with adaptive rate, elitism, random
     immigrants, and clone removal (genomes that make the same trades count once).
  4. Exams: the training part is cut into consecutive slices and a genome is rewarded for profiting in most
     of them; the most recent part of the window is held back for validation; champions must also survive a
     stress test (extra latency and fees). Champions are re-checked whenever the data refreshes.
  5. Scoring favours simple rules (penalty per filter), steady equity (drawdown penalty), more trades when
     profitable, and different ideas (genomes that trade the same tokens as many others are marked down).
     With dropBest (pump.fun), genomes are scored and examined without their best trade(s), so a strategy whose
     profit is one lucky pump can't win.
  6. Live feedback: champions that fail in live paper trading are blacklisted and their close relatives are
     marked down; live winners are fed back into the population.
  7. Slippage calibration (calibrateSlippage): recorded snapshots can't show how far the price moves between a
     signal and its fill, but live fills measure it. The measured per-side slippage is charged on every simulated
     trade, so the lab's returns match what live trading actually gets.
  8. Long-run record: the lab only trains on a short recent window, so on its own it forgets. Every tracked genome
     (the best of each window, champions, live winners) is also scored on each new stretch of data that arrived
     after it was added, i.e. data it was never bred on. Only running totals are kept, so the record survives the
     raw data being deleted and keeps growing for weeks. Genomes with a long, positive record are "proven": they
     breed with a bonus, are always put back in the population, and go live first.
"""
import bisect
import heapq
import math
import random
import statistics
import time

from . import strategy as S
from .market import Market
from .sim import model_venue, sell_gross

CLONE = -1000.0  # fitness given to a genome that duplicates another's trades
SERIES_CAP = 160  # long-run record: chart points per genome; when full, neighbours merge so it spans the whole life
UNQUALIFIED = -80.0  # base fitness below minTrades (plus 2 per trade), so selection still pushes toward trading
RANGE_KEYS = [k for k, v in S.FILTERS.items() if v[2] == "range"]
ENUM_KEYS = [k for k, v in S.FILTERS.items() if v[2] == "enum"]
FEAT_KEYS = RANGE_KEYS + ENUM_KEYS
FI = {k: i for i, k in enumerate(FEAT_KEYS)}

DEFAULT_LAB = {"enabled": True, "windowHours": 12, "validateFrac": 0.3, "population": 60, "elite": 4,
               "tournament": 3, "crossover": 0.7, "mutation": 0.12, "immigrants": 0.1, "minTrades": 12,
               "minValTrades": 15, "rebuildMin": 90, "sampleEverySec": 10, "duty": 0.3,
               "promoteCount": 3, "maxGensPerData": 50, "calibrateSlippage": False, "candGapSec": 0,
               # exams
               "folds": 4, "minFoldShare": 0.5, "stressLatencyMs": 750, "stressFeeBps": 50,
               # scoring
               "complexityPenalty": 0.5, "consistencyPenalty": 4.0, "ddPenalty": 0.1, "activityBonus": 0.5,
               "nicheSimilarity": 0.5, "nichePenalty": 1.0,
               "dropBest": 0,  # best trades left out of scoring and exams (pump.fun: 1; 0 keeps pons as it was)
               # live feedback
               "feedbackPenalty": 3.0, "feedbackBonus": 1.5,
               # long-run record (forward test on every new stretch of data)
               "ledgerSize": 400, "ledgerAdd": 20, "segmentMin": 30, "segmentTailMin": 15,
               "provenSegments": 12, "provenTrades": 40, "provenShare": 0.55, "longRunWeight": 1.0}


class Dataset:
    """Entry candidates (features when a token trades) + per-token price paths."""

    def __init__(self, records, ex, universe, sample_every=10.0, yield_every=0.0, cand_gap=0.0):
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
        self.tick_rows = 0
        for t, items, ticks, tick_gap in records:
            prev = self.t1
            if self.t0 is None:
                self.t0 = t
            elif t - self.t1 < 120:
                self.covered += t - self.t1
            self.t1 = t
            pending = []
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
                # a candidate when the token traded (at most every cand_gap s), else every sample_every s
                if t - last_c.get(i, 0) < (cand_gap if changed else sample_every):
                    continue
                pending.append((tok, i))
            # trades are applied before features, exactly like the live engine; a recording gap restarts windows
            if ticks is not None:
                if mk.tick_start is None or tick_gap or (prev is not None and t - prev > 60):
                    mk.start_ticks(t)
                mk.add_tick_rows(ticks, t)
                self.tick_rows += len(ticks)
            elif mk.tick_start is not None and prev is not None and t - prev > 60:
                mk.tick_start = None  # feed wasn't recorded: tick signals unknown until it resumes
                for tk in mk.tokens.values():
                    tk.tick_since = math.inf
            for tok, i in pending:
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
    return sell_gross(Q, T, tokens) * (1.0 - ff) * qu


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
    def __init__(self, ds, ex, sizing, extra_latency=0.0, extra_fee=0.0):
        self.ds = ds
        self.extra_fee = extra_fee
        self.ex = ex
        self.size = float(sizing["sizeUsd"])
        self.max_open = int(sizing["maxOpen"])
        self.bankroll = float(sizing["bankrollUsd"])
        self.cooldown = float(sizing.get("cooldownMin") or 0) * 60
        self.lat = ex["latencyMs"] / 1000.0 + extra_latency
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
        tokens *= 1.0 - self.extra_fee
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
            ret = (_sell_usd(p, tokens) * (1.0 - self.extra_fee) - self.gas) / cost - 1.0
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
        proceeds = _sell_usd(pts[ei], tokens) * (1.0 - self.extra_fee) - self.gas
        r = (t_exit + (self.lat if reason != "end of data" else 0.0), cost, proceeds, reason)
        if len(self.memo) > 120000:  # keep memory bounded on small servers
            self.memo.clear()
        self.memo[mk] = r
        return r

    def evaluate(self, g, t_from=-math.inf, t_to=math.inf, keep_trades=False, folds=None, want_entries=False):
        """folds: sorted boundary times; results are also broken down per slice between them."""
        enum, rng = compile_filters(g.get("filters"))
        nf = len(folds) + 1 if folds else 0
        fold_n, fold_sum = [0] * nf, [0.0] * nf
        entries = set() if want_entries else None
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
                if nf:
                    fx = bisect.bisect_right(folds, t_in)
                    fold_n[fx] += 1
                    fold_sum[fx] += proceeds / cost - 1.0
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
            if want_entries:
                entries.add(ci)
        while open_h:
            et, ti, cost, proceeds, why, t_in = heapq.heappop(open_h)
            pnl += proceeds - cost
            peak_eq = max(peak_eq, pnl)
            dd = max(dd, peak_eq - pnl)
            rets.append(proceeds / cost - 1.0)
            holds.append(et - t_in)
            reasons[why] = reasons.get(why, 0) + 1
            if nf:
                fx = bisect.bisect_right(folds, t_in)
                fold_n[fx] += 1
                fold_sum[fx] += proceeds / cost - 1.0
            if keep_trades:
                trades.append((t_in, et, self.ds.tok_sym[ti], proceeds / cost - 1.0, why))
        n = len(rets)
        mean = sum(rets) / n if n else None
        sd = statistics.pstdev(rets) if n > 1 else None
        top = heapq.nlargest(5, rets)
        res = {"n": n, "mean": mean, "sd": sd, "lcb": (mean - sd / math.sqrt(n)) if n > 1 else None,
               "win": (sum(1 for r in rets if r > 0) / n) if n else None, "pnl": pnl, "maxDD": dd / self.bankroll,
               "avgHoldMin": (sum(holds) / n / 60.0) if n else None, "missed": missed, "reasons": reasons,
               "sig": hash(tuple(sig)), "nfilters": len(enum) + len(rng), "top": top}
        if nf:
            res["folds"] = [[fold_n[x], fold_sum[x] / fold_n[x] if fold_n[x] else None] for x in range(nf)]
            res["consistency"] = sum(1 for x in range(nf) if fold_n[x] and fold_sum[x] > 0) / nf
        if want_entries:
            res["entries"] = entries
        if keep_trades:
            res["trades"] = trades
        return res


def robust_mean(res, drop):
    """Average net return per trade with the `drop` best trades left out (None if that leaves none)."""
    n = res["n"]
    if not drop or not n:
        return res["mean"]
    if n <= drop:
        return None
    return (res["mean"] * n - sum(res["top"][:drop])) / (n - drop)


def _positive(x):
    return x is not None and x > 0


def fitness(res, lc, hours):
    """-> (fitness, parts). Base = lower 1-sigma bound of average net return per trade (%), the average taken
    without the `dropBest` best trades, then:
    - per active filter (simpler rules generalise better)
    - for training slices that weren't profitable (consistency)
    - for drawdown beyond 10% of bankroll
    + a small bonus for trading more often, only when the average is positive.
    Below minTrades a genome is ranked by trade count only, below every qualifying genome."""
    n = res["n"]
    drop = int(lc.get("dropBest") or 0)
    if n < max(2, lc["minTrades"], drop + 2):
        return UNQUALIFIED + 2.0 * n, {"trades": n}
    mean = robust_mean(res, drop)
    parts = {"base": 100.0 * (mean - res["sd"] / math.sqrt(n)) if drop else 100.0 * res["lcb"],
             "simplicity": -lc["complexityPenalty"] * res.get("nfilters", 0),
             "consistency": -lc["consistencyPenalty"] * (1.0 - res.get("consistency", 1.0)),
             "drawdown": -lc["ddPenalty"] * max(0.0, 100.0 * res["maxDD"] - 10.0),
             "activity": lc["activityBonus"] * math.log2(1.0 + n / max(0.1, hours)) if mean > 0 else 0.0}
    return sum(parts.values()), parts


def gene_vector(g):
    return tuple(json_key(S._get(g, path)) for path in S.GENE_PATHS)


def json_key(v):
    return tuple(v) if isinstance(v, list) else v


def gene_distance(a, b):
    """Number of genes that differ between two gene vectors."""
    return sum(1 for x, y in zip(a, b) if x != y)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


class Lab:
    def __init__(self, cfg, rng=None):
        self.cfg = cfg  # full app config; lab settings in cfg["lab"]
        self.rng = rng or random.Random()
        self.ds = None
        self.ev = None
        self.ev_stress = None
        self.pop = []           # genomes
        self.scored = []        # current generation: dicts with fit, sel, genome, res, parts
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
        self.feedback = {}      # genome key -> live result {status, score, n, pnl, why, genome, at}
        self.gens_on_data = 0
        self.split = None
        self.folds = []
        self.slip, self.slip_n = 0.0, 0  # per-side slippage measured on live fills (fraction), and from how many
        self.ledger = {}        # genome key -> long-run record (see _ledger_update); replaced, never resized in place
        self.ledger_t = None    # data time the last long-run segment ended
        self.ledger_first = None
        self.ledger_segs = 0

    @property
    def lc(self):
        return self.cfg["lab"]

    # ---- data
    def build(self, records, yield_every=0.0, throttle=None):
        self.phase = "building dataset"
        ds = Dataset(records, self.cfg["execution"], self.cfg["universe"], self.lc["sampleEverySec"], yield_every,
                     self.lc.get("candGapSec") or 0.0)
        if not ds.cands or ds.hours < 0.25:
            self.phase = "waiting for data"
            raise ValueError(f"not enough recorded data yet ({ds.hours:.2f}h, {len(ds.cands)} candidates)")
        lc = self.lc
        ex = self.cfg["execution"]
        self.ds = ds
        self.gens_on_data = 0
        self.best_seen = -math.inf
        slip = self.slip if lc.get("calibrateSlippage") else 0.0
        self.ev = Evaluator(ds, ex, self.cfg["sizing"], extra_fee=slip)
        self.ev_stress = Evaluator(ds, ex, self.cfg["sizing"], extra_latency=lc["stressLatencyMs"] / 1000.0,
                                   extra_fee=lc["stressFeeBps"] / 1e4 + slip)
        self.slip_used = slip
        self.split = ds.split_time(lc["validateFrac"])
        m = bisect.bisect_left(ds.cands, (self.split,))
        k = max(1, int(lc["folds"]))
        self.folds = [ds.cands[int(m * j / k)][0] for j in range(1, k)] if m > k else []
        self.train_hours = ds.hours * (1.0 - lc["validateFrac"])
        self._ledger_update(ds, throttle)
        self._revalidate_hall()
        proven = [e["genome"] for e in self.ledger.values() if e["status"] == "proven"]
        if proven and self.pop:
            self.inject(proven)  # proven genomes never drop out of the gene pool
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

    def inject(self, specs):
        """Put genomes (e.g. live winners) into the population, replacing its tail."""
        keys = {S.genome_key(g) for g in self.pop}
        new = [S.genome(sp) for sp in specs]
        new = [g for g in new if S.genome_key(g) not in keys][: max(0, len(self.pop) // 3)]
        if new:
            self.pop = self.pop[: len(self.pop) - len(new)] + new

    def _judge(self, g, want_entries=False):
        res = self.ev.evaluate(g, t_to=self.split, folds=self.folds, want_entries=want_entries)
        self.evals += 1
        fit, parts = fitness(res, self.lc, self.train_hours)
        return fit, parts, res

    def _feedback_adjust(self, vec):
        """Live feedback: mark down relatives of champions that failed live, mark up relatives of live winners."""
        lc = self.lc
        adj = 0.0
        for fb in self.feedback.values():
            fv = fb.get("vec")
            if fv is None or gene_distance(vec, fv) > 2:
                continue
            if fb["status"] == "failed":
                adj = min(adj, -lc["feedbackPenalty"])
            elif fb["status"] == "winning":
                adj = max(adj, lc["feedbackBonus"]) if adj >= 0 else adj
        return adj

    def _tournament(self, pool):
        k = max(2, int(self.lc["tournament"]))
        return max(self.rng.sample(pool, min(k, len(pool))), key=lambda x: x["sel"])["genome"]

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
            fit, parts, res = self._judge(g, want_entries=True)
            e = {"fit": fit, "sel": fit, "genome": g, "res": res, "parts": parts, "similar": 0}
            scored.append(e)
            # clone removal: genomes that make exactly the same trades are one strategy; only the best counts
            if res["n"] > 0:
                prev = by_sig.get(res["sig"])
                if prev is None:
                    by_sig[res["sig"]] = e
                elif e["fit"] > prev["fit"]:
                    prev["fit"] = prev["sel"] = CLONE
                    by_sig[res["sig"]] = e
                else:
                    e["fit"] = e["sel"] = CLONE
            if throttle:
                throttle(time.time() - te)
        # niching: genomes trading mostly the same tokens as many others are the same idea; mark them down for
        # selection so the search keeps exploring different ideas. Live feedback adjusts selection too.
        live = [e for e in scored if e["fit"] > CLONE and e["res"]["n"] >= lc["minTrades"]]
        for i, a in enumerate(live):
            a["similar"] = sum(1 for j, b in enumerate(live)
                               if i != j and jaccard(a["res"]["entries"], b["res"]["entries"]) >= lc["nicheSimilarity"])
        for e in scored:
            if e["fit"] > CLONE:
                e["vec"] = gene_vector(e["genome"])
                e["feedback"] = self._feedback_adjust(e["vec"])
                e["longrun"] = self._longrun_adjust(S.genome_key(e["genome"]))
                e["sel"] = e["fit"] - lc["nichePenalty"] * e["similar"] + e["feedback"] + e["longrun"]
        scored.sort(key=lambda x: x["fit"], reverse=True)
        self.scored = scored
        qual = [e["fit"] for e in live]
        best = scored[0]["fit"]
        uniq = len({e["res"]["sig"] for e in scored if e["res"]["n"] > 0})
        # adaptive mutation: raise it when progress stalls, relax it after an improvement
        if best > self.best_seen + 1e-9:
            self.best_seen = best
            self.stall = 0
            self.mutation = max(lc["mutation"], self.mutation * 0.8)
        else:
            self.stall += 1
            if self.stall >= 4:
                self.mutation = min(0.35, self.mutation * 1.3)
        self._validate(sorted(live, key=lambda e: e["sel"], reverse=True)[:8])
        self.history.append({"gen": self.gen, "best": best, "bestQual": max(qual) if qual else None,
                             "median": statistics.median(qual) if qual else None, "unique": uniq,
                             "mutation": self.mutation, "validated": len(self.hall), "t": time.time()})
        self.history = self.history[-300:]
        # elites: best by selection fitness, but no two that trade mostly the same tokens
        nxt, keys, elite_ents = [], set(), []
        for e in sorted(scored, key=lambda x: x["sel"], reverse=True):
            if len(nxt) >= lc["elite"] or e["fit"] <= CLONE:
                break
            ents = e["res"].get("entries") or set()
            if any(jaccard(ents, o) > 0.7 for o in elite_ents):
                continue
            k = S.genome_key(e["genome"])
            if k not in keys:
                keys.add(k)
                nxt.append(e["genome"])
                elite_ents.append(ents)
        pool = [e for e in scored if e["fit"] > CLONE] or scored
        tries = 0
        while len(nxt) < lc["population"] and tries < lc["population"] * 20:
            tries += 1
            if self.rng.random() < lc["immigrants"]:
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
        for e in scored:  # entry sets are only needed within a generation
            e["res"].pop("entries", None)
        self.pop = nxt
        self.gen += 1
        self.gens_on_data += 1
        if self.gens_on_data == int(lc.get("maxGensPerData", 50)):
            # the round is over: its best start their out-of-sample record from the end of this data, which gives
            # them the whole next stretch instead of waiting for the refresh
            self._ledger_track(self.ds.t1)
        self.gen_secs = time.time() - t0

    # ---- long-run record
    def _ledger_update(self, ds, throttle=None):
        """Score every tracked genome on the data that arrived since the previous update, counting only entries
        after the genome was added (so it was never bred on them), then start tracking this round's best."""
        lc = self.lc
        seg_from = max(self.ledger_t or ds.t0, ds.t0)
        seg_to = ds.t1 - lc["segmentTailMin"] * 60  # leave a tail so most of the segment's trades can close
        if seg_to - seg_from >= lc["segmentMin"] * 60:
            for e in list(self.ledger.values()):
                t_from = max(seg_from, e["since"])
                if seg_to - t_from < lc["segmentMin"] * 60:
                    continue
                te = time.time()
                res = self.ev.evaluate(e["genome"], t_from=t_from, t_to=seg_to)
                n = res["n"]
                e["seen"] += 1
                if n:
                    mean, sd = res["mean"], res["sd"] or 0.0
                    e["segs"] += 1
                    e["pos"] += 1 if mean > 0 else 0
                    e["n"] += n
                    e["sr"] += mean * n
                    e["sr2"] += (sd * sd + mean * mean) * n
                    e["wins"] += round(res["win"] * n)
                    e["pnl"] += res["pnl"]
                e["series"] = _append_point(e["series"], [round(seg_to), n, round(res["mean"] * n, 5) if n else 0.0])
                e["last"] = seg_to
                status = self._ledger_status(e)
                e["wasProven"] = e.get("wasProven") or status == "proven"
                e["status"] = status
                if throttle:
                    throttle(time.time() - te)
            self.ledger_t = seg_to
            self.ledger_first = self.ledger_first or seg_from
            self.ledger_segs += 1
        elif self.ledger_t is None:
            self.ledger_t = seg_to
        self._ledger_track(ds.t1)

    def _ledger_track(self, since):
        """Start tracking this round's best genomes, champions and live winners, scored on data after `since`."""
        lc = self.lc
        new = {}
        picks = [e["genome"] for e in sorted(self.scored, key=lambda x: x["sel"], reverse=True)
                 if e["fit"] > CLONE and e["res"]["n"] >= lc["minTrades"]][: int(lc["ledgerAdd"])]
        if not self.scored:  # just restarted: the population (elites first) is the best we have
            picks = [S.genome(g) for g in self.pop[: int(lc["ledgerAdd"])]]
        picks += [h["genome"] for h in self.hall]
        picks += [f["genome"] for f in self.feedback.values() if f["status"] == "winning"]
        for g in picks:
            k = S.genome_key(g)
            if k not in self.ledger and k not in new:
                new[k] = {"genome": S.genome(g), "since": since, "added": time.time(), "seen": 0, "segs": 0, "pos": 0,
                          "n": 0, "sr": 0.0, "sr2": 0.0, "wins": 0, "pnl": 0.0, "series": [], "status": "tracking",
                          "last": None}
        ledger = {**self.ledger, **new}
        if len(ledger) > lc["ledgerSize"]:  # keep proven, then the most promising; drop proven losers first
            keep = sorted(ledger.items(), key=lambda kv: self._keep_rank(kv[1]), reverse=True)[: int(lc["ledgerSize"])]
            ledger = dict(keep)
        self.ledger = ledger  # replaced in one go: the dashboard may be reading the old one

    def _ledger_status(self, e):
        lc = self.lc
        if e["segs"] < lc["provenSegments"] or e["n"] < lc["provenTrades"]:
            return "tracking"
        lcb = ledger_lcb(e)
        if lcb is not None and lcb > 0 and e["pos"] / e["segs"] >= lc["provenShare"]:
            return "proven"
        return "losing"

    @staticmethod
    def _keep_rank(e):
        lcb = ledger_lcb(e)
        lcb = lcb if lcb is not None else 0.0
        if e["status"] == "proven":
            return (3, lcb)
        if e["status"] == "tracking":
            return (1, lcb) if e["segs"] >= 4 and lcb < 0 else (2, lcb, -e["added"])
        return (0, lcb)

    def _longrun_adjust(self, key):
        """Breeding bonus (or penalty) from the long-run record, in fitness points (% per trade)."""
        e = self.ledger.get(key)
        if not e or e["segs"] < 3 or e["n"] < self.lc["minTrades"]:  # a few lucky trades must not steer breeding
            return 0.0
        lcb = ledger_lcb(e)
        return 0.0 if lcb is None else self.lc.get("longRunWeight", 1.0) * max(-10.0, min(10.0, lcb))

    def pick(self, keys):
        """Champions or long-run genomes with these keys (for a forced live deploy)."""
        out = [h for h in self.hall if h["key"] in keys]
        have = {h["key"] for h in out}
        out += [{"key": k, "genome": e["genome"], "gen": 0, "longrun": True}
                for k, e in self.ledger.items() if k in keys and k not in have]
        return out

    # ---- exams
    def _exam(self, g, fit=None, parts=None, res=None):
        """Full champion check -> (passed, entry, reason). Train: enough trades, positive average, profitable
        in most slices. Validation: enough trades and positive. Stress (whole window, worse latency and fees):
        positive. Each average also has to stay positive without the `dropBest` best trades. Not blacklisted by
        live results."""
        lc = self.lc
        drop = int(lc.get("dropBest") or 0)
        lucky = f"only profitable because of its best trade{'s' if drop > 1 else ''}"
        if res is None:
            fit, parts, res = self._judge(g)
        key = S.genome_key(g)
        entry = {"key": key, "genome": g, "fitness": fit, "parts": parts, "train": _brief(res),
                 "consistency": res.get("consistency"), "folds": res.get("folds"), "gen": self.gen, "at": time.time()}
        fb = self.feedback.get(key)
        if fb and fb["status"] == "failed":
            return False, entry, "failed in live trading"
        if fit <= CLONE or res["n"] < lc["minTrades"] or not res["mean"] or res["mean"] <= 0:
            return False, entry, "not profitable on training data"
        if not _positive(robust_mean(res, drop)):
            return False, entry, f"training: {lucky}"
        if (res.get("consistency") or 0) < lc["minFoldShare"]:
            return False, entry, "profitable in too few training slices"
        val = self.ev.evaluate(g, t_from=self.split)
        entry["val"] = _brief(val)
        if val["n"] < lc["minValTrades"] or not val["mean"] or val["mean"] <= 0:
            return False, entry, "failed validation"
        val_mean = robust_mean(val, drop)
        if not _positive(val_mean):
            return False, entry, f"validation: {lucky}"
        stress = self.ev_stress.evaluate(g)
        entry["stress"] = _brief(stress)
        if stress["n"] < lc["minTrades"] or not stress["mean"] or stress["mean"] <= 0:
            return False, entry, "failed the stress test"
        stress_mean = robust_mean(stress, drop)
        if not _positive(stress_mean):
            return False, entry, f"stress test: {lucky}"
        entry["rank"] = min(fit, 100.0 * val_mean, 100.0 * stress_mean)
        entry["vec"] = gene_vector(g)
        return True, entry, None

    def _admit(self, entry):
        """Add to the hall, keeping only the better of two near-identical genomes (<= 1 gene apart)."""
        for h in self.hall:
            if h["key"] != entry["key"] and gene_distance(h.get("vec") or gene_vector(h["genome"]), entry["vec"]) <= 1:
                if h["rank"] >= entry["rank"]:
                    return
                self.hall.remove(h)
                break
        self.hall = [h for h in self.hall if h["key"] != entry["key"]] + [entry]

    def _validate(self, top):
        for e in top:
            ok, entry, why = self._exam(e["genome"], e["fit"], e["parts"], e["res"])
            if ok:
                self._admit(entry)
            else:
                self.hall = [h for h in self.hall if h["key"] != entry["key"]]
        self.hall.sort(key=lambda h: h["rank"], reverse=True)
        self.hall = self.hall[:20]

    def _revalidate_hall(self):
        """Champions must keep passing on fresh data; ones that stop passing are dropped."""
        old, self.hall, self.dropped = self.hall, [], []
        for h in old:
            ok, entry, why = self._exam(S.genome(h["genome"]))
            if ok:
                entry["gen"] = h.get("gen", entry["gen"])
                self._admit(entry)
            else:
                self.dropped.append({"desc": S.describe(h["genome"]), "why": why})
        self.hall.sort(key=lambda h: h["rank"], reverse=True)

    def record_live(self, spec, status, score, n, pnl, why=None):
        """Called by the live engine for strategies that came from the lab."""
        g = S.genome(spec)
        key = S.genome_key(g)
        prev = self.feedback.get(key)
        if prev and prev["status"] == "failed":
            return
        self.feedback[key] = {"status": status, "score": score, "n": n, "pnl": pnl, "why": why,
                              "genome": g, "vec": gene_vector(g), "at": time.time()}
        if status == "failed":
            self.hall = [h for h in self.hall if h["key"] != key]
        if len(self.feedback) > 300:
            oldest = sorted(self.feedback, key=lambda k: self.feedback[k]["at"])[:50]
            for k in oldest:
                del self.feedback[k]

    def champions(self, n):
        """Genomes for live trading, not yet deployed: long-run proven ones first, then validated champions."""
        proven = sorted((kv for kv in self.ledger.items() if kv[1]["status"] == "proven" and kv[0] not in self.deployed),
                        key=lambda kv: ledger_lcb(kv[1]) or 0.0, reverse=True)
        picks = [{"key": k, "genome": e["genome"], "gen": 0, "longrun": True} for k, e in proven]
        have = {p["key"] for p in picks}
        picks += [h for h in self.hall if h["key"] not in self.deployed and h["key"] not in have]
        return picks[:n]

    def view(self):
        ds = self.ds

        def fb(key):
            f = self.feedback.get(key)
            return {k: f[k] for k in ("status", "score", "n", "pnl", "why")} if f else None
        return {
            "phase": self.phase, "lastError": self.last_error, "gen": self.gen, "evals": self.evals,
            "genSecs": self.gen_secs, "mutation": self.mutation,
            "slippage": {"perSide": getattr(self, "slip_used", 0.0), "measured": self.slip, "fills": self.slip_n},
            "dataset": None if ds is None else {"hours": ds.hours, "rows": ds.rows, "cands": len(ds.cands),
                                                "tokens": len(ds.tok_addr), "t0": ds.t0, "t1": ds.t1,
                                                "split": self.split, "builtAt": ds.built_at, "folds": len(self.folds) + 1},
            "history": self.history[-150:],
            "hall": [{k: h.get(k) for k in ("key", "fitness", "parts", "train", "val", "stress", "consistency", "folds", "rank", "gen", "at")}
                     | {"desc": S.describe(h["genome"]), "deployed": h["key"] in self.deployed, "live": fb(h["key"])}
                     for h in self.hall],
            "dropped": getattr(self, "dropped", [])[-10:],
            "ledger": self._ledger_view(fb),
            "feedback": {"failed": sum(1 for f in self.feedback.values() if f["status"] == "failed"),
                         "winning": sum(1 for f in self.feedback.values() if f["status"] == "winning"),
                         "losing": sum(1 for f in self.feedback.values() if f["status"] == "losing")},
            "top": [{"fitness": e["fit"], "sel": e["sel"], "parts": e["parts"], "similar": e["similar"],
                     "feedback": e.get("feedback", 0.0), "longrun": e.get("longrun", 0.0),
                     "desc": S.describe(e["genome"]), "train": _brief(e["res"]),
                     "consistency": e["res"].get("consistency"), "nfilters": e["res"].get("nfilters"),
                     "clone": e["fit"] <= CLONE} for e in self.scored[:12]],
        }

    def _ledger_view(self, fb, limit=40):
        order = {"proven": 0, "tracking": 1, "losing": 2}
        rows = sorted(self.ledger.items(), key=lambda kv: (order.get(kv[1]["status"], 3), -(ledger_lcb(kv[1]) or -1e9)))
        out = []
        for k, e in rows[:limit]:
            n = e["n"]
            cum, run = [], 0.0
            for _, sn, ssum in e["series"]:
                run += ssum
                cum.append(round(run * 100, 2))
            out.append({"key": k, "desc": S.describe(e["genome"]), "status": e["status"], "wasProven": e.get("wasProven", False),
                        "segs": e["segs"], "seen": e["seen"], "pos": e["pos"], "n": n,
                        "mean": e["sr"] / n if n else None, "lcb": ledger_lcb(e), "win": e["wins"] / n if n else None,
                        "pnl": e["pnl"], "since": e["since"], "last": e["last"], "curve": cum,
                        "deployed": k in self.deployed, "live": fb(k)})
        return {"tracked": len(self.ledger), "proven": sum(1 for e in self.ledger.values() if e["status"] == "proven"),
                "segments": self.ledger_segs, "first": self.ledger_first, "last": self.ledger_t, "rows": out}

    def to_dict(self):
        strip = lambda h: {k: v for k, v in h.items() if k != "vec"}
        return {"pop": self.pop, "hall": [strip(h) for h in self.hall], "gen": self.gen, "evals": self.evals,
                "history": self.history, "deployed": list(self.deployed), "mutation": self.mutation,
                "best_seen": self.best_seen if math.isfinite(self.best_seen) else None,
                "feedback": {k: strip(v) for k, v in self.feedback.items()},
                "ledger": dict(self.ledger), "ledger_t": self.ledger_t, "ledger_first": self.ledger_first,
                "ledger_segs": self.ledger_segs}

    def load(self, d):
        if not d:
            return
        self.pop = [S.genome(g) for g in d.get("pop", [])]
        self.hall = [h for h in d.get("hall", []) if h.get("rank") is not None]
        self.gen = d.get("gen", 0)
        self.evals = d.get("evals", 0)
        self.history = d.get("history", [])
        self.deployed = set(d.get("deployed", []))
        self.mutation = d.get("mutation")
        bs = d.get("best_seen")
        self.best_seen = bs if isinstance(bs, (int, float)) else -math.inf
        for k, v in (d.get("feedback") or {}).items():
            v["vec"] = gene_vector(v["genome"])
            self.feedback[k] = v
        self.ledger = {k: dict(e, genome=S.genome(e["genome"])) for k, e in (d.get("ledger") or {}).items()}
        self.ledger_t = d.get("ledger_t")
        self.ledger_first = d.get("ledger_first")
        self.ledger_segs = d.get("ledger_segs", 0)


def ledger_lcb(e):
    """Long-run record: average net return per trade minus one standard error, in % (None without trades)."""
    n = e["n"]
    if n < 2:  # no standard error from a single trade
        return None
    mean = e["sr"] / n
    sd = math.sqrt(max(0.0, e["sr2"] / n - mean * mean))
    return 100.0 * (mean - sd / math.sqrt(n))


def _append_point(series, point):
    """Add a [t, trades, sum of returns] point; when over SERIES_CAP, merge neighbouring pairs (sums add up, the
    later time is kept) so the series always covers the genome's whole life at a coarser step."""
    series = series + [point]
    if len(series) > SERIES_CAP:
        merged = [[b[0], a[1] + b[1], round(a[2] + b[2], 5)] for a, b in zip(series[0::2], series[1::2])]
        series = merged + ([series[-1]] if len(series) % 2 else [])
    return series


def _brief(r):
    return {k: r.get(k) for k in ("n", "mean", "lcb", "win", "pnl", "maxDD", "avgHoldMin", "missed", "reasons")}
