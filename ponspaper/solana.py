"""Solana reads for pump.fun coins (PONS_VENUE=pumpfun), so fills and marks use the state a real transaction
would meet, not the pump.fun API's copy of it (which can lag a busy coin by several percent).

Bonding curve: the curve account's virtual/real reserves and `complete` flag. The pump program prices with
constant product on the virtual reserves plus 0.95% protocol + 0.30% creator fee on the SOL side; that reproduces
real on-chain buys to the token. A whole batch is one getMultipleAccounts call (up to 100 accounts).

Graduated coins are not read here: PumpSwap trades don't match constant product on the pool's vault balances
(checked against real swaps), so those fills use a fresh pump.fun API read instead.

The RPC endpoint is SOLANA_RPC_URL (e.g. a free Helius/QuickNode URL) or the public mainnet RPC. The URL is never
stored in the config or shown on the dashboard, since it usually carries an API key.
"""
import base64
import os
import struct
import urllib.parse

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_DECIMALS, SOL_DECIMALS = 6, 9


def rpc_url():
    return os.environ.get("SOLANA_RPC_URL") or PUBLIC_RPC


class SolanaChain:
    def __init__(self, client, url):
        self.client = client
        self.url = url
        self.host = urllib.parse.urlsplit(url).hostname  # safe to display (no path / key)
        self.calls = 0
        self.failures = 0

    def _accounts(self, keys):
        """[pubkey] -> {pubkey: (owner, data bytes)}; missing accounts are left out."""
        out = {}
        keys = list(dict.fromkeys(k for k in keys if k))
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            self.calls += 1
            try:
                r = self.client.post_json(self.url, {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                                                     "params": [chunk, {"encoding": "base64", "commitment": "processed"}]})
            except Exception:
                self.failures += 1
                raise
            if "error" in r:
                self.failures += 1
                raise RuntimeError(f"getMultipleAccounts: {r['error']}")
            for key, acc in zip(chunk, (r.get("result") or {}).get("value") or []):
                if acc and acc.get("data"):
                    out[key] = (acc.get("owner"), base64.b64decode(acc["data"][0]))
        return out

    def states(self, tokens):
        """tokens: pump.fun token dicts -> {address: state} for SOL-paired coins still on the bonding curve:
        {"kind": "curve", "Q": virtual SOL, "T": virtual tokens, "sellable": real tokens, "raised": real SOL}.
        Coins whose curve is complete (graduated or migrating) are left out."""
        toks = [d for d in tokens if (d.get("quote") or {}).get("symbol") == "SOL" and d.get("curve")
                and d.get("stage") != "graduated"]
        if not toks:
            return {}
        accs = self._accounts([d["curve"] for d in toks])
        out = {}
        for d in toks:
            acc = accs.get(d["curve"])
            if not acc or acc[0] != PUMP_PROGRAM or len(acc[1]) < 49:
                continue
            vt, vs, rt, rs, _supply = struct.unpack_from("<5Q", acc[1], 8)
            if acc[1][48] or not vt or not vs:  # complete: trading has moved to the pool
                continue
            out[d["address"]] = {"kind": "curve", "Q": vs / 10 ** SOL_DECIMALS, "T": vt / 10 ** TOKEN_DECIMALS,
                                 "sellable": rt / 10 ** TOKEN_DECIMALS, "raised": rs / 10 ** SOL_DECIMALS}
        return out
