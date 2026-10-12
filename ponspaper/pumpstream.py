"""On-chain trade stream for pump.fun (PONS_VENUE=pumpfun): every bonding-curve trade, launch and graduation as it
lands on Solana, so the paper trader decides, marks and fills on what a real bot on a chain feed would see, instead of
the pump.fun API's copy (polled every 2 s and lagging busy coins).

Sources (the first one configured wins, see open_stream):
  grpc  Yellowstone gRPC (Geyser): SOLANA_GRPC_URL, plus SOLANA_GRPC_TOKEN when the provider wants an x-token.
        Transactions touching the pump program at `processed` commitment. PublicNode has a free endpoint with a
        token; others are paid (Chainstack, Helius, QuickNode, Triton, ...). Needs `pip install grpcio`. The
        protobuf messages are encoded and decoded here, so no generated stubs or protobuf package are needed.
        While gRPC can't connect, the websocket stands in (FallbackSource).
  ws    Solana websocket logsSubscribe on the pump program: SOLANA_WS_URL, else derived from SOLANA_RPC_URL, else the
        public endpoint. Free and the same events, but slower, and the public endpoint drops connections.

Both deliver the transaction's log lines. pump.fun emits its events as "Program data:" lines (pump IDL, checked
against real transactions). An event only counts when the line was logged by the pump program itself: any program
can log arbitrary "Program data", and fake pump events are a known trick against trading bots.
  TradeEvent     mint, sol_amount, token_amount, is_buy, user, timestamp, reserves after the trade, fee_basis_points,
                 creator_fee_basis_points, ... (sol_amount is what entered/left the curve, fees are on top of it)
  CreateEvent    name, symbol, uri, mint, bonding_curve, user, creator, timestamp, reserves, ..., is_mayhem_mode
  CompleteEvent  user, mint, bonding_curve, timestamp: the curve is full and the coin moves to PumpSwap
Coins paired with other quote tokens report zero SOL reserves and are skipped (the trading universe is SOL only).
"""
import base64
import json
import os
import statistics
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections import deque

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
GRPC_METHOD = "/geyser.Geyser/Subscribe"
WALLET_CHARS = 10      # wallets are kept as their first 10 base58 characters: plenty to tell one coin's traders apart
MAX_LAG_SEC = 5.0      # a stream this far behind the chain is not live any more: unhealthy, and it reconnects
SLOT_SEC = 0.25        # Solana slot time until the stream has measured it (about 0.21 s in October 2026)
HIST_SEC = 20.0        # per-coin trade history kept for pricing orders at the slot they land in
TOKEN_DECIMALS, SOL_DECIMALS = 6, 9

TRADE, CREATE, COMPLETE = "trade", "create", "complete"
_DISC = {bytes.fromhex("bddb7fd34ee661ee"): TRADE, bytes.fromhex("1b72a94ddeeb6376"): CREATE,
         bytes.fromhex("5f72619cd42e9808"): COMPLETE}
_B64_PREFIX = {base64.b64encode(d[:6]).decode(): kind for d, kind in _DISC.items()}  # 8 base64 chars per discriminator
_DATA = "Program data: "
_INVOKE = "Program " + PUMP_PROGRAM + " invoke"
_PROGRAM_OUTPUT = ("Program log:", "Program return:", "Program consumption:")

# ---------------------------------------------------------------------------------------------------- base58
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_b58_cache = {}


def b58(b):
    """32-byte public key -> base58 text. Mints repeat a lot, so recent ones are cached."""
    s = _b58_cache.get(b)
    if s is not None:
        return s
    n = int.from_bytes(b, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(_B58[r])
    s = "1" * (len(b) - len(b.lstrip(b"\0"))) + "".join(reversed(out))
    if len(_b58_cache) > 20000:
        _b58_cache.clear()
    _b58_cache[b] = s
    return s


# ---------------------------------------------------------------------------------------------------- pump events
def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def _str(b, o):
    n = struct.unpack_from("<I", b, o)[0]
    return b[o + 4:o + 4 + n].decode("utf8", "replace"), o + 4 + n


def decode_trade(b):
    """TradeEvent -> dict, or None for coins not paired with SOL. Offsets follow the pump IDL (after the 8-byte
    discriminator): mint, sol_amount, token_amount, is_buy, user, timestamp, 4 reserves, fee_recipient,
    fee_basis_points, fee, creator, creator_fee_basis_points, creator_fee, ..., ix_name, mayhem_mode, ..."""
    if len(b) < 225:
        return None
    vs, vt, rs, rt = struct.unpack_from("<4Q", b, 97)
    if not vs or not vt:  # paired with another quote token: its reserves are in other fields
        return None
    mayhem = None
    try:
        _, o = _str(b, 258)
        mayhem = bool(b[o]) if o < len(b) else None
    except (struct.error, IndexError):
        pass
    return {"kind": TRADE, "mint": b58(b[8:40]), "side": 1 if b[56] else -1, "sol": _u64(b, 40) / 10 ** SOL_DECIMALS,
            "tokens": _u64(b, 48) / 10 ** TOKEN_DECIMALS, "wallet": b58(b[57:89])[:WALLET_CHARS],
            "ts": struct.unpack_from("<q", b, 89)[0], "Q": vs / 10 ** SOL_DECIMALS, "T": vt / 10 ** TOKEN_DECIMALS,
            "raised": rs / 10 ** SOL_DECIMALS, "sellable": rt / 10 ** TOKEN_DECIMALS,
            "feeBps": _u64(b, 161) + _u64(b, 209), "mayhem": mayhem}


def decode_create(b):
    """CreateEvent -> dict: name, symbol, uri, mint, bonding_curve, user, creator, timestamp, virtual token, virtual
    SOL, real token reserves, supply, token program, is_mayhem_mode, ..."""
    try:
        name, o = _str(b, 8)
        symbol, o = _str(b, o)
        _uri, o = _str(b, o)
        mint, curve, creator = b58(b[o:o + 32]), b58(b[o + 32:o + 64]), b58(b[o + 96:o + 128])
        ts = struct.unpack_from("<q", b, o + 128)[0]
        vt, vs, rt, supply = struct.unpack_from("<4Q", b, o + 136)
        mayhem = bool(b[o + 200]) if len(b) > o + 200 else None
    except (struct.error, IndexError):
        return None
    if not vs or not vt:
        return None
    return {"kind": CREATE, "mint": mint, "curve": curve, "creator": creator, "name": name, "symbol": symbol, "ts": ts,
            "Q": vs / 10 ** SOL_DECIMALS, "T": vt / 10 ** TOKEN_DECIMALS, "sellable": rt / 10 ** TOKEN_DECIMALS,
            "supply": supply / 10 ** TOKEN_DECIMALS, "mayhem": mayhem}


def decode_complete(b):
    if len(b) < 112:
        return None
    return {"kind": COMPLETE, "mint": b58(b[40:72]), "ts": struct.unpack_from("<q", b, 104)[0]}


_DECODERS = {TRADE: decode_trade, CREATE: decode_create, COMPLETE: decode_complete}


def events_from_logs(logs):
    """A transaction's log lines -> pump events, in order. Tracks the program call stack so only data logged by the
    pump program counts. Truncated logs simply end the scan."""
    out, stack = [], []
    for line in logs:
        if line.startswith(_DATA):
            if stack and stack[-1] and line[14:22] in _B64_PREFIX:
                try:
                    raw = base64.b64decode(line[14:])
                except ValueError:
                    continue
                kind = _DISC.get(raw[:8])
                ev = _DECODERS[kind](raw) if kind else None
                if ev is not None:
                    out.append(ev)
        elif line.startswith("Program ") and not line.startswith(_PROGRAM_OUTPUT):
            # only the runtime writes "Program <id> invoke/success/failed"; programs can only add log/data/return lines
            if line.endswith("]") and " invoke [" in line:
                stack.append(line.startswith(_INVOKE))
            elif (line.endswith(" success") or " failed" in line) and stack:
                stack.pop()
    return out


# ---------------------------------------------------------------------------------------------------- protobuf
def _varint(b, i):
    x = s = 0
    while True:
        c = b[i]
        i += 1
        x |= (c & 0x7F) << s
        if c < 0x80:
            return x, i
        s += 7


def _fields(b, i, end):
    """Yield (field number, value) for a protobuf message in b[i:end]; length-delimited values are (start, end)
    spans into b, so nested messages are read without copying."""
    while i < end:
        key, i = _varint(b, i)
        wt = key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            n, i = _varint(b, i)
            v = (i, i + n)
            i += n
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        yield key >> 3, v


def _enc_varint(x):
    out = bytearray()
    while True:
        if x < 0x80:
            out.append(x)
            return bytes(out)
        out.append((x & 0x7F) | 0x80)
        x >>= 7


def _pb_int(field, x):
    return _enc_varint(field << 3) + _enc_varint(x)


def _pb_bytes(field, data):
    return _enc_varint(field << 3 | 2) + _enc_varint(len(data)) + data


def subscribe_request(program=PUMP_PROGRAM, commitment=0):
    """Yellowstone SubscribeRequest: transactions (not votes, not failed) that include `program`, at commitment
    0 = processed. transactions is a map<string, SubscribeRequestFilterTransactions> (field 3); commitment field 6."""
    flt = _pb_int(1, 0) + _pb_int(2, 0) + _pb_bytes(3, program.encode())  # vote=false, failed=false, account_include
    return _pb_bytes(3, _pb_bytes(1, b"pump") + _pb_bytes(2, flt)) + _pb_int(6, commitment)


def ping_request(n):
    return _pb_bytes(9, _pb_int(1, n))  # SubscribeRequest.ping = SubscribeRequestPing{id}


def parse_update(b):
    """Yellowstone SubscribeUpdate -> ("tx", slot, logs, created_at) | ("ping",) | ("pong",) | ("other",).
    Path: SubscribeUpdate.transaction(4) -> SubscribeUpdateTransaction{transaction(1), slot(2)} ->
    SubscribeUpdateTransactionInfo{meta(4)} -> TransactionStatusMeta{err(1), log_messages(6)}."""
    tx = created = None
    kind = "other"
    for f, v in _fields(b, 0, len(b)):
        if f == 4:
            tx = v
        elif f == 6:
            kind = "ping"
        elif f == 9:
            kind = "pong"
        elif f == 11:  # google.protobuf.Timestamp{seconds(1), nanos(2)}: when the server produced the update
            secs = nanos = 0
            for g, x in _fields(b, *v):
                if g == 1:
                    secs = x
                elif g == 2:
                    nanos = x
            created = secs + nanos / 1e9
    if tx is None:
        return (kind,)
    slot, info, meta = 0, None, None
    for f, v in _fields(b, *tx):
        if f == 1:
            info = v
        elif f == 2:
            slot = v
    if info is not None:
        for f, v in _fields(b, *info):
            if f == 4:
                meta = v
    logs = []
    if meta is not None:
        for f, v in _fields(b, *meta):
            if f == 1:  # failed transaction: nothing changed on chain
                return ("tx", slot, [], created)
            if f == 6:
                logs.append(bytes(b[v[0]:v[1]]).decode("utf8", "replace"))
    return ("tx", slot, logs, created)


# ---------------------------------------------------------------------------------------------------- sources
def _system_roots():
    """The operating system's trusted root certificates as PEM, or None for gRPC's own list. gRPC ships its own
    roots, so behind a proxy that re-signs TLS with a company certificate (trusted by Windows, and so by everything
    else here) its handshake fails. On Linux hosts like Render this list comes back empty and gRPC's own is used."""
    try:
        ders = ssl.create_default_context().get_ca_certs(binary_form=True)
    except (ssl.SSLError, OSError):
        return None
    return "".join(ssl.DER_cert_to_PEM_cert(d) for d in ders).encode() if ders else None


class GrpcSource:
    """Yellowstone gRPC subscription. run() blocks for the life of one connection."""
    name = "gRPC"

    def __init__(self, url, token=None, proxy=None):
        u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
        self.secure = u.scheme != "http"
        self.target = f"{u.hostname}:{u.port or (443 if self.secure else 80)}"
        self.host = u.hostname
        self.token = token
        self.proxy = proxy
        self._call = None

    def run(self, stream):
        # gRPC's C core logs every failed handshake to the console; failures are reported in the stream's status
        os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
        import grpc  # optional dependency: only needed when SOLANA_GRPC_URL is set
        opts = [("grpc.max_receive_message_length", 64 * 1024 * 1024), ("grpc.keepalive_time_ms", 15000),
                ("grpc.keepalive_timeout_ms", 10000), ("grpc.keepalive_permit_without_calls", 1),
                ("grpc.http2.max_pings_without_data", 0)]
        if self.proxy:
            opts.append(("grpc.http_proxy", f"http://{self.proxy[0]}:{self.proxy[1]}"))
        ch = (grpc.secure_channel(self.target, grpc.ssl_channel_credentials(_system_roots()), opts) if self.secure
              else grpc.insecure_channel(self.target, opts))
        out, done = queue.Queue(), threading.Event()
        out.put(subscribe_request())

        def requests():
            while not done.is_set():
                try:
                    yield out.get(timeout=1.0)
                except queue.Empty:
                    continue
        md = [("x-token", self.token)] if self.token else None
        try:
            self._call = call = ch.stream_stream(GRPC_METHOD, request_serializer=None,
                                                 response_deserializer=None)(requests(), metadata=md)
            pings, last_ping = 0, time.time()
            for msg in call:
                stream.bytes_in += len(msg)
                kind = parse_update(msg)
                if kind[0] == "tx":
                    stream.on_logs(kind[1], kind[2], kind[3])
                elif kind[0] == "ping":  # providers drop clients that don't answer their pings
                    pings += 1
                    out.put(ping_request(pings))
                    last_ping = time.time()
                if time.time() - last_ping > 15:  # and some expect the client to ping on its own
                    pings += 1
                    out.put(ping_request(pings))
                    last_ping = time.time()
                if stream.stopping.is_set():
                    break
        finally:
            done.set()
            self._call = None
            ch.close()
        if not stream.stopping.is_set():
            raise ConnectionError("gRPC stream ended")

    def close(self):
        call = self._call
        if call is not None:
            call.cancel()


class WsSource:
    """Solana websocket logsSubscribe (free RPCs offer it). A minimal RFC 6455 client on the standard library,
    through the HTTPS proxy when there is one. run() blocks for the life of one connection."""
    name = "websocket"

    def __init__(self, url, proxy=None):
        self.url = url
        u = urllib.parse.urlsplit(url)
        self.secure = u.scheme == "wss"
        self.host, self.port = u.hostname, u.port or (443 if self.secure else 80)
        self.path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.proxy = proxy
        self.sock = None
        self._buf = bytearray()

    def _connect(self):
        if self.proxy:
            s = socket.create_connection(self.proxy, timeout=20)
            s.sendall(f"CONNECT {self.host}:{self.port} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n\r\n".encode())
            head = self._read_head(s)
            if b" 200" not in head.split(b"\r\n", 1)[0]:
                raise ConnectionError(f"proxy refused: {head[:80]!r}")
        else:
            s = socket.create_connection((self.host, self.port), timeout=20)
        if self.secure:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: pons-paper-trader\r\n\r\n").encode())
        status = self._read_head(s).split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"websocket upgrade refused: {status[:80]!r}")
        s.settimeout(30)
        self.sock = s

    def _read_head(self, s):
        self._buf = bytearray()
        while b"\r\n\r\n" not in self._buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during handshake")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise ConnectionError("handshake too long")
        i = self._buf.index(b"\r\n\r\n") + 4
        head, self._buf = bytes(self._buf[:i]), self._buf[i:]
        return head

    def _recv(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self._buf += chunk
        out, self._buf = bytes(self._buf[:n]), self._buf[n:]
        return out

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def _frame(self):
        b0, b1 = self._recv(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv(8))[0]
        mask = self._recv(4) if b1 & 0x80 else None
        data = self._recv(n)
        if mask:
            data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
        return b0 & 0x80, b0 & 0x0F, data

    def run(self, stream):
        self._connect()
        try:
            sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                   "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}]}
            self._send(1, json.dumps(sub).encode())
            parts, idle = [], 0
            while not stream.stopping.is_set():
                try:
                    fin, op, data = self._frame()
                    idle = 0
                except socket.timeout:
                    idle += 1
                    if idle >= 2:
                        raise ConnectionError("websocket silent for 60 s")
                    self._send(9, b"keepalive")
                    continue
                if op == 9:
                    self._send(10, data)
                    continue
                if op == 8:
                    raise ConnectionError("websocket closed by the server")
                if op not in (0, 1, 2):
                    continue
                parts.append(data)
                if not fin:
                    continue
                raw, parts = b"".join(parts), []
                stream.bytes_in += len(raw)
                msg = json.loads(raw)
                if "params" in msg:
                    res = msg["params"].get("result") or {}
                    v = res.get("value") or {}
                    if v.get("err") is None:
                        stream.on_logs((res.get("context") or {}).get("slot", 0), v.get("logs") or [], None)
                elif msg.get("error"):
                    raise ConnectionError(f"logsSubscribe refused: {msg['error']}")
        finally:
            self.close()

    def close(self):
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


class FallbackSource:
    """gRPC first. When it fails `tries` times in a row without delivering anything (an endpoint this network can't
    reach, e.g. through a proxy that doesn't speak HTTP/2, a bad token, no grpcio), the websocket takes over for `hold`
    seconds, then gRPC is tried again. The trader is never left without a stream because gRPC is down."""

    def __init__(self, primary, fallback, tries=3, hold=1800.0):
        self.primary, self.fallback = primary, fallback
        self.tries, self.hold = tries, hold
        self.current = primary
        self.fails = 0
        self.until = 0.0
        self.note = None  # why the fallback is in use

    @property
    def name(self):
        return self.current.name

    @property
    def host(self):
        return self.current.host

    def run(self, stream):
        if self.current is self.fallback and time.time() >= self.until:
            self.current, self.fails = self.primary, 0
        src, seen = self.current, stream.events
        try:
            src.run(stream)
        except Exception as e:
            if src is self.primary:
                self.fails = self.fails + 1 if stream.events == seen else 0
                if self.fails >= self.tries:
                    self.current, self.until = self.fallback, time.time() + self.hold
                    msg = str(getattr(e, "details", lambda: None)() or e).split("\n")[0][:200]
                    self.note = f"gRPC failed {self.fails}x ({msg}); websocket until {time.strftime('%H:%M', time.localtime(self.until))}"
            raise
        finally:
            if self.current is self.primary and stream.events > seen:
                self.note = None

    def close(self):
        self.current.close()


# ---------------------------------------------------------------------------------------------------- stream
class TradeStream:
    """Runs a source on its own thread (reconnecting forever) and keeps, for the engine:
    - curves: mint -> the curve after its latest trade (Q, T, sellable, raised, feeBps, t, slot, epoch). While the
      stream has stayed connected since that trade, this *is* the curve's current state: nothing changed it, or we'd
      know.
    - hist: mint -> the curve after each of its trades in the last HIST_SEC, with their slots, so an order can be
      priced on the curve as it stood in the slot it landed in (landing_state).
    - feed_lag: how far the stream runs behind the chain. An order landing at time t meets the chain as it was at t,
      which the stream only shows feed_lag later.
    - the events since the engine last drained them, stamped with the time they reached us."""

    def __init__(self, source):
        self.source = source
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.curves = {}
        self._trades, self._creates, self._completes = [], [], []
        self._gap = True          # the next drain starts a new stretch of coverage (start-up, reconnects)
        self.epoch = 0            # bumped on every (re)connect: curve states from older epochs may be stale
        self.connected = False
        self.connected_at = None
        self.last_event_at = None
        self.events = self.trades_seen = self.reconnects = self.errors = self.bytes_in = 0
        self.last_error = None
        self.lag = None           # EMA of arrival time minus the event's block time (1 s resolution): health checks
        self.lag_drops = 0        # reconnects because the stream fell more than MAX_LAG_SEC behind
        self._lags = deque(maxlen=3000)
        self._lag_at = 0.0
        self.feed_lag = None      # seconds behind the chain: median(arrival - block time) - 0.5 s (block times are
        #                           whole seconds, rounded down), over the last few thousand trades
        self.max_slot = 0         # newest slot the stream has delivered
        self._slot_marks = deque(maxlen=120)  # (time, newest slot) about once a second, to measure the slot time
        self.slot_sec = None      # measured seconds per slot
        self.hist = {}
        self._hist_pruned = 0.0
        self._rate = (time.time(), 0)
        self.rate = None          # trades per second over the last minute or so
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="pumpstream", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.source.close()

    def _run(self):
        backoff = 1.0
        while not self.stopping.is_set():
            seen = self.events
            with self.lock:
                self.epoch += 1
                self._gap = True
                self.lag = None
                self.hist = {}
                self._slot_marks.clear()
            try:
                self.source.run(self)
            except Exception as e:
                self.errors += 1
                self.last_error = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {e!r}"
            with self.lock:
                self.connected = False
                self._gap = True
            if self.stopping.is_set():
                break
            self.reconnects += 1
            backoff = 1.0 if self.events > seen else min(60.0, backoff * 2)
            self.stopping.wait(backoff)

    def on_logs(self, slot, logs, created_at=None):
        if not self.connected:
            self.connected, self.connected_at = True, time.time()
        evs = events_from_logs(logs)
        if not evs:
            return
        now = time.time()
        with self.lock:
            for e in evs:
                e["t"], e["slot"] = now, slot
                self.events += 1
                kind = e["kind"]
                if slot > self.max_slot:
                    self.max_slot = slot
                if kind == TRADE:
                    self.trades_seen += 1
                    self.curves[e["mint"]] = {"Q": e["Q"], "T": e["T"], "sellable": e["sellable"], "raised": e["raised"],
                                              "feeBps": e["feeBps"], "t": now, "slot": slot, "epoch": self.epoch}
                    self._trades.append(e)
                    self.hist.setdefault(e["mint"], deque()).append((now, slot, e["Q"], e["T"], e["sellable"], e["feeBps"]))
                    lag = now - e["ts"]
                    if -5 < lag < 600:
                        self.lag = lag if self.lag is None else 0.98 * self.lag + 0.02 * lag
                        self._lags.append(lag)
                elif kind == CREATE:
                    self.curves[e["mint"]] = {"Q": e["Q"], "T": e["T"], "sellable": e["sellable"], "raised": 0.0,
                                              "feeBps": None, "t": now, "slot": slot, "epoch": self.epoch}
                    self.hist.setdefault(e["mint"], deque()).append((now, slot, e["Q"], e["T"], e["sellable"], None))
                    self._creates.append(e)
                else:
                    self.curves[e["mint"]] = {"complete": True, "t": now, "slot": slot, "epoch": self.epoch}
                    self.hist.pop(e["mint"], None)
                    self._completes.append(e)
            self.last_event_at = now
            if now - self._lag_at >= 2.0 and len(self._lags) >= 50:
                self._lag_at = now
                self.feed_lag = max(0.0, statistics.median(self._lags) - 0.5)
            if not self._slot_marks or now - self._slot_marks[-1][0] >= 1.0:
                self._slot_marks.append((now, self.max_slot))
                (t0, s0), (t1, s1) = self._slot_marks[0], self._slot_marks[-1]
                if t1 - t0 >= 20 and s1 > s0:
                    self.slot_sec = min(1.0, max(0.05, (t1 - t0) / (s1 - s0)))
            if now - self._hist_pruned >= 5.0:  # keep HIST_SEC per coin, plus the trade before (the curve then)
                self._hist_pruned = now
                cut = now - HIST_SEC
                for m in [m for m, h in self.hist.items() if h[-1][0] < cut]:
                    del self.hist[m]
                for h in self.hist.values():
                    while len(h) > 1 and h[1][0] < cut:
                        h.popleft()
            t0, n0 = self._rate
            if now - t0 >= 60:
                self.rate = (self.trades_seen - n0) / (now - t0)
                self._rate = (now, self.trades_seen)
            if len(self._trades) > 200000:  # the engine stopped draining: don't grow without bound
                del self._trades[:100000]
                self._gap = True
        if self.lag is not None and self.lag > MAX_LAG_SEC + 1.0:  # hopelessly behind: start afresh
            self.lag_drops += 1
            self.connected = False
            raise ConnectionError(f"stream fell {self.lag:.1f} s behind the chain")

    def healthy(self, now=None, quiet=20.0):
        """Connected, events arriving, and close enough to the chain that its curve states are current."""
        now = now or time.time()
        return (self.connected and self.last_event_at is not None and now - self.last_event_at < quiet
                and (self.lag is None or self.lag < MAX_LAG_SEC))

    def curve(self, mint):
        """The coin's curve right now, if the stream has watched it continuously since its latest trade."""
        st = self.curves.get(mint)
        if st is None or st["epoch"] != self.epoch or not self.healthy():
            return None
        return st

    def landing_state(self, mint, land_slot, rng):
        """The coin's curve as an order landing in `land_slot` met it: after every trade in earlier slots, and after
        a random number of the trades in its own slot (where in the slot a transaction lands isn't up to the
        sender). -> {"Q", "T", "sellable", "feeBps", "before", "inSlot"} or None if unknown."""
        with self.lock:
            cur = self.curves.get(mint)
            h = list(self.hist.get(mint) or ())
            epoch = self.epoch
        if cur is None or cur.get("complete") or cur["epoch"] != epoch:
            return None
        prev, same = None, []
        for e in h:
            if e[1] < land_slot:
                prev = e
            elif e[1] == land_slot:
                same.append(e)
        k = rng.randint(0, len(same)) if same else 0
        e = same[k - 1] if k else prev
        if e is None:
            if cur["slot"] >= land_slot:  # it traded at or after our slot and its history doesn't reach back
                return None
            e = (cur["t"], cur["slot"], cur["Q"], cur["T"], cur["sellable"], cur.get("feeBps"))  # unchanged since
        return {"Q": e[2], "T": e[3], "sellable": e[4], "feeBps": e[5] if e[5] is not None else cur.get("feeBps"),
                "before": k, "inSlot": len(same)}

    def drain(self):
        """-> (trades, creates, completes, gap) since the previous call. gap: coverage restarted in between."""
        with self.lock:
            out = (self._trades, self._creates, self._completes, self._gap)
            self._trades, self._creates, self._completes, self._gap = [], [], [], False
        return out

    def prune(self, keep=(), max_idle=3600.0):
        now = time.time()
        with self.lock:
            self.curves = {m: s for m, s in self.curves.items() if now - s["t"] < max_idle or m in keep}

    def view(self):
        now = time.time()
        return {"source": self.source.name, "host": getattr(self.source, "host", None),
                "connected": self.connected, "healthy": self.healthy(now),
                "connectedFor": (now - self.connected_at) if self.connected and self.connected_at else None,
                "lastEventAgo": (now - self.last_event_at) if self.last_event_at else None,
                "events": self.events, "trades": self.trades_seen, "tradesPerSec": self.rate,
                "lagSec": self.feed_lag if self.feed_lag is not None else self.lag, "slot": self.max_slot,
                "slotMs": self.slot_sec * 1000 if self.slot_sec else None,
                "reconnects": self.reconnects, "lagDrops": self.lag_drops, "errors": self.errors,
                "lastError": self.last_error, "mbIn": self.bytes_in / 1e6, "curves": len(self.curves),
                "note": getattr(self.source, "note", None)}


def ws_url():
    """SOLANA_WS_URL, else the websocket twin of SOLANA_RPC_URL, else the public endpoint."""
    if os.environ.get("SOLANA_WS_URL"):
        return os.environ["SOLANA_WS_URL"]
    rpc = os.environ.get("SOLANA_RPC_URL")
    if rpc and rpc.startswith(("https://", "http://")):
        return "ws" + rpc[4:]
    return PUBLIC_WS


def open_stream(cfg, proxy=None):
    """-> TradeStream (not started) or None. cfg: the engine's "stream" settings. source "auto" = gRPC when
    SOLANA_GRPC_URL is set (the websocket while gRPC can't connect), else the websocket; "grpc" / "ws" force one;
    enabled False = no stream."""
    if not cfg.get("enabled"):
        return None
    want = (cfg.get("source") or "auto").lower()
    grpc_url = os.environ.get("SOLANA_GRPC_URL")
    if want == "grpc":
        return TradeStream(GrpcSource(grpc_url, os.environ.get("SOLANA_GRPC_TOKEN"), proxy)) if grpc_url else None
    if want == "auto" and grpc_url:
        return TradeStream(FallbackSource(GrpcSource(grpc_url, os.environ.get("SOLANA_GRPC_TOKEN"), proxy),
                                          WsSource(ws_url(), proxy)))
    return TradeStream(WsSource(ws_url(), proxy))
