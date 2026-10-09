"""Export the best strategies, their full rules and their trading history from a running trader (pons or pump.fun,
local or on Render), using only its read-only endpoints.

  python -m ponspaper export https://pumpfun-paper-trader.onrender.com
  python -m ponspaper export http://127.0.0.1:8787 --recording

Writes exports/<venue>_<date-time>/:
  strategies.json   everything: config, live strategies (rules, stats, equity curve, open positions, recent trades),
                    hall of fame, lab champions and the long-run record, each with rules you can paste back in
  strategies.csv    live strategies ranked by score, one row each
  hall_of_fame.csv  best auto strategies seen (including retired ones)
  long_run.csv      the lab's long-run record: forward-tested genomes and their out-of-sample results
  trades.csv        every stored closed trade (all strategies) with the entry features
  recording.jsonl.gz  (with --recording) the recorded market data, for replay / ga on your PC
"""
import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

from .net import Client


class _Local:
    """Plain HTTP straight to a trader on this PC (the shared client is HTTPS-only and goes through the proxy)."""

    def __init__(self, timeout=90):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get_bytes(self, url):
        return self.opener.open(url, timeout=self.timeout).read()

    def get_json(self, url):
        return json.loads(self.get_bytes(url))


def _client(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        return _Local()
    return Client(timeout=90, retries=3)  # a sleeping free Render service takes up to a minute to wake


def _rules(genome_key):
    """A lab genome key is its canonical rules as JSON: {"f": filters, "x": exits}."""
    try:
        g = json.loads(genome_key)
        return {"filters": g.get("f", {}), "exits": g.get("x", {})}
    except (TypeError, ValueError):
        return None


def _pct(x, digits=2):
    return "" if x is None else round(100.0 * x, digits)


def _num(x, digits=2):
    return "" if x is None else round(x, digits)


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def export(url, out_root="exports", recording=False, log=print):
    base = url.rstrip("/")
    c = _client(base)
    log(f"reading {base} ...")
    health = c.get_json(base + "/health")
    venue = health.get("venue", "pons")
    state = c.get_json(base + "/api/state")
    lab = c.get_json(base + "/api/lab")
    cfg = state["cfg"]

    live = []
    for rank, row in enumerate(state["rows"], 1):
        det = c.get_json(f"{base}/api/strategy/{row['id']}") or {}
        sp = det.get("spec") or {}
        live.append({"rank": rank, "id": row["id"], "name": row["name"], "kind": row["kind"], "enabled": row.get("enabled"),
                     "description": row["desc"], "rules": {k: sp.get(k) for k in ("filters", "exits", "sizing")},
                     "created": sp.get("created"), "origin": sp.get("origin"), "stats": {k: v for k, v in row.items() if k != "curve"},
                     "equityCurve": det.get("equity"), "openPositions": det.get("positions"), "recentTrades": det.get("trades")})
    hall = []
    for h in state["evo"]["hall"]:
        det = c.get_json(f"{base}/api/strategy/{h['id']}") or {}
        sp = det.get("spec") or {}
        hall.append({"id": h["id"], "name": h["name"], "alive": h["alive"], "description": h["desc"], "score": h["score"],
                     "rules": {k: sp.get(k) for k in ("filters", "exits", "sizing")}, "stats": h["summary"], "at": h["at"]})
    champions = [{"description": h["desc"], "rules": _rules(h["key"]), "fitness": h.get("fitness"), "train": h.get("train"),
                  "validation": h.get("val"), "stress": h.get("stress"), "deployed": h.get("deployed"), "live": h.get("live")}
                 for h in lab.get("hall") or []]
    led = lab.get("ledger") or {}
    long_run = [dict(r, rules=_rules(r["key"])) for r in led.get("rows", [])]
    for r in long_run:
        r.pop("key", None)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = os.path.join(out_root, f"{venue}_{stamp}")
    os.makedirs(out, exist_ok=True)
    doc = {"venue": venue, "source": base, "exportedAt": time.time(), "status": state["status"],
           "config": {k: cfg.get(k) for k in ("execution", "universe", "sizing", "evolution", "lab")},
           "totals": state["totals"], "live": live, "hallOfFame": hall,
           "lab": {"phase": lab.get("phase"), "generation": lab.get("gen"), "dataset": lab.get("dataset"),
                   "champions": champions, "longRun": {k: v for k, v in led.items() if k != "rows"} | {"genomes": long_run}}}
    with open(os.path.join(out, "strategies.json"), "w", encoding="utf8") as fh:
        json.dump(doc, fh, indent=1, default=str)

    _write_csv(os.path.join(out, "strategies.csv"),
               ["rank", "id", "name", "kind", "score_pct", "trades", "open", "win_pct", "avg_ret_pct", "pnl_usd", "realized_usd",
                "max_dd_pct", "profit_factor", "avg_hold_min", "fees_gas_usd", "reverts", "description", "rules_json"],
               [[s["rank"], s["id"], s["name"], s["kind"], _num(s["stats"].get("score")), s["stats"].get("trades"),
                 s["stats"].get("open"), _pct(s["stats"].get("winRate"), 1), _pct(s["stats"].get("avgRet")),
                 _num(s["stats"].get("pnl")), _num(s["stats"].get("realized")), _pct(s["stats"].get("maxDD"), 1),
                 _num(s["stats"].get("profitFactor")), _num(s["stats"].get("avgHoldMin"), 1),
                 _num((s["stats"].get("fees") or 0) + (s["stats"].get("gas") or 0)),
                 (s["stats"].get("missed") or 0) + (s["stats"].get("sellFails") or 0), s["description"],
                 json.dumps(s["rules"], separators=(",", ":"))] for s in live])
    _write_csv(os.path.join(out, "hall_of_fame.csv"),
               ["id", "name", "alive", "score_pct", "trades", "win_pct", "avg_ret_pct", "pnl_usd", "description", "rules_json"],
               [[h["id"], h["name"], h["alive"], _num(h["score"]), (h["stats"] or {}).get("trades"),
                 _pct((h["stats"] or {}).get("winRate"), 1), _pct((h["stats"] or {}).get("avgRet")), _num((h["stats"] or {}).get("pnl")),
                 h["description"], json.dumps(h["rules"], separators=(",", ":"))] for h in hall])
    _write_csv(os.path.join(out, "long_run.csv"),
               ["status", "tracked_days", "stretches_won", "stretches_with_trades", "stretches_scored", "trades", "avg_ret_pct",
                "confidence_pct", "win_pct", "pnl_usd", "deployed", "description", "rules_json"],
               [[r["status"], _num((r["last"] - r["since"]) / 86400 if r.get("last") else None), r["pos"], r["segs"], r["seen"],
                 r["n"], _pct(r.get("mean")), _num(r.get("lcb")), _pct(r.get("win"), 1), _num(r.get("pnl")), r.get("deployed"),
                 r["desc"], json.dumps(r["rules"], separators=(",", ":"))] for r in long_run])
    trades = c.get_bytes(base + "/download/trades.csv")
    with open(os.path.join(out, "trades.csv"), "wb") as fh:
        fh.write(trades)
    n_trades = max(0, trades.count(b"\n") - 1)
    if recording:
        log("downloading the market recording (can take a while) ...")
        with open(os.path.join(out, "recording.jsonl.gz"), "wb") as fh:
            fh.write(c.get_bytes(base + "/download/snapshots.jsonl.gz"))

    log(f"\n{venue}: {len(live)} live strategies, {len(hall)} in the hall of fame, {len(champions)} lab champions, "
        f"{len(long_run)} long-run genomes ({led.get('proven', 0)} proven), {n_trades} stored trades")
    log(f"{'#':>2} {'score%':>7} {'trades':>6} {'win%':>5} {'P&L $':>9}  strategy")
    for s in live[:10]:
        st = s["stats"]
        win = f"{st['winRate'] * 100:.0f}" if st.get("winRate") is not None else "-"
        log(f"{s['rank']:>2} {st['score']:7.2f} {st['trades']:6d} {win:>5} {st['pnl']:9.2f}  {s['name']}: {s['description'][:90]}")
    log(f"\nwritten to {os.path.abspath(out)}")
    return out
