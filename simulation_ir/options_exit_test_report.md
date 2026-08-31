# DeltaX option exit timing test

Entry configuration held constant:

- DTE: 7
- Moneyness: OTM_1
- Slippage: 2.5% per side

## Exit comparison

| Exit | Trades | Win rate | Avg return | Median | PF | Portfolio | DD | Avg P&L/contract |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SAME_DAY_1550 | 113 | 49.6% | +6.31% | -0.06% | 1.66 | +29.11% | -5.07% | $49.22 |
| NEXT_OPEN | 108 | 42.6% | -0.64% | -11.60% | 0.97 | +16.87% | -14.34% | $-18.50 |
| NEXT_CLOSE | 108 | 36.1% | -1.40% | -16.20% | 0.94 | +11.84% | -14.13% | $-41.69 |
| DAY2_CLOSE | 103 | 29.1% | -12.73% | -38.33% | 0.67 | -15.07% | -45.67% | $-178.78 |

## Daily results

| Exit | Signal date | Trades | Win rate | Daily return |
|---|---|---:|---:|---:|
| DAY2_CLOSE | 2026-03-03 | 12 | 50.0% | +56.30% |
| DAY2_CLOSE | 2026-03-04 | 28 | 25.0% | -18.12% |
| DAY2_CLOSE | 2026-03-05 | 19 | 42.1% | -0.08% |
| DAY2_CLOSE | 2026-03-06 | 44 | 20.5% | -33.59% |
| NEXT_CLOSE | 2026-03-03 | 15 | 46.7% | +12.49% |
| NEXT_CLOSE | 2026-03-04 | 29 | 24.1% | -9.70% |
| NEXT_CLOSE | 2026-03-05 | 20 | 55.0% | +28.23% |
| NEXT_CLOSE | 2026-03-06 | 44 | 31.8% | -14.13% |
| NEXT_OPEN | 2026-03-03 | 15 | 53.3% | +17.05% |
| NEXT_OPEN | 2026-03-04 | 29 | 44.8% | -5.95% |
| NEXT_OPEN | 2026-03-05 | 20 | 50.0% | +23.94% |
| NEXT_OPEN | 2026-03-06 | 44 | 34.1% | -14.34% |
| SAME_DAY_1550 | 2026-03-03 | 15 | 53.3% | +12.34% |
| SAME_DAY_1550 | 2026-03-04 | 29 | 37.9% | -5.07% |
| SAME_DAY_1550 | 2026-03-05 | 20 | 50.0% | +10.64% |
| SAME_DAY_1550 | 2026-03-06 | 49 | 55.1% | +9.43% |

## Caveat

This compares exit timing on the same historical entry signals and option contracts. Overnight holding introduces gap, theta, IV and liquidity risk that did not exist in the same-day baseline.