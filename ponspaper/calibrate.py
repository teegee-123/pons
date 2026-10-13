"""Calibrate the pump.fun simulator with real orders: small real round trips from your own wallet, each measured
against what the paper trader assumes, so latency, drops and costs come from your own setup instead of estimates.

Run by you, on your PC (never by the paper trader or on Render):

  python -m ponspaper calibrate --keypair C:\\path\\wallet.json
      dry run (the default): picks a coin, builds the exact buy and sell transactions and has a Solana node simulate
      them. Nothing is signed or sent: the transactions carry a blank signature and are simulated with signature
      checks off (a signed one sent "just to simulate" could be broadcast by the node).
  python -m ponspaper calibrate --keypair C:\\path\\wallet.json --live --size-sol 0.02 --trades 10
      real round trips: asks you to type YES first.

Each round trip picks an actively traded standard bonding-curve coin, buys --size-sol worth (buy_exact_sol_in, with
the --slippage limit the paper trader uses), holds --hold seconds, then sells everything and closes the token
account (its rent comes back). For every order it records:
  - landing: when the transaction landed (its trade arriving on the chain stream, less the stream's lag) after it was
    signed and sent, and in which slot;
  - resends: the same signed transaction is sent again every second until it lands (20 s at most), so slow and
    dropped transactions show up;
  - failures, with the program's error (e.g. the slippage limit);
  - cost: the fee paid (getTransaction); the priority fee offered is the --fee-pct percentile of what recent pump.fun
    trades paid;
  - price: tokens / SOL received vs the paper trader's pricing on the curve right before our trade in its slot (the
    pricing check: should be ~0) and vs the curve when the order was signed (how far the price moved while landing).
The summary suggests Latency, Latency jitter and Dropped transactions settings and is written to calibration.json.

Safety: --size-sol is capped at 0.1 SOL and --trades at 50; it stops when the wallet is down more than --max-loss-sol
(0.05 by default) from the start. The keypair file is only read to sign; it is never copied, printed or sent.
Requires `pip install solders`.
"""
import base64
import json
import os
import random
import statistics
import struct
import time

from . import pumpstream as P
from .net import Client
from .pumpfun import CURVE_TOKENS, STD_CURVE_K
from .sim import Venue
from .solana import rpc_url

SYSTEM = "11111111111111111111111111111111"
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
FEE_CONFIG_SEED = bytes.fromhex("0156e0f693665acf44db1568bf175baa5189cb97f5d2ff3b655d2bb6fd6d18b0")  # pump IDL
BUY_EXACT_SOL_IN = bytes.fromhex("38fc74089edfcd5f")
SELL = bytes.fromhex("33e685a4017f83ad")
GLOBAL_FEE_RECIPIENT = 41   # offset of fee_recipient in pump's Global account
GLOBAL_BUYBACK = 741        # offset of buyback_fee_recipients (8 pubkeys) in pump's Global account
CU_BUY, CU_SELL = 150_000, 120_000
MAX_SIZE_SOL, MAX_TRADES = 0.1, 50


class Rpc:
    def __init__(self, client, url):
        self.client, self.url = client, url

    def call(self, method, params):
        r = self.client.post_json(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r["result"]

    def accounts(self, keys):
        vals = self.call("getMultipleAccounts", [[str(k) for k in keys], {"encoding": "base64", "commitment": "processed"}])["value"]
        return [(v["owner"], base64.b64decode(v["data"][0]), v["lamports"]) if v else None for v in vals]

    def blockhash(self):
        return self.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]


class Pump:
    """pump.fun buy / sell transactions (accounts and PDAs from the pump IDL)."""

    def __init__(self, rpc):
        from solders.pubkey import Pubkey
        self.Pk = Pubkey
        self.rpc = rpc
        self.program = Pubkey.from_string(P.PUMP_PROGRAM)
        pda = lambda seeds, prog=None: Pubkey.find_program_address(seeds, prog or self.program)[0]  # noqa: E731
        self.pda = pda
        self.global_ = pda([b"global"])
        self.event_authority = pda([b"__event_authority"])
        self.global_volume = pda([b"global_volume_accumulator"])
        self.fee_program = Pubkey.from_string(FEE_PROGRAM)
        self.fee_config = pda([b"fee_config", FEE_CONFIG_SEED], self.fee_program)
        g = rpc.accounts([self.global_])[0]
        if g is None:
            raise RuntimeError("pump.fun global account not found")
        self.fee_recipient = Pubkey.from_bytes(g[1][GLOBAL_FEE_RECIPIENT:GLOBAL_FEE_RECIPIENT + 32])
        self.buyback = [Pubkey.from_bytes(g[1][GLOBAL_BUYBACK + 32 * i:GLOBAL_BUYBACK + 32 * (i + 1)]) for i in range(8)]

    def coin(self, mint):
        """-> dict(mint, curve, assoc_curve, token_program, creator_vault) read from chain, or None."""
        Pk = self.Pk
        mint_pk = Pk.from_string(mint)
        curve = self.pda([b"bonding-curve", bytes(mint_pk)])
        acc_curve, acc_mint = self.rpc.accounts([curve, mint_pk])
        if acc_curve is None or acc_mint is None or acc_curve[0] != P.PUMP_PROGRAM or len(acc_curve[1]) < 81:
            return None
        if acc_curve[1][48]:  # complete: it trades on PumpSwap now
            return None
        token_program = Pk.from_string(acc_mint[0])
        creator = Pk.from_bytes(acc_curve[1][49:81])
        return {"mint": mint_pk, "curve": curve, "token_program": token_program,
                "assoc_curve": self.ata(curve, mint_pk, token_program),
                "creator_vault": self.pda([b"creator-vault", bytes(creator)])}

    def ata(self, owner, mint, token_program):
        return self.Pk.find_program_address([bytes(owner), bytes(token_program), bytes(mint)],
                                            self.Pk.from_string(ATA_PROGRAM))[0]

    def _metas(self, c, user, buy):
        from solders.instruction import AccountMeta as M
        Pk = self.Pk
        head = [M(self.global_, False, False), M(self.fee_recipient, False, True), M(c["mint"], False, False),
                M(c["curve"], False, True), M(c["assoc_curve"], False, True),
                M(self.ata(user, c["mint"], c["token_program"]), False, True), M(user, True, True),
                M(Pk.from_string(SYSTEM), False, False)]
        if buy:  # buy: token program, then creator vault; sell: the other way round (pump IDL)
            mid = [M(c["token_program"], False, False), M(c["creator_vault"], False, True)]
        else:
            mid = [M(c["creator_vault"], False, True), M(c["token_program"], False, False)]
        tail = [M(self.event_authority, False, False), M(self.program, False, False)]
        if buy:
            tail += [M(self.global_volume, False, False),
                     M(self.pda([b"user_volume_accumulator", bytes(user)]), False, True)]
        # two accounts real trades pass after the IDL's list (the program refuses trades without them): the coin's
        # bonding-curve-v2 address (read only) and one of the global config's buyback fee recipients
        extra = [M(self.pda([b"bonding-curve-v2", bytes(c["mint"])]), False, False), M(random.choice(self.buyback), False, True)]
        return head + mid + tail + [M(self.fee_config, False, False), M(self.fee_program, False, False)] + extra

    def buy_ixs(self, c, user, lamports_in, min_tokens_raw, cu_price):
        from solders.instruction import Instruction, AccountMeta as M
        Pk = self.Pk
        ata = self.ata(user, c["mint"], c["token_program"])
        create_ata = Instruction(Pk.from_string(ATA_PROGRAM), bytes([1]), [  # CreateIdempotent
            M(user, True, True), M(ata, False, True), M(user, False, False), M(c["mint"], False, False),
            M(Pk.from_string(SYSTEM), False, False), M(c["token_program"], False, False)])
        data = BUY_EXACT_SOL_IN + struct.pack("<QQ", int(lamports_in), int(min_tokens_raw)) + bytes([1, 0])  # track volume, no partial fill
        return _budget(CU_BUY, cu_price) + [create_ata, Instruction(self.program, data, self._metas(c, user, True))]

    def sell_ixs(self, c, user, tokens_raw, min_lamports_out, cu_price):
        from solders.instruction import Instruction, AccountMeta as M
        ata = self.ata(user, c["mint"], c["token_program"])
        data = SELL + struct.pack("<QQ", int(tokens_raw), int(min_lamports_out))
        close = Instruction(c["token_program"], bytes([9]), [M(ata, False, True), M(user, False, True), M(user, True, False)])
        return _budget(CU_SELL, cu_price) + [Instruction(self.program, data, self._metas(c, user, False)), close]


def _budget(limit, price):
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey
    cb = Pubkey.from_string(COMPUTE_BUDGET)
    return [Instruction(cb, bytes([2]) + struct.pack("<I", limit), []),
            Instruction(cb, bytes([3]) + struct.pack("<Q", int(price)), [])]


def _tx(ixs, payer, blockhash, keypair=None):
    """-> raw transaction bytes: signed with keypair, or with a blank signature for simulation."""
    from solders.hash import Hash
    from solders.message import Message
    from solders.signature import Signature
    from solders.transaction import Transaction
    bh = Hash.from_string(blockhash)
    msg = Message.new_with_blockhash(ixs, payer, bh)
    tx = Transaction([keypair], msg, bh) if keypair is not None else Transaction.populate(msg, [Signature.default()])
    return bytes(tx), (str(tx.signatures[0]) if keypair is not None else None)


def _pick_coin(stream, exclude, now):
    """An actively traded standard curve coin: 5+ trades in the last 20 s, 5-85% along the curve."""
    best = None
    with stream.lock:
        items = [(m, list(h)) for m, h in stream.hist.items()]
        curves = dict(stream.curves)
    for mint, h in items:
        st = curves.get(mint)
        if mint in exclude or not st or st.get("pool") or st.get("complete") or "Q" not in st:
            continue
        recent = [e for e in h if now - e[0] <= 20]
        prog = 1.0 - st["sellable"] / CURVE_TOKENS
        if len(recent) >= 5 and abs(st["Q"] * st["T"] / STD_CURVE_K - 1) < 0.005 and 0.05 <= prog <= 0.85:
            if best is None or len(recent) > best[1]:
                best = (mint, len(recent))
    return best[0] if best else None


def _fee_bps(st):
    return st["feeBps"] if st.get("feeBps") is not None else 125


def _real_sell(Q, T, tokens, fee_bps):
    """SOL a real sell of `tokens` gets on curve (Q, T), after fees. Unlike a paper sell, our own buy really is in the
    reserves, so this is plain constant product (sim.sell_gross prices paper sells as if it were)."""
    return tokens * Q / (T + tokens) * (1 - fee_bps / 1e4)


def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None


class Calibrator:
    def __init__(self, args):
        self.a = args
        self.client = Client(timeout=15)
        self.rpc = Rpc(self.client, rpc_url())
        self.kp = None
        if args.keypair:
            from solders.keypair import Keypair
            with open(args.keypair, encoding="utf8") as fh:
                self.kp = Keypair.from_bytes(bytes(json.load(fh)))
        from solders.pubkey import Pubkey
        self.user = self.kp.pubkey() if self.kp else Pubkey.from_string(args.pubkey)
        self.wallet = str(self.user)[:P.WALLET_CHARS]
        self.pump = Pump(self.rpc)
        self.stream = P.open_stream({"enabled": True, "source": "auto"}, self.client._proxy)
        self.results = []

    def _cu_price(self):
        """Priority fee to offer (micro-lamports per compute unit): the --fee-pct percentile of what recent pump.fun
        trades paid to land, less the 5,000-lamport base fee."""
        sol = self.stream.tx_cost(self.a.fee_pct, min_samples=10)
        lamports = max(0.0, (sol or 0.0) * 1e9 - 5000)
        return int(lamports * 1e6 / CU_BUY)

    def _sample_costs(self, n=15):
        for _ in range(n):
            sig = self.stream.cost_sample_sig(min_age=3.0)
            if sig is None:
                time.sleep(1)
                continue
            try:
                tx = self.rpc.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
                if tx:
                    self.stream.costs.append(P.landing_cost(tx))
            except Exception:
                pass

    def run(self):
        a = self.a
        print(f"wallet {self.user} | {'LIVE: real transactions' if a.live else 'dry run: simulated, nothing signed or sent'}")
        self.stream.start()
        t0 = time.time()
        while not self.stream.healthy() and time.time() - t0 < 60:
            time.sleep(1)
        if not self.stream.healthy():
            raise SystemExit("the trade stream didn't come up; check SOLANA_GRPC_URL / SOLANA_WS_URL")
        time.sleep(8)  # some trade history to pick a coin and measure the stream's lag
        self._sample_costs()
        print(f"stream {self.stream.source.name}, {self.stream.feed_lag or 0:.2f} s behind the chain; priority fee offered "
              f"{self._cu_price()} µlamports/CU (p{a.fee_pct} of recent trades' landing cost)")
        try:
            return self._live() if a.live else self._dry()
        finally:
            self.stream.stop()

    # -------------------------------------------------------------------------------- dry run
    def _dry(self):
        mint = _pick_coin(self.stream, set(), time.time())
        if mint is None:
            raise SystemExit("no actively traded curve coin right now; try again in a minute")
        c = self.pump.coin(mint)
        st = self.stream.curves[mint]
        lamports = int(self.a.size_sol * 1e9)
        v = Venue("curve", st["Q"], st["T"], st["sellable"], _fee_bps(st), 0)
        exp_tokens = v.buy(self.a.size_sol)[0]
        bh = self.rpc.blockhash()
        ixs = self.pump.buy_ixs(c, self.user, lamports, exp_tokens * (1 - self.a.slippage / 100) * 1e6, self._cu_price())
        raw, _ = _tx(ixs, self.user, bh)
        sim = self.rpc.call("simulateTransaction", [base64.b64encode(raw).decode(), {"encoding": "base64", "sigVerify": False,
                                                                                    "replaceRecentBlockhash": True, "commitment": "processed"}])["value"]
        evs = [e for e in P.events_from_logs(sim.get("logs") or []) if e["kind"] == P.TRADE]
        print(f"\ncoin {mint} ({(1 - st['sellable'] / CURVE_TOKENS) * 100:.0f}% along the curve, fee {_fee_bps(st)} bps)")
        print(f"buy simulation: {'OK' if sim.get('err') is None else 'FAILED: ' + json.dumps(sim.get('err'))}, "
              f"{sim.get('unitsConsumed')} compute units")
        ok = sim.get("err") is None
        if evs:
            e = evs[-1]
            # the curve the simulation traded on: its reserves after our buy, less our buy
            pre = Venue("curve", e["Q"] - e["sol"], e["T"] + e["tokens"], e["sellable"] + e["tokens"], e["feeBps"], 0)
            model = pre.buy(self.a.size_sol)[0]
            print(f"  tokens for {self.a.size_sol} SOL: simulated {e['tokens']:,.2f}; paper pricing on the same curve "
                  f"{model:,.2f} ({e['tokens'] / model - 1:+.4%}); on the curve read before {exp_tokens:,.0f} "
                  f"({e['tokens'] / exp_tokens - 1:+.2%}: the curve moved in between)")
        elif not ok:
            print("  last logs:", *[ln[:160] for ln in (sim.get("logs") or [])[-6:]], sep="\n    ")
        # the sell, simulated as the coin's biggest holder (our wallet holds none): 1% of its tokens, no account close
        holder = self._holder(c)
        if holder is None:
            print("sell simulation: no holder found to simulate as")
            return 0 if ok else 1
        owner, raw_amount = holder
        amount = max(1, raw_amount // 100)
        sell = self.pump.sell_ixs(c, owner, amount, 0, self._cu_price())[:-1]
        raw, _ = _tx(sell, owner, bh)
        sim2 = self.rpc.call("simulateTransaction", [base64.b64encode(raw).decode(), {"encoding": "base64", "sigVerify": False,
                                                                                     "replaceRecentBlockhash": True, "commitment": "processed"}])["value"]
        ev2 = [e for e in P.events_from_logs(sim2.get("logs") or []) if e["kind"] == P.TRADE]
        print(f"sell simulation (as holder {str(owner)[:8]}..., {amount / 1e6:,.0f} tokens): "
              f"{'OK' if sim2.get('err') is None else 'FAILED: ' + json.dumps(sim2.get('err'))}, {sim2.get('unitsConsumed')} compute units")
        if ev2:
            e = ev2[-1]
            got = e["sol"] * (1 - e["feeBps"] / 1e4)
            model = _real_sell(e["Q"] + e["sol"], e["T"] - e["tokens"], e["tokens"], e["feeBps"])
            print(f"  SOL received: simulated {got:.9f}; paper pricing on the same curve {model:.9f} ({got / model - 1:+.4%})")
        elif sim2.get("err") is not None:
            print("  last logs:", *[ln[:160] for ln in (sim2.get("logs") or [])[-6:]], sep="\n    ")
        ok = ok and sim2.get("err") is None
        return 0 if ok else 1

    def _holder(self, c):
        """(owner, raw token amount) of the coin's biggest holder other than its bonding curve, or None."""
        from solders.pubkey import Pubkey
        # its recent transactions list every wallet's token balance after the trade (getTokenLargestAccounts would be
        # simpler, but public RPCs rate-limit it)
        try:
            sigs = self.rpc.call("getSignaturesForAddress", [str(c["mint"]), {"limit": 10, "commitment": "confirmed"}])
        except Exception:
            return None
        curve = str(c["curve"])
        for s in sigs:
            if s.get("err"):
                continue
            try:
                tx = self.rpc.call("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                                       "commitment": "confirmed"}])
            except Exception:
                continue
            for b in (tx or {}).get("meta", {}).get("postTokenBalances") or []:
                amt = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
                owner = b.get("owner")
                if b.get("mint") == str(c["mint"]) and owner and owner != curve and amt > 0:
                    pk = Pubkey.from_string(owner)
                    if pk.is_on_curve():  # a wallet, not a program's account
                        return pk, amt
        return None

    # -------------------------------------------------------------------------------- live
    def _balance(self):
        return self.rpc.call("getBalance", [str(self.user), {"commitment": "processed"}])["value"] / 1e9

    def _send_and_track(self, raw, sig, mint, side, t_signal):
        """Send, resend every second until our trade shows up on the stream (or the transaction fails / 20 s).
        -> dict(landed, resends, arrival, slot, event, err). We only see our trade the stream's lag after it landed,
        so resends sent after the landing don't count: resends = sends that went out before it landed, less one."""
        b64 = base64.b64encode(raw).decode()
        sent, deadline, last_status = [], time.time() + 20, 0.0
        self.stream.drain()  # only trades arriving from now on are scanned for ours
        while time.time() < deadline:
            if not sent or time.time() - sent[-1] >= 1.0:
                try:
                    self.rpc.call("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}])
                except Exception as e:
                    if not sent:
                        return {"landed": False, "resends": 0, "err": f"send failed: {e}"}
                sent.append(time.time())
            for e in self.stream.drain()[0]:
                if e["mint"] == mint and e["wallet"] == self.wallet and e["side"] == side:
                    landed_at = e["t"] - (self.stream.feed_lag or 0.0)
                    needed = max(1, sum(1 for t in sent if t <= landed_at))
                    return {"landed": True, "resends": needed - 1, "sends": len(sent), "arrival": e["t"],
                            "slot": e["slot"], "event": e}
            if time.time() - last_status >= 1.0:
                last_status = time.time()
                try:
                    stv = self.rpc.call("getSignatureStatuses", [[sig]])["value"][0]
                    if stv and stv.get("err"):
                        return {"landed": False, "failed": True, "resends": len(sent) - 1, "err": json.dumps(stv["err"]),
                                "slot": stv.get("slot")}
                except Exception:
                    pass
            time.sleep(0.05)
        return {"landed": False, "resends": len(sent) - 1, "err": "not seen within 20 s"}

    def _order(self, c, mint, side, size_sol=None, tokens_raw=None):
        st = self.stream.curves.get(mint)
        if not st or "Q" not in st:
            return {"landed": False, "err": "no curve state"}
        v = Venue("curve", st["Q"], st["T"], st["sellable"], _fee_bps(st), 0)
        t_signal = time.time()
        bh = self._bh if time.time() - self._bh_at < 30 else self._refresh_bh()
        if side > 0:
            exp = v.buy(size_sol)[0]
            ixs = self.pump.buy_ixs(c, self.user, size_sol * 1e9, exp * (1 - self.a.slippage / 100) * 1e6, self._cu_price())
        else:
            exp = _real_sell(st["Q"], st["T"], tokens_raw / 1e6, _fee_bps(st))
            ixs = self.pump.sell_ixs(c, self.user, tokens_raw, exp * (1 - self.a.sell_slippage / 100) * 1e9, self._cu_price())
        raw, sig = _tx(ixs, self.user, bh, self.kp)
        r = self._send_and_track(raw, sig, mint, side, t_signal)
        r.update(side="buy" if side > 0 else "sell", sig=sig, mint=mint, tSignal=t_signal, expected=exp,
                 lag=self.stream.feed_lag or 0.0)
        if r.get("landed"):
            ev = r["event"]
            r["landedAfterMs"] = round((r["arrival"] - r["lag"] - t_signal) * 1000)
            got = ev["tokens"] if side > 0 else ev["sol"] * (1 - ev["feeBps"] / 1e4)  # sol_amount is before fees
            r["got"] = got
            r["vsSignal"] = got / exp - 1 if exp else None  # how far the price moved while landing
            prior = self._state_before(mint, ev)
            if prior:  # the paper trader's pricing on the curve right before our trade
                pv = Venue("curve", prior[2], prior[3], prior[4], ev["feeBps"], 0)
                model = pv.buy(size_sol)[0] if side > 0 else _real_sell(prior[2], prior[3], tokens_raw / 1e6, ev["feeBps"])
                r["vsModel"] = got / model - 1 if model else None
            r.pop("event", None)
        return r

    def _state_before(self, mint, ev):
        with self.stream.lock:
            h = list(self.stream.hist.get(mint) or ())
        prev = None
        for e in h:
            if e[0] >= ev["t"] and e[1] == ev["slot"] and abs(e[2] - ev["Q"]) < 1e-12:
                return prev
            prev = e
        return None

    def _refresh_bh(self):
        self._bh, self._bh_at = self.rpc.blockhash(), time.time()
        return self._bh

    def _live(self):
        a = self.a
        start = self._balance()
        print(f"balance {start:.4f} SOL; stops if it falls below {start - a.max_loss_sol:.4f}")
        self._refresh_bh()
        used = set()
        for n in range(a.trades):
            if self._balance() < start - a.max_loss_sol:
                print("max loss reached: stopping")
                break
            mint = _pick_coin(self.stream, used, time.time())
            if mint is None:
                time.sleep(5)
                continue
            used.add(mint)
            c = self.pump.coin(mint)
            if c is None:
                continue
            buy = self._order(c, mint, +1, size_sol=a.size_sol)
            self.results.append(buy)
            _print(n, buy)
            if not buy.get("landed"):
                continue
            time.sleep(a.hold)
            ata = self.pump.ata(self.user, c["mint"], c["token_program"])
            bal = self.rpc.call("getTokenAccountBalance", [str(ata), {"commitment": "processed"}])["value"]
            sell = self._order(c, mint, -1, tokens_raw=int(bal["amount"]))
            self.results.append(sell)
            _print(n, sell)
        time.sleep(5)
        self._fees()
        return self._summary(start)

    def _fees(self):
        for r in self.results:
            if not r.get("sig") or not (r.get("landed") or r.get("failed")):
                continue
            try:
                tx = self.rpc.call("getTransaction", [r["sig"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
                if tx:
                    r["feeSol"] = tx["meta"]["fee"] / 1e9
            except Exception:
                pass

    def _summary(self, start):
        landed = [r for r in self.results if r.get("landed")]
        lat = [r["landedAfterMs"] for r in landed]
        resent = [r for r in landed if r.get("resends")]
        failed = [r for r in self.results if r.get("failed")]
        lost = [r for r in self.results if not r.get("landed") and not r.get("failed")]
        out = {"at": time.time(), "orders": len(self.results), "landed": len(landed), "failed": len(failed),
               "neverLanded": len(lost), "resent": len(resent),
               "landedAfterMs": {"p10": _q(lat, .1), "p50": _q(lat, .5), "p90": _q(lat, .9)},
               "vsSignal": {s: statistics.median([r["vsSignal"] for r in landed if r["side"] == s and r.get("vsSignal") is not None] or [0])
                            for s in ("buy", "sell")},
               "vsModel": [round(r["vsModel"], 5) for r in landed if r.get("vsModel") is not None],
               "feeSol": statistics.median([r["feeSol"] for r in self.results if r.get("feeSol")] or [0]),
               "balanceChangeSol": self._balance() - start, "results": self.results}
        if lat:
            out["suggest"] = {"execution.latencyMs": out["landedAfterMs"]["p50"],
                              "execution.latencyJitterMs": max(0, out["landedAfterMs"]["p90"] - out["landedAfterMs"]["p10"]),
                              "execution.dropPct": round(100.0 * (len(resent) + len(lost)) / max(1, len(self.results)), 1)}
        path = os.path.join(self.a.data, "calibration.json")
        with open(path, "w", encoding="utf8") as fh:
            json.dump(out, fh, indent=1, default=str)
        print(f"\n{out['orders']} orders: {out['landed']} landed ({out['resent']} needed a resend), {out['failed']} failed, "
              f"{out['neverLanded']} never landed")
        if lat:
            print(f"landed after (from signing): p10 {out['landedAfterMs']['p10']} ms, median {out['landedAfterMs']['p50']} ms, "
                  f"p90 {out['landedAfterMs']['p90']} ms")
            print(f"price moved while landing (median, + = better for us): buys {out['vsSignal']['buy']:+.2%}, sells {out['vsSignal']['sell']:+.2%}")
            if out["vsModel"]:
                print(f"paper pricing vs real fill on the same curve: median {statistics.median(out['vsModel']):+.4%}")
            print(f"fee paid per transaction: median {out['feeSol']:.6f} SOL; wallet change {out['balanceChangeSol']:+.5f} SOL")
            print("suggested settings:", json.dumps(out["suggest"]))
        print(f"written to {path}")
        return 0


def _print(n, r):
    if r.get("landed"):
        print(f"#{n + 1} {r['side']:4s} landed after {r['landedAfterMs']} ms, slot {r['slot']}, resends {r['resends']}, "
              f"vs signal {r.get('vsSignal') or 0:+.2%}, vs model {r.get('vsModel') if r.get('vsModel') is None else format(r['vsModel'], '+.3%')}")
    else:
        print(f"#{n + 1} {r['side'] if 'side' in r else ''} NOT landed: {r.get('err')}")


def calibrate(args):
    if not args.keypair and not args.pubkey:
        raise SystemExit("give --keypair (your wallet's keypair file) or, for a dry run only, --pubkey")
    if args.live and not args.keypair:
        raise SystemExit("--live needs --keypair")
    if args.size_sol <= 0 or args.size_sol > MAX_SIZE_SOL:
        raise SystemExit(f"--size-sol must be above 0 and at most {MAX_SIZE_SOL}")
    if args.trades < 1 or args.trades > MAX_TRADES:
        raise SystemExit(f"--trades must be 1-{MAX_TRADES}")
    import importlib.util
    if importlib.util.find_spec("solders") is None:
        raise SystemExit("calibration needs the solders package: pip install solders")
    if args.live and not args.yes:
        worst = args.max_loss_sol
        ans = input(f"This sends REAL transactions from your wallet: up to {args.trades} round trips of {args.size_sol} SOL, "
                    f"stopping if the wallet is down {worst} SOL. Type YES to go on: ")
        if ans.strip() != "YES":
            raise SystemExit("cancelled")
    random.seed()
    return Calibrator(args).run()
