"""Token state + rolling history, and the features strategies filter on."""
import bisect
import heapq
import math
from collections import deque

from . import venue as V
from .pumpfun import STD_CURVE_K
from .pumpstream import WALLET_CHARS

TOKEN_SUPPLY = 1e9
CURVE_V0_RATIO = 0.4   # virtual quote reserve at launch = 0.4 x graduation threshold (on-chain: 1.68 ETH vs 4.2 ETH)
HIST_SEC = 1800        # keep 30 minutes of samples per token
SAMPLE_EVERY = 15      # add a sample at least this often even if nothing changed
TICK_SEC = 900         # keep 15 minutes of individual trades per token
TICK_KEYS = ("buyRatio1m", "netFlow1m", "buyers5m", "sellers5m", "whale1m", "volSpike", "devSoldUsd")
LAUNCH_SEC = 3.0       # pump.fun: buys this soon after launch count as sniper / bundled launch buys
ORPHAN_SEC = 120       # pump.fun: trades of coins the market doesn't know yet are kept this long
BORN_SEC = 10          # pump.fun: a coin first seen this soon after launch has been watched since launch


def curve_params(threshold):
    v0 = CURVE_V0_RATIO * threshold
    return v0, v0 * TOKEN_SUPPLY


def socials_count(d):
    s = d.get("socials") or {}
    return sum(1 for v in s.values() if v)


def launch_price_quote(d):
    """Price at launch, in quote units: given by the venue (pump.fun) or derived from the pons curve threshold."""
    if d.get("launchPriceQuote"):
        return d["launchPriceQuote"]
    thr = d.get("thresholdQuote") or 0
    return curve_params(thr)[0] / TOKEN_SUPPLY if thr > 0 else None


def spot_quote(d):
    """Current marginal price in quote units. For curve tokens it is derived from raisedQuote (the API's
    priceQuote is the last trade's average fill, not the spot)."""
    p = d.get("priceQuote")
    if d.get("stage") == "graduated":
        return p
    thr = d.get("thresholdQuote") or 0
    raised = d.get("raisedQuote")
    if thr > 0 and raised is not None and raised >= 0:
        v0, k = curve_params(thr)
        q = v0 + raised
        s = q * q / k
        if not p or 0.75 < s / p < 1.33:
            return s
    return p


def spot_usd(d):
    s = spot_quote(d)
    qu = d.get("quoteUsd")
    if s is None or not qu:
        return d.get("priceUsd")
    return s * qu


class Token:
    __slots__ = ("addr", "d", "first_seen", "updated", "hist", "_t", "_peak", "_peak_t",
                 "ticks", "tick_since", "dev_sold", "dev", "peers", "holders", "launch_buys", "born")

    def __init__(self, addr, d, now):
        self.addr = addr
        self.d = d
        self.first_seen = now
        self.updated = now
        self.hist = []   # (t, spot_usd, volume_usd, trade_count, raised_quote)
        self._t = []     # parallel list of t for bisect
        self._peak, self._peak_t = 0.0, 0.0  # running max of spot over the history window
        self.ticks = deque()                   # (t, side, usd, wallet)
        self.tick_since = math.inf             # trades are known completely from this time on
        self.dev_sold = 0.0                    # USD the creator has sold (since we started watching)
        self.dev = (d.get("deployer") or "").lower()[2:18]
        self.peers = None                      # pump.fun: {token: createdAt} of every launch by the same creator
        self.holders = None                    # pump.fun, coins watched since launch: wallet -> tokens held
        self.launch_buys = 0.0                 # pump.fun: tokens others bought in the launch's first LAUNCH_SEC
        self.born = d.get("createdAt") or now  # launch time as first seen (the launch window never moves)
        if V.PUMP:  # base58 wallets are case-sensitive; trades carry their first WALLET_CHARS characters
            self.dev = (d.get("deployer") or "")[:WALLET_CHARS]
        created = d.get("createdAt") or now
        launch = launch_price_quote(d)
        if d.get("stage") != "graduated" and launch and now - created < HIST_SEC and d.get("quoteUsd"):
            self._push(created, launch * d["quoteUsd"], 0.0, 0, 0.0)
        self._sample(now)

    def _push(self, t, px, vol, trades, raised=None):
        if self._t and t < self._t[-1]:
            return
        self.hist.append((t, px, vol, trades, raised))
        self._t.append(t)
        if px >= self._peak:
            self._peak, self._peak_t = px, t

    def _sample(self, now):
        d = self.d
        px = spot_usd(d)
        if px is None:
            return
        self._push(now, px, d.get("volumeUsd") or 0.0, d.get("tradeCount") or 0, d.get("raisedQuote"))

    def update(self, d, now):
        old = self.d
        if old.get("socials") and set(old["socials"]) - set(d.get("socials") or {}):
            d["socials"] = {**old["socials"], **(d.get("socials") or {})}  # a partial payload keeps known links
        if d.get("mayhem") is None and old.get("mayhem") is not None:
            d["mayhem"] = old["mayhem"]
        if d.get("feeBps") is None and old.get("feeBps") is not None:  # learnt from on-chain trades, not the API
            d["feeBps"] = old["feeBps"]
        if V.PUMP and d.get("stage") == "graduated" and old.get("stage") != "graduated":
            self.end_watch()  # PumpSwap trades aren't streamed: per-trade signals stop here
        changed = (d.get("lastTradeAt") != old.get("lastTradeAt") or d.get("raisedQuote") != old.get("raisedQuote")
                   or d.get("priceQuote") != old.get("priceQuote") or d.get("stage") != old.get("stage"))
        self.d = d
        self.updated = now
        if changed or not self._t or now - self._t[-1] >= SAMPLE_EVERY:
            self._sample(now)
        cut = now - HIST_SEC
        if self._t and self._t[0] < cut:
            i = bisect.bisect_left(self._t, cut)
            i = max(0, i - 1)  # keep one sample before the cutoff as a baseline
            if i:
                del self.hist[:i]
                del self._t[:i]
                if self._peak_t < self._t[0]:  # the peak aged out: rescan (rare)
                    j = max(range(len(self.hist)), key=lambda k: self.hist[k][1])
                    self._peak, self._peak_t = self.hist[j][1], self.hist[j][0]
        return changed

    def start_watch(self, since, now):
        """pump.fun: the trade stream covers this coin from `since` on. A coin first seen within BORN_SEC of its
        launch is watched since launch, so its holders are known exactly; other coins' trade windows count from now."""
        if self.d.get("stage") == "graduated":
            self.end_watch()
            return
        born = self.born >= since - 5 and now - self.born <= BORN_SEC
        self.tick_since = max(since, self.born) if born else max(since, now)
        self.holders = {} if born else None
        self.launch_buys = 0.0

    def end_watch(self):
        self.tick_since = math.inf
        self.holders = None

    def add_tick(self, t, side, usd, wallet, is_dev=False, tokens=None):
        self.ticks.append((t, side, usd, wallet))
        if is_dev and side < 0:
            self.dev_sold += usd
        if tokens is not None and self.holders is not None and wallet:
            h = self.holders.get(wallet, 0.0) + side * tokens
            if h > 0.5:
                self.holders[wallet] = h
            else:  # sold out (or sold tokens it got some other way)
                self.holders.pop(wallet, None)
            if side > 0 and not is_dev and t - self.born <= LAUNCH_SEC:
                self.launch_buys += tokens
        cut = t - TICK_SEC
        while self.ticks and self.ticks[0][0] < cut:
            self.ticks.popleft()

    def tick_features(self, now):
        """Signals from individual trades. A window only reports once trades have been watched for its whole
        length (or since the token was created), so a value is never a partial guess."""
        out = dict.fromkeys(TICK_KEYS)
        known = self.tick_since
        if known == math.inf:
            return out
        created = self.d.get("createdAt") or 0
        born_watched = created >= known - 5

        def valid(w):
            return born_watched or now - w >= known

        b60 = s60 = whale = vol60 = vol900 = 0.0
        n60 = 0
        buyers, sellers = set(), set()
        for t, side, usd, wallet in reversed(self.ticks):
            age = now - t
            if age > TICK_SEC:
                break
            vol900 += usd
            if age <= 60:
                vol60 += usd
                n60 += 1
                if side > 0:
                    b60 += usd
                    whale = max(whale, usd)
                else:
                    s60 += usd
            if age <= 300 and wallet:
                (buyers if side > 0 else sellers).add(wallet)
        if valid(60):
            out["buyRatio1m"] = 100.0 * b60 / (b60 + s60) if b60 + s60 > 0 else None
            out["netFlow1m"] = b60 - s60
            out["whale1m"] = whale
            if V.PUMP:  # the API reports no volume or trade count: count the streamed trades instead
                out["tpm1"], out["vol1m"] = float(n60), vol60
        if valid(300):
            out["buyers5m"] = len(buyers)
            out["sellers5m"] = len(sellers)
        span = min(TICK_SEC, now - max(known, created))
        if span >= 120 and vol900 > 0:
            out["volSpike"] = vol60 / (vol900 / (span / 60.0))
        out["devSoldUsd"] = self.dev_sold
        return out

    def at(self, t):
        """Last sample at or before t, or None if history doesn't reach back that far."""
        i = bisect.bisect_right(self._t, t)
        return self.hist[i - 1] if i else None

    def features(self, now):
        d = self.d
        px = spot_usd(d)
        f = {
            "ageMin": (now - (d.get("createdAt") or now)) / 60.0,
            "mcapUsd": d.get("marketCapUsd"),
            "progressPct": (d["progress"] * 100.0) if d.get("progress") is not None else (100.0 if d.get("stage") == "graduated" else None),
            "volumeUsd": d.get("volumeUsd"),
            "tradeCount": d.get("tradeCount"),
            "idleSec": max(0.0, now - (d.get("lastTradeAt") or now)),
            "taxBps": d.get("creatorTaxBps") or 0,
            "socials": socials_count(d),
            "stage": d.get("stage"),
            "quote": V.QUOTE if (d.get("quote") or {}).get("symbol") == V.QUOTE else "OTHER",
            "buyback": "yes" if d.get("buybackEnabled") else "no",
            "spotUsd": px,
        }
        if px and f["mcapUsd"] is None:
            f["mcapUsd"] = px * TOKEN_SUPPLY
        vol, trades = d.get("volumeUsd"), d.get("tradeCount")
        flow = vol is not None or trades is not None  # pump.fun reports neither: leave the rates empty
        vol, trades = vol or 0.0, trades or 0
        for key, win in (("chg1m", 60), ("chg5m", 300), ("chg15m", 900)):
            h = self.at(now - win)
            f[key] = (px / h[1] - 1.0) * 100.0 if (h and px and h[1]) else None
        h = self.at(now - 60)
        if h and flow:
            span = max(1.0, now - h[0]) / 60.0
            f["tpm1"] = (trades - h[3]) / span
            f["vol1m"] = (vol - h[2]) / span
        else:
            f["tpm1"] = f["vol1m"] = None
        if self.hist and px:
            peak = max(self._peak, px)
            f["ddPeak"] = max(0.0, (1.0 - px / peak) * 100.0) if peak > 0 else None
        else:
            f["ddPeak"] = None
        f.update(self.tick_features(now))
        if V.PUMP:
            f.update(self.pump_features(now, f))
        return f

    def pump_features(self, now, f):
        """Signals built from pump.fun data (venue.PUMP_ONLY)."""
        d = self.d
        out = {}
        curve = d.get("stage") != "graduated"
        raised, qu = d.get("raisedQuote"), d.get("quoteUsd")
        for key, win in (("inflow1m", 60), ("inflow5m", 300)):  # net buys: growth of the curve's real reserves
            h = self.at(now - win)
            out[key] = (raised - h[4]) * qu if curve and h and raised is not None and h[4] is not None and qu else None
        prog, age = f.get("progressPct"), f.get("ageMin")
        out["fillRate"] = prog / max(0.25, age) if curve and prog is not None and age is not None else None
        ath, mc = d.get("athMcapUsd"), f.get("mcapUsd")
        out["athDdPct"] = max(0.0, (1.0 - mc / ath) * 100.0) if ath and mc else None
        out["replies"] = d.get("replies")
        out["live"] = None if d.get("isLive") is None else ("yes" if d["isLive"] else "no")
        out["mayhem"] = None if d.get("mayhem") is None else ("yes" if d["mayhem"] else "no")
        out["creatorCoins"] = sum(1 for c in self.peers.values() if c >= now - 86400) if self.peers is not None else None
        res, pool_k, p = d.get("reserves"), d.get("poolK"), d.get("priceQuote")
        if curve and res and res[0] and qu:
            out["depthUsd"] = res[0] * qu
        elif not curve and pool_k and p and qu:  # the modelled pool's quote reserve
            out["depthUsd"] = math.sqrt(pool_k * p) * qu
        else:
            out["depthUsd"] = None
        if curve and res and res[0] and res[1]:
            out["stdCurve"] = "yes" if abs(res[0] * res[1] / STD_CURVE_K - 1.0) < 0.005 else "no"
        else:
            out["stdCurve"] = None if curve else "yes"  # graduated: priced on the modelled pool
        if self.holders is not None:  # watched since launch: every wallet's balance is known
            sup = d.get("supply") or TOKEN_SUPPLY
            out["holders"] = len(self.holders)
            out["devHoldPct"] = 100.0 * self.holders.get(self.dev, 0.0) / sup if self.dev else None
            out["top10Pct"] = 100.0 * sum(heapq.nlargest(10, self.holders.values())) / sup
            out["launchBuyPct"] = 100.0 * self.launch_buys / sup
        else:
            out["holders"] = out["devHoldPct"] = out["top10Pct"] = out["launchBuyPct"] = None
        return out


class Market:
    def __init__(self):
        self.tokens = {}
        self.by_curve = {}      # curve contract -> token address
        self.tick_start = None  # when the trade feed started (or last restarted after a gap)
        self._pending = deque()  # trades for curves we haven't seen in the launch list yet
        self.creators = {}       # pump.fun: creator wallet -> {token: createdAt}
        self._orphans = {}       # pump.fun: mint -> streamed trades of a coin not known yet (kept ORPHAN_SEC)
        self._orphans_pruned = 0.0

    def ingest(self, items, now):
        seen = []
        for d in items:
            addr = V.addr_key(d.get("address"))
            if not addr:
                continue
            t = self.tokens.get(addr)
            if t is None:
                t = self.tokens[addr] = Token(addr, d, now)
                if self.tick_start is not None:
                    t.tick_since = max(self.tick_start, d.get("createdAt") or 0)
                    if V.PUMP:
                        t.start_watch(self.tick_start, now)
                if V.PUMP and d.get("deployer"):
                    t.peers = self.creators.setdefault(d["deployer"], {})
                    t.peers[addr] = d.get("createdAt") or now
            else:
                t.update(d, now)
            if d.get("curve") and not V.PUMP:  # curve -> token, for the pons tick feed
                self.by_curve[d["curve"].lower()] = addr
            seen.append(t)
        return seen

    def start_ticks(self, now):
        """The trade feed is (re)starting: windows are only complete from now on."""
        self.tick_start = now
        for t in self.tokens.values():
            t.tick_since = max(now, t.d.get("createdAt") or 0)
            if V.PUMP:
                t.start_watch(now, now)

    def stop_ticks(self):
        """The trade feed is down: per-trade signals are unknown until it restarts (start_ticks)."""
        self.tick_start = None
        self._orphans = {}
        for t in self.tokens.values():
            t.end_watch()

    def add_stream_trades(self, trades, now):
        """pump.fun: trades from the on-chain stream (pumpstream trade events) -> recording rows
        [token, t, side, usd, wallet, is_dev, tokens]. Trades of coins the market doesn't know yet wait up to
        ORPHAN_SEC for the coin to show up."""
        if self.tick_start is None:
            self.start_ticks(now)
        rows = []
        for a in [a for a in self._orphans if a in self.tokens]:
            tok = self.tokens[a]
            rows += [self._stream_trade(tok, e) for e in self._orphans.pop(a)]
        for e in trades:
            tok = self.tokens.get(e["mint"])
            if tok is None:
                self._orphans.setdefault(e["mint"], []).append(e)
            else:
                rows.append(self._stream_trade(tok, e))
        if now - self._orphans_pruned > 10:
            cut = now - ORPHAN_SEC
            self._orphans = {a: [e for e in es if e["t"] >= cut] for a, es in self._orphans.items() if es[-1]["t"] >= cut}
            self._orphans_pruned = now
        return rows

    @staticmethod
    def _stream_trade(tok, e):
        qu = tok.d.get("quoteUsd")
        usd = e["sol"] * qu if qu else 0.0
        is_dev = bool(tok.dev) and e["wallet"] == tok.dev
        tok.add_tick(e["t"], e["side"], usd, e["wallet"], is_dev, e["tokens"])
        return [tok.addr, round(e["t"], 2), e["side"], round(usd, 2), e["wallet"], 1 if is_dev else 0,
                float(f"{e['tokens']:.6g}")]

    def add_ticks(self, raw, now):
        """raw: decoded log ticks (curve, t, side, quote_raw, wallet). Returns the resolved trades as
        [token, t, side, usd, wallet, is_dev] rows (also used for recording)."""
        if self.tick_start is None:
            self.start_ticks(now)
        rows = []
        queue = list(self._pending) + [(now, r) for r in raw]
        self._pending.clear()
        for seen_at, r in queue:
            addr = self.by_curve.get(r["curve"])
            tok = self.tokens.get(addr) if addr else None
            if tok is None:
                if now - seen_at < 120:
                    self._pending.append((seen_at, r))
                continue
            d = tok.d
            qu = d.get("quoteUsd")
            if not qu:
                continue
            dec = (d.get("quote") or {}).get("decimals", 18)
            usd = r["quote_raw"] / 10 ** dec * qu
            is_dev = bool(tok.dev and r["wallet"] and r["wallet"] == tok.dev)
            t = r["t"] or now
            tok.add_tick(t, r["side"], usd, r["wallet"], is_dev)
            rows.append([addr, t, r["side"], round(usd, 2), r["wallet"], 1 if is_dev else 0])
        return rows

    def add_tick_rows(self, rows, now):
        """Replay path: rows already resolved to [token, t, side, usd, wallet, is_dev] (pump.fun: + tokens)."""
        if self.tick_start is None:
            self.start_ticks(now)
        for row in rows:
            tok = self.tokens.get(row[0])
            if tok is not None:
                tok.add_tick(row[1], row[2], row[3], row[4], bool(row[5]), row[6] if len(row) > 6 else None)

    def get(self, addr):
        return self.tokens.get(addr)

    def prune(self, now, keep, max_idle=7200):
        for a in [a for a, t in self.tokens.items() if now - t.updated > max_idle and a not in keep]:
            del self.tokens[a]
        cut = now - 86400
        for dev in list(self.creators):  # launches older than a day no longer count
            peers = self.creators[dev]
            for a in [a for a, c in peers.items() if c < cut]:
                del peers[a]
            if not peers:
                del self.creators[dev]


def finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)
