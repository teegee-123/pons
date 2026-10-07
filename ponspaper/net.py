"""Small HTTP/JSON client: keep-alive HTTPS (through the system proxy when one is set) with retries.

Connections are per-thread because http.client connections are not thread-safe.
"""
import http.client
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (pons-paper-trader)"
RETRYABLE_STATUS = {407, 408, 425, 429, 500, 502, 503, 504}


def _pac_proxies():
    """Proxies named in the Windows auto-config (PAC) script, in order of appearance."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            pac_url = winreg.QueryValueEx(k, "AutoConfigURL")[0]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        text = opener.open(pac_url, timeout=5).read().decode("utf8", "replace")
    except Exception:
        return []
    return list(dict.fromkeys(re.findall(r"PROXY\s+([A-Za-z0-9.\-]+:\d+)", text)))


def _proxy_works(hostport, target="ponsfamily.com"):
    host, _, port = hostport.partition(":")
    try:
        c = http.client.HTTPSConnection(host, int(port or 8080), timeout=6)
        c.set_tunnel(target, 443)
        c.request("HEAD", "/")
        c.getresponse().read()
        c.close()
        return True
    except Exception:
        return False


class HttpError(Exception):
    def __init__(self, status, msg=""):
        super().__init__(f"HTTP {status} {msg}".strip())
        self.status = status


class Client:
    def __init__(self, timeout=10.0, retries=3):
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()
        self._ctx = ssl.create_default_context()
        self._proxy = self._detect_proxy()
        self.requests = 0
        self.errors = 0

    @staticmethod
    def _detect_proxy():
        """PONS_PROXY env > HTTPS_PROXY env / static Windows proxy > first working PROXY from the Windows PAC script."""
        p = os.environ.get("PONS_PROXY") or urllib.request.getproxies().get("https")
        if not p:
            p = next((c for c in _pac_proxies() if _proxy_works(c)), None)
        if not p or p.lower() in ("none", "direct"):
            return None
        u = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
        return (u.hostname, u.port or 8080)

    def _conn(self, host):
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        c = conns.get(host)
        if c is None:
            if self._proxy:
                c = http.client.HTTPSConnection(self._proxy[0], self._proxy[1], timeout=self.timeout, context=self._ctx)
                c.set_tunnel(host, 443)
            else:
                c = http.client.HTTPSConnection(host, 443, timeout=self.timeout, context=self._ctx)
            conns[host] = c
        return c

    def _drop(self, host):
        conns = getattr(self._local, "conns", {})
        c = conns.pop(host, None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def request(self, method, url, body=None, headers=None):
        u = urllib.parse.urlsplit(url)
        host = u.hostname
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        h = {"User-Agent": UA, "Accept": "application/json", "Connection": "keep-alive"}
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        last = None
        for attempt in range(self.retries):
            self.requests += 1
            try:
                c = self._conn(host)
                c.request(method, path, body=data, headers=h)
                r = c.getresponse()
                raw = r.read()
                if r.status in RETRYABLE_STATUS:
                    raise HttpError(r.status, "retryable")
                if r.status >= 400:
                    self.errors += 1
                    raise HttpError(r.status)
                return json.loads(raw)
            except HttpError as e:
                if e.status not in RETRYABLE_STATUS:
                    raise
                last = e
            except (OSError, http.client.HTTPException, ValueError) as e:
                last = e
            self.errors += 1
            self._drop(host)
            time.sleep(0.2 * (attempt + 1))
        raise last

    def get_json(self, url, headers=None):
        return self.request("GET", url, headers=headers)

    def post_json(self, url, body, headers=None):
        return self.request("POST", url, body=body, headers=headers)
