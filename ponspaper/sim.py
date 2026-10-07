"""Trade math (fees + price impact) and per-strategy paper portfolios."""
import math
from collections import deque

from .market import curve_params


class Venue:
    """Constant-product reserves in human units. Fees (protocol/hook + creator tax) come off the quote side:
    from the input on buys and from the output on sells, exactly like the pons curve contract."""
    __slots__ = ("kind", "Q", "T", "sellable", "fee_bps", "tax_bps", "src")

    def __init__(self, kind, Q, T, sellable, fee_bps, tax_bps, src="model"):
        self.kind, self.Q, self.T, self.sellable = kind, Q, T, sellable
        self.fee_bps, self.tax_bps, self.src = fee_bps, tax_bps, src

    @property
    def spot(self):
        return self.Q / self.T

    @property
    def fee_frac(self):
        return (self.fee_bps + self.tax_bps) / 1e4

    def buy(self, quote_in):
        """-> (tokens_out, quote_used). quote_used < quote_in only when the buy completes the curve."""
        keep = 1.0 - self.fee_frac
        net = quote_in * keep
        out = net * self.T / (self.Q + net)
        if out > self.sellable:
            out = max(0.0, self.sellable)
            need = out * self.Q / max(1e-30, self.T - out)
            return out, min(quote_in, need / keep)
        return out, quote_in

    def sell(self, tokens_in):
        gross = tokens_in * self.Q / (self.T + tokens_in)
        return gross * (1.0 - self.fee_frac)


def model_venue(d, ex):
    """Venue estimated from API data. Curve: exact virtual-reserve model (k = 0.4*threshold*1e9).
    Graduated: constant-product approximation of the v4 pool seeded with `threshold` quote at graduation."""
    thr = d.get("thresholdQuote") or 0
    tax = d.get("creatorTaxBps") or 0
    p = d.get("priceQuote")
    if thr <= 0:
        return None
    v0, k = curve_params(thr)
    if d.get("stage") != "graduated":
        raised = d.get("raisedQuote")
        q = v0 + raised if raised is not None and raised >= 0 else None
        if q is None or (p and not 0.75 < (q * q / k) / p < 1.33):
            if not p:
                return None
            q = math.sqrt(k * p)
        t = k / q
        sellable = max(0.0, t - k / (v0 + thr))
        return Venue("curve", q, t, sellable, ex["protocolFeeBps"], tax)
    if not p:
        return None
    mult = max(0.05, float(ex.get("gradLiquidityMult", 1.0)))
    kp = (thr * thr * k / (v0 + thr) ** 2) * mult * mult
    return Venue("pool", math.sqrt(kp * p), math.sqrt(kp / p), float("inf"), ex["hookFeeBps"], tax)


def liquidation_usd(d, tokens, ex):
    """What selling `tokens` right now would return in USD, after fees, impact and gas."""
    v = model_venue(d, ex)
    qu = d.get("quoteUsd")
    if v is None or not qu:
        px = d.get("priceUsd") or 0.0
        return tokens * px * 0.95 - ex["gasUsd"]
    return v.sell(tokens) * qu - ex["gasUsd"]


class Position:
    __slots__ = ("id", "addr", "symbol", "t_entry", "tokens", "cost_usd", "entry_spot_usd", "feats", "peak_ret",
                 "mark_usd", "mark_ret", "exiting", "sell_fails", "stage", "entry_drift", "fees_usd", "src")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.peak_ret = self.peak_ret or 0.0
        self.mark_ret = self.mark_ret or 0.0
        self.mark_usd = self.mark_usd if self.mark_usd is not None else self.cost_usd
        self.exiting = False
        self.sell_fails = self.sell_fails or 0
        self.fees_usd = self.fees_usd or 0.0

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__}
        d["exiting"] = False
        return d


STAT_KEYS = ("n", "wins", "sum_ret", "sum_ret2", "gross_win", "gross_loss", "realized", "fees", "gas", "missed",
             "sell_fails", "hold_sum", "best", "worst", "entries")


class Portfolio:
    def __init__(self, bankroll):
        self.bankroll = float(bankroll)
        self.cash = float(bankroll)
        self.reserved = {}      # order id -> usd held back for a pending buy
        self.positions = {}     # pos id -> Position
        self.trades = deque(maxlen=300)
        self.stats = {k: 0.0 for k in STAT_KEYS}
        self.stats["best"] = None
        self.stats["worst"] = None
        self.equity_curve = []  # [t, equity]
        self.peak_equity = float(bankroll)
        self.max_dd = 0.0

    # --- accounting ---
    def unrealized(self):
        return sum(p.mark_usd - p.cost_usd for p in self.positions.values())

    def equity(self):
        return self.cash + sum(self.reserved.values()) + sum(p.mark_usd for p in self.positions.values())

    def sample_equity(self, t, max_points=720):
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0:
            self.max_dd = max(self.max_dd, (self.peak_equity - eq) / self.peak_equity)
        self.equity_curve.append([round(t), round(eq, 2)])
        if len(self.equity_curve) > max_points:
            self.equity_curve = self.equity_curve[::2]

    def record_close(self, tr):
        s = self.stats
        r = tr["ret"]
        s["n"] += 1
        s["wins"] += 1 if tr["pnl"] > 0 else 0
        s["sum_ret"] += r
        s["sum_ret2"] += r * r
        if tr["pnl"] > 0:
            s["gross_win"] += tr["pnl"]
        else:
            s["gross_loss"] += -tr["pnl"]
        s["realized"] += tr["pnl"]
        s["hold_sum"] += tr["hold"]
        s["best"] = r if s["best"] is None else max(s["best"], r)
        s["worst"] = r if s["worst"] is None else min(s["worst"], r)
        self.trades.appendleft(tr)

    def summary(self):
        s = self.stats
        n = s["n"]
        mean = s["sum_ret"] / n if n else None
        sd = math.sqrt(max(0.0, s["sum_ret2"] / n - mean * mean)) if n > 1 else None
        return {
            "trades": int(n), "open": len(self.positions), "pending": len(self.reserved),
            "winRate": (s["wins"] / n) if n else None,
            "avgRet": mean, "sdRet": sd,
            "realized": s["realized"], "unrealized": self.unrealized(),
            "pnl": s["realized"] + self.unrealized(),
            "equity": self.equity(), "bankroll": self.bankroll,
            "retOnBankroll": (self.equity() / self.bankroll - 1.0) if self.bankroll else None,
            "profitFactor": (s["gross_win"] / s["gross_loss"]) if s["gross_loss"] > 0 else (None if not s["gross_win"] else 999.0),
            "avgWin": (s["gross_win"] / s["wins"]) if s["wins"] else None,
            "avgLoss": (-s["gross_loss"] / (n - s["wins"])) if n - s["wins"] > 0 else None,
            "maxDD": self.max_dd, "fees": s["fees"], "gas": s["gas"], "missed": int(s["missed"]),
            "sellFails": int(s["sell_fails"]), "avgHoldMin": (s["hold_sum"] / n / 60.0) if n else None,
            "best": s["best"], "worst": s["worst"], "entries": int(s["entries"]),
        }

    # --- persistence ---
    def to_dict(self):
        # pending orders don't survive a restart, so their reserved cash is folded back in
        return {"bankroll": self.bankroll, "cash": self.cash + sum(self.reserved.values()),
                "positions": [p.to_dict() for p in self.positions.values()],
                "trades": list(self.trades), "stats": self.stats, "equity_curve": self.equity_curve,
                "peak_equity": self.peak_equity, "max_dd": self.max_dd}

    @classmethod
    def from_dict(cls, d):
        pf = cls(d.get("bankroll", 1000))
        pf.cash = d.get("cash", pf.bankroll)
        for pd in d.get("positions", []):
            p = Position(**pd)
            pf.positions[p.id] = p
        pf.trades = deque(d.get("trades", []), maxlen=300)
        pf.stats.update(d.get("stats", {}))
        pf.equity_curve = d.get("equity_curve", [])
        pf.peak_equity = d.get("peak_equity", pf.bankroll)
        pf.max_dd = d.get("max_dd", 0.0)
        return pf
