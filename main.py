#!/usr/bin/env python3
"""Pump.fun Bot Sprint: Make Bot And Win — entry bot.

Strategy in one paragraph: a conservative momentum scalper for the
agent-fun/replay/v2 harness. It watches the trade tape of every mint in the
universe, and buys a small fixed stake (0.02 SOL) only when a mint is already
liquid, has traded for a while, and shows broad, accelerating buy pressure
with a rising but not parabolic price. Every position is closed quickly:
take-profit at +10% over fee-inclusive cost, stop-loss at -7%, or a hard
time stop. Each mint gets at most one round trip, and the total number of
round trips is capped, because in this harness every order pays a fixed
transaction fee and every trade pays curve fees — the cheapest trade is the
one never sent. The bar is beating hold-cash (net PnL > 0), so the bot is
built to do few, well-filtered trades rather than chase volume.

Determinism: the bot is a pure function of the message stream. Integer math
only (prices are compared by cross-multiplication, never floats), no clock,
no randomness, no environment reads, and iteration order never depends on
hash order (dicts are insertion-ordered; sets are only used for membership).
"""

import json
import sys
from collections import deque

# --------------------------------------------------------------------------
# Tunables (all amounts in lamports; prices compared as integer ratios).
# --------------------------------------------------------------------------

BUY_LAMPORTS = 20_000_000          # 0.02 SOL fixed stake per entry.
MIN_COUNTED_BUY = 10_000_000       # guardrail: a counted round trip needs >= 0.01 SOL of buy fills.
MAX_TOTAL_ENTRIES = 32             # hard cap on round trips for the whole run.
MAX_NEW_BUYS_PER_BATCH = 2         # pacing: never open more than this per slot.

MIN_AGE_SLOTS = 30                 # skip the launch chaos: mint must be this old.
MIN_REAL_QUOTE = 8_000_000_000     # mint's real SOL reserve must be >= 8 SOL (exit liquidity + size cap).
TAPE_MAX = 48                      # trades of tape kept per mint.
TAPE_MIN_TRADES = 12               # need at least this many tape trades before judging momentum.
WINDOW_TRADES = 40                 # momentum is judged over the last this-many tape trades.
MIN_WINDOW_SPAN_SLOTS = 8          # those trades must span at least this many slots.
MIN_WINDOW_VOLUME = 1_000_000_000  # window buy+sell volume must be >= 1 SOL.
MIN_DISTINCT_BUYERS = 5            # distinct buyer wallets inside the window.
# Buy pressure: window_buy_sol * BUY_NUM >= window_sell_sol * BUY_DEN  (i.e. buys >= 2x sells).
BUY_NUM = 1
BUY_DEN = 2
# Trend: price now must be >= +5% vs the price WINDOW_TREND trades back ...
TREND_NUM = 105
TREND_DEN = 100
# ... but no more than +35% above it (do not buy a top).
TOP_NUM = 135
TOP_DEN = 100

TAKE_PROFIT_NUM = 110              # sell when mark price >= 110% of fee-inclusive cost price.
TAKE_PROFIT_DEN = 100
STOP_LOSS_NUM = 93                 # sell when mark price <= 93% of cost price.
STOP_LOSS_DEN = 100
MAX_HOLD_SLOTS = 150               # time stop: exit after this many slots regardless.

BUY_SLIPPAGE_BPS = 800
SELL_SLIPPAGE_BPS = 1200
SELL_RETRY_SLOTS = 10              # resend an exit if its fill never showed up after this long.

MAX_TRACKED_MINTS = 6000           # memory bound for the tape table.
EVICT_BEFORE_SLOTS = 2000          # evict mints idle longer than this when over the bound.


class MintState:
    """Everything tracked about one mint: latest reserves plus a short tape."""

    __slots__ = (
        "created_slot", "last_slot", "complete", "trades",
        "vq", "vt", "rq", "tape",
    )

    def __init__(self, created_slot, slot):
        self.created_slot = created_slot
        self.last_slot = slot
        self.complete = False
        self.trades = 0
        self.vq = 0          # virtual quote (SOL) reserves, latest known
        self.vt = 0          # virtual token reserves, latest known
        self.rq = 0          # real quote (SOL) reserves, latest known
        # tape entries: (slot, sol_amount, is_buy, user, vq_after, vt_after)
        self.tape = deque(maxlen=TAPE_MAX)


class Bot:
    def __init__(self, hello):
        self.hello = hello
        caps = hello.get("caps", {})
        self.max_positions = int(caps.get("max_open_positions", 5))
        self.max_orders = int(caps.get("max_orders_per_batch", 16))
        self.size_bps = int(caps.get("max_order_bps_of_real_reserve", 100))
        fees = hello.get("fees", {})
        self.tx_fee = int(fees.get("base_fee_lamports", "0")) + int(fees.get("priority_fee_lamports", "0"))
        self.cash = int(hello.get("capital", {}).get("lamports", "0"))
        self.positions = {}        # mint -> (token_amount, cost_lamports), from the latest account
        self.mints = {}            # mint -> MintState, insertion ordered
        self.entered = set()       # mints we have ever sent a buy for (one round trip each)
        self.entry_slot = {}       # mint -> slot its position was first seen in the account
        self.pending = {}          # client_id -> (side, mint, decided_slot)
        self.pending_sell = {}     # mint -> decided_slot of the latest unfilled sell
        self.entries_made = 0
        self.counter = 0

    # -- wire helpers ------------------------------------------------------

    def client_id(self):
        self.counter += 1
        return "o%d" % self.counter

    # -- state folding -----------------------------------------------------

    def fold_batch(self, batch):
        slot = batch["slot"]
        for f in batch.get("fills", []):
            p = self.pending.pop(f.get("client_id"), None)
            if p is not None and p[0] == "sell":
                if self.pending_sell.get(p[1]) == p[2]:
                    del self.pending_sell[p[1]]
        account = batch["account"]
        self.cash = int(account["cash_lamports"])
        self.positions = {}
        for p in account.get("positions", []):
            tokens = int(p["token_amount"])
            if tokens > 0:
                self.positions[p["mint"]] = (tokens, int(p["cost_lamports"]))
                if p["mint"] not in self.entry_slot:
                    self.entry_slot[p["mint"]] = slot
        for mint in list(self.entry_slot):
            if mint not in self.positions:
                del self.entry_slot[mint]
        touched = []
        seen = set()
        for e in batch.get("events", []):
            kind = e.get("event")
            mint = e.get("mint")
            if mint is None:
                continue
            st = self.mints.get(mint)
            if kind == "CreateEvent":
                if st is None:
                    st = MintState(e["slot"], e["slot"])
                    self.mints[mint] = st
                st.vq = int(e["virtual_quote_reserves"])
                st.vt = int(e["virtual_token_reserves"])
                st.rq = int(e.get("real_quote_reserves", "0"))
                st.last_slot = e["slot"]
            elif kind == "TradeEvent":
                if st is None:
                    st = MintState(None, e["slot"])
                    self.mints[mint] = st
                st.vq = int(e["virtual_quote_reserves"])
                st.vt = int(e["virtual_token_reserves"])
                st.rq = int(e["real_quote_reserves"])
                st.last_slot = e["slot"]
                st.trades += 1
                st.tape.append((
                    e["slot"],
                    int(e["sol_amount"]),
                    bool(e["is_buy"]),
                    e.get("user", ""),
                    st.vq,
                    st.vt,
                ))
                if mint not in seen:
                    seen.add(mint)
                    touched.append(mint)
            elif kind == "CompleteEvent":
                if st is None:
                    st = MintState(None, e["slot"])
                    self.mints[mint] = st
                st.complete = True
                st.last_slot = e["slot"]
        return touched

    # -- entry logic -------------------------------------------------------

    def committed_positions(self):
        mints = set(self.positions)
        for side, mint, _slot in self.pending.values():
            if side == "buy":
                mints.add(mint)
        return len(mints)

    def free_cash(self):
        """Account cash minus what pending orders will take if they resolve.

        The account only reflects resolved fills, so without this the bot
        could stack pending buys past its cash and eat insufficient_cash
        rejections. Every sent order also pays the fixed tx fee.
        """
        free = self.cash
        for side, _mint, _slot in self.pending.values():
            free -= self.tx_fee
            if side == "buy":
                free -= BUY_LAMPORTS
        return free

    def momentum_ok(self, st, slot):
        """True when the mint's recent tape shows broad, rising buy pressure."""
        if st.complete or st.created_slot is None:
            return False
        if slot - st.created_slot < MIN_AGE_SLOTS:
            return False
        if st.rq < MIN_REAL_QUOTE:
            return False
        tape = list(st.tape)
        if len(tape) < TAPE_MIN_TRADES:
            return False
        window = tape[-WINDOW_TRADES:]
        # The window must span some time: a single-slot burst is a launch
        # spike, not momentum.
        if window[-1][0] - window[0][0] < MIN_WINDOW_SPAN_SLOTS:
            return False
        buy_sol = 0
        sell_sol = 0
        buyers = set()
        for _s, amount, is_buy, user, _vq, _vt in window:
            if is_buy:
                buy_sol += amount
                buyers.add(user)
            else:
                sell_sol += amount
        if buy_sol + sell_sol < MIN_WINDOW_VOLUME:
            return False
        if len(buyers) < MIN_DISTINCT_BUYERS:
            return False
        if buy_sol * BUY_NUM < sell_sol * BUY_DEN:
            return False
        # Trend: compare the latest price with the price at the window's start
        # by cross-multiplication: price = vq / vt.
        _s0, _a0, _b0, _u0, vq0, vt0 = window[0]
        if vt0 <= 0 or st.vt <= 0:
            return False
        # price_now >= price_ref * TREND_NUM/TREND_DEN
        if st.vq * vt0 * TREND_DEN < vq0 * st.vt * TREND_NUM:
            return False
        # price_now <= price_ref * TOP_NUM/TOP_DEN (not already parabolic)
        if st.vq * vt0 * TOP_DEN > vq0 * st.vt * TOP_NUM:
            return False
        return True

    # -- exit logic --------------------------------------------------------

    def exit_reason(self, mint, marks_by_mint, slot):
        tokens, cost = self.positions[mint]
        if tokens <= 0 or cost <= 0:
            return False
        mark = marks_by_mint.get(mint)
        if mark is None:
            return False
        vq = int(mark["virtual_quote_reserves"])
        vt = int(mark["virtual_token_reserves"])
        if vq <= 0 or vt <= 0:
            return False
        # mark_price vs cost_price (cost includes buy fees), cross-multiplied:
        # mark vq/vt  vs  cost/tokens scaled by NUM/DEN.
        if vq * tokens * TAKE_PROFIT_DEN >= vt * cost * TAKE_PROFIT_NUM:
            return True
        if vq * tokens * STOP_LOSS_DEN <= vt * cost * STOP_LOSS_NUM:
            return True
        entered_slot = self.entry_slot.get(mint)
        if entered_slot is not None and slot - entered_slot >= MAX_HOLD_SLOTS:
            return True
        return False

    # -- one batch ----------------------------------------------------------

    def on_batch(self, batch):
        slot = batch["slot"]
        touched = self.fold_batch(batch)
        marks_by_mint = {m["mint"]: m for m in batch.get("marks", [])}
        orders = []

        # Exits first: freeing slots and locking outcomes beats new entries.
        for mint in list(self.positions):
            if len(orders) >= self.max_orders:
                break
            sent_at = self.pending_sell.get(mint)
            if sent_at is not None and slot - sent_at <= SELL_RETRY_SLOTS:
                continue  # a sell is already in flight; fills land 2 slots later
            # No fill reported long after a sell was sent: it was rejected or
            # failed on chain. Fall through and send a fresh exit.
            if self.exit_reason(mint, marks_by_mint, slot):
                tokens, _cost = self.positions[mint]
                cid = self.client_id()
                orders.append({
                    "side": "sell",
                    "client_id": cid,
                    "mint": mint,
                    "token_amount": str(tokens),
                    "max_slippage_bps": SELL_SLIPPAGE_BPS,
                })
                self.pending[cid] = ("sell", mint, slot)
                self.pending_sell[mint] = slot

        # Entries: only mints that traded in this batch can newly qualify.
        new_buys = 0
        for mint in touched:
            if len(orders) >= self.max_orders or new_buys >= MAX_NEW_BUYS_PER_BATCH:
                break
            if self.entries_made >= MAX_TOTAL_ENTRIES:
                break
            if mint in self.entered or mint in self.positions:
                continue
            if self.committed_positions() >= self.max_positions:
                break
            if self.free_cash() < BUY_LAMPORTS + self.tx_fee:
                break
            st = self.mints[mint]
            if not self.momentum_ok(st, slot):
                continue
            # Respect the harness size cap: this reply's buys on the mint may
            # spend at most size_bps of its real quote reserve.
            if BUY_LAMPORTS * 10_000 > st.rq * self.size_bps:
                continue
            cid = self.client_id()
            orders.append({
                "side": "buy",
                "client_id": cid,
                "mint": mint,
                "quote_lamports": str(BUY_LAMPORTS),
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            })
            self.pending[cid] = ("buy", mint, slot)
            self.entered.add(mint)
            self.entries_made += 1
            new_buys += 1

        self.evict(slot)
        return orders

    def evict(self, slot):
        if len(self.mints) <= MAX_TRACKED_MINTS:
            return
        held = set(self.positions)
        for mint in list(self.mints):
            if len(self.mints) <= MAX_TRACKED_MINTS:
                break
            if mint in held or mint in self.entered:
                continue
            if slot - self.mints[mint].last_slot >= EVICT_BEFORE_SLOTS:
                del self.mints[mint]


def main():
    bot = None
    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line.decode("utf-8"))
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            orders = bot.on_batch(msg)
            reply = {"type": "reply", "seq": msg["seq"], "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
