# DeltaX partial exit backtest

Overnight-eligible symbols: APP, LRCX, WDAY, XYZ

Non-eligible symbols remain 100% same-day in every scenario.

## Scenario comparison

| Scenario | Trades | Overnight used | Win rate | Avg return | Median | PF | Portfolio | DD | Avg P&L/contract |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0_SAME_100_NEXT | 113 | 8 | 50.4% | +8.88% | +0.51% | 1.94 | +45.79% | -2.87% | $76.20 |
| 25_SAME_75_NEXT | 113 | 8 | 50.4% | +8.24% | +0.51% | 1.87 | +41.50% | -3.42% | $69.45 |
| 50_SAME_50_NEXT | 113 | 8 | 50.4% | +7.59% | +0.51% | 1.80 | +37.29% | -3.97% | $62.71 |
| 75_SAME_25_NEXT | 113 | 8 | 49.6% | +6.95% | -0.06% | 1.74 | +33.16% | -4.52% | $55.97 |
| 100_SAME_0_NEXT | 113 | 0 | 49.6% | +6.31% | -0.06% | 1.66 | +29.11% | -5.07% | $49.22 |

## Daily results

| Scenario | Date | Trades | Win rate | Daily return |
|---|---|---:|---:|---:|
| 0_SAME_100_NEXT | 2026-03-03 | 15 | 53.3% | +20.18% |
| 0_SAME_100_NEXT | 2026-03-04 | 29 | 41.4% | -2.87% |
| 0_SAME_100_NEXT | 2026-03-05 | 20 | 50.0% | +12.70% |
| 0_SAME_100_NEXT | 2026-03-06 | 49 | 55.1% | +10.81% |
| 100_SAME_0_NEXT | 2026-03-03 | 15 | 53.3% | +12.34% |
| 100_SAME_0_NEXT | 2026-03-04 | 29 | 37.9% | -5.07% |
| 100_SAME_0_NEXT | 2026-03-05 | 20 | 50.0% | +10.64% |
| 100_SAME_0_NEXT | 2026-03-06 | 49 | 55.1% | +9.43% |
| 25_SAME_75_NEXT | 2026-03-03 | 15 | 53.3% | +18.22% |
| 25_SAME_75_NEXT | 2026-03-04 | 29 | 41.4% | -3.42% |
| 25_SAME_75_NEXT | 2026-03-05 | 20 | 50.0% | +12.19% |
| 25_SAME_75_NEXT | 2026-03-06 | 49 | 55.1% | +10.47% |
| 50_SAME_50_NEXT | 2026-03-03 | 15 | 53.3% | +16.26% |
| 50_SAME_50_NEXT | 2026-03-04 | 29 | 41.4% | -3.97% |
| 50_SAME_50_NEXT | 2026-03-05 | 20 | 50.0% | +11.67% |
| 50_SAME_50_NEXT | 2026-03-06 | 49 | 55.1% | +10.12% |
| 75_SAME_25_NEXT | 2026-03-03 | 15 | 53.3% | +14.30% |
| 75_SAME_25_NEXT | 2026-03-04 | 29 | 37.9% | -4.52% |
| 75_SAME_25_NEXT | 2026-03-05 | 20 | 50.0% | +11.15% |
| 75_SAME_25_NEXT | 2026-03-06 | 49 | 55.1% | +9.77% |

## Caveat

This is still based on the same five-session event sample. Partial overnight holding may improve historical returns for the whitelist, but the whitelist itself was selected using the same event data, so treat it as in-sample evidence.