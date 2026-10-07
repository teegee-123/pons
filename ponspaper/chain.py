"""On-chain reads for pons launches on Robinhood Chain (chain id 4663).

Used at fill time so paper fills match what a real transaction would have received:
  * bonding curve: getReserves()/sellableTokens()/feeBps() on the curve contract, then the same
    constant-product math the pons frontend uses (fee + creator tax taken from quote in/out);
  * graduated: Uniswap v4 quoter (quoteExactInputSingle), which includes the hook fee and creator tax.
Contract addresses and ABIs are taken from the ponsfamily.com frontend bundle.
"""
import threading

# ---- keccak-256 (Ethereum flavour), only used to derive function selectors ----
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _f(A):
    for rc in _RC:
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        A = [[A[x][y] ^ D[x] for y in range(5)] for x in range(5)]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
        A = [[B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        A[0][0] ^= rc
    return A


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data)
    p.append(1)
    while len(p) % rate:
        p.append(0)
    p[-1] |= 0x80
    A = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        for i in range(rate // 8):
            A[i % 5][i // 5] ^= int.from_bytes(p[off + 8 * i: off + 8 * i + 8], "little")
        A = _f(A)
    return b"".join(A[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def selector(sig):
    return keccak256(sig.encode()).hex()[:8]


S_GET_RESERVES = selector("getReserves()")
S_SELLABLE = selector("sellableTokens()")
S_FEE_BPS = selector("feeBps()")
S_GRADUATED = selector("graduated()")
S_READY = selector("readyToGraduate()")
S_GET_LAUNCHED = selector("getLaunchedToken(address)")
S_QUOTE_EXACT_IN = selector("quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))")

QUOTER = "0xe202BB8dd524eE9C5E679e5B5809f7A373a982Ef"
HOOKS = {  # factory -> v4 hook (from the frontend bundle)
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e": "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044",
    "0xd3c2280f23d813be8f9a6b5452753efff7799fd7": "0x8E397FA55822437BCA19cEBfe5aF9B23B09D2044",
    "0x7e1eabd52ae29598e6483f72dcf1a70b14284db8": "0x8e99D2009D60A917e9B1c00C04C077b8c0c3a044",
    "0x050e5c224466e2d377a7e555e139d51268239b39": "0x107251FFCC1fc808643DC8dA345e901f59EC2044",
}
ZERO = "0x0000000000000000000000000000000000000000"


def _w(x):
    return format(x, "064x")


def _a(addr):
    return addr[2:].lower().rjust(64, "0")


def _words(hexstr):
    h = hexstr[2:] if hexstr and hexstr.startswith("0x") else (hexstr or "")
    return [int(h[i:i + 64], 16) for i in range(0, len(h) - 63, 64)]


class Chain:
    def __init__(self, client, rpc_url, origin="https://ponsfamily.com"):
        self.client = client
        self.rpc_url = rpc_url
        self.headers = {"Origin": origin, "Referer": origin + "/"}
        self._fee_cache = {}
        self._key_cache = {}
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0

    def _batch(self, calls):
        """calls: [(to, data)] -> [hex result or None]."""
        if not calls:
            return []
        body = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": "0x" + data}, "latest"]}
                for i, (to, data) in enumerate(calls)]
        self.calls += 1
        try:
            res = self.client.post_json(self.rpc_url, body, headers=self.headers)
        except Exception:
            self.failures += 1
            raise
        if isinstance(res, dict):
            res = [res]
        out = [None] * len(calls)
        for r in res:
            i = r.get("id")
            if isinstance(i, int) and 0 <= i < len(out) and "result" in r:
                out[i] = r["result"]
        return out

    def curve_states(self, curves):
        """curves: list of curve addresses -> {curve: dict(Q, T, sellable, fee_bps, graduated, ready)} (raw ints)."""
        curves = list(dict.fromkeys(c.lower() for c in curves))
        calls = []
        for c in curves:
            calls += [(c, S_GET_RESERVES), (c, S_SELLABLE), (c, S_GRADUATED), (c, S_READY)]
            if c not in self._fee_cache:
                calls.append((c, S_FEE_BPS))
        res = self._batch(calls)
        out, i = {}, 0
        for c in curves:
            r = _words(res[i]) if res[i] else None
            sell = _words(res[i + 1]) if res[i + 1] else None
            grad = _words(res[i + 2]) if res[i + 2] else [0]
            ready = _words(res[i + 3]) if res[i + 3] else [0]
            i += 4
            if c not in self._fee_cache:
                fee = _words(res[i]) if res[i] else None
                i += 1
                if fee:
                    self._fee_cache[c] = fee[0]
            if r and len(r) >= 2 and sell:
                out[c] = {"Q": r[0], "T": r[1], "sellable": sell[0], "fee_bps": self._fee_cache.get(c),
                          "graduated": bool(grad and grad[0]), "ready": bool(ready and ready[0])}
        return out

    def pool_key(self, token, factory, quote_addr):
        token = token.lower()
        if token in self._key_cache:
            return self._key_cache[token]
        hook = HOOKS.get((factory or "").lower())
        if not hook:
            return None
        res = self._batch([(factory, S_GET_LAUNCHED + _a(token))])
        w = _words(res[0]) if res and res[0] else []
        if len(w) < 15 or not w[14]:
            return None
        pool_fee, tick = w[6], w[7]
        if tick >= 1 << 255:
            tick -= 1 << 256
        q = (quote_addr or ZERO).lower()
        c0, c1 = (q, token) if int(q, 16) < int(token, 16) else (token, q)
        key = (c0, c1, pool_fee, tick, hook)
        self._key_cache[token] = key
        return key

    def pool_quotes(self, reqs):
        """reqs: [(key, token, side, amount_in_raw)] -> [amount_out_raw or None]."""
        calls = []
        for key, token, side, amt in reqs:
            c0, c1, fee, tick, hook = key
            token_is_c0 = c0 == token.lower()
            zero_for_one = token_is_c0 if side == "sell" else not token_is_c0
            tick_enc = tick if tick >= 0 else tick + (1 << 256)
            data = (S_QUOTE_EXACT_IN + _w(32) + _a(c0) + _a(c1) + _w(fee) + _w(tick_enc) + _a(hook)
                    + _w(1 if zero_for_one else 0) + _w(int(amt)) + _w(0x100) + _w(0))
            calls.append((QUOTER, data))
        res = self._batch(calls)
        return [(_words(r)[0] if r and _words(r) else None) for r in res]
