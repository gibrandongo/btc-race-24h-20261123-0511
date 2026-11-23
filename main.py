#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Momentum surge-catcher for cbBTC (agent-fun/bars-replay/v1).

Strategy, in one paragraph: long-only, one position, full-size entries on a
confirmed 15-minute momentum burst, with three exits that all cut losers
fast and let a real trend run: a 1.2% trailing stop off the high-water close,
a 1.0% hard stop under the fill price, and a time stop that exits dead money
(held 180 bars without gaining 0.5%). After any exit the bot cools down
(120 bars, 360 after a hard-stop exit) so it cannot churn on the same chop
that stopped it out.

Why this shape: on 1-minute bars BTC noise dominates (per-bar stdev ~8 bps)
while real moves arrive as multi-hour surges. Plain EMA crosses whipsaw
~80 times a day and bleed ~$120 to fees; a 15-minute momentum filter fires
only on genuine bursts (~7 trades/day on the dev window), and the trailing
stop keeps the bot in the one big move instead of selling it on the first
pullback. All comparisons are integer cross-multiplications on the raw
integer prices, so the bot is exactly deterministic: no floats, no random
numbers, no wall clock, no dict/set ordering dependence.

Protocol notes: the harness sends hello once, then one batch per minute
(bar, fills of the previous batch's orders, account), then done. Every
batch gets exactly one reply echoing its seq. Position and cash are always
taken from the batch's account field (ground truth); entry price and
high-water come from the fills the harness reports. Orders decided on a
batch fill at the next bar's open; buys spend all cash, sells empty the
whole position, both with max_slippage_bps 150 (dev max 1-minute move was
72 bps, max open-vs-prev-close gap 20 bps, so 150 bps never blocks a
normal fill but still caps a disaster fill).
"""

import json
import sys

SYMBOL = "CBBTC"

# Entry: 15-bar momentum above 0.40% (close[i] / close[i-15] > 1.004).
MOM_LOOKBACK = 15
MOM_NUM = 10040
MOM_DEN = 10000

# Exits.
TRAIL_NUM = 988    # 1.2% trailing stop: close <= high_water * 0.988
TRAIL_DEN = 1000
STOP_NUM = 99      # 1.0% hard stop: close <= entry_price * 0.99
STOP_DEN = 100
TS_BARS = 180      # time stop: held >= 180 bars without +0.5% -> exit
TS_NUM = 201       # close < entry_price * 1.005
TS_DEN = 200

# Cooldowns after an exit, in bars (no new entry until then).
COOL_TRAIL = 120
COOL_STOP = 360

MAX_SLIPPAGE_BPS = 150


def main():
    closes = []
    warmup_bars = 0
    price_factor_exp = 7  # cost * 10**7 / qty -> price units (scales 8/6/5)
    cash = 0
    pos_qty = 0
    entry_price = None
    high_water = None
    bars_held = 0
    cool_until = -1
    order_no = 0
    out = sys.stdout

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            # A line we cannot parse carries no seq we could echo; skip it
            # and stay alive for the next batch rather than crashing the run.
            continue
        mtype = msg.get("type")

        if mtype == "hello":
            warmup_bars = int(msg.get("warmup_bars", 0) or 0)
            for s in msg.get("symbols", []) or []:
                if s.get("symbol") == SYMBOL:
                    price_factor_exp = (
                        int(s.get("qty_scale", 8))
                        - int(msg.get("capital", {}).get("quote_scale", 6))
                        + int(s.get("price_scale", 5))
                    )
            continue
        if mtype == "done":
            break
        if mtype != "batch":
            continue

        seq = msg["seq"]

        # Account is ground truth for cash and position.
        acct = msg.get("account") or {}
        cash = int(acct.get("cash_quote_units", "0"))
        pos_qty = 0
        pos_cost = 0
        for p in acct.get("positions", []) or []:
            if p.get("symbol") == SYMBOL:
                pos_qty = int(p.get("qty", "0"))
                pos_cost = int(p.get("cost_quote_units", "0"))

        # Fills of the previous batch's orders: set entry state on a buy.
        for f in msg.get("fills", []) or []:
            if f.get("symbol") != SYMBOL or f.get("status") != "filled":
                continue
            if f.get("side") == "buy":
                entry_price = int(f.get("price", "0"))
                high_water = entry_price
                bars_held = 0
            elif f.get("side") == "sell":
                entry_price = None
                high_water = None
                bars_held = 0

        # This batch's bar (may be absent on a gap minute).
        bar_close = None
        for b in msg.get("bars", []) or []:
            if b.get("symbol") == SYMBOL:
                bar_close = int(b.get("c", "0"))
                break

        if bar_close is not None:
            closes.append(bar_close)
            if pos_qty > 0:
                if entry_price is None:
                    # Fallback: derive average cost as a price-scale integer.
                    # A zero/unknown cost means we cannot trust the derived
                    # level, so anchor on the latest close instead.
                    if pos_cost > 0:
                        entry_price = (
                            (pos_cost * (10 ** price_factor_exp)) // pos_qty
                        )
                    else:
                        entry_price = bar_close
                    high_water = bar_close
                    bars_held = 0
                if bar_close > high_water:
                    high_water = bar_close
                bars_held += 1

        orders = []
        n_bars = len(closes)
        if bar_close is not None and n_bars >= warmup_bars:
            if pos_qty > 0 and entry_price is not None:
                c = bar_close
                stop_hit = c * STOP_DEN <= entry_price * STOP_NUM
                trail_hit = c * TRAIL_DEN <= high_water * TRAIL_NUM
                ts_hit = (
                    bars_held >= TS_BARS and c * TS_DEN < entry_price * TS_NUM
                )
                if stop_hit or trail_hit or ts_hit:
                    order_no += 1
                    orders.append(
                        {
                            "side": "sell",
                            "client_id": "s%d" % order_no,
                            "symbol": SYMBOL,
                            "qty": str(pos_qty),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        }
                    )
                    cool_until = n_bars + (
                        COOL_STOP if stop_hit else COOL_TRAIL
                    )
            elif pos_qty == 0 and cash >= 1000000:
                if n_bars > cool_until and n_bars > MOM_LOOKBACK:
                    if (
                        closes[-1] * MOM_DEN
                        > closes[-1 - MOM_LOOKBACK] * MOM_NUM
                    ):
                        order_no += 1
                        orders.append(
                            {
                                "side": "buy",
                                "client_id": "b%d" % order_no,
                                "symbol": SYMBOL,
                                "notional_quote_units": str(cash),
                                "max_slippage_bps": MAX_SLIPPAGE_BPS,
                            }
                        )

        out.write(
            json.dumps({"type": "reply", "seq": seq, "orders": orders}) + "\n"
        )
        out.flush()


if __name__ == "__main__":
    main()
