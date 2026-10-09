"""Order execution with latency.

LiveExecutor: each order waits `latencyMs` after the signal, then is quoted against live on-chain state (curve
reserves or the v4 quoter) and checked against the slippage limit, like a real transaction with minOut. On
pump.fun the live state is the coin's bonding curve / PumpSwap pool read from Solana (then a fresh pump.fun API
read if Solana can't be reached).
ReplayExecutor: for backtests on recorded data; fills at the first recorded snapshot at/after the due time.
"""
import heapq
import threading
import time

from . import venue as V
from .sim import Venue, model_venue


def _result(ok, t, src, out=0.0, used=0.0, spot=None, qu=None, fee_frac=0.0, err=None):
    return {"ok": ok, "t_fill": t, "src": src, "out": out, "used_quote": used, "spot_quote": spot,
            "quote_usd": qu, "fee_frac": fee_frac, "err": err}


def fill_with_venue(o, v, qu, ex, t):
    if v is None or not qu:
        return _result(False, t, "none", err="no price")
    if o["side"] == "buy":
        quote_in = (o["size_usd"] - ex["gasUsd"]) / qu
        out, used = v.buy(quote_in)
        if out <= 0:
            return _result(False, t, v.src, spot=v.spot, qu=qu, err="curve full")
        if out < o["expected_out"] * (1.0 - o["tol"]):
            return _result(False, t, v.src, spot=v.spot, qu=qu,
                           err=f"slippage: got {out / o['expected_out'] - 1:+.1%} vs quote at signal")
        return _result(True, t, v.src, out=out, used=used, spot=v.spot, qu=qu, fee_frac=v.fee_frac)
    out = v.sell(o["tokens"])
    if o["expected_out"] > 0 and out < o["expected_out"] * (1.0 - o["tol"]):
        return _result(False, t, v.src, spot=v.spot, qu=qu, err=f"slippage: got {out / o['expected_out'] - 1:+.1%}")
    return _result(True, t, v.src, out=out, spot=v.spot, qu=qu, fee_frac=v.fee_frac)


class LiveExecutor:
    def __init__(self, engine):
        self.engine = engine
        self.heap = []
        self.seq = 0
        self.cv = threading.Condition()
        self.thread = threading.Thread(target=self._run, name="executor", daemon=True)
        self.last_error = None
        self.rpc_rtt = 0.6  # EMA of the quote round trip; calls start rtt/2 early so state is read at the due time
        self.thread.start()

    def submit(self, order):
        with self.cv:
            self.seq += 1
            heapq.heappush(self.heap, (order["due"], self.seq, order))
            self.cv.notify()

    def pending(self):
        with self.cv:
            return len(self.heap)

    def _run(self):
        while not self.engine.stopping.is_set():
            with self.cv:
                if not self.heap:
                    self.cv.wait(0.5)
                    continue
                lead = self.rpc_rtt / 2 if self.engine.cfg["execution"].get("useChainQuotes", True) else 0.0
                wait = self.heap[0][0] - lead - time.time()
                if wait > 0:
                    self.cv.wait(min(wait, 0.5))
                    continue
                cutoff = time.time() + lead + 0.05
                batch = []
                while self.heap and self.heap[0][0] <= cutoff:
                    batch.append(heapq.heappop(self.heap)[2])
            try:
                results = self._fill(batch)
            except Exception as e:  # never lose orders: fail them so cash is released
                self.last_error = repr(e)
                results = [_result(False, time.time(), "error", err=repr(e)) for _ in batch]
            self.engine.apply_fills(batch, results)

    def _fill(self, orders):
        eng = self.engine
        ex = eng.cfg["execution"]
        results = [None] * len(orders)
        toks = [eng.market.get(o["addr"]) for o in orders]
        if ex.get("useChainQuotes", True) and eng.chain is not None:
            try:
                (self._solana_fill if V.PUMP else self._chain_fill)(orders, toks, results, ex)
            except Exception as e:
                self.last_error = f"chain quote failed, used {'API' if V.PUMP else 'model'}: {e!r}"
        if V.PUMP and ex.get("useChainQuotes", True) and None in results:
            try:  # Solana unreadable for some coins: a fresh API read is the next best thing
                self._api_fill(orders, results, ex)
            except Exception as e:
                self.last_error = f"fresh API read failed, used last poll: {e!r}"
        t = time.time()
        for i, o in enumerate(orders):
            if results[i] is None:
                d = toks[i].d if toks[i] else None
                results[i] = fill_with_venue(o, model_venue(d, ex) if d else None, d and d.get("quoteUsd"), ex, t)
        return results

    def _solana_fill(self, orders, toks, results, ex):
        """pump.fun: quote against the coin's bonding curve / PumpSwap pool as read from Solana right now."""
        ds = {o["addr"]: toks[i].d for i, o in enumerate(orders) if toks[i] is not None}
        t0 = time.time()
        states = self.engine.chain.states(list(ds.values()))
        t = self._read_time(t0)
        for i, o in enumerate(orders):
            d = ds.get(o["addr"])
            st = states.get(d["address"]) if d else None
            if st is None:  # graduated or unreadable: filled from a fresh API read instead
                continue
            v = Venue("curve", st["Q"], st["T"], st["sellable"], ex["protocolFeeBps"], d.get("creatorTaxBps") or 0,
                      src="chain")
            results[i] = fill_with_venue(o, v, d.get("quoteUsd"), ex, t)

    def _api_fill(self, orders, results, ex):
        """pump.fun: re-read the coins still unfilled from the API and fill on that curve / pool state."""
        todo = [i for i, r in enumerate(results) if r is None]
        t0 = time.time()
        fresh = self.engine.fetch_fresh(list(dict.fromkeys(orders[i]["addr"] for i in todo)))
        t = self._read_time(t0)
        for i in todo:
            o = orders[i]
            d = fresh.get(o["addr"])
            v = model_venue(d, ex) if d else None
            if v is not None:
                v.src = "api"
                results[i] = fill_with_venue(o, v, d.get("quoteUsd"), ex, t)

    def _read_time(self, t0):
        rtt = time.time() - t0
        self.rpc_rtt = 0.8 * self.rpc_rtt + 0.2 * min(rtt, 5.0)
        return t0 + rtt / 2

    def _chain_fill(self, orders, toks, results, ex):
        chain = self.engine.chain
        curve_idx, pool_idx = [], []
        for i, tok in enumerate(toks):
            if tok is None:
                continue
            (pool_idx if tok.d.get("stage") == "graduated" else curve_idx).append(i)
        if curve_idx:
            t0 = time.time()
            states = chain.curve_states([toks[i].d["curve"] for i in curve_idx if toks[i].d.get("curve")])
            t = self._read_time(t0)
            for i in curve_idx:
                d = toks[i].d
                st = states.get((d.get("curve") or "").lower())
                if st is None:
                    continue
                if st["graduated"]:
                    pool_idx.append(i)
                    continue
                qu = d.get("quoteUsd")
                if st["ready"]:
                    results[i] = _result(False, t, "chain", qu=qu, err="curve full, awaiting graduation")
                    continue
                qd = (d.get("quote") or {}).get("decimals", 18)
                td = d.get("decimals", 18)
                fee = st["fee_bps"] if st["fee_bps"] is not None else ex["protocolFeeBps"]
                v = Venue("curve", st["Q"] / 10 ** qd, st["T"] / 10 ** td, st["sellable"] / 10 ** td, fee,
                          d.get("creatorTaxBps") or 0, src="chain")
                results[i] = fill_with_venue(orders[i], v, qu, ex, t)
        if pool_idx:
            reqs, idx = [], []
            for i in pool_idx:
                d, o = toks[i].d, orders[i]
                qa = (d.get("quote") or {}).get("address")
                key = chain.pool_key(d["address"], d.get("factory"), qa)
                if key is None:
                    continue
                qd = (d.get("quote") or {}).get("decimals", 18)
                td = d.get("decimals", 18)
                qu = d.get("quoteUsd")
                if not qu:
                    continue
                if o["side"] == "buy":
                    amt = int((o["size_usd"] - ex["gasUsd"]) / qu * 10 ** qd)
                else:
                    amt = int(o["tokens"] * 10 ** td)
                reqs.append((key, d["address"], o["side"], amt))
                idx.append((i, qd, td, qu, amt))
            t0 = time.time()
            outs = chain.pool_quotes(reqs) if reqs else []
            t = self._read_time(t0)
            fee_frac = (ex["hookFeeBps"]) / 1e4
            for (i, qd, td, qu, amt), raw in zip(idx, outs):
                if raw is None:
                    continue
                o, d = orders[i], toks[i].d
                fee_frac_i = fee_frac + (d.get("creatorTaxBps") or 0) / 1e4
                if o["side"] == "buy":
                    out = raw / 10 ** td
                    used = amt / 10 ** qd
                    if out < o["expected_out"] * (1.0 - o["tol"]):
                        results[i] = _result(False, t, "chain", qu=qu, err=f"slippage: got {out / o['expected_out'] - 1:+.1%}")
                    else:
                        results[i] = _result(True, t, "chain", out=out, used=used, qu=qu, fee_frac=fee_frac_i)
                else:
                    out = raw / 10 ** qd
                    if o["expected_out"] > 0 and out < o["expected_out"] * (1.0 - o["tol"]):
                        results[i] = _result(False, t, "chain", qu=qu, err=f"slippage: got {out / o['expected_out'] - 1:+.1%}")
                    else:
                        results[i] = _result(True, t, "chain", out=out, qu=qu, fee_frac=fee_frac_i)


class ReplayExecutor:
    def __init__(self, engine):
        self.engine = engine
        self.heap = []
        self.seq = 0
        self.last_error = None

    def submit(self, order):
        self.seq += 1
        heapq.heappush(self.heap, (order["due"], self.seq, order))

    def pending(self):
        return len(self.heap)

    def process(self, now):
        batch = []
        while self.heap and self.heap[0][0] <= now:
            batch.append(heapq.heappop(self.heap)[2])
        if not batch:
            return
        ex = self.engine.cfg["execution"]
        results = []
        for o in batch:
            tok = self.engine.market.get(o["addr"])
            d = tok.d if tok else None
            results.append(fill_with_venue(o, model_venue(d, ex) if d else None, d and d.get("quoteUsd"), ex, now))
        self.engine.apply_fills(batch, results)
