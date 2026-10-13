"""Order execution with latency.

LiveExecutor: each order waits `latencyMs` after the signal, then is quoted against live on-chain state (curve
reserves or the v4 quoter) and checked against the slippage limit, like a real transaction with minOut. On
pump.fun the live state is the coin's bonding curve as it stood in the slot the order landed in, from the chain stream
(pumpstream.py): the stream runs a little behind the chain, so the order waits until the stream has caught up with
its landing moment and one more slot, then fills after the trades of earlier slots and a random number of the
trades in its own slot. Without the stream: the curve read from Solana, else a fresh pump.fun API read.
ReplayExecutor: for backtests on recorded data; fills at the first recorded snapshot at/after the due time.
"""
import heapq
import random
import threading
import time

from . import venue as V
from .pumpstream import SLOT_SEC
from .sim import Venue, model_venue

WAIT = object()  # result of an order that isn't filled yet: back in the queue until order["wake"]


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


def _fee(st, d, ex):
    """pump.fun curve fee in bps: what the coin's latest on-chain trade was charged (protocol + its creator fee), else
    the configured default."""
    for src in (st, d):
        if src is not None and src.get("feeBps") is not None:
            return src["feeBps"]
    return ex["protocolFeeBps"]


class LiveExecutor:
    def __init__(self, engine):
        self.engine = engine
        self.heap = []
        self.seq = 0
        self.cv = threading.Condition()
        self.thread = threading.Thread(target=self._run, name="executor", daemon=True)
        self.last_error = None
        self.rpc_rtt = 0.6  # EMA of the quote round trip; calls start rtt/2 early so state is read at the due time
        self.rng = random.Random()  # where in its landing slot an order lands
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
                # a chain read takes a round trip, so it starts half of one early; the stream's state needs no read
                stream = self.engine.stream
                lead = (self.rpc_rtt / 2 if self.engine.cfg["execution"].get("useChainQuotes", True)
                        and not (stream is not None and stream.healthy()) else 0.0)
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
            later = [o for o, r in zip(batch, results) if r is WAIT]
            if later:
                with self.cv:
                    for o in later:
                        self.seq += 1
                        heapq.heappush(self.heap, (o["wake"], self.seq, o))
            done = [(o, r) for o, r in zip(batch, results) if r is not WAIT]
            if done:
                self.engine.apply_fills([o for o, _ in done], [r for _, r in done])

    def _fill(self, orders):
        eng = self.engine
        ex = eng.ex()
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
        """pump.fun: quote against the coin's bonding curve as it stood when the order landed: from the chain stream
        when it has watched the coin, else read from Solana right now."""
        ds = {o["addr"]: toks[i].d for i, o in enumerate(orders) if toks[i] is not None}
        stream = self.engine.stream
        now = time.time()
        rest = {}
        for i, o in enumerate(orders):
            d = ds.get(o["addr"])
            if d is None:
                continue
            st = stream.curve(d["address"]) if stream is not None else None
            if st is not None and st.get("complete"):  # moving to PumpSwap: filled from a fresh API read
                continue
            if st is not None and (st.get("pool") or d.get("stage") != "graduated"):  # its curve or its PumpSwap pool
                results[i] = self._stream_fill(o, d, ex, now)
            if results[i] is None:
                rest[d["address"]] = d
        if not rest:
            return
        t0 = time.time()
        states = self.engine.chain.states(list(rest.values()))
        t = self._read_time(t0)
        for i, o in enumerate(orders):
            d = rest.get(o["addr"])
            st = states.get(d["address"]) if d else None
            if st is None:  # graduated or unreadable: filled from a fresh API read instead
                continue
            v = Venue("curve", st["Q"], st["T"], st["sellable"], _fee(None, d, ex), d.get("creatorTaxBps") or 0,
                      src="chain")
            results[i] = fill_with_venue(o, v, d.get("quoteUsd"), ex, t)

    def _stream_fill(self, o, d, ex, now):
        """Price an order on the curve in the slot it landed in -> result, WAIT, or None (no stream state: read the
        chain instead). Landing time is o["due"]; the stream shows that moment feed_lag later. At that point the
        newest slot it has delivered is the landing slot; one slot later, all of that slot's trades are in."""
        stream = self.engine.stream
        if "land_slot" not in o:
            land_seen = o["due"] + (stream.feed_lag or 0.0)
            if now < land_seen - 0.02:
                o["wake"] = land_seen
                return WAIT
            o["land_slot"], o["settle_by"] = stream.max_slot, now + 1.5
            o["wake"] = now + (stream.slot_sec or SLOT_SEC)
            return WAIT
        if stream.max_slot <= o["land_slot"] and now < o["settle_by"]:
            o["wake"] = now + 0.1
            return WAIT
        st = stream.landing_state(o["addr"], o["land_slot"], self.rng)
        if st is None:
            return None
        if st["pool"]:
            v = Venue("pool", st["Q"], st["T"], st["sellable"], st["feeBps"] if st["feeBps"] is not None else ex["hookFeeBps"],
                      d.get("creatorTaxBps") or 0, src="stream")
        else:
            v = Venue("curve", st["Q"], st["T"], st["sellable"], _fee(st, d, ex), d.get("creatorTaxBps") or 0, src="stream")
        r = fill_with_venue(o, v, d.get("quoteUsd"), ex, o["due"])
        r["slot"] = (o["land_slot"], st["before"], st["inSlot"])
        return r

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
        ex = self.engine.ex()
        results = []
        for o in batch:
            tok = self.engine.market.get(o["addr"])
            d = tok.d if tok else None
            results.append(fill_with_venue(o, model_venue(d, ex) if d else None, d and d.get("quoteUsd"), ex, now))
        self.engine.apply_fills(batch, results)
