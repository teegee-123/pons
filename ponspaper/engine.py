"""Paper-trading engine: polls the venue (pons or pump.fun), runs every strategy against the same live feed, fills with latency and
fees, and evolves the auto strategies so the profitable filter/exit combinations rise to the top."""
import concurrent.futures
import copy
import gzip
import json
import os
import random
import threading
import time
from collections import Counter, deque
from datetime import datetime

from . import pumpfun
from . import strategy as S
from . import venue as V
from .chain import Chain
from .solana import SolanaChain, rpc_url as solana_rpc_url
from .edge import EdgeMap
from .execution import LiveExecutor, ReplayExecutor
from .market import Market, socials_count, spot_usd
from .net import Client, HttpError
from .sim import Portfolio, Position, liquidation_usd, model_venue
from .store import open_store
from .ticks import TickFeed
from .lab import DEFAULT_LAB, Lab

CONFIG_VERSION = 4
HANDOFF_SEC = 300  # after start, how long to watch for the replaced instance's final save

DEFAULT_CONFIG = {
    "configVersion": CONFIG_VERSION,
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
    "evolution": {"enabled": True, "population": 40, "epochMin": 10, "minTrades": 6, "judgeAfterMin": 60,
                  "cullFrac": 0.3, "mutateFrac": 0.7, "idleEpochs": 3, "shrinkK": 5, "stuckMin": 45,
                  "retireClones": True,
                  # off on pons (its original behaviour); pump.fun turns them on (see pumpfun.CONFIG)
                  "scoreDropBest": False,  # score each strategy without its best closed trade
                  "parentMinTrades": 0,    # closed trades a live strategy needs before it can be bred from
                  "maxChildren": 0,        # live children one parent may have at once (0 = no limit)
                  "cloneOverlap": 1.0},    # retire a strategy sharing this share of its recent trades with an older one
    "lab": DEFAULT_LAB,
    "edge": {"enabled": True, "sampleEverySec": 120, "horizonsMin": [1, 5, 15, 30], "sizeUsd": 50.0},
    "record": {"enabled": True, "dedupeSec": 0},  # dedupeSec: skip a token's unchanged row for this long
    "ticks": {"enabled": True},
}

PONS_SEEDS = [
    {"name": "Fresh momentum", "filters": {"ageMin": {"max": 10}, "chg1m": {"min": 5}, "tpm1": {"min": 3}, "mcapUsd": {"max": 12000}},
     "exits": {"tpPct": 40, "slPct": 20, "trailPct": 15, "trailArmPct": 20, "maxHoldMin": 15, "staleMin": 3}},
    {"name": "Mid-curve breakout", "filters": {"progressPct": {"min": 25, "max": 70}, "chg5m": {"min": 15}, "vol1m": {"min": 500}},
     "exits": {"tpPct": 30, "slPct": 15, "trailPct": None, "maxHoldMin": 20, "staleMin": 5}},
    {"name": "Dip bounce", "filters": {"ageMin": {"max": 120}, "ddPeak": {"min": 30}, "chg1m": {"min": 2}, "tradeCount": {"min": 100}},
     "exits": {"tpPct": 25, "slPct": 15, "trailPct": None, "maxHoldMin": 15, "staleMin": 5}},
    {"name": "Graduation run", "filters": {"progressPct": {"min": 75}, "stage": {"in": ["curve"]}, "chg5m": {"min": 0}},
     "exits": {"tpPct": 35, "slPct": 15, "trailPct": None, "maxHoldMin": 30, "staleMin": 5}},
]
SEEDS = pumpfun.SEEDS if V.PUMP else PONS_SEEDS

TRADE_FEAT_COLS = V.TRADE_FEAT_COLS  # entry conditions saved with every closed trade
FEAT_KEEP = ["ageMin", "mcapUsd", "progressPct", "chg1m", "chg5m", "chg15m", "tpm1", "vol1m", "ddPeak", "tradeCount",
             "idleSec", "taxBps", "socials", "stage", "buyRatio1m", "netFlow1m", "buyers5m", "sellers5m", "whale1m",
             "volSpike", "devSoldUsd"]
if V.PUMP:
    FEAT_KEEP += ["inflow1m", "inflow5m", "fillRate", "athDdPct", "replies", "creatorCoins", "live", "mayhem", "depthUsd"]


def deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "universe":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


if V.PUMP:
    DEFAULT_CONFIG = deep_merge(DEFAULT_CONFIG, pumpfun.CONFIG)


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

    def score(self, k=5, drop_best=False):
        """Mark-to-market expected net return per trade (%): closed trades plus open positions valued at what
        selling now would return, shrunk toward 0 by k phantom zero-return trades. drop_best: leave the best closed
        trade out, so a single lucky trade can't make a strategy look like a winner."""
        s = self.pf.stats
        opens = list(self.pf.positions.values())
        tot = s["sum_ret"] + sum(p.mark_ret for p in opens)
        cnt = s["n"] + len(opens)
        if drop_best and s["n"] and s["best"] is not None:
            tot -= s["best"]
            cnt -= 1
        return 100.0 * tot / (cnt + k) if cnt else 0.0

    def mtm_pnl(self):
        return self.pf.stats["realized"] + self.pf.unrealized()

    def stuck_minutes(self, now):
        """Longest time any open position has been continuously under water."""
        return max(((now - p.under_since) / 60.0 for p in self.pf.positions.values() if p.under_since), default=0.0)

    def signature(self):
        """Recent entries (token, second). Strategies with the same signature are making the same trades."""
        ents = [(t["addr"], round(t["tIn"])) for t in list(self.pf.trades)[:10]]
        ents += [(p.addr, round(p.t_entry)) for p in self.pf.positions.values()]
        return tuple(sorted(ents)) if len(ents) >= 5 else None

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
            saved = self.store.load_config() or {}
            self.cfg = deep_merge(DEFAULT_CONFIG, saved)
            if saved and saved.get("configVersion", 1) < 2:  # v2: faster live epochs to go with stuck/clone retirement
                self.cfg["evolution"]["epochMin"] = DEFAULT_CONFIG["evolution"]["epochMin"]
            if saved and saved.get("configVersion", 1) < 3:  # v3: a champion needs far more validation trades
                self.cfg["lab"]["minValTrades"] = max(self.cfg["lab"].get("minValTrades", 0),
                                                      DEFAULT_CONFIG["lab"]["minValTrades"])
            if V.PUMP and saved and saved.get("configVersion", 1) < 4:  # v4: pump.fun skips coins it can't price
                for key in ("depthUsd", "mayhem"):
                    self.cfg["universe"].setdefault(key, copy.deepcopy(DEFAULT_CONFIG["universe"][key]))
            self.cfg["configVersion"] = CONFIG_VERSION
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
                       "lastError": None, "gaps": 0, "refreshes": 0, "quoteUsd": None, "quote": V.QUOTE, "lastEquitySample": 0,
                       "tickErrors": 0, "tickGaps": 0, "tickLastError": None, "tickCount": 0}
        self.client = Client() if live else None
        if live and V.PUMP:  # Solana reads for fills and open positions; no tick feed yet
            self.chain, self.tickfeed = SolanaChain(self.client, solana_rpc_url()), None
        else:  # Robinhood Chain: fill quotes and the tick feed
            self.chain = Chain(self.client, self.cfg["rpcUrl"], self.cfg["apiBase"]) if live else None
            self.tickfeed = TickFeed(self.client, self.cfg["rpcUrl"], self.cfg["apiBase"]) if live else None
        self.executor = LiveExecutor(self) if live else ReplayExecutor(self)
        self._prev_newest_trade = {}  # per list sorted by last trade: newest trade seen in the previous poll
        self._dirty = set()
        self._last_save = time.time()
        self._rec_buf = []
        self._rec_meta = set()
        self._rec_file = None
        self._rec_last = {}  # addr -> (last recorded row, time), for record.dedupeSec
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8 if V.PUMP else 4) if live else None
        self.lab = Lab(self.cfg, random.Random(seed)) if live else None
        self._lab_rebuild = True
        self._lab_reload = None
        self._lab_wake = threading.Event()  # lets a handoff interrupt the lab's sleeps
        # Deploy handoff: Render starts this instance and only stops the old one ~60 s later, when the old one saves
        # its final state. Watch for that newer state for a few minutes and continue from it.
        self._handoff_until = time.time() + HANDOFF_SEC if live and not fresh else 0.0
        self._handoff_checked = 0.0
        self._saved = False  # this instance has written its own state at least once
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

    def _score(self, r):
        ev = self.cfg["evolution"]
        return r.score(ev["shrinkK"], ev.get("scoreDropBest", False))

    def _fill_population(self, parents=None):
        ev = self.cfg["evolution"]
        autos = [r for r in self.runs.values() if r.spec["kind"] == "auto"]
        need = int(ev["population"]) - len(autos)
        cap = int(ev.get("maxChildren") or 0)
        kids = Counter(r.spec.get("parent") for r in autos)
        for _ in range(max(0, need)):
            # a parent with `cap` live children already makes room for other ideas
            open_parents = [p for p in parents or [] if not cap or kids[p.spec["id"]] < cap]
            if open_parents and self.rng.random() < ev["mutateFrac"]:
                parent = self.rng.choice(open_parents)
                kids[parent.spec["id"]] += 1
                run = self._new_auto(parent)
            else:
                run = self._new_auto()
            self.runs[run.spec["id"]] = run

    def save_state(self):
        if not self.live:
            return
        with self.lock:
            doc = {"version": 1, "savedAt": time.time(), "runs": [r.to_dict() for r in self.runs.values()],
                   "hall": self.hall, "retired": list(self.retired), "epoch": self.epoch, "lastEpoch": self.last_epoch,
                   "edge": self.edge.to_dict(), "fills": list(self.fills)[:200], "closed": list(self.closed)[:200],
                   "lab": self.lab.to_dict() if self.lab else None}
            text = json.dumps(doc, separators=(",", ":"), default=_json_default)
        self.store.save_state(text)
        self.store.flush()
        self._last_save = time.time()
        self._saved = True

    def _load_state(self, doc=None):
        doc = doc or self.store.load_state()
        if not doc:
            return
        self.runs = {}
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
        if self.lab is not None:
            try:
                self.lab.load(doc.get("lab"))
            except Exception as e:
                self.lab.last_error = f"could not restore lab state: {e!r}"

    def _check_handoff(self, now):
        """During the first minutes after start: if a newer state was saved (by the instance this one replaced, as
        it shut down), continue from it. This instance's own overlapping minute of trading is the same market and
        the same strategies the old one was still trading, so it is simply dropped."""
        if not self._handoff_until:
            return
        if now > self._handoff_until or self._saved:  # window over, or this instance's own state is the newest now
            self._handoff_until = 0.0
            return
        if now - self._handoff_checked < 15:
            return
        self._handoff_checked = now
        try:
            updated = self.store.state_updated()
            loaded = getattr(self.store, "loaded_at", None)
            if updated is None or (loaded is not None and updated <= loaded + 0.5):
                return
            doc = self.store.load_state()
        except Exception as e:
            self.status["lastError"] = f"{_iso(now)} handoff check failed: {e!r}"
            return
        if not doc:
            return
        with self.lock:
            lab_doc = doc.pop("lab", None)
            self._load_state(doc)
            self._lab_reload = lab_doc  # applied by the lab thread between steps
            self._lab_wake.set()
            self._dirty |= self._held_addrs()
        self.status["handoffs"] = self.status.get("handoffs", 0) + 1
        self.status["lastHandoffAt"] = now

    # ------------------------------------------------------------------ live loop
    def start(self):
        t = threading.Thread(target=self._loop, name="poller", daemon=True)
        t.start()
        if self.lab is not None:
            threading.Thread(target=self._lab_loop, name="lab", daemon=True).start()
        return t

    def _lab_loop(self):
        """Background GA on recorded data. Sleeps between evaluations so it uses about `duty` of one CPU and
        never starves the live poller."""
        lab = self.lab
        while not self.stopping.is_set():
            if self._lab_reload is not None:  # handoff: continue from the replaced instance's lab
                fresh_lab = Lab(self.cfg, random.Random())
                try:
                    fresh_lab.load(self._lab_reload)
                    self.lab = lab = fresh_lab
                    self._lab_rebuild = True
                except Exception as e:
                    lab.last_error = f"could not take over the previous instance's lab: {e!r}"
                self._lab_reload = None
            lc = self.cfg["lab"]
            if not lc.get("enabled", True):
                lab.phase = "off"
                self._lab_sleep(5)
                continue
            duty = min(1.0, max(0.05, float(lc.get("duty", 0.5))))

            def throttle(dt, duty=duty):
                time.sleep(dt * (1.0 - duty) / duty)
            try:
                stale = lab.ds is None or time.time() - lab.ds.built_at > lc["rebuildMin"] * 60
                if self._lab_rebuild or stale:
                    self._lab_rebuild = False
                    with self.lock:
                        lab.slip, lab.slip_n = self.live_slippage()
                    since = time.time() - lc["windowHours"] * 3600
                    lab.build(snapshot_records(self.store.snapshot_lines(since)), yield_every=(1.0 - duty) / duty * 0.25,
                              throttle=throttle)
                    lab.last_error = None
                    with self.lock:
                        winners = [r.spec for r in self.runs.values()
                                   if r.pf.stats["n"] >= self.cfg["evolution"]["minTrades"] and self._score(r) > 0]
                        seeds = [r.spec for r in self.runs.values()] + [h["spec"] for h in self.hall if h.get("spec")]
                    if not lab.pop:
                        lab.seed(seeds)
                    lab.inject(winners)  # live winners compete in the lab too
                if lab.gens_on_data >= lc.get("maxGensPerData", 50):
                    # more generations on the same data only overfit it: wait for the next refresh
                    lab.phase = "waiting for new data"
                    self._lab_sleep(10)
                    continue
                lab.phase = "evolving"
                lab.step(throttle)
            except ValueError as e:  # not enough recorded data yet
                lab.last_error = str(e)
                self._lab_rebuild = True
                self._lab_sleep(300)
            except Exception as e:
                lab.last_error = f"{_iso(time.time())} {e!r}"
                lab.phase = "error (retrying)"
                self._lab_rebuild = True
                self._lab_sleep(60)

    def live_slippage(self, min_fills=20):
        """-> (per-side cost as a fraction, fills used): how much worse live fills were than the spot each strategy
        saw at its signal, averaged over buys and sells (middle 80%, so a few wild fills don't dominate). Never
        negative: luckier-than-signal fills don't make the lab more optimistic."""
        def trimmed_mean(xs):
            xs = sorted(xs)
            cut = len(xs) // 10
            xs = xs[cut: len(xs) - cut] or xs
            return sum(xs) / len(xs) if xs else 0.0
        buys = [f["drift"] for f in self.fills if f["ok"] and f["side"] == "buy" and f.get("drift") is not None]
        sells = [f["drift"] for f in self.fills if f["ok"] and f["side"] == "sell" and f.get("drift") is not None]
        if len(buys) + len(sells) < min_fills:
            return 0.0, len(buys) + len(sells)
        per_side = (trimmed_mean(buys) if buys else 0.0) / 2 - (trimmed_mean(sells) if sells else 0.0) / 2
        return min(0.2, max(0.0, per_side)), len(buys) + len(sells)

    def _lab_sleep(self, sec):
        """Sleep in the lab thread; returns early on shutdown or when a handoff needs the lab."""
        self._lab_wake.wait(sec)
        self._lab_wake.clear()

    def stop(self):
        self.stopping.set()
        self._lab_wake.set()
        self._flush_recording(force=True)
        self.save_state()

    def _loop(self):
        while not self.stopping.is_set():
            t0 = time.time()
            try:
                items, listed, tick_res = self._poll()
                self.tick(items, time.time(), listed, live_ticks=tick_res)
                self.status["polls"] += 1
                self.status["lastPollAt"] = time.time()
                self.status["lastPollMs"] = round((time.time() - t0) * 1000)
            except Exception as e:
                self.status["pollErrors"] += 1
                self.status["lastError"] = f"{_iso(time.time())} {e!r}"
            self._check_handoff(time.time())
            if time.time() - self._last_save > self.store.save_every:
                self.save_state()
            self._flush_recording()
            self.stopping.wait(max(0.2, float(self.cfg["poll"]["intervalSec"]) - (time.time() - t0)))

    def _fetch_lists(self):
        """-> (items, cover, errors). cover: [(list, lastTradeAt times, full page)] for each list ordered by last
        trade; errors: lists that failed while others worked."""
        if V.PUMP:
            return pumpfun.fetch_lists(self.client, self.cfg, self._pool)
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
        lts = [d.get("lastTradeAt") or 0 for d in items]
        return items, [("active", lts, len(items) >= 40 * max(1, int(pc["pages"])))], []

    def _fetch_one(self, addr):
        if V.PUMP:
            return pumpfun.fetch_coin(self.client, self.cfg, addr)
        return self.client.get_json(f"{self.cfg['apiBase'].rstrip('/')}/api/launches/{addr}")

    def fetch_fresh(self, addrs, timeout=10):
        """Re-read tokens from the API right now (pump.fun fills) -> {addr: token dict}."""
        futs = {a: self._pool.submit(self._fetch_one, a) for a in addrs}
        out = {}
        for a, fut in futs.items():
            try:
                d = fut.result(timeout=timeout)
                if isinstance(d, dict) and d.get("address"):
                    out[a] = d
            except Exception:
                pass
        return out

    def _chain_marks(self):
        """pump.fun: the bonding curves of held coins and of coins the edge map is about to value, read from Solana
        -> {addr: (token dict, state)}, so exits and edge samples use live chain state (at most 2 RPC calls). Never
        raises: what Solana can't answer falls back to API refreshes."""
        with self.lock:
            held = self._held_addrs()
            want = list(held) + [a for a in self.edge.due_addrs(time.time(), self.market) if a not in held]
            ds = [tok.d for a in want if (tok := self.market.get(a)) is not None][:200]
        if not ds:
            return {}
        try:
            states = self.chain.states(ds)
        except Exception as e:
            self.status["lastError"] = f"{_iso(time.time())} Solana read failed: {e!r}"
            return {}
        return {V.addr_key(d["address"]): (d, states[d["address"]]) for d in ds if d["address"] in states}

    def _poll(self):
        pc = self.cfg["poll"]
        tick_fut = (self._pool.submit(self.tickfeed.fetch)
                    if self.tickfeed is not None and self.cfg["ticks"].get("enabled", True) else None)
        chain_fut = self._pool.submit(self._chain_marks) if V.PUMP and self.chain is not None else None
        items, cover, list_errors = self._fetch_lists()
        if list_errors:
            self.status["lastError"] = f"{_iso(time.time())} {'; '.join(list_errors)}"
        listed = {V.addr_key(d.get("address")) for d in items}
        marked = chain_fut.result() if chain_fut is not None else {}
        if marked:  # chain state on top of the newest API data for each held coin
            now = time.time()
            items = [pumpfun.apply_state(d, marked[a][1], now) if (a := V.addr_key(d.get("address"))) in marked else d
                     for d in items]
            items += [pumpfun.apply_state(d, st, now) for a, (d, st) in marked.items() if a not in listed]
        fresh = listed | set(marked)
        with self.lock:
            now = time.time()
            held = self._held_addrs()
            # A list ordered by last trade misses a token only if it hasn't traded since we last saw it -- unless
            # the oldest listed trade is newer than the newest one from the previous poll (a coverage gap).
            gap = False
            for key, lts, full in cover:
                prev = self._prev_newest_trade.get(key)
                if lts and prev is not None and full and min(lts) > prev:
                    gap = True
                if lts:
                    self._prev_newest_trade[key] = max(lts)
            if gap:
                self.status["gaps"] += 1
                self._dirty |= held | self.edge.due_addrs(now, self.market)
            for a in held:
                tok = self.market.get(a)
                if a not in fresh and (tok is None or now - tok.updated > pc["heldRefreshSec"]):
                    self._dirty.add(a)
            self._dirty |= {a for a in self.edge.due_addrs(now, self.market) if a not in fresh}

            def stale_first(a):  # held positions first, then whatever was updated longest ago
                tok = self.market.get(a)
                return (a not in held, tok.updated if tok else 0.0)
            todo = sorted((a for a in self._dirty if a not in fresh), key=stale_first)[: int(pc["refreshCap"])]
        if todo:
            futs = {self._pool.submit(self._fetch_one, a): a for a in todo}
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
        for a in fresh:
            self._dirty.discard(a)
        tick_res = None
        if tick_fut is not None:
            try:
                ticks, gap = tick_fut.result(timeout=20)
                tick_res = (ticks, gap, True)
            except Exception as e:
                self.status["tickErrors"] += 1
                self.status["tickLastError"] = f"{_iso(time.time())} {e!r}"
                tick_res = ([], True, False)
        return items, listed, tick_res

    def _held_addrs(self):
        out = set()
        for r in self.runs.values():
            out.update(p.addr for p in r.pf.positions.values())
            out.update(r.pending_tokens)
        return out

    # ------------------------------------------------------------------ core tick (live + replay)
    def tick(self, items, now, listed=None, live_ticks=None, tick_rows=None, tick_gap=False):
        """live_ticks: (decoded trades, gap, ok) from the chain feed; tick_rows: recorded trades (replay)."""
        with self.lock:
            toks = self.market.ingest(items, now)
            rec_ticks = None
            if live_ticks is not None:
                raw, gap, ok = live_ticks
                if gap or not ok:
                    self.status["tickGaps"] += 1
                    self.market.start_ticks(now)  # coverage restarts: windows aren't complete any more
                rec_ticks = self.market.add_ticks(raw, now) if ok else None
                self.status["tickCount"] += len(rec_ticks or [])
            elif tick_rows is not None:
                if tick_gap:
                    self.market.start_ticks(now)
                self.market.add_tick_rows(tick_rows, now)
            feats = {t.addr: t.features(now) for t in toks}
            rec_items = items
            if self.live and self.cfg["record"].get("universeOnly"):  # only what the lab and replays can use
                uni, held = self.cfg["universe"], self._held_addrs()
                rec_items = [d for d in items if (a := V.addr_key(d.get("address"))) in held
                             or (a in feats and S.passes(uni, feats[a]))]
            self._record(rec_items, now, rec_ticks, live_ticks is not None and (live_ticks[1] or not live_ticks[2]))
            if not self.live:
                self.executor.process(now)
            ex = self.cfg["execution"]
            for d in items:
                if (d.get("quote") or {}).get("symbol") == V.QUOTE and d.get("quoteUsd"):
                    self.status["quoteUsd"] = d["quoteUsd"]
                    break
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
                self.market.prune(now, self._held_addrs(), V.FORGET_IDLE_SEC)

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
                if pos.mark_ret < 0:
                    pos.under_since = pos.under_since or now
                else:
                    pos.under_since = None
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
        if xs.get("stuckMin") is not None and pos.under_since and now - pos.under_since >= xs["stuckMin"] * 60:
            return "time stop"
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
        """Retire auto strategies that are losing, stuck, cloned or idle; refill with lab champions, then
        mutations of the live winners, then random genomes."""
        now = now or time.time()
        with self.lock:
            ev = self.cfg["evolution"]
            self.epoch += 1
            self.last_epoch = now
            autos = [r for r in self.runs.values() if r.spec["kind"] == "auto"]
            age = lambda r: (now - r.spec.get("created", now)) / 60.0
            # judged once it has enough closed trades OR has been alive long enough with any exposure
            judged = [r for r in autos if r.pf.stats["n"] >= ev["minTrades"]
                      or (age(r) >= ev["judgeAfterMin"] and r.pf.stats["n"] + len(r.pf.positions) > 0)]
            judged.sort(key=self._score)
            retire = {}
            n_cull = int(len(judged) * ev["cullFrac"]) if len(judged) >= 4 else 0
            for r in judged[:n_cull]:
                if self._score(r) < 0:
                    retire[r.spec["id"]] = (r, "underperformed")
            for r in autos:
                if r.stuck_minutes(now) >= ev["stuckMin"] and r.mtm_pnl() < 0:
                    retire.setdefault(r.spec["id"], (r, f"stuck: a position under water for {r.stuck_minutes(now):.0f}m"))
            if ev.get("retireClones", True):
                # the same recent trades as an older strategy (or, with cloneOverlap below 1, mostly the same) make
                # it the same strategy, whatever its rules say
                thr = float(ev.get("cloneOverlap", 1.0))
                kept = []  # (recent trades, name) of the older strategies that stay
                for r in sorted(autos, key=lambda r: r.spec.get("created", 0)):
                    sig = r.signature()
                    if sig is None:
                        continue
                    sig = set(sig)
                    ov, twin = max(((len(o & sig) / len(o | sig), name) for o, name in kept), default=(0.0, None))
                    if ov >= thr:
                        retire.setdefault(r.spec["id"], (r, f"clone of {twin}" if ov >= 1.0 else
                                                         f"{ov:.0%} the same trades as {twin}"))
                    else:
                        kept.append((sig, r.spec["name"]))
            idle_after = ev["idleEpochs"] * ev["epochMin"] * 60
            for r in autos:
                if (r.pf.stats["n"] == 0 and not r.pf.positions and not r.pf.reserved
                        and now - r.spec.get("created", now) >= idle_after):
                    retire.setdefault(r.spec["id"], (r, "never traded"))
            # never wipe out more than half the population in one epoch
            victims = sorted(retire.values(), key=lambda x: self._score(x[0]))[: max(1, len(autos) // 2)]
            if self.lab is not None:
                judged_ids = {r.spec["id"] for r in judged}
                for r in autos:
                    if r.spec.get("origin") == "lab" and r.spec["id"] in judged_ids:
                        sc = self._score(r)
                        self.lab.record_live(r.spec, "winning" if sc > 0 else "losing", sc, r.pf.stats["n"], r.mtm_pnl())
            for r in judged:
                self._hall_update(r, now)
            # parents: the best quarter of the strategies with enough closed trades to tell skill from luck
            breeders = [r for r in judged if r.pf.stats["n"] >= int(ev.get("parentMinTrades") or 0)]
            parents = breeders[-max(1, len(breeders) // 4):] if breeders else []
            parents = [p for p in parents if self._score(p) > 0 and p.spec["id"] not in retire] or [
                p for p in parents if p.spec["id"] not in retire]
            for r, why in victims:
                if r.spec["id"] not in self.runs:
                    continue
                self._retire(r, why, now)
            self._inject_lab_champions()
            self._fill_population(parents)

    def _retire(self, r, why, now):
        if self.lab is not None and r.spec.get("origin") == "lab":
            self.lab.record_live(r.spec, "failed", self._score(r), r.pf.stats["n"], r.mtm_pnl(), why)
        self.retired.appendleft({"id": r.spec["id"], "name": r.spec["name"], "desc": S.describe(r.spec),
                                 "gen": r.spec.get("gen", 0), "score": self._score(r), "summary": r.pf.summary(),
                                 "at": now, "why": why})
        del self.runs[r.spec["id"]]

    def _inject_lab_champions(self, force_keys=None):
        """Add validated lab genomes to the live population, using free slots or replacing the weakest auto
        strategy when the population is full."""
        if self.lab is None:
            return []
        lc = self.cfg["lab"]
        if force_keys:
            picks = self.lab.pick(force_keys)
        else:
            picks = self.lab.champions(int(lc["promoteCount"]))
        added = []
        live_keys = {S.genome_key(S.genome(r.spec)) for r in self.runs.values()}
        for h in picks:
            if h["key"] in live_keys:
                self.lab.deployed.add(h["key"])
                continue
            autos = sorted((r for r in self.runs.values() if r.spec["kind"] == "auto"), key=self._score)
            if autos and len(autos) >= int(self.cfg["evolution"]["population"]):
                self._retire(autos[0], "replaced by a lab champion", time.time())
            spec = S.normalize({"kind": "auto", "filters": h["genome"]["filters"], "exits": h["genome"]["exits"],
                                "gen": h.get("gen", 0), "origin": "lab"}, self.cfg["sizing"])
            spec["name"] = f"LR-{spec['id'][2:7]}" if h.get("longrun") else f"L-{spec['id'][2:7]} g{h.get('gen', 0)}"
            self.runs[spec["id"]] = Run(spec)
            self.lab.deployed.add(h["key"])
            added.append(spec)
        return added

    def _hall_update(self, r, now):
        entry = {"id": r.spec["id"], "name": r.spec["name"], "desc": S.describe(r.spec), "spec": r.spec,
                 "score": self._score(r), "summary": r.pf.summary(), "at": now}
        self.hall = [h for h in self.hall if h["id"] != r.spec["id"]] + [entry]
        self.hall.sort(key=lambda h: h["score"], reverse=True)
        self.hall = self.hall[:15]

    # ------------------------------------------------------------------ recording (for replay/backtests)
    def _record(self, items, now, tick_rows=None, tick_gap=False):
        if not self.live or not self.cfg["record"]["enabled"]:
            return
        dedupe = float(self.cfg["record"].get("dedupeSec") or 0)
        rows = []
        for d in items:
            a = V.addr_key(d.get("address"))
            if not a:
                continue
            if a not in self._rec_meta:
                self._rec_meta.add(a)
                if V.PUMP:
                    m = pumpfun.record_meta(d)
                else:
                    m = {k: d.get(k) for k in (
                        "address", "symbol", "name", "createdAt", "thresholdQuote", "creatorTaxBps", "buybackEnabled",
                        "curve", "factory", "decimals", "deployer")} | {"quote": d.get("quote"), "socials": d.get("socials")}
                self._rec_buf.append(json.dumps({"m": m}, separators=(",", ":")))
            if V.PUMP:
                row = pumpfun.record_row(d)  # compact: curve reserves; the rest is derived on replay
            else:
                row = [a, d.get("stage"), d.get("lastTradeAt"), d.get("raisedQuote"), d.get("priceQuote"),
                       d.get("priceUsd"), d.get("marketCapUsd"), d.get("volumeUsd"), d.get("tradeCount"),
                       d.get("progress"), d.get("quoteUsd"), d.get("graduatedAt")]
            if dedupe:
                last = self._rec_last.get(a)
                if last and last[0] == row and now - last[1] < dedupe:
                    continue
                self._rec_last[a] = (row, now)
            rows.append(row)
        if dedupe and len(self._rec_last) > 5000:
            self._rec_last = {a: v for a, v in self._rec_last.items() if now - v[1] < dedupe}
        if rows or tick_rows is not None or tick_gap:
            rec = {"t": round(now, 2), "v" if V.PUMP else "u": rows}
            if tick_rows is not None:
                rec["x"] = tick_rows   # trades since the previous poll (present, possibly empty, while the feed works)
            if tick_gap:
                rec["xg"] = 1          # the feed missed something: rolling trade windows restart here
            self._rec_buf.append(json.dumps(rec, separators=(",", ":")))

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
    def _row(self, r):
        s = r.pf.summary()
        return {"id": r.spec["id"], "name": r.spec["name"], "kind": r.spec["kind"], "enabled": r.spec.get("enabled", True),
                "gen": r.spec.get("gen", 0), "desc": S.describe(r.spec), "score": self._score(r), **s,
                "curve": [p[1] for p in r.pf.equity_curve[-90:]]}

    def state_view(self):
        with self.lock:
            rows = [self._row(r) for r in self.runs.values()]
            rows.sort(key=lambda x: x["score"], reverse=True)
            man = [x for x in rows if x["kind"] == "manual"]
            st = dict(self.status)
            st.update(tokens=len(self.market.tokens), pendingOrders=self.executor.pending(),
                      storage=self.store.kind if self.store else None,
                      memMB=memory_mb()[0], memPeakMB=memory_mb()[1],
                      tickLagSec=(round(time.time() - self.tickfeed.last_block_ts, 1)
                                  if self.tickfeed and self.tickfeed.last_block_ts else None),
                      storageError=getattr(self.store, "last_error", None),
                      executorError=self.executor.last_error,
                      chainCalls=self.chain.calls if self.chain else 0, chainFailures=self.chain.failures if self.chain else 0,
                      httpRequests=self.client.requests if self.client else 0, httpErrors=self.client.errors if self.client else 0,
                      httpErrorKinds=dict(sorted(self.client.error_kinds.items(), key=lambda kv: -kv[1])[:6]) if self.client else {})
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
            now = time.time()
            pos = [{"id": p.id, "sym": p.symbol, "addr": p.addr, "age": now - p.t_entry, "cost": p.cost_usd,
                    "value": p.mark_usd, "ret": p.mark_ret, "peak": p.peak_ret, "drift": p.entry_drift,
                    "exiting": p.exiting, "src": p.src, "feats": p.feats} for p in r.pf.positions.values()]
            return {"spec": r.spec, "row": self._row(r), "positions": pos, "trades": list(r.pf.trades)[:100],
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
        return {"venue": {"id": V.NAME, "label": V.LABEL, "quote": V.QUOTE, "tokenUrl": V.TOKEN_URL,
                          "unavailable": sorted(V.UNAVAILABLE)},
                "filters": {k: {"label": v[0], "unit": v[1], "kind": v[2], "help": v[3], "options": S.ENUM_OPTIONS.get(k)}
                            for k, v in S.FILTERS.items()},
                "exits": {k: {"label": v[0], "unit": v[1], "help": v[2]} for k, v in S.EXIT_FIELDS.items()},
                "sizing": {k: {"label": v[0], "unit": v[1], "help": v[2]} for k, v in S.SIZING_FIELDS.items()},
                "defaults": {"exits": S.DEFAULT_EXITS, "sizing": self.cfg["sizing"]}}

    # ------------------------------------------------------------------ mutations from the dashboard
    def update_settings(self, patch):
        with self.lock:
            allowed = {"poll", "execution", "universe", "sizing", "evolution", "edge", "record", "lab"}
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
            if self.lab is not None:
                self.lab.cfg = self.cfg
                lab_data_keys = {"windowHours", "sampleEverySec", "validateFrac"}
                if {"universe", "execution", "sizing"} & set(patch) or lab_data_keys & set(patch.get("lab") or {}):
                    self._lab_rebuild = True
            self.save_config()
            return self.cfg

    def storage_view(self):
        """Database card: what the recordings and stored trades take (nothing is ever deleted automatically)."""
        u = self.store.usage()
        u["labWindowHours"] = self.cfg["lab"]["windowHours"]
        return u

    def storage_clean(self, body):
        """Dashboard button: delete recordings and/or stored trades older than the given number of days (empty = keep
        all). The recordings the lab trains on (its data window) are always kept. Strategy stats, the hall of fame and
        the lab's long-run record live in the saved state and aren't touched."""
        def days(key, floor):
            v = body.get(key)
            return None if v is None or v == "" else max(floor, float(v))
        rec = days("recordingsDays", self.cfg["lab"]["windowHours"] / 24.0)
        trades = days("tradesDays", 0.0)
        before = self.store.usage()["databaseBytes"]
        out = self.store.clean(rec, trades) if rec is not None or trades is not None else {"deleted": {}, "compacted": True}
        out.update(recordingsDays=rec, tradesDays=trades, bytesBefore=before, bytesAfter=self.store.usage()["databaseBytes"])
        return out

    def lab_view(self):
        if self.lab is None:
            return {"phase": "off"}
        with self.lock:
            v = self.lab.view()
        v["cfg"] = self.cfg["lab"]
        return v

    def lab_deploy(self, key):
        with self.lock:
            return self._inject_lab_champions(force_keys={key})

    def lab_rebuild(self):
        self._lab_rebuild = True

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
                key = S.genome_key(S.genome(h["spec"]))
                for r in self.runs.values():
                    if S.genome_key(S.genome(r.spec)) == key:
                        return r.spec
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


def memory_mb():
    """(current, peak) resident memory in MB on Linux hosts like Render; (None, None) elsewhere."""
    try:
        with open("/proc/self/status") as fh:
            vals = {line.split(":")[0]: line.split()[1] for line in fh if line.startswith(("VmRSS", "VmHWM"))}
        return round(int(vals["VmRSS"]) / 1024), round(int(vals["VmHWM"]) / 1024)
    except (OSError, KeyError, ValueError):
        return None, None


def _json_default(o):
    if isinstance(o, set):
        return list(o)
    return str(o)


def snapshot_items(paths):
    """Yield (t, items) from recorded snapshot files (used by replay)."""
    def lines():
        for path in paths:
            with gzip.open(path, "rt", encoding="utf8") as fh:
                yield from fh
    yield from snapshot_records(lines())


def snapshot_records(lines):
    """Yield (t, items, ticks, tick_gap) from recorded snapshot lines. ticks is None when the trade feed wasn't
    running for that poll. Token metadata carries across files, so rows near an hour boundary aren't dropped.
    Rows are pons rows ("u") or compact pump.fun rows ("v")."""
    meta = {}
    last_t = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "t" in rec:  # during a deploy two instances record the same minute: drop polls that go back in time
            if last_t is not None and rec["t"] <= last_t:
                continue
            last_t = rec["t"]
        if "m" in rec:
            m = rec["m"]
            meta[V.addr_key(m.get("address"))] = m
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
        for row in rec.get("v", []):
            m = meta.get(row[0])
            if m is not None:
                items.append(pumpfun.expand_row(row, m))
        yield rec["t"], items, rec.get("x"), bool(rec.get("xg"))
