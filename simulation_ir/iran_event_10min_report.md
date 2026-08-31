# Iran event 10-minute strategy backtest

## Best tested configuration

- Bias threshold: **2.50%**
- 10-minute reversal threshold: **0.25%**
- Entry mode: **threshold_only**
- Trades: **249**
- Win rate: **67.5%**
- Average trade: **+0.85%**
- Median trade: **+0.71%**
- Profit factor: **3.47**
- Equal-weight portfolio return: **+3.68%**
- Max drawdown: **+0.00%**

## Top 15 symbols

| Symbol | Side | Trades | Win rate | Avg return | Best | Worst |
|---|---|---:|---:|---:|---:|---:|
| LITE | SHORT | 2 | 100.0% | +5.26% | +9.88% | +0.63% |
| APO | LONG | 2 | 100.0% | +4.69% | +6.11% | +3.26% |
| APP | LONG | 2 | 100.0% | +3.75% | +6.33% | +1.18% |
| WFC | LONG | 3 | 100.0% | +2.40% | +2.95% | +1.57% |
| FFIV | LONG | 2 | 100.0% | +3.20% | +3.42% | +2.97% |
| CRWD | LONG | 1 | 100.0% | +6.16% | +6.16% | +6.16% |
| LRCX | SHORT | 2 | 100.0% | +2.98% | +3.80% | +2.16% |
| F | SHORT | 2 | 100.0% | +2.84% | +3.16% | +2.51% |
| ARES | LONG | 2 | 100.0% | +2.81% | +5.10% | +0.51% |
| TTD | LONG | 1 | 100.0% | +5.20% | +5.20% | +5.20% |
| LYB | LONG | 2 | 100.0% | +2.45% | +4.51% | +0.39% |
| BX | LONG | 2 | 100.0% | +2.43% | +4.22% | +0.63% |
| CSGP | LONG | 2 | 100.0% | +2.40% | +3.68% | +1.13% |
| MAS | SHORT | 2 | 100.0% | +2.38% | +4.35% | +0.41% |
| RDDT | LONG | 2 | 100.0% | +2.33% | +3.68% | +0.98% |

## Daily portfolio

| Date | Trades | Win rate | Daily return |
|---|---:|---:|---:|
| 2026-03-03 | 36 | 69.4% | +1.51% |
| 2026-03-04 | 67 | 58.2% | +0.38% |
| 2026-03-05 | 58 | 65.5% | +0.69% |
| 2026-03-06 | 88 | 75.0% | +1.06% |

## Important

This remains a five-session event study. The script removes look-ahead from the daily event-bias calculation, but the parameter search itself still chooses the best configuration from the same historical window. Treat the best grid result as a candidate live rule, not a guarantee.