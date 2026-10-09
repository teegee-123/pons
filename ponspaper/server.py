"""Dashboard + JSON API. Binds to 127.0.0.1 locally; on a host like Render pass --host 0.0.0.0."""
import json
import math
import mimetypes
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import venue as V

STATIC = os.path.join(os.path.dirname(__file__), "static")


def _clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, set):
        return [_clean(v) for v in o]
    return o


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ponspaper"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", filename=None):
            data = body if isinstance(body, bytes) else json.dumps(_clean(body), separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            p = u.path
            if p == "/health":
                st = engine.status
                fresh = st["lastPollAt"] is not None and time.time() - st["lastPollAt"] < 60
                return self._send(200, {"ok": True, "venue": V.NAME, "polling": fresh, "polls": st["polls"]})
            if p == "/api/state":
                return self._send(200, engine.state_view())
            if p == "/download/trades.csv":
                return self._send(200, engine.store.trades_csv(), "text/csv; charset=utf-8", f"{V.FILE_PREFIX}_trades.csv")
            if p == "/download/snapshots.jsonl.gz":
                return self._send(200, engine.store.snapshots_gz(), "application/gzip", f"{V.FILE_PREFIX}_snapshots.jsonl.gz")
            if p == "/api/meta":
                return self._send(200, engine.meta())
            if p == "/api/lab":
                return self._send(200, engine.lab_view())
            if p == "/api/market":
                return self._send(200, engine.market_view())
            if p == "/api/trades":
                return self._send(200, engine.trades_view())
            if p == "/api/edge":
                return self._send(200, engine.edge_view(int((q.get("h") or ["1"])[0])))
            if p.startswith("/api/strategy/"):
                v = engine.strategy_view(p.rsplit("/", 1)[1])
                return self._send(200 if v else 404, v or {"error": "not found"})
            if p == "/api/replay":
                try:
                    with open(os.path.join(engine.data_dir, "replay_results.json"), encoding="utf8") as fh:
                        return self._send(200, json.load(fh))
                except (OSError, ValueError):
                    return self._send(200, {"rows": []})
            if p == "/":
                p = "/index.html"
            path = os.path.normpath(os.path.join(STATIC, p.lstrip("/")))
            if not path.startswith(STATIC) or not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            with open(path, "rb") as fh:
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                return self._send(200, fh.read(), ctype)

        def do_POST(self):
            # Same-origin guard: only the dashboard itself (whatever host it is served from) may change state.
            origin = self.headers.get("Origin")
            host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(":")[0]
            if origin and urllib.parse.urlsplit(origin).hostname not in ("127.0.0.1", "localhost", host):
                return self._send(403, {"error": "forbidden"})
            p = urllib.parse.urlsplit(self.path).path
            b = self._body()
            if p == "/api/settings":
                return self._send(200, engine.update_settings(b))
            if p == "/api/strategy":
                return self._send(200, engine.upsert_strategy(b))
            if p.startswith("/api/strategy/") and p.endswith("/action"):
                sid = p.split("/")[3]
                res = engine.strategy_action(sid, b.get("action"))
                return self._send(200 if res else 404, res or {"error": "not found"})
            if p == "/api/lab/deploy":
                added = engine.lab_deploy(b.get("key"))
                return self._send(200, {"added": added})
            if p == "/api/lab/rebuild":
                engine.lab_rebuild()
                return self._send(200, {"ok": True})
            if p == "/api/evolve":
                engine.evolve()
                return self._send(200, {"ok": True})
            if p == "/api/reset":
                engine.reset_all()
                return self._send(200, {"ok": True})
            if p == "/api/save":
                engine.save_state()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(engine, port, host="127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), make_handler(engine))
    httpd.daemon_threads = True
    return httpd
