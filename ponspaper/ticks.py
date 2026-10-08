"""Tick data: every individual pons bonding-curve trade, read from Robinhood Chain event logs.

One eth_getLogs query per poll (no contract address, filtered by the two event topics) returns every buy and
sell on every pons curve since the previous poll, with exact block timestamps.

Event layouts (decoded from live logs and checked against the curve's fee maths):
  Buy:  topics = [BUY_TOPIC,  sender, recipient]   data = quoteIn (gross), tokensOut, protocolFee, creatorTax
  Sell: topics = [SELL_TOPIC, sender, recipient]   data = tokensIn, quoteOut (net), protocolFee, creatorTax
The wallet that ends up holding tokens is the recipient of a buy and the sender of a sell; a buy whose
recipient is the zero address is a protocol buyback and is not counted as a buyer.
"""
BUY_TOPIC = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
SELL_TOPIC = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
ZERO40 = "0" * 40

import time  # noqa: E402


def decode(log):
    """-> dict(curve, t, side, quote_raw, wallet, key) or None."""
    topics = log.get("topics") or []
    if len(topics) < 3 or topics[0] not in (BUY_TOPIC, SELL_TOPIC):
        return None
    data = (log.get("data") or "0x")[2:]
    words = [int(data[i:i + 64], 16) for i in range(0, len(data) - 63, 64)]
    if len(words) < 2:
        return None
    buy = topics[0] == BUY_TOPIC
    sender, recipient = topics[1][-40:], topics[2][-40:]
    wallet = recipient if buy else sender
    ts = log.get("blockTimestamp")
    return {"curve": (log.get("address") or "").lower(), "t": int(ts, 16) if ts else None,
            "side": 1 if buy else -1, "quote_raw": words[0] if buy else words[1],
            "wallet": None if wallet == ZERO40 else wallet[:16],
            "key": (log.get("transactionHash"), log.get("logIndex")), "block": int(log.get("blockNumber", "0x0"), 16)}


class TickFeed:
    def __init__(self, client, rpc_url, origin="https://ponsfamily.com", start_back_blocks=300, max_catchup=3000):
        self.client = client
        self.rpc_url = rpc_url
        self.headers = {"Origin": origin, "Referer": origin + "/"}
        self.next_block = None
        self.start_back = start_back_blocks
        self.max_catchup = max_catchup
        self.seen = {}           # recent (tx, logIndex) -> block, to drop duplicates between polls
        self.calls = self.errors = self.gaps = self.received = 0
        self.last_error = None
        self.last_block_ts = None
        self.last_ok = 0.0

    def fetch(self):
        """New trades since the previous call -> (ticks, gap). gap=True means blocks were skipped (after an
        outage only the last `max_catchup` blocks are fetched), so rolling windows are no longer complete."""
        gap = False
        if self.next_block is None or time.time() - self.last_ok > 240:
            head = self._block_number()
            oldest = head - (self.start_back if self.next_block is None else self.max_catchup)
            if self.next_block is not None and self.next_block < oldest:
                gap = True
                self.gaps += 1
            frm = max(0, oldest if self.next_block is None else max(self.next_block, oldest))
        else:
            frm = self.next_block
        body = [{"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                {"jsonrpc": "2.0", "id": 2, "method": "eth_getLogs",
                 "params": [{"fromBlock": hex(frm), "toBlock": "latest", "topics": [[BUY_TOPIC, SELL_TOPIC]]}]}]
        self.calls += 1
        res = self.client.post_json(self.rpc_url, body, headers=self.headers)
        res = {r.get("id"): r for r in (res if isinstance(res, list) else [res])}
        if "result" not in res.get(1, {}) or "result" not in res.get(2, {}):
            err = (res.get(2) or res.get(1) or {}).get("error")
            raise RuntimeError(f"getLogs failed: {err}")
        head = int(res[1]["result"], 16)
        ticks = []
        for lg in res[2]["result"]:
            tk = decode(lg)
            if tk is None or tk["key"] in self.seen:
                continue
            self.seen[tk["key"]] = tk["block"]
            ticks.append(tk)
        self.next_block = max(frm, head + 1)
        self.last_ok = time.time()
        if len(self.seen) > 20000:  # forget keys for blocks we'll never query again
            cut = self.next_block - 50
            self.seen = {k: b for k, b in self.seen.items() if b >= cut}
        stamps = [t["t"] for t in ticks if t["t"]]
        if stamps:
            self.last_block_ts = max(stamps)
        self.received += len(ticks)
        return ticks, gap

    def _block_number(self):
        r = self.client.post_json(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                                  headers=self.headers)
        return int(r["result"], 16)

    def skip_to_head(self):
        """After an outage: don't try to replay a huge range, start fresh from (almost) now."""
        self.next_block = None
