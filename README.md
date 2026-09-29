# London Reclaim

An MNQ / NQ futures strategy: **London and previous-day levels, breakout, then retest.**

> **For strategy research and testing only. Do not use it with live funds.** This is not financial advice. Past results do not predict future results. Futures trading can lose more than you put in.

## Results

Backtest on MNQ, 1-minute bars, $100 risk per trade, 1 tick slippage, $0.37 commission per side, $50,000 start.

| Period | Trades | Profit factor | Sharpe | Win rate | Max drawdown |
| --- | --- | --- | --- | --- | --- |
| **Full, 2019-05 to 2026-08** | 1,367 | 1.26 | 1.44 | 32% | $2,107 |
| Tuned window, 2021-07 to 2026-07 | 891 | 1.29 | 1.55 | 31% | $1,512 |
| Before the tuned window, 2019-05 to 2021-07 | 470 | 1.21 | 1.31 | 32% | $2,107 |

Over the full period the account grew from $50,000 to $69,029 (+38%, about 4.5% a year). The 95% confidence range of the full-period Sharpe is 0.69 to 2.15.

Limits you should know:

- The parameters were chosen on the tuned window. The 2019–2021 row is the fairer test. It is still positive, but weaker.
- About 1 trade in 3 wins. Winners average about $211 and losers about $77, so a losing streak is normal.
- In an earlier 200-run TopStep Combine simulation, only 2.5% of runs passed phase 1 and none passed phase 2. This is **not** a prop-firm passer.
- There is no test on years before 2019.
- Fills are simulated. When a stop and a target could both trade in one bar, the test assumes the stop is hit first.

## The rules

1. Each day, build four levels: the **London high and low** (02:00–08:30 New York time) and the **previous day's high and low** (PDH / PDL, cut at the 18:00 New York session boundary).
2. A level is **swept** when price trades beyond it. The strategy treats the sweep as a real breakout and arms the setup.
3. Wait for a confirmed 3-bar **pullback swing**. Then place a **stop order one tick beyond the extreme of the breakout** (the retest entry). The stop loss sits one tick beyond the pullback swing.
4. Place the order only if it rests beyond the current price, the stop is 45 points or less, and the time is 09:30–11:00 or 13:30–15:30 New York time. The afternoon window skips a level that the morning already swept.
5. Target: 3.5 times the risk. One trade per direction per level per day, at most 4 trades a day. Everything is flat at 15:55.

## Files

- `strategy/london_reclaim.py`: the full strategy logic (Python, event-driven).
- `pine/london_reclaim_variant_a.pine`: TradingView version. Run it on a 1-minute MNQ or NQ chart to **see** the levels and signals. It will not match the backtest to the dollar.
- `configs/`: the exact parameters behind the table. `lr_insample.json`, `lr_oos_pre.json`, `lr_oos_post.json` are the three periods. `lr_full_slip2.json`, `lr_full_slip3.json`, `lr_full_comm2x.json` are cost stress tests.

The Python file needs an event-driven backtesting engine that supplies `Bar`, `Order`, and a strategy base class. The engine is not part of this repo.

## License

MIT. Copyright (c) 2026 Stoplosses.ai LLC. Contact: alvin@askelira.com.
