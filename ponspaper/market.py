"""Token state + rolling history, and the features strategies filter on."""
import bisect
import math

TOKEN_SUPPLY = 1e9
CURVE_V0_RATIO = 0.4   # virtual quote reserve at launch = 0.4 x graduation threshold (on-chain: 1.68 ETH vs 4.2 ETH)
HIST_SEC = 1800        # keep 30 minutes of samples per token
SAMPLE_EVERY = 15      # add a sample at least this often even if nothing changed


def curve_params(threshold):
    v0 = CURVE_V0_RATIO * threshold
    return v0, v0 * TOKEN_SUPPLY


def socials_count(d):
    s = d.get("socials") or {}
    return sum(1 for v in s.values() if v)


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
    __slots__ = ("addr", "d", "first_seen", "updated", "hist", "_t")

    def __init__(self, addr, d, now):
        self.addr = addr
        self.d = d
        self.first_seen = now
        self.updated = now
        self.hist = []   # (t, spot_usd, volume_usd, trade_count)
        self._t = []     # parallel list of t for bisect
        created = d.get("createdAt") or now
        thr = d.get("thresholdQuote") or 0
        if d.get("stage") != "graduated" and thr > 0 and now - created < HIST_SEC and d.get("quoteUsd"):
            v0, k = curve_params(thr)
            self._push(created, (v0 / TOKEN_SUPPLY) * d["quoteUsd"], 0.0, 0)
        self._sample(now)

    def _push(self, t, px, vol, trades):
        if self._t and t < self._t[-1]:
            return
        self.hist.append((t, px, vol, trades))
        self._t.append(t)

    def _sample(self, now):
        d = self.d
        px = spot_usd(d)
        if px is None:
            return
        self._push(now, px, d.get("volumeUsd") or 0.0, d.get("tradeCount") or 0)

    def update(self, d, now):
        old = self.d
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
        return changed

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
            "quote": "ETH" if (d.get("quote") or {}).get("symbol") == "ETH" else "OTHER",
            "buyback": "yes" if d.get("buybackEnabled") else "no",
            "spotUsd": px,
        }
        if px and f["mcapUsd"] is None:
            f["mcapUsd"] = px * TOKEN_SUPPLY
        vol, trades = d.get("volumeUsd") or 0.0, d.get("tradeCount") or 0
        for key, win in (("chg1m", 60), ("chg5m", 300), ("chg15m", 900)):
            h = self.at(now - win)
            f[key] = (px / h[1] - 1.0) * 100.0 if (h and px and h[1]) else None
        h = self.at(now - 60)
        if h:
            span = max(1.0, now - h[0]) / 60.0
            f["tpm1"] = (trades - h[3]) / span
            f["vol1m"] = (vol - h[2]) / span
        else:
            f["tpm1"] = f["vol1m"] = None
        if self.hist and px:
            peak = max(x[1] for x in self.hist)
            f["ddPeak"] = max(0.0, (1.0 - px / peak) * 100.0) if peak > 0 else None
        else:
            f["ddPeak"] = None
        return f


class Market:
    def __init__(self):
        self.tokens = {}

    def ingest(self, items, now):
        seen = []
        for d in items:
            addr = (d.get("address") or "").lower()
            if not addr:
                continue
            t = self.tokens.get(addr)
            if t is None:
                t = self.tokens[addr] = Token(addr, d, now)
            else:
                t.update(d, now)
            seen.append(t)
        return seen

    def get(self, addr):
        return self.tokens.get(addr)

    def prune(self, now, keep, max_idle=7200):
        for a in [a for a, t in self.tokens.items() if now - t.updated > max_idle and a not in keep]:
            del self.tokens[a]


def finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)
