# Pump.fun Bot Sprint: Make Bot And Win

[![Competition](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/Falken21/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-entry/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/Falken21/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-entry/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

A conservative momentum-scalping bot for the make.fun **Pump.fun Bot Sprint**
(`agent-fun/replay/v2` harness). It trades paper SOL on a replay of pump.fun
bonding-curve events and is ranked on net PnL. The qualifying bar is beating
hold-cash — net PnL above 0 SOL after every fee — so the bot is built to make
few, well-filtered trades and to keep friction (fixed per-order transaction
fees plus curve fees on both sides) as small as possible, instead of chasing
volume.

## Strategy

The bot keeps a short trade tape (last 48 trades) and the latest reserves for
every mint in the universe. A mint becomes a candidate only when one of its
own trades arrives; the entry filter then requires **all** of the following:

- **Maturity** — the mint's CreateEvent is at least 30 slots old, so the
  launch-slot chaos (bundled buys, instant dumps) is skipped entirely.
- **Liquidity** — the mint's real SOL reserve is at least 8 SOL. That keeps
  the fixed 0.02 SOL stake far inside the harness size cap (1% of real
  reserve per reply) and leaves room to exit.
- **Broad buy pressure** — over the last 40 tape trades (which must span at
  least 8 slots): at least 12 trades, at least 1 SOL of combined volume, at
  least 5 distinct buyer wallets, and buy volume at least 2× sell volume.
- **Rising, not parabolic** — the current price (virtual SOL ÷ virtual token
  reserves) is at least +5% above the price at the start of the window, but
  no more than +35% above it. The first condition confirms momentum; the
  second refuses to buy a top.

Each mint gets **at most one round trip**, at most **2 new positions per
slot**, at most **32 entries in the whole run**, and never more than the
harness's 5 concurrent positions. A free-cash guard subtracts pending buys
and their transaction fees from the account balance before any new buy, so
the bot never eats an `insufficient_cash` rejection.

## Position sizing

One fixed stake: **0.02 SOL per entry** — twice the 0.01 SOL minimum a round
trip needs to count toward the 20-coin guardrail, small enough that 5
concurrent positions risk only 0.1 SOL of the 10 SOL bankroll, and large
enough that the fixed 0.000105 SOL per-order transaction fee stays a minor
part of round-trip friction. Position size never scales up after wins or
down after losses; the edge is supposed to come from the filter, not sizing.

## Exit rules

Every held position is re-evaluated on every batch against the batch's marks,
with its cost basis taken from the account (so the comparison price already
includes the buy-side fees that must be recovered):

- **Take profit** — sell everything when the mark price reaches **+10%** over
  fee-inclusive cost.
- **Stop loss** — sell everything when the mark price falls to **−7%** under
  cost.
- **Time stop** — sell everything after **150 slots** (~60 seconds), win or
  lose. Nothing is held "to see what happens", and nothing rides into the
  end-of-window flatten if it can be closed earlier.
- If a sell's fill never arrives (rejected, or one of the harness's seeded
  failed transactions), the exit is re-sent after 10 slots.

Buys allow 800 bps of slippage, sells 1200 bps: entries can afford to miss,
exits should land.

## Risk controls

- Drawdown is structurally tiny: maximum concurrent exposure is 5 × 0.02 SOL
  = 1% of capital, every position carries a −7% stop, and the run's total
  entries are capped at 32 — far inside the 50% max-drawdown guardrail.
- One round trip per mint: a coin that already dumped on us (or paid us) is
  never re-entered on a second signal.
- The bot never buys a completed mint, never buys without the reserves to
  satisfy the harness size cap, and never sends more than the per-batch
  order cap.
- **Determinism**: integer math only (prices are compared by
  cross-multiplication, never floats), no wall clock, no randomness, no
  environment or network access. The same message stream always produces the
  same orders, byte for byte, across the harness's two executions.

## How to run

The bot is a single file, `main.py`, standard library only, speaking the
`agent-fun/replay/v2` NDJSON protocol on stdin/stdout:

```sh
PYTHONHASHSEED=0 python3 -P -s main.py < replay.ndjson
```

It reads one `hello`, answers each `batch` with exactly one `reply` line
echoing the batch's `seq`, and exits silently on `done`. Diagnostics never
touch stdout. In the competition it is executed by the harness as
`python3 -P -s /bot/main.py` under Python 3.13.

## Layout

- `main.py` — the bot (wire protocol, tape tracking, entry filter, exits).
- `LICENSE` — MIT.
- `.makefun/owner.json` — make.fun claim file naming the payout wallet.
