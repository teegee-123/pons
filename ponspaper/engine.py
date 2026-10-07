"""Paper-trading engine: polls pons, runs every strategy against the same live feed, fills with latency and
fees, and evolves the auto strategies so the profitable filter/exit combinations rise to the top."""
import concurrent.futures
import copy
import gzip
import json
import os
import random
import threading
import time
from collections import deque
from datetime import datetime

from . import strategy as S
from .chain import Chain
from .edge import EdgeMap
from .execution import LiveExecutor, ReplayExecutor
from .market import Market, socials_count, spot_usd
from .net import Client, HttpError
from .sim import Portfolio, Position, liquidation_usd, model_venue
from .store import open_store

DEFAULT_CONFIG = {
    "apiBase": "https://ponsfamily.com",
    "rpcUrl": "https://ponsfamily.com/api/rpc",
    "poll": {"intervalSec": 2.0, "pages": 1, "sort": "active", "refreshCap": 6, "heldRefreshSec": 60.0},
    "execution": {
        "latencyMs": 750,          # signal -> transaction landing
        "gasUsd": 0.02,            # per transaction (reverted ones too)
        "buySlippagePct": 10.0,    # minOut tolerance vs the quote at signal time; worse fills revert
        "sellSlippagePct": 40.0,
        "protocolFeeBps": 100,     # curve feeBps (read on-chain: 100)
        "hookFeeBps": 100,         # graduated v4 hook fee (read on-chain: 100)
        "gradLiquidityMult": 1.0,  # scales the modelled v4 pool depth (model fills/marks only)
        "useChainQuotes": True,    # fill against live on-chain state; off = model fills from API data
    },
    "universe": {"quote": {"in": ["ETH"]}, "ageMin": {"min": None, "max": 1440}},
    "sizing": {"sizeUsd": 50.0, "maxOpen": 5, "bankrollUsd": 1000.0, "cooldownMin": 60.0},
    "evolution": {"enabled": True, "population": 40, "epochMin": 20, "minTrades": 6, "cullFrac": 0.3,
                  "mutateFrac": 0.7, "idleEpochs": 3, "shrinkK": 5},
    "edge": {"enabled": True, "sampleEverySec": 120, "horizonsMin": [1, 5, 15, 30], "sizeUsd": 50.0},
    "record": {"enabled": True},
}

SEEDS = [
    {"name": "Fresh momentum", "filters": {"ageMin": {"max": 10}, "chg1m": {"min": 5}, "tpm1": {"min": 3}, "mcapUsd": {"max": 12000}},
     "exits": {"tpPct": 40, "slPct": 20, "trailPct": 15, "trailArmPct": 20, "maxHoldMin": 15, "staleMin": 3}},
    {"name": "Mid-curve breakout", "filters": {"progressPct": {"min": 25, "max": 70}, "chg5m": {"min": 15}, "vol1m": {"min": 500}},
     "exits": {"tpPct": 30, "slPct": 15, "trailPct": None, "maxHoldMin": 20, "staleMin": 5}},
    {"name": "Dip bounce", "filters": {"ageMin": {"max": 120}, "ddPeak": {"min": 30}, "chg1m": {"min": 2}, "tradeCount": {"min": 100}},
     "exits": {"tpPct": 25, "slPct": 15, "trailPct": None, "maxHoldMin": 15, "staleMin": 5}},
    {"name": "Graduation run", "filters": {"progressPct": {"min": 75}, "stage": {"in": ["curve"]}, "chg5m": {"min": 0}},
     "exits": {"tpPct": 35, "slPct": 15, "trailPct": None, "maxHoldMin": 30, "staleMin": 5}},
]

TRADE_FEAT_COLS = ["ageMin", "mcapUsd", "progressPct", "chg1m", "chg5m", "tpm1", "vol1m", "ddPeak", "taxBps", "socials"]
FEAT_KEEP = ["ageMin", "mcapUsd", "progressPct", "chg1m", "chg5m", "chg15m", "tpm1", "vol1m", "ddPeak", "tradeCount",
             "idleSec", "taxBps", "socials", "stage"]


def deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "universe":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _iso(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


class Run:
    """A strategy spec plus its paper portfolio."""

    def __init__(self, spec, pf=None, last_entry=None):
        self.spec = spec
        self.pf = pf or Portfolio(spec["sizing"]["bankrollUsd"])
        self.last_entry = last_entry or {}
        self.pending_tokens = set()

    def holds(self, addr):
        return any(p.addr == addr for p in self.pf.positions.values())

    def score(self, k=5):
        """Expected net return per trade (%), shrunk toward 0 by k phantom zero-return trades; open positions
        count half."""
        s = self.pf.stats
        opens = list(self.pf.positions.values())
        tot = s["sum_ret"] + 0.5 * sum(p.mark_ret for p in opens)
        cnt = s["n"] + 0.5 * len(opens)
        return 100.0 * tot / (cnt + k) if cnt else 0.0

    def to_dict(self):
        return {"spec": self.spec, "pf": self.pf.to_dict(),
                "last_entry": {a: t for a, t in self.last_entry.items() if time.time() - t < 86400}}


class Engine:
    def __init__(self, data_dir, live=True, fresh=False, seed=None, cfg=None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.live = live
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.store = open_store(data_dir) if live else None
        if cfg is not None:
            self.cfg = deep_merge(DEFAULT_CONFIG, cfg)
        elif live and not fresh:
            self.cfg = deep_merge(DEFAULT_CONFIG, self.store.load_config() or {})
        self.market = Market()
        self.runs = {}
        self.fills = deque(maxlen=400)
        self.closed = deque(maxlen=400)
        self.hall = []
        self.retired = deque(maxlen=80)
        self.epoch = 0
        self.last_epoch = time.time()
        self.edge = EdgeMap(self.cfg["edge"], data_dir if live else None)
        self.status = {"startedAt": time.time(), "polls": 0, "pollErrors": 0, "lastPollAt": None, "lastPollMs": None,
                       "lastError": None, "gaps": 0, "refreshes": 0, "ethUsd": None, "lastEquitySample": 0}
        self.client = Client() if live else None
        self.chain = Chain(self.client, self.cfg["rpcUrl"], self.cfg["apiBase"]) if live else None
        self.executor = LiveExecutor(self) if live else ReplayExecutor(self)
        self._prev_newest_trade = None
        self._dirty = set()
        self._last_save = time.time()
        self._rec_buf = []
        self._rec_meta = set()
        self._rec_file = None
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4) if live else None
        if live and not fresh:
            self._load_state()
        if not self.runs:
            self._seed()
        if live:
            self._dirty |= self._held_addrs()

    # ------------------------------------------------------------------ setup / persistence
    def save_config(self):
        if not self.live:
            return
        try:
            self.store.save_config(self.cfg)
        except Exception as e:
            self.status["lastError"] = f"{_iso(time.time())} saving config: {e!r}"

    def _seed(self):
        for sd in SEEDS:
            spec = S.normalize(dict(sd, kind="manual"), self.cfg["sizing"])
            self.runs[spec["id"]] = Run(spec)
        self._fill_population()

    def _new_auto(self, parent=None):
        if parent is not None:
            body = S.mutate(parent.spec, self.rng)
            gen = parent.spec.get("gen", 0) + 1
            pid = parent.spec["id"]
        else:
            body = S.random_spec(self.rng)
            gen, pid = 0, None
        spec = S.normalize({"kind": "auto", "filters": body["filters"], "exits": body["exits"], "gen": gen, "parent": pid},
                           self.cfg["sizing"])
        spec["name"] = f"A-{spec['id'][2:7]}" + (f" g{gen}" if gen else "")
        return Run(spec)

    def _fill_population(self, parents=None):
        autos = [r for r in self.runs.values() if r.spec["kind"] == "auto"]
        need = int(self.cfg["evolution"]["population"]) - len(autos)
        for _ in range(max(0, need)):
            if parents and self.rng.random() < self.cfg["evolution"]["mutateFrac"]:
                run = self._new_auto(self.rng.choice(parents))
            else:
                run = self._new_auto()
            self.runs[run.spec["id"]] = run

    def save_state(self):
        if not self.live:
            return
        with self.lock:
            doc = {"version": 1, "savedAt": time.time(), "runs": [r.to_dict() for r in self.runs.values()],
                   "hall": self.hall, "retired": list(self.retired), "epoch": self.epoch, "lastEpoch": self.last_epoch,
                   "edge": self.edge.to_dict(), "fills": list(self.fills)[:200], "closed": list(self.closed)[:200]}
            text = json.dumps(doc, separators=(",", ":"), default=_json_default)
        self.store.save_state(text)
        self.store.flush()
        self._last_save = time.time()

    def _load_state(self):
        doc = self.store.load_state()
        if not doc:
            return
        for rd in doc.get("runs", []):
            spec = S.normalize(rd["spec"], self.cfg["sizing"])
            self.runs[spec["id"]] = Run(spec, Portfolio.from_dict(rd.get("pf", {})), rd.get("last_entry", {}))
        self.hall = doc.get("hall", [])
        self.retired = deque(doc.get("retired", []), maxlen=80)
        self.epoch = doc.get("epoch", 0)
        self.last_epoch = time.time()
        self.edge.load(doc.get("edge"))
        self.fills = deque(doc.get("fills", []), maxlen=400)
        self.closed = deque(doc.get("closed", []), maxlen=400)

    # ------------------------------------------------------------------ live loop
    def start(self):
        t = threading.Thread(target=self._loop, name="poller", daemon=True)
        t.start()
        return t

    def stop(self):
        self.stopping.set()
        self._flush_recording(force=True)
        self.save_state()

    def _loop(self):
        while not self.stopping.is_set():
            t0 = time.time()
            try:
                items, listed = self._poll()
                self.tick(items, time.time(), listed)
                self.status["polls"] += 1
                self.status["lastPollAt"] = time.time()
                self.status["lastPollMs"] = round((time.time() - t0) * 1000)
            except Exception as e:
                self.status["pollErrors"] += 1
                self.status["lastError"] = f"{_iso(time.time())} {e!r}"
            if time.time() - self._last_save > self.store.save_every:
                self.save_state()
            self._flush_recording()
            self.stopping.wait(max(0.2, float(self.cfg["poll"]["intervalSec"]) - (time.time() - t0)))

    def _poll(self):
        base = self.cfg["apiBase"].rstrip("/")
        pc = self.cfg["poll"]
        items, cursor = [], None
        for _ in range(max(1, int(pc["pages"]))):
            url = f"{base}/api/launches?sort={pc['sort']}" + (f"&cursor={cursor}" if cursor else "")
            page = self.client.get_json(url)
            items += page.get("items", [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        listed = {(d.get("address") or "").lower() for d in items}
        lts = [d.get("lastTradeAt") or 0 for d in items]
        with self.lock:
            now = time.time()
            held = self._held_addrs()
            # The list is ordered by last trade, so a token missing from it hasn't traded since we last saw it --
            # unless the oldest listed trade is newer than the newest one from the previous poll (a coverage gap).
            if lts and self._prev_newest_trade is not None and len(items) >= 40 * max(1, int(pc["pages"])):
                if min(lts) > self._prev_newest_trade:
                    self.status["gaps"] += 1
                    self._dirty |= held | self.edge.due_addrs(now, self.market)
            if lts:
                self._prev_newest_trade = max(lts)
            for a in held:
                tok = self.market.get(a)
                if a not in listed and (tok is None or now - tok.updated > pc["heldRefreshSec"]):
                    self._dirty.add(a)
            self._dirty |= {a for a in self.edge.due_addrs(now, self.market) if a not in listed}
            todo = [a for a in self._dirty if a not in listed][: int(pc["refreshCap"])]
        if todo:
            futs = {self._pool.submit(self.client.get_json, f"{base}/api/launches/{a}"): a for a in todo}
            for fut, a in futs.items():
                try:
                    d = fut.result(timeout=15)
                    if isinstance(d, dict) and d.get("address"):
                        items.append(d)
                    self._dirty.discard(a)
                    self.status["refreshes"] += 1
                except HttpError as e:
                    if e.status == 404:
                        self._dirty.discard(a)
                except Exception:
                    pass
        for a in listed:
            self._dirty.discard(a)
        return items, listed

    def _held_addrs(self):
        out = set()
        for r in self.runs.values():
            out.update(p.addr for p in r.pf.positions.values())
            out.update(r.pending_tokens)
        return out

    # ------------------------------------------------------------------ core tick (live + replay)
    def tick(self, items, now, listed=None):
        with self.lock:
            toks = self.market.ingest(items, now)
            self._record(items, now)
            if not self.live:
                self.executor.process(now)
            ex = self.cfg["execution"]
            for d in items:
                if (d.get("quote") or {}).get("symbol") == "ETH" and d.get("quoteUsd"):
                    self.status["ethUsd"] = d["quoteUsd"]
                    break
            feats = {t.addr: t.features(now) for t in toks}
            self._exits(now, ex)
            cands = [t for t in toks if listed is None or t.addr in listed]
            self._entries(cands, feats, now, ex)
            self.edge.resolve(now, self.market, ex)
            if now - self.status["lastEquitySample"] >= 60:
                self.status["lastEquitySample"] = now
                for r in self.runs.values():
                    r.pf.sample_equity(now)
            ev = self.cfg["evolution"]
            if ev["enabled"] and now - self.last_epoch >= ev["epochMin"] * 60:
                self.evolve(now)
            if self.status.get("lastPrune", 0) < now - 300:
                self.status["lastPrune"] = now
                self.market.prune(now, self._held_addrs())

    def _entries(self, cands, feats, now, ex):
        uni = self.cfg["universe"]
        runs = [r for r in self.runs.values() if r.spec.get("enabled", True)]
        for tok in cands:
            f = feats.get(tok.addr)
            if f is None or not S.passes(uni, f):
                continue
            self.edge.maybe_sample(tok, f, now, ex)
            for run in runs:
                sp, pf = run.spec, run.pf
                sz = sp["sizing"]
                if tok.addr in run.pending_tokens or run.holds(tok.addr):
                    continue
                le = run.last_entry.get(tok.addr)
                if le and now - le < (sz.get("cooldownMin") or 0) * 60:
                    continue
                if len(pf.positions) + len(pf.reserved) >= sz["maxOpen"] or pf.cash < sz["sizeUsd"]:
                    continue
                if not S.passes(sp["filters"], f):
                    continue
                self._submit_buy(run, tok, f, now, ex)

    def _submit_buy(self, run, tok, f, now, ex):
        size = float(run.spec["sizing"]["sizeUsd"])
        d = tok.d
        v, qu = model_venue(d, ex), d.get("quoteUsd")
        if v is None or not qu or size <= ex["gasUsd"]:
            return
        exp_tokens, _ = v.buy((size - ex["gasUsd"]) / qu)
        if exp_tokens <= 0:
            return
        oid = S.new_id("o")
        run.pf.cash -= size
        run.pf.reserved[oid] = size
        run.pending_tokens.add(tok.addr)
        run.last_entry[tok.addr] = now
        self.executor.submit({
            "id": oid, "sid": run.spec["id"], "side": "buy", "addr": tok.addr, "sym": d.get("symbol"),
            "t_signal": now, "due": now + ex["latencyMs"] / 1000.0, "size_usd": size, "tokens": None, "pos_id": None,
            "reason": "entry", "sig_spot_usd": spot_usd(d), "expected_out": exp_tokens,
            "tol": ex["buySlippagePct"] / 100.0, "stage": d.get("stage"),
            "feats": {k: (round(f[k], 4) if isinstance(f.get(k), float) else f.get(k)) for k in FEAT_KEEP},
        })

    def _submit_sell(self, run, pos, reason, now, ex):
        tok = self.market.get(pos.addr)
        d = tok.d if tok else {}
        v, qu = model_venue(d, ex) if d else None, d.get("quoteUsd")
        exp_q = v.sell(pos.tokens) if v else 0.0
        force = pos.sell_fails >= 3
        pos.exiting = True
        self.executor.submit({
            "id": S.new_id("o"), "sid": run.spec["id"], "side": "sell", "addr": pos.addr, "sym": pos.symbol,
            "t_signal": now, "due": now + ex["latencyMs"] / 1000.0, "size_usd": None, "tokens": pos.tokens,
            "pos_id": pos.id, "reason": reason, "sig_spot_usd": spot_usd(d) if d else None, "expected_out": exp_q,
            "tol": 1.0 if force else ex["sellSlippagePct"] / 100.0, "stage": d.get("stage"),
        })

    def _exits(self, now, ex):
        for run in self.runs.values():
            xs = run.spec["exits"]
            for pos in list(run.pf.positions.values()):
                tok = self.market.get(pos.addr)
                if tok is None:
                    continue
                d = tok.d
                pos.mark_usd = liquidation_usd(d, pos.tokens, ex)
                pos.mark_ret = pos.mark_usd / pos.cost_usd - 1.0 if pos.cost_usd else 0.0
                pos.peak_ret = max(pos.peak_ret, pos.mark_ret)
                if pos.exiting:
                    continue
                reason = self._exit_reason(xs, pos, d, now)
                if reason:
                    self._submit_sell(run, pos, reason, now, ex)

    @staticmethod
    def _exit_reason(xs, pos, d, now):
        r = pos.mark_ret
        if xs.get("tpPct") is not None and r >= xs["tpPct"] / 100.0:
            return "take profit"
        if xs.get("slPct") is not None and r <= -xs["slPct"] / 100.0:
            return "stop loss"
        if xs.get("trailPct") is not None and pos.peak_ret >= (xs.get("trailArmPct") or 0) / 100.0:
            if (1.0 + r) <= (1.0 + pos.peak_ret) * (1.0 - xs["trailPct"] / 100.0):
                return "trailing stop"
        if xs.get("maxHoldMin") is not None and now - pos.t_entry >= xs["maxHoldMin"] * 60:
            return "max hold"
        if xs.get("staleMin") is not None and now - (d.get("lastTradeAt") or now) >= xs["staleMin"] * 60:
            return "stale"
        return None

    # ------------------------------------------------------------------ fills
    def apply_fills(self, orders, results):
        with self.lock:
            ex = self.cfg["execution"]
            gas = ex["gasUsd"]
            for o, r in zip(orders, results):
                run = self.runs.get(o["sid"])
                lat = round((r["t_fill"] - o["t_signal"]) * 1000)
                ev = {"t": r["t_fill"], "sid": o["sid"], "name": run.spec["name"] if run else "(deleted)", "side": o["side"],
                      "sym": o.get("sym"), "addr": o["addr"], "ok": r["ok"], "src": r["src"], "latMs": lat,
                      "reason": o["reason"], "err": r.get("err"), "usd": None, "drift": None, "pnl": None}
                if run is None:
                    self.fills.appendleft(ev)
                    continue
                pf = run.pf
                if o["side"] == "buy":
                    run.pending_tokens.discard(o["addr"])
                    if o["id"] not in pf.reserved:  # portfolio was reset while the order was in flight
                        continue
                    size = pf.reserved.pop(o["id"])
                    pf.stats["gas"] += gas
                    if not r["ok"]:
                        pf.cash += size - gas
                        pf.stats["missed"] += 1
                        ev["usd"] = size
                        self.fills.appendleft(ev)
                        continue
                    qu = r["quote_usd"]
                    used_usd = r["used_quote"] * qu
                    pf.cash += max(0.0, size - gas - used_usd)
                    fees = used_usd * r["fee_frac"]
                    pf.stats["fees"] += fees
                    pf.stats["entries"] += 1
                    spot_fill = r["spot_quote"] * qu if r["spot_quote"] else None
                    drift = (spot_fill / o["sig_spot_usd"] - 1.0) if (spot_fill and o["sig_spot_usd"]) else None
                    tok = self.market.get(o["addr"])
                    pos = Position(id=o["id"], addr=o["addr"], symbol=o.get("sym"), t_entry=r["t_fill"], tokens=r["out"],
                                   cost_usd=used_usd + gas, entry_spot_usd=spot_fill or o["sig_spot_usd"], feats=o["feats"],
                                   stage=o.get("stage"), entry_drift=drift, fees_usd=fees, src=r["src"])
                    if tok:
                        pos.mark_usd = liquidation_usd(tok.d, pos.tokens, ex)
                        pos.mark_ret = pos.mark_usd / pos.cost_usd - 1.0
                    pf.positions[pos.id] = pos
                    ev.update(usd=used_usd + gas, drift=drift, px=(used_usd / r["out"]) if r["out"] else None)
                    self.fills.appendleft(ev)
                else:
                    pos = pf.positions.get(o["pos_id"])
                    if pos is None:
                        continue
                    pf.stats["gas"] += gas
                    if not r["ok"]:
                        pos.exiting = False
                        pos.sell_fails += 1
                        pf.cash -= gas
                        pf.stats["sell_fails"] += 1
                        self.fills.appendleft(ev)
                        continue
                    qu = r["quote_usd"]
                    out_usd = r["out"] * qu
                    fees = out_usd / max(1e-9, 1.0 - r["fee_frac"]) - out_usd
                    proceeds = out_usd - gas
                    pnl = proceeds - pos.cost_usd
                    spot_fill = r["spot_quote"] * qu if r["spot_quote"] else None
                    drift = (spot_fill / o["sig_spot_usd"] - 1.0) if (spot_fill and o["sig_spot_usd"]) else None
                    pf.cash += proceeds
                    pf.stats["fees"] += fees
                    del pf.positions[pos.id]
                    tr = {"sid": o["sid"], "name": run.spec["name"], "kind": run.spec["kind"], "sym": pos.symbol,
                          "addr": pos.addr, "tIn": pos.t_entry, "tOut": r["t_fill"], "hold": r["t_fill"] - pos.t_entry,
                          "cost": pos.cost_usd, "proceeds": proceeds, "pnl": pnl, "ret": pnl / pos.cost_usd if pos.cost_usd else 0.0,
                          "reason": o["reason"], "driftIn": pos.entry_drift, "driftOut": drift, "fees": pos.fees_usd + fees,
                          "src": r["src"], "feats": pos.feats, "peak": pos.peak_ret}
                    pf.record_close(tr)
                    self.closed.appendleft(tr)
                    self._append_trade_csv(tr)
                    ev.update(usd=proceeds, drift=drift, pnl=pnl, px=(out_usd / pos.tokens) if pos.tokens else None)
                    self.fills.appendleft(ev)

    def _append_trade_csv(self, tr):
        if not self.live:
            return
        f = tr.get("feats") or {}
        row = [_iso(tr["tOut"]), tr["sid"], tr["name"], tr["kind"], tr["sym"], tr["addr"], _iso(tr["tIn"]),
               round(tr["hold"] / 60, 2), round(tr["cost"], 4), round(tr["proceeds"], 4), round(tr["pnl"], 4),
               round(tr["ret"] * 100, 3), tr["reason"],
               round(tr["driftIn"] * 100, 3) if tr.get("driftIn") is not None else "", tr["src"]]
        row += [f.get(k, "") for k in TRADE_FEAT_COLS]
        self.store.append_trade(row)

    # ------------------------------------------------------------------ evolution
    def evolve(self, now=None):
        now = now or time.time()
        with self.lock:
            ev = self.cfg["evolution"]
            k = ev["shrinkK"]
            self.epoch += 1
            self.last_epoch = now
            autos = [r for r in self.runs.values() if r.spec["kind"] == "auto"]
            judged = sorted([r for r in autos if r.pf.stats["n"] >= ev["minTrades"]], key=lambda r: r.score(k))
            idle_after = ev["idleEpochs"] * ev["epochMin"] * 60
            idle = [r for r in autos if r.pf.stats["n"] == 0 and not r.pf.positions and not r.pf.reserved
                    and now - r.spec.get("created", now) >= idle_after]
            n_cull = int(len(judged) * ev["cullFrac"]) if len(judged) >= 4 else 0
            losers = [r for r in judged[:n_cull] if r.score(k) < 0 or len(judged) >= ev["population"] * 0.6]
            for r in judged:
                self._hall_update(r, k, now)
            parents = judged[-max(1, len(judged) // 4):] if judged else []
            parents = [p for p in parents if p.score(k) > 0] or parents
            for r, why in [(r, "underperformed") for r in losers] + [(r, "never traded") for r in idle]:
                if r.spec["id"] not in self.runs:
                    continue
                self.retired.appendleft({"id": r.spec["id"], "name": r.spec["name"], "desc": S.describe(r.spec),
                                         "gen": r.spec.get("gen", 0), "score": r.score(k), "summary": r.pf.summary(),
                                         "at": now, "why": why})
                del self.runs[r.spec["id"]]
            self._fill_population(parents)

    def _hall_update(self, r, k, now):
        entry = {"id": r.spec["id"], "name": r.spec["name"], "desc": S.describe(r.spec), "spec": r.spec,
                 "score": r.score(k), "summary": r.pf.summary(), "at": now}
        self.hall = [h for h in self.hall if h["id"] != r.spec["id"]] + [entry]
        self.hall.sort(key=lambda h: h["score"], reverse=True)
        self.hall = self.hall[:15]

    # ------------------------------------------------------------------ recording (for replay/backtests)
    def _record(self, items, now):
        if not self.live or not self.cfg["record"]["enabled"]:
            return
        rows = []
        for d in items:
            a = (d.get("address") or "").lower()
            if not a:
                continue
            if a not in self._rec_meta:
                self._rec_meta.add(a)
                self._rec_buf.append(json.dumps({"m": {k: d.get(k) for k in (
                    "address", "symbol", "name", "createdAt", "thresholdQuote", "creatorTaxBps", "buybackEnabled",
                    "curve", "factory", "decimals", "deployer")} | {"quote": d.get("quote"), "socials": d.get("socials")}},
                    separators=(",", ":")))
            rows.append([a, d.get("stage"), d.get("lastTradeAt"), d.get("raisedQuote"), d.get("priceQuote"),
                         d.get("priceUsd"), d.get("marketCapUsd"), d.get("volumeUsd"), d.get("tradeCount"),
                         d.get("progress"), d.get("quoteUsd"), d.get("graduatedAt")])
        if rows:
            self._rec_buf.append(json.dumps({"t": round(now, 2), "u": rows}, separators=(",", ":")))

    def _flush_recording(self, force=False):
        if not self._rec_buf:
            return
        if not force and len(self._rec_buf) < 30:
            return
        hour = datetime.now().strftime("%Y-%m-%d_%H")
        if hour != self._rec_file:
            self._rec_file = hour
            self._rec_meta = set()  # re-emit token metadata hourly so each hourly file stands alone
        buf, self._rec_buf = self._rec_buf, []
        self.store.append_snapshots(buf)

    # ------------------------------------------------------------------ views for the dashboard
    def _row(self, r, k):
        s = r.pf.summary()
        return {"id": r.spec["id"], "name": r.spec["name"], "kind": r.spec["kind"], "enabled": r.spec.get("enabled", True),
                "gen": r.spec.get("gen", 0), "desc": S.describe(r.spec), "score": r.score(k), **s,
                "curve": [p[1] for p in r.pf.equity_curve[-90:]]}

    def state_view(self):
        with self.lock:
            k = self.cfg["evolution"]["shrinkK"]
            rows = [self._row(r, k) for r in self.runs.values()]
            rows.sort(key=lambda x: x["score"], reverse=True)
            man = [x for x in rows if x["kind"] == "manual"]
            st = dict(self.status)
            st.update(tokens=len(self.market.tokens), pendingOrders=self.executor.pending(),
                      storage=self.store.kind if self.store else None,
                      storageError=getattr(self.store, "last_error", None),
                      executorError=self.executor.last_error,
                      chainCalls=self.chain.calls if self.chain else 0, chainFailures=self.chain.failures if self.chain else 0,
                      httpRequests=self.client.requests if self.client else 0, httpErrors=self.client.errors if self.client else 0)
            tot = lambda xs, key: sum(x[key] or 0 for x in xs)
            return {"status": st, "cfg": self.cfg, "rows": rows,
                    "totals": {"manualPnl": tot(man, "pnl"), "manualRealized": tot(man, "realized"),
                               "allPnl": tot(rows, "pnl"), "trades": int(tot(rows, "trades")),
                               "open": int(tot(rows, "open")), "strategies": len(rows)},
                    "evo": {"epoch": self.epoch, "nextEpochIn": max(0, self.cfg["evolution"]["epochMin"] * 60 - (time.time() - self.last_epoch)),
                            "hall": [{kk: h[kk] for kk in ("id", "name", "desc", "score", "summary", "at")} | {"alive": h["id"] in self.runs}
                                     for h in self.hall],
                            "retired": list(self.retired)[:30]},
                    "now": time.time()}

    def strategy_view(self, sid):
        with self.lock:
            r = self.runs.get(sid)
            if r is None:
                h = next((h for h in self.hall if h["id"] == sid), None)
                return {"spec": h["spec"], "dead": True, "summary": h["summary"]} if h else None
            k = self.cfg["evolution"]["shrinkK"]
            now = time.time()
            pos = [{"id": p.id, "sym": p.symbol, "addr": p.addr, "age": now - p.t_entry, "cost": p.cost_usd,
                    "value": p.mark_usd, "ret": p.mark_ret, "peak": p.peak_ret, "drift": p.entry_drift,
                    "exiting": p.exiting, "src": p.src, "feats": p.feats} for p in r.pf.positions.values()]
            return {"spec": r.spec, "row": self._row(r, k), "positions": pos, "trades": list(r.pf.trades)[:100],
                    "equity": r.pf.equity_curve, "cash": r.pf.cash}

    def market_view(self, limit=120):
        with self.lock:
            now = time.time()
            uni = self.cfg["universe"]
            held = {}
            for r in self.runs.values():
                for p in r.pf.positions.values():
                    held[p.addr] = held.get(p.addr, 0) + 1
            out = []
            for t in sorted(self.market.tokens.values(), key=lambda t: t.d.get("lastTradeAt") or 0, reverse=True)[:limit]:
                f = t.features(now)
                d = t.d
                out.append({"addr": t.addr, "sym": d.get("symbol"), "name": d.get("name"), "stage": d.get("stage"),
                            "quote": (d.get("quote") or {}).get("symbol"), "inUniverse": S.passes(uni, f),
                            "held": held.get(t.addr, 0), **{k: f.get(k) for k in FEAT_KEEP}, "spotUsd": f.get("spotUsd")})
            return {"tokens": out, "now": now}

    def trades_view(self):
        with self.lock:
            return {"fills": list(self.fills)[:250], "closed": list(self.closed)[:250]}

    def edge_view(self, h_idx):
        with self.lock:
            h_idx = max(0, min(len(self.cfg["edge"]["horizonsMin"]) - 1, h_idx))
            return self.edge.view(h_idx)

    def meta(self):
        return {"filters": {k: {"label": v[0], "unit": v[1], "kind": v[2], "help": v[3], "options": S.ENUM_OPTIONS.get(k)}
                            for k, v in S.FILTERS.items()},
                "exits": {k: {"label": v[0], "unit": v[1], "help": v[2]} for k, v in S.EXIT_FIELDS.items()},
                "sizing": {k: {"label": v[0], "unit": v[1], "help": v[2]} for k, v in S.SIZING_FIELDS.items()},
                "defaults": {"exits": S.DEFAULT_EXITS, "sizing": self.cfg["sizing"]}}

    # ------------------------------------------------------------------ mutations from the dashboard
    def update_settings(self, patch):
        with self.lock:
            allowed = {"poll", "execution", "universe", "sizing", "evolution", "edge", "record"}
            patch = {k: v for k, v in (patch or {}).items() if k in allowed}
            if "universe" in patch:
                patch["universe"] = S.clean_filters(patch["universe"])
            old_h = list(self.cfg["edge"]["horizonsMin"])
            self.cfg = deep_merge(self.cfg, patch)
            if self.cfg["edge"]["horizonsMin"] != old_h:
                self.edge.reset_aggs()
            self.edge.cfg = self.cfg["edge"]
            if "evolution" in patch:
                self._fill_population()
            self.save_config()
            return self.cfg

    def upsert_strategy(self, spec):
        with self.lock:
            sid = spec.get("id")
            if sid and sid in self.runs:
                run = self.runs[sid]
                merged = dict(run.spec)
                for key in ("name", "filters", "exits", "sizing", "enabled"):
                    if key in spec:
                        merged[key] = spec[key]
                run.spec = S.normalize(merged, self.cfg["sizing"])
                return run.spec
            spec = dict(spec)
            spec.pop("id", None)
            spec["kind"] = "manual"
            spec = S.normalize(spec, self.cfg["sizing"])
            self.runs[spec["id"]] = Run(spec)
            return spec

    def strategy_action(self, sid, action):
        with self.lock:
            run = self.runs.get(sid)
            if action == "revive":
                h = next((h for h in self.hall if h["id"] == sid), None)
                if not h:
                    return None
                spec = dict(h["spec"], kind="manual", name=h["name"] + " (revived)")
                spec.pop("id", None)
                spec = S.normalize(spec, self.cfg["sizing"])
                self.runs[spec["id"]] = Run(spec)
                return spec
            if run is None:
                return None
            if action == "promote":
                run.spec["kind"] = "manual"
                if run.spec["name"].startswith("A-"):
                    run.spec["name"] = "Promoted " + run.spec["name"][2:]
                self._fill_population()
            elif action == "pause":
                run.spec["enabled"] = False
            elif action == "resume":
                run.spec["enabled"] = True
            elif action == "clone":
                spec = copy.deepcopy(run.spec)
                spec.pop("id", None)
                spec.update(kind="manual", name=run.spec["name"] + " copy", enabled=True)
                spec = S.normalize(spec, self.cfg["sizing"])
                self.runs[spec["id"]] = Run(spec)
                return spec
            elif action == "reset":
                run.pf = Portfolio(run.spec["sizing"]["bankrollUsd"])
                run.last_entry = {}
                run.pending_tokens = set()
            elif action == "delete":
                del self.runs[sid]
                if run.spec["kind"] == "auto":
                    self._fill_population()
            return run.spec

    def reset_all(self, reseed=True):
        with self.lock:
            self.runs = {}
            self.hall, self.retired = [], deque(maxlen=80)
            self.fills.clear()
            self.closed.clear()
            self.edge.reset_aggs()
            self.edge.pending = []
            self.epoch = 0
            self.last_epoch = time.time()
            if reseed:
                self._seed()


def _json_default(o):
    if isinstance(o, set):
        return list(o)
    return str(o)


def snapshot_items(paths):
    """Yield (t, items) from recorded snapshot files (used by replay)."""
    meta = {}  # token metadata carries across files, so rows near an hour boundary aren't dropped
    for path in paths:
        with gzip.open(path, "rt", encoding="utf8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if "m" in rec:
                    m = rec["m"]
                    meta[(m.get("address") or "").lower()] = m
                    continue
                items = []
                for row in rec.get("u", []):
                    a = row[0]
                    m = meta.get(a)
                    if m is None:
                        continue
                    d = dict(m)
                    (d["stage"], d["lastTradeAt"], d["raisedQuote"], d["priceQuote"], d["priceUsd"], d["marketCapUsd"],
                     d["volumeUsd"], d["tradeCount"], d["progress"], d["quoteUsd"], d["graduatedAt"]) = row[1:12]
                    d["address"] = a
                    items.append(d)
                yield rec["t"], items
