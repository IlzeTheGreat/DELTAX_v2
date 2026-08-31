from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

INPUT_FILE = SCRIPT_DIR / "market_5min_sp500_2026-02-28_5d.csv"

TRADES_FILE = SCRIPT_DIR / "iran_event_strategy_sp500_trades.csv"
THRESHOLD_FILE = SCRIPT_DIR / "iran_event_strategy_sp500_thresholds.csv"
SYMBOL_FILE = SCRIPT_DIR / "iran_event_strategy_sp500_symbols.csv"
DAILY_FILE = SCRIPT_DIR / "iran_event_strategy_sp500_daily.csv"


# ============================================================
# CONFIG
# ============================================================

OPENING_WINDOW_MINUTES = 30
MIN_OPENING_BARS = 6

# Reversal thresholds to test
REVERSAL_THRESHOLDS = [
    0.0050,   # 0.50%
    0.0075,   # 0.75%
    0.0100,   # 1.00%
    0.0125,   # 1.25%
    0.0150,   # 1.50%
]

# A stock becomes an event LONG/SHORT candidate only if it has
# outperformed / underperformed the universe by this amount
# using ONLY completed previous trading days.
BIAS_THRESHOLD = 0.02   # 2 percentage points


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )


# ============================================================
# HELPERS
# ============================================================

def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def profit_factor(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0

    gross_profit = returns[returns > 0].sum()
    gross_loss = abs(returns[returns < 0].sum())

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0

    return float(gross_profit / gross_loss)


def max_drawdown_from_daily_returns(
    returns: pd.Series,
) -> float:
    """
    Starts equity curve at 1.0 before first trading day.
    This correctly counts a loss on the first trade/day.
    """

    if returns.empty:
        return 0.0

    equity_values = [1.0]

    equity = 1.0

    for r in returns.fillna(0):
        equity *= (1 + float(r))
        equity_values.append(equity)

    curve = pd.Series(equity_values)

    peak = curve.cummax()
    drawdown = curve / peak - 1

    return float(drawdown.min())


# ============================================================
# LOAD
# ============================================================

def load_data() -> pd.DataFrame:

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file not found:\n{INPUT_FILE}"
        )

    print(f"Loading: {INPUT_FILE}")

    df = pd.read_csv(INPUT_FILE)

    df["timestamp_et"] = pd.to_datetime(
        df["timestamp_et"],
        utc=True,
    ).dt.tz_convert("America/New_York")

    df["trading_date"] = pd.to_datetime(
        df["trading_date"]
    ).dt.date

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "symbol",
            "open",
            "close",
        ]
    )

    df = df.sort_values(
        [
            "trading_date",
            "symbol",
            "timestamp_et",
        ]
    ).reset_index(drop=True)

    print(f"Rows: {len(df):,}")
    print(f"Symbols: {df['symbol'].nunique()}")
    print(f"Days: {df['trading_date'].nunique()}")

    return df


# ============================================================
# DAILY STOCK RETURNS
# ============================================================

def build_daily_returns(
    df: pd.DataFrame,
) -> pd.DataFrame:

    rows = []

    for (
        trading_date,
        symbol,
    ), g in df.groupby(
        [
            "trading_date",
            "symbol",
        ]
    ):

        g = g.sort_values("timestamp_et")

        day_open = float(
            g.iloc[0]["open"]
        )

        day_close = float(
            g.iloc[-1]["close"]
        )

        rows.append(
            {
                "trading_date": trading_date,
                "symbol": symbol,
                "day_open": day_open,
                "day_close": day_close,
                "day_return":
                    day_close / day_open - 1,
            }
        )

    return (
        pd.DataFrame(rows)
        .sort_values(
            [
                "trading_date",
                "symbol",
            ]
        )
        .reset_index(drop=True)
    )


# ============================================================
# CAUSAL EVENT BIAS
# ============================================================

def build_event_bias(
    daily: pd.DataFrame,
) -> pd.DataFrame:
    """
    IMPORTANT:

    For date D, bias is calculated ONLY from dates < D.

    Example:
    - 2 March: no previous event-session data => NO BIAS
    - 3 March: bias based only on 2 March
    - 4 March: bias based on 2 + 3 March
    etc.

    This eliminates the original 5-day look-ahead bias.
    """

    dates = sorted(
        daily["trading_date"].unique()
    )

    symbols = sorted(
        daily["symbol"].unique()
    )

    rows = []

    for current_date in dates:

        historical = daily[
            daily["trading_date"] < current_date
        ]

        # First event day has no prior event data.
        if historical.empty:

            for symbol in symbols:
                rows.append(
                    {
                        "trading_date": current_date,
                        "symbol": symbol,
                        "stock_cumulative_return": 0.0,
                        "universe_cumulative_return": 0.0,
                        "relative_return": 0.0,
                        "event_bias": "NONE",
                    }
                )

            continue

        # --------------------------------------------
        # Market cumulative return through yesterday
        # --------------------------------------------

        market_daily = (
            historical
            .groupby("trading_date")[
                "day_return"
            ]
            .mean()
            .sort_index()
        )

        market_cumulative = (
            (1 + market_daily).prod() - 1
        )

        # --------------------------------------------
        # Stock cumulative return through yesterday
        # --------------------------------------------

        for symbol in symbols:

            stock_history = historical[
                historical["symbol"] == symbol
            ]

            if stock_history.empty:

                stock_cumulative = 0.0

            else:

                stock_cumulative = (
                    (
                        1
                        + stock_history[
                            "day_return"
                        ]
                    ).prod()
                    - 1
                )

            relative = (
                stock_cumulative
                - market_cumulative
            )

            if relative >= BIAS_THRESHOLD:
                bias = "LONG"

            elif relative <= -BIAS_THRESHOLD:
                bias = "SHORT"

            else:
                bias = "NONE"

            rows.append(
                {
                    "trading_date": current_date,
                    "symbol": symbol,
                    "stock_cumulative_return":
                        stock_cumulative,
                    "universe_cumulative_return":
                        market_cumulative,
                    "relative_return":
                        relative,
                    "event_bias":
                        bias,
                }
            )

    return pd.DataFrame(rows)


# ============================================================
# RUN ONE THRESHOLD
# ============================================================

def run_threshold(
    df: pd.DataFrame,
    bias_df: pd.DataFrame,
    threshold: float,
) -> pd.DataFrame:

    trades = []

    bias_lookup = {
        (
            row["trading_date"],
            row["symbol"],
        ): row

        for _, row in bias_df.iterrows()
    }

    for (
        trading_date,
        symbol,
    ), g in df.groupby(
        [
            "trading_date",
            "symbol",
        ]
    ):

        bias_row = bias_lookup.get(
            (
                trading_date,
                symbol,
            )
        )

        if bias_row is None:
            continue

        event_bias = bias_row[
            "event_bias"
        ]

        if event_bias == "NONE":
            continue

        g = (
            g.sort_values("timestamp_et")
            .reset_index(drop=True)
        )

        opening_bars = g.iloc[
            :OPENING_WINDOW_MINUTES // 5
        ]

        if len(opening_bars) < MIN_OPENING_BARS:
            continue

        day_open = float(
            opening_bars.iloc[0]["open"]
        )

        entry_price = float(
            opening_bars.iloc[-1]["close"]
        )

        exit_price = float(
            g.iloc[-1]["close"]
        )

        opening_return = (
            entry_price / day_open - 1
        )

        # ====================================================
        # LONG EVENT BIAS
        #
        # Stock has already demonstrated relative strength
        # through yesterday.
        #
        # Today it sells off sharply in first 30 min.
        # Buy reversal.
        # ====================================================

        if (
            event_bias == "LONG"
            and opening_return <= -threshold
        ):

            trade_return = (
                exit_price / entry_price - 1
            )

            trades.append(
                {
                    "threshold": threshold,
                    "trading_date": trading_date,
                    "symbol": symbol,
                    "event_bias": "LONG",
                    "trade_direction": "LONG",
                    "prior_relative_return":
                        bias_row[
                            "relative_return"
                        ],
                    "opening_return":
                        opening_return,
                    "entry_price":
                        entry_price,
                    "exit_price":
                        exit_price,
                    "trade_return":
                        trade_return,
                }
            )

        # ====================================================
        # SHORT EVENT BIAS
        #
        # Stock has already demonstrated relative weakness.
        #
        # Today it rallies sharply in first 30 min.
        # Short reversal.
        # ====================================================

        elif (
            event_bias == "SHORT"
            and opening_return >= threshold
        ):

            trade_return = (
                entry_price - exit_price
            ) / entry_price

            trades.append(
                {
                    "threshold": threshold,
                    "trading_date": trading_date,
                    "symbol": symbol,
                    "event_bias": "SHORT",
                    "trade_direction": "SHORT",
                    "prior_relative_return":
                        bias_row[
                            "relative_return"
                        ],
                    "opening_return":
                        opening_return,
                    "entry_price":
                        entry_price,
                    "exit_price":
                        exit_price,
                    "trade_return":
                        trade_return,
                }
            )

    return pd.DataFrame(trades)


# ============================================================
# DAILY PORTFOLIO RETURNS
# ============================================================

def build_daily_portfolio(
    trades: pd.DataFrame,
) -> pd.DataFrame:
    """
    If several trades occur on same date,
    assume equal capital allocation across them.

    This is much more defensible than pretending
    100% of the portfolio was sequentially reinvested
    into every simultaneous trade.
    """

    if trades.empty:
        return pd.DataFrame()

    daily = (
        trades
        .groupby("trading_date")
        .agg(
            trades=("symbol", "count"),
            daily_return=(
                "trade_return",
                "mean",
            ),
            wins=(
                "trade_return",
                lambda x: int(
                    (x > 0).sum()
                ),
            ),
        )
        .reset_index()
        .sort_values(
            "trading_date"
        )
    )

    daily["win_rate"] = (
        daily["wins"]
        / daily["trades"]
    )

    return daily


# ============================================================
# SUMMARY FOR EACH THRESHOLD
# ============================================================

def summarize_threshold(
    threshold: float,
    trades: pd.DataFrame,
) -> dict:

    if trades.empty:

        return {
            "threshold": threshold,
            "trades": 0,
            "long_trades": 0,
            "short_trades": 0,
            "win_rate": 0.0,
            "avg_trade_return": 0.0,
            "median_trade_return": 0.0,
            "profit_factor": 0.0,
            "portfolio_return": 0.0,
            "max_drawdown": 0.0,
        }

    returns = trades["trade_return"]

    daily = build_daily_portfolio(
        trades
    )

    if daily.empty:

        portfolio_return = 0.0
        max_dd = 0.0

    else:

        portfolio_return = (
            (
                1
                + daily[
                    "daily_return"
                ]
            ).prod()
            - 1
        )

        max_dd = max_drawdown_from_daily_returns(
            daily["daily_return"]
        )

    return {
        "threshold": threshold,
        "trades": len(trades),
        "long_trades": int(
            (
                trades["trade_direction"]
                == "LONG"
            ).sum()
        ),
        "short_trades": int(
            (
                trades["trade_direction"]
                == "SHORT"
            ).sum()
        ),
        "win_rate": (
            returns > 0
        ).mean(),
        "avg_trade_return":
            returns.mean(),
        "median_trade_return":
            returns.median(),
        "profit_factor":
            profit_factor(
                returns
            ),
        "portfolio_return":
            portfolio_return,
        "max_drawdown":
            max_dd,
    }


# ============================================================
# SYMBOL SUMMARY
# ============================================================

def build_symbol_summary(
    trades: pd.DataFrame,
) -> pd.DataFrame:

    if trades.empty:
        return pd.DataFrame()

    result = (
        trades
        .groupby(
            [
                "threshold",
                "symbol",
                "trade_direction",
            ]
        )
        .agg(
            trades=(
                "trade_return",
                "count",
            ),
            wins=(
                "trade_return",
                lambda x:
                    int((x > 0).sum()),
            ),
            win_rate=(
                "trade_return",
                lambda x:
                    (x > 0).mean(),
            ),
            avg_return=(
                "trade_return",
                "mean",
            ),
            total_return=(
                "trade_return",
                "sum",
            ),
        )
        .reset_index()
    )

    return result.sort_values(
        [
            "threshold",
            "avg_return",
        ],
        ascending=[
            True,
            False,
        ],
    )


# ============================================================
# PRINT RESULTS
# ============================================================

def print_threshold_summary(
    summary: pd.DataFrame,
) -> None:

    print()
    print("=" * 92)
    print("THRESHOLD COMPARISON — NO LOOK-AHEAD")
    print("=" * 92)

    for _, r in summary.iterrows():

        pf = r["profit_factor"]

        if pf == float("inf"):
            pf_text = "INF"
        else:
            pf_text = f"{pf:.2f}"

        print(
            f"{r['threshold'] * 100:4.2f}% | "
            f"trades={int(r['trades']):3} | "
            f"L={int(r['long_trades']):2} "
            f"S={int(r['short_trades']):2} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(r['avg_trade_return']):>8} | "
            f"PF={pf_text:>5} | "
            f"portfolio={pct(r['portfolio_return']):>8} | "
            f"DD={pct(r['max_drawdown']):>8}"
        )


def print_best_threshold(
    summary: pd.DataFrame,
    all_trades: pd.DataFrame,
) -> None:

    valid = summary[
        summary["trades"] > 0
    ].copy()

    if valid.empty:
        return

    # Prefer:
    # 1. positive expectancy
    # 2. decent sample size
    # 3. strong PF
    #
    # Simple ranking score.
    valid["score"] = (
        valid["avg_trade_return"]
        * valid["win_rate"]
        * valid["trades"]
    )

    best = valid.sort_values(
        "score",
        ascending=False,
    ).iloc[0]

    threshold = float(
        best["threshold"]
    )

    trades = all_trades[
        all_trades["threshold"]
        == threshold
    ]

    print()
    print("=" * 92)
    print(
        f"BEST TESTED THRESHOLD: "
        f"{threshold * 100:.2f}%"
    )
    print("=" * 92)

    print(
        f"Trades:           {int(best['trades'])}"
    )

    print(
        f"Win rate:         "
        f"{best['win_rate'] * 100:.1f}%"
    )

    print(
        f"Average trade:    "
        f"{pct(best['avg_trade_return'])}"
    )

    print(
        f"Portfolio return: "
        f"{pct(best['portfolio_return'])}"
    )

    print(
        f"Max drawdown:     "
        f"{pct(best['max_drawdown'])}"
    )

    print()
    print("TRADES")
    print("-" * 92)

    for _, r in trades.sort_values(
        [
            "trading_date",
            "symbol",
        ]
    ).iterrows():

        result = (
            "WIN"
            if r["trade_return"] > 0
            else "LOSS"
        )

        print(
            f"{r['trading_date']} | "
            f"{r['symbol']:6} | "
            f"{r['trade_direction']:5} | "
            f"prior rel="
            f"{pct(r['prior_relative_return']):>8} | "
            f"open="
            f"{pct(r['opening_return']):>8} | "
            f"P&L="
            f"{pct(r['trade_return']):>8} | "
            f"{result}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    print("=" * 92)
    print("IRAN EVENT STRATEGY V2")
    print("CAUSAL EVENT BIAS + OPENING REVERSAL")
    print("=" * 92)

    print()
    print(
        f"Bias threshold: "
        f"{BIAS_THRESHOLD * 100:.2f}% "
        f"relative performance"
    )

    print(
        f"Opening window: "
        f"{OPENING_WINDOW_MINUTES} min"
    )

    print(
        "Reversal thresholds: "
        + ", ".join(
            f"{x * 100:.2f}%"
            for x in REVERSAL_THRESHOLDS
        )
    )

    df = load_data()

    print()
    print(
        "Building daily returns..."
    )

    daily = build_daily_returns(
        df
    )

    print(
        "Building causal event bias..."
    )

    bias_df = build_event_bias(
        daily
    )

    # Useful console inspection:
    print()
    print("EVENT BIAS BY DAY")
    print("-" * 92)

    for trading_date, g in bias_df.groupby(
        "trading_date"
    ):

        longs = list(
            g[
                g["event_bias"] == "LONG"
            ]["symbol"]
        )

        shorts = list(
            g[
                g["event_bias"] == "SHORT"
            ]["symbol"]
        )

        print()
        print(trading_date)

        print(
            "  LONG:  "
            + (
                ", ".join(longs)
                if longs
                else "none"
            )
        )

        print(
            "  SHORT: "
            + (
                ", ".join(shorts)
                if shorts
                else "none"
            )
        )

    # ========================================================
    # RUN ALL THRESHOLDS
    # ========================================================

    summaries = []
    trade_frames = []

    print()
    print(
        "Running threshold tests..."
    )

    for threshold in REVERSAL_THRESHOLDS:

        trades = run_threshold(
            df=df,
            bias_df=bias_df,
            threshold=threshold,
        )

        summary = summarize_threshold(
            threshold=threshold,
            trades=trades,
        )

        summaries.append(
            summary
        )

        if not trades.empty:
            trade_frames.append(
                trades
            )

    summary_df = pd.DataFrame(
        summaries
    )

    if trade_frames:

        all_trades = pd.concat(
            trade_frames,
            ignore_index=True,
        )

    else:

        all_trades = pd.DataFrame()

    # ========================================================
    # SAVE
    # ========================================================

    summary_df.to_csv(
        THRESHOLD_FILE,
        index=False,
    )

    if not all_trades.empty:

        all_trades.to_csv(
            TRADES_FILE,
            index=False,
        )

        symbols = build_symbol_summary(
            all_trades
        )

        symbols.to_csv(
            SYMBOL_FILE,
            index=False,
        )

        all_daily = []

        for threshold, g in all_trades.groupby(
            "threshold"
        ):

            x = build_daily_portfolio(
                g
            )

            x.insert(
                0,
                "threshold",
                threshold,
            )

            all_daily.append(x)

        if all_daily:

            pd.concat(
                all_daily,
                ignore_index=True,
            ).to_csv(
                DAILY_FILE,
                index=False,
            )

    # ========================================================
    # PRINT
    # ========================================================

    print_threshold_summary(
        summary_df
    )

    if not all_trades.empty:

        print_best_threshold(
            summary_df,
            all_trades,
        )

    print()
    print("=" * 92)
    print("FILES")
    print("=" * 92)

    print(
        f"Threshold summary: {THRESHOLD_FILE}"
    )

    print(
        f"Trades:            {TRADES_FILE}"
    )

    print(
        f"Symbols:           {SYMBOL_FILE}"
    )

    print(
        f"Daily portfolio:   {DAILY_FILE}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())