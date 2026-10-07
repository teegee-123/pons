"""pons paper trader.

  python -m ponspaper                 live paper trading + dashboard on http://127.0.0.1:8787
  python -m ponspaper replay          backtest/evolve strategies on recorded snapshots (data/snapshots)
"""
import argparse
import glob
import json
import os
import signal
import sys
import threading
import time
import webbrowser

from . import strategy as S
from .engine import Engine, deep_merge, DEFAULT_CONFIG, snapshot_items
from .server import serve

# Outside OneDrive so the constantly-changing state and snapshot files don't sync. Override with --data or PONS_DATA.
DEFAULT_DATA = os.environ.get("PONS_DATA") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ponspaper", "data")


def cmd_run(a):
    eng = Engine(a.data, live=True, fresh=a.fresh)
    eng.start()
    httpd = serve(eng, a.port, a.host)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"pons paper trader running -> {url}   (storage: {eng.store.kind}, data: {a.data})  Ctrl+C to stop", flush=True)
    # Render (and most hosts) stop the process with SIGTERM: shut down cleanly so state is saved.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("saving state...")
        eng.stop()
        httpd.server_close()


def cmd_replay(a):
    files = sorted(sum((glob.glob(p) for p in (a.files or [os.path.join(a.data, "snapshots", "*.jsonl.gz")])), []))
    if not files:
        print("no snapshot files found - run the live trader for a while first (it records data/snapshots/)")
        return 1
    cfg = DEFAULT_CONFIG
    try:
        with open(os.path.join(a.data, "config.json"), encoding="utf8") as fh:
            cfg = deep_merge(DEFAULT_CONFIG, json.load(fh))
    except (OSError, ValueError):
        pass
    cfg = deep_merge(cfg, {"evolution": {"population": a.population, "epochMin": a.epoch_min},
                           "edge": {"enabled": False}, "record": {"enabled": False}})
    if a.latency_ms is not None:
        cfg = deep_merge(cfg, {"execution": {"latencyMs": a.latency_ms}})
    eng = Engine(os.path.join(a.data, "replay_tmp"), live=False, cfg=cfg, seed=a.seed)
    # bring in the live manual strategies (fresh portfolios) so they're compared on the same data
    try:
        with open(os.path.join(a.data, "state.json"), encoding="utf8") as fh:
            manual = [rd["spec"] for rd in json.load(fh).get("runs", []) if rd["spec"].get("kind") == "manual"]
    except (OSError, ValueError, KeyError):
        manual = []
    if manual:  # replace the default seeds with the live manual strategies
        for sid in [sid for sid, r in eng.runs.items() if r.spec["kind"] == "manual"]:
            del eng.runs[sid]
        for sp in manual:
            eng.upsert_strategy({k: v for k, v in sp.items() if k != "id"})
    t0, n, first, last = time.time(), 0, None, None
    print(f"replaying {len(files)} file(s) with {len(eng.runs)} strategies...")
    for t, items in snapshot_items(files):
        eng.tick(items, t)
        first = first or t
        last = t
        n += 1
    span = (last - first) / 3600 if first else 0
    k = cfg["evolution"]["shrinkK"]
    rows = sorted((eng._row(r, k) for r in eng.runs.values()), key=lambda x: x["score"], reverse=True)
    hall = eng.hall
    print(f"\n{n} snapshots, {span:.1f}h of market data, {eng.epoch} evolution epochs, {time.time() - t0:.1f}s\n")
    print(f"{'score%':>7} {'trades':>6} {'win%':>5} {'pnl$':>9} {'maxDD':>6}  strategy")
    for x in rows[: a.top]:
        wr = f"{x['winRate'] * 100:.0f}" if x["winRate"] is not None else "-"
        print(f"{x['score']:7.2f} {x['trades']:6d} {wr:>5} {x['pnl']:9.2f} {x['maxDD'] * 100:5.1f}%  [{x['kind'][0]}] {x['name']}: {x['desc']}")
    out = {"at": time.time(), "files": files, "hours": span, "snapshots": n, "cfg": cfg,
           "rows": [dict(x, spec=eng.runs[x["id"]].spec) for x in rows[: max(a.top, 30)]],
           "hall": hall}
    path = os.path.join(a.data, "replay_results.json")
    with open(path, "w", encoding="utf8") as fh:
        json.dump(out, fh, default=str)
    print(f"\nresults written to {path} (visible in the dashboard's Backtest tab)")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ponspaper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default=DEFAULT_DATA, help=f"data directory (default: {DEFAULT_DATA})")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="live paper trading + dashboard (default)")
    r.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    r.add_argument("--host", default=os.environ.get("PONS_HOST", "127.0.0.1"), help="0.0.0.0 to accept outside connections")
    r.add_argument("--fresh", action="store_true", help="ignore saved state/config and start over")
    r.add_argument("--no-browser", action="store_true")
    b = sub.add_parser("replay", help="backtest + evolve on recorded snapshots")
    b.add_argument("files", nargs="*", help="snapshot .jsonl.gz files (default: all in data/snapshots)")
    b.add_argument("--population", type=int, default=150)
    b.add_argument("--epoch-min", type=float, default=30, help="evolution epoch length in market minutes")
    b.add_argument("--latency-ms", type=float, default=None)
    b.add_argument("--top", type=int, default=25)
    b.add_argument("--seed", type=int, default=None)
    a = p.parse_args(argv)
    if a.cmd == "replay":
        return cmd_replay(a)
    if a.cmd is None:
        a = p.parse_args(["--data", a.data, "run"])
    return cmd_run(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
