# DeltaX historical options backtest

Underlying stock signals tested: **249**

## Best realistic tested configuration

- Target DTE: **7**
- Moneyness: **OTM_1**
- Assumed slippage per side: **2.5%**
- Executed trades: **113**
- Historical-data coverage: **45.4%**
- Win rate: **49.6%**
- Average option return: **+6.30%**
- Median option return: **-0.47%**
- Profit factor: **1.66**
- Equal-premium portfolio return: **+28.58%**
- Max drawdown: **-4.73%**
- Average contract cost: **$677.82**

## Daily

| Date | Trades | Win rate | Daily return |
|---|---:|---:|---:|
| 2026-03-03 | 15 | 53.3% | +11.40% |
| 2026-03-04 | 29 | 37.9% | -4.73% |
| 2026-03-05 | 20 | 50.0% | +10.64% |
| 2026-03-06 | 49 | 55.1% | +9.51% |

## Top symbols

| Symbol | Side | Trades | Win rate | Avg option return | Best | Worst | Avg contract cost |
|---|---|---:|---:|---:|---:|---:|---:|
| COHR | SHORT | 1 | 100.0% | +117.38% | +117.38% | +117.38% | $902.00 |
| SBUX | LONG | 1 | 100.0% | +112.52% | +112.52% | +112.52% | $113.78 |
| F | SHORT | 2 | 100.0% | +47.28% | +62.06% | +32.49% | $28.19 |
| WFC | LONG | 3 | 100.0% | +29.46% | +48.49% | +2.07% | $196.46 |
| BX | LONG | 2 | 100.0% | +43.06% | +81.48% | +4.63% | $348.50 |
| CRWD | LONG | 1 | 100.0% | +77.74% | +77.74% | +77.74% | $1435.00 |
| LRCX | SHORT | 2 | 100.0% | +38.26% | +63.97% | +12.55% | $827.17 |
| STZ | SHORT | 1 | 100.0% | +75.98% | +75.98% | +75.98% | $205.00 |
| TXN | SHORT | 1 | 100.0% | +75.73% | +75.73% | +75.73% | $389.50 |
| LITE | SHORT | 1 | 100.0% | +73.73% | +73.73% | +73.73% | $3884.75 |
| GLW | SHORT | 1 | 100.0% | +62.21% | +62.21% | +62.21% | $486.87 |
| BAC | LONG | 2 | 100.0% | +29.83% | +30.63% | +29.03% | $90.20 |
| INTU | LONG | 1 | 100.0% | +58.51% | +58.51% | +58.51% | $1281.25 |
| TTD | LONG | 1 | 100.0% | +49.48% | +49.48% | +49.48% | $107.62 |
| XYZ | LONG | 2 | 100.0% | +23.59% | +38.40% | +8.78% | $193.21 |
| APP | LONG | 2 | 100.0% | +21.51% | +40.78% | +2.24% | $2410.80 |
| GDDY | LONG | 1 | 100.0% | +39.09% | +39.09% | +39.09% | $217.30 |
| FISV | LONG | 1 | 100.0% | +35.77% | +35.77% | +35.77% | $119.92 |
| IBM | LONG | 1 | 100.0% | +33.96% | +33.96% | +33.96% | $542.22 |
| T | LONG | 1 | 100.0% | +30.45% | +30.45% | +30.45% | $35.87 |
| CRM | LONG | 1 | 100.0% | +29.67% | +29.67% | +29.67% | $389.50 |
| BBY | LONG | 1 | 100.0% | +29.49% | +29.49% | +29.49% | $158.88 |
| WDAY | LONG | 2 | 50.0% | +23.64% | +53.28% | -6.00% | $434.09 |
| CMCSA | LONG | 2 | 50.0% | +20.52% | +59.23% | -18.20% | $49.20 |
| NCLH | SHORT | 1 | 100.0% | +18.51% | +18.51% | +18.51% | $62.52 |

## Caveats

- Historical option bars are used for premium returns. This does not reconstruct historical bid/ask quotes.
- The script therefore runs several explicit slippage assumptions.
- Historical Greeks are not reconstructed; strike moneyness is used instead of historical delta.
- This is still the same five-session geopolitical event sample, so parameter selection is in-sample and should not be treated as a guarantee of live results.