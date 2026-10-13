"""Solana reads for pump.fun coins (PONS_VENUE=pumpfun), so fills and marks use the state a real transaction
would meet, not the pump.fun API's copy of it (which can lag a busy coin by several percent).

Bonding curve: the curve account's virtual/real reserves and `complete` flag. The pump program prices with
constant product on the virtual reserves plus 0.95% protocol + 0.30% creator fee on the SOL side; that reproduces
real on-chain buys to the token. A whole batch is one getMultipleAccounts call (up to 100 accounts).

Graduated coins: their PumpSwap pool (pool_states). PumpSwap prices with constant product on (quote vault + the
pool's virtual quote reserve, base vault); read that way, the pool matches the state its own trade events report
exactly (checked live), where the vault balances alone are 10-25% off.

The RPC endpoint is SOLANA_RPC_URL (e.g. a free Helius/QuickNode URL) or the public mainnet RPC. The URL is never
stored in the config or shown on the dashboard, since it usually carries an API key.
"""
import base64
import os
import struct
import urllib.parse

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
TOKEN_DECIMALS, SOL_DECIMALS = 6, 9


def _b58(b):
    from .pumpstream import b58
    return b58(bytes(b))


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

    def pool_states(self, tokens):
        """tokens: graduated pump.fun token dicts with a poolAddress -> {address: {"kind": "pool", "Q": quote vault +
        virtual quote reserve (SOL), "T": base vault (tokens)}}. Two RPC calls: the pools, then their vaults. Pool
        layout (pump_amm IDL, after the 8-byte discriminator): pool_bump u8, index u16, creator, base_mint,
        quote_mint, lp_mint, pool_base_token_account @139, pool_quote_token_account @171, lp_supply, coin_creator,
        is_mayhem_mode, is_cashback_coin, virtual_quote_reserves i128 @245."""
        toks = [d for d in tokens if d.get("stage") == "graduated" and d.get("poolAddress")
                and (d.get("quote") or {}).get("symbol") == "SOL"]
        if not toks:
            return {}
        pools = self._accounts([d["poolAddress"] for d in toks])
        vaults = {}
        for d in toks:
            acc = pools.get(d["poolAddress"])
            if acc and acc[0] == AMM_PROGRAM and len(acc[1]) >= 261:
                lo, hi = struct.unpack_from("<Qq", acc[1], 245)
                vaults[d["address"]] = (_b58(acc[1][139:171]), _b58(acc[1][171:203]), lo + (hi << 64))
        if not vaults:
            return {}
        bal = self._accounts([k for v in vaults.values() for k in v[:2]])
        out = {}
        for addr, (base_v, quote_v, vq) in vaults.items():
            b, q = bal.get(base_v), bal.get(quote_v)
            if not b or not q or len(b[1]) < 72 or len(q[1]) < 72:
                continue
            base = struct.unpack_from("<Q", b[1], 64)[0]   # token account: mint, owner, amount
            quote = struct.unpack_from("<Q", q[1], 64)[0]
            if base and quote + vq > 0:
                out[addr] = {"kind": "pool", "Q": (quote + vq) / 10 ** SOL_DECIMALS, "T": base / 10 ** TOKEN_DECIMALS}
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
