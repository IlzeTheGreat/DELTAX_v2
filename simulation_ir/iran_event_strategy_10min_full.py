from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

INPUT_FILE = SCRIPT_DIR / "market_5min_sp500_2026-02-28_5d.csv"

GRID_FILE = SCRIPT_DIR / "iran_event_10min_grid.csv"
TRADES_FILE = SCRIPT_DIR / "iran_event_10min_all_trades.csv"
BEST_TRADES_FILE = SCRIPT_DIR / "iran_event_10min_best_trades.csv"
BEST_SYMBOLS_FILE = SCRIPT_DIR / "iran_event_10min_best_symbols.csv"
BEST_DAILY_FILE = SCRIPT_DIR / "iran_event_10min_best_daily.csv"
REPORT_FILE = SCRIPT_DIR / "iran_event_10min_report.md"


# ============================================================
# CONFIG
# ============================================================

OPENING_WINDOW_MINUTES = 10
MIN_OPENING_BARS = 2

# We test several causal relative-strength thresholds.
BIAS_THRESHOLDS = [
    0.0100,   # 1.00%
    0.0150,   # 1.50%
    0.0200,   # 2.00%
    0.0250,   # 2.50%
    0.0300,   # 3.00%
]

# 10-minute reversal thresholds.
REVERSAL_THRESHOLDS = [
    0.0015,   # 0.15%
    0.0025,   # 0.25%
    0.0035,   # 0.35%
    0.0050,   # 0.50%
    0.0075,   # 0.75%
    0.0100,   # 1.00%
]

# Minimum sample to consider a grid result "usable".
MIN_TRADES_FOR_BEST = 25

# Optional stricter confirmation:
# threshold_only:
#   LONG bias + first 10 min down enough -> LONG at 10:00
#   SHORT bias + first 10 min up enough -> SHORT at 10:00
#
# second_bar_reversal:
#   same condition, but second 5-min bar must already move
#   back in the expected trade direction.
ENTRY_MODES = [
    "threshold_only",
    "second_bar_reversal",
]


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# HELPERS
# ============================================================

def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def profit_factor(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0

    gross_profit = float(returns[returns > 0].sum())
    gross_loss = abs(float(returns[returns < 0].sum()))

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0

    return gross_profit / gross_loss


def max_drawdown_from_daily_returns(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0

    equity_values = [1.0]
    equity = 1.0

    for r in returns.fillna(0.0):
        equity *= (1.0 + float(r))
        equity_values.append(equity)

    curve = pd.Series(equity_values, dtype=float)
    peak = curve.cummax()
    drawdown = curve / peak - 1.0

    return float(drawdown.min())


def load_data() -> pd.DataFrame:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input file not found:\n{INPUT_FILE}")

    print(f"Loading: {INPUT_FILE}")

    df = pd.read_csv(INPUT_FILE)

    required = {
        "timestamp_et",
        "trading_date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }

    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing required columns: {sorted(missing)}")

    df["timestamp_et"] = pd.to_datetime(
        df["timestamp_et"],
        utc=True,
    ).dt.tz_convert("America/New_York")

    df["trading_date"] = pd.to_datetime(df["trading_date"]).dt.date

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["symbol", "open", "close"])

    df = df.sort_values(
        ["trading_date", "symbol", "timestamp_et"]
    ).reset_index(drop=True)

    print(f"Rows: {len(df):,}")
    print(f"Symbols: {df['symbol'].nunique()}")
    print(f"Days: {df['trading_date'].nunique()}")

    return df


# ============================================================
# DAILY RETURNS + CAUSAL BIAS
# ============================================================

def build_daily_returns(df: pd.DataFrame) -> pd.DataFrame:
    rows = []

    for (trading_date, symbol), g in df.groupby(
        ["trading_date", "symbol"],
        sort=True,
    ):
        g = g.sort_values("timestamp_et")

        day_open = float(g.iloc[0]["open"])
        day_close = float(g.iloc[-1]["close"])

        rows.append(
            {
                "trading_date": trading_date,
                "symbol": symbol,
                "day_open": day_open,
                "day_close": day_close,
                "day_return": day_close / day_open - 1.0,
            }
        )

    return pd.DataFrame(rows).sort_values(
        ["trading_date", "symbol"]
    ).reset_index(drop=True)


def build_event_bias(
    daily: pd.DataFrame,
    bias_threshold: float,
) -> pd.DataFrame:
    """
    No look-ahead:
    bias for date D uses only event-session returns from dates < D.
    """

    dates = sorted(daily["trading_date"].unique())
    symbols = sorted(daily["symbol"].unique())

    rows = []

    for current_date in dates:
        historical = daily[daily["trading_date"] < current_date]

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

        market_daily = (
            historical
            .groupby("trading_date")["day_return"]
            .mean()
            .sort_index()
        )

        market_cumulative = float((1.0 + market_daily).prod() - 1.0)

        stock_returns = (
            historical
            .groupby("symbol")["day_return"]
            .apply(lambda x: float((1.0 + x).prod() - 1.0))
            .to_dict()
        )

        for symbol in symbols:
            stock_cumulative = float(stock_returns.get(symbol, 0.0))
            relative = stock_cumulative - market_cumulative

            if relative >= bias_threshold:
                bias = "LONG"
            elif relative <= -bias_threshold:
                bias = "SHORT"
            else:
                bias = "NONE"

            rows.append(
                {
                    "trading_date": current_date,
                    "symbol": symbol,
                    "stock_cumulative_return": stock_cumulative,
                    "universe_cumulative_return": market_cumulative,
                    "relative_return": relative,
                    "event_bias": bias,
                }
            )

    return pd.DataFrame(rows)


# ============================================================
# ENTRY LOGIC
# ============================================================

def second_bar_confirms(opening_bars: pd.DataFrame, direction: str) -> bool:
    """
    First 10 minutes = two 5-minute bars.

    LONG:
      second 5-min bar closes above its own open.
    SHORT:
      second 5-min bar closes below its own open.
    """
    if len(opening_bars) < 2:
        return False

    second = opening_bars.iloc[1]
    second_open = float(second["open"])
    second_close = float(second["close"])

    if direction == "LONG":
        return second_close > second_open

    if direction == "SHORT":
        return second_close < second_open

    return False


def run_strategy(
    df: pd.DataFrame,
    bias_df: pd.DataFrame,
    bias_threshold: float,
    reversal_threshold: float,
    entry_mode: str,
) -> pd.DataFrame:

    bias_lookup = {
        (row["trading_date"], row["symbol"]): row
        for _, row in bias_df.iterrows()
    }

    trades = []

    for (trading_date, symbol), g in df.groupby(
        ["trading_date", "symbol"],
        sort=True,
    ):
        bias_row = bias_lookup.get((trading_date, symbol))
        if bias_row is None:
            continue

        event_bias = bias_row["event_bias"]
        if event_bias == "NONE":
            continue

        g = g.sort_values("timestamp_et").reset_index(drop=True)

        opening_bars = g.iloc[: OPENING_WINDOW_MINUTES // 5]

        if len(opening_bars) < MIN_OPENING_BARS:
            continue

        day_open = float(opening_bars.iloc[0]["open"])
        entry_price = float(opening_bars.iloc[-1]["close"])
        exit_price = float(g.iloc[-1]["close"])

        opening_return = entry_price / day_open - 1.0

        trade_direction = None

        if event_bias == "LONG" and opening_return <= -reversal_threshold:
            trade_direction = "LONG"

        elif event_bias == "SHORT" and opening_return >= reversal_threshold:
            trade_direction = "SHORT"

        if trade_direction is None:
            continue

        if entry_mode == "second_bar_reversal":
            if not second_bar_confirms(opening_bars, trade_direction):
                continue

        if trade_direction == "LONG":
            trade_return = exit_price / entry_price - 1.0
        else:
            trade_return = (entry_price - exit_price) / entry_price

        trades.append(
            {
                "bias_threshold": bias_threshold,
                "reversal_threshold": reversal_threshold,
                "entry_mode": entry_mode,
                "trading_date": trading_date,
                "symbol": symbol,
                "event_bias": event_bias,
                "trade_direction": trade_direction,
                "prior_relative_return": float(bias_row["relative_return"]),
                "opening_return_10m": opening_return,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "trade_return": trade_return,
                "result": "WIN" if trade_return > 0 else "LOSS",
            }
        )

    return pd.DataFrame(trades)


# ============================================================
# PORTFOLIO / SUMMARY
# ============================================================

def build_daily_portfolio(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Equal capital across all same-day trades.
    """
    if trades.empty:
        return pd.DataFrame()

    daily = (
        trades
        .groupby("trading_date")
        .agg(
            trades=("symbol", "count"),
            wins=("trade_return", lambda x: int((x > 0).sum())),
            daily_return=("trade_return", "mean"),
        )
        .reset_index()
        .sort_values("trading_date")
    )

    daily["win_rate"] = daily["wins"] / daily["trades"]

    return daily


def summarize_run(
    trades: pd.DataFrame,
    bias_threshold: float,
    reversal_threshold: float,
    entry_mode: str,
) -> dict:

    if trades.empty:
        return {
            "bias_threshold": bias_threshold,
            "reversal_threshold": reversal_threshold,
            "entry_mode": entry_mode,
            "trades": 0,
            "long_trades": 0,
            "short_trades": 0,
            "win_rate": 0.0,
            "avg_trade_return": 0.0,
            "median_trade_return": 0.0,
            "profit_factor": 0.0,
            "portfolio_return": 0.0,
            "max_drawdown": 0.0,
            "positive_days": 0,
            "trading_days": 0,
        }

    returns = trades["trade_return"]

    daily = build_daily_portfolio(trades)

    portfolio_return = float((1.0 + daily["daily_return"]).prod() - 1.0)
    max_dd = max_drawdown_from_daily_returns(daily["daily_return"])

    return {
        "bias_threshold": bias_threshold,
        "reversal_threshold": reversal_threshold,
        "entry_mode": entry_mode,
        "trades": len(trades),
        "long_trades": int((trades["trade_direction"] == "LONG").sum()),
        "short_trades": int((trades["trade_direction"] == "SHORT").sum()),
        "win_rate": float((returns > 0).mean()),
        "avg_trade_return": float(returns.mean()),
        "median_trade_return": float(returns.median()),
        "profit_factor": profit_factor(returns),
        "portfolio_return": portfolio_return,
        "max_drawdown": max_dd,
        "positive_days": int((daily["daily_return"] > 0).sum()),
        "trading_days": len(daily),
    }


def build_symbol_summary(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    out = (
        trades
        .groupby(["symbol", "trade_direction"])
        .agg(
            trades=("trade_return", "count"),
            wins=("trade_return", lambda x: int((x > 0).sum())),
            win_rate=("trade_return", lambda x: float((x > 0).mean())),
            avg_return=("trade_return", "mean"),
            median_return=("trade_return", "median"),
            total_return=("trade_return", "sum"),
            best_trade=("trade_return", "max"),
            worst_trade=("trade_return", "min"),
            avg_prior_relative=("prior_relative_return", "mean"),
            avg_opening_10m=("opening_return_10m", "mean"),
        )
        .reset_index()
    )

    out["score"] = (
        out["avg_return"]
        * out["win_rate"]
        * out["trades"].clip(upper=5)
    )

    return out.sort_values(
        ["score", "avg_return"],
        ascending=[False, False],
    ).reset_index(drop=True)


# ============================================================
# BEST-RUN SELECTION
# ============================================================

def choose_best_run(grid: pd.DataFrame) -> pd.Series:
    valid = grid[
        (grid["trades"] >= MIN_TRADES_FOR_BEST)
        & (grid["avg_trade_return"] > 0)
        & (grid["profit_factor"] > 1)
    ].copy()

    if valid.empty:
        valid = grid[grid["trades"] > 0].copy()

    # Composite score:
    # reward expectancy, win rate, PF and sample size;
    # lightly penalize drawdown.
    pf_for_score = valid["profit_factor"].replace(float("inf"), 10.0).clip(upper=10.0)

    valid["rank_score"] = (
        valid["avg_trade_return"]
        * valid["win_rate"]
        * pf_for_score
        * valid["trades"].clip(upper=250) ** 0.5
        * (1.0 + valid["portfolio_return"].clip(lower=-0.99))
        / (1.0 + valid["max_drawdown"].abs())
    )

    return valid.sort_values(
        ["rank_score", "portfolio_return", "avg_trade_return"],
        ascending=False,
    ).iloc[0]


# ============================================================
# REPORT
# ============================================================

def generate_report(
    grid: pd.DataFrame,
    best: pd.Series,
    best_trades: pd.DataFrame,
    symbols: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:

    lines = []

    lines.append("# Iran event 10-minute strategy backtest")
    lines.append("")
    lines.append("## Best tested configuration")
    lines.append("")
    lines.append(f"- Bias threshold: **{best['bias_threshold'] * 100:.2f}%**")
    lines.append(f"- 10-minute reversal threshold: **{best['reversal_threshold'] * 100:.2f}%**")
    lines.append(f"- Entry mode: **{best['entry_mode']}**")
    lines.append(f"- Trades: **{int(best['trades'])}**")
    lines.append(f"- Win rate: **{best['win_rate'] * 100:.1f}%**")
    lines.append(f"- Average trade: **{pct(float(best['avg_trade_return']))}**")
    lines.append(f"- Median trade: **{pct(float(best['median_trade_return']))}**")

    pf = float(best["profit_factor"])
    lines.append(
        f"- Profit factor: **{'INF' if math.isinf(pf) else f'{pf:.2f}'}**"
    )

    lines.append(f"- Equal-weight portfolio return: **{pct(float(best['portfolio_return']))}**")
    lines.append(f"- Max drawdown: **{pct(float(best['max_drawdown']))}**")
    lines.append("")

    lines.append("## Top 15 symbols")
    lines.append("")
    lines.append("| Symbol | Side | Trades | Win rate | Avg return | Best | Worst |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")

    for _, r in symbols.head(15).iterrows():
        lines.append(
            f"| {r['symbol']} | {r['trade_direction']} | {int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | {pct(float(r['avg_return']))} | "
            f"{pct(float(r['best_trade']))} | {pct(float(r['worst_trade']))} |"
        )

    lines.append("")
    lines.append("## Daily portfolio")
    lines.append("")
    lines.append("| Date | Trades | Win rate | Daily return |")
    lines.append("|---|---:|---:|---:|")

    for _, r in daily.iterrows():
        lines.append(
            f"| {r['trading_date']} | {int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | {pct(float(r['daily_return']))} |"
        )

    lines.append("")
    lines.append("## Important")
    lines.append("")
    lines.append(
        "This remains a five-session event study. The script removes look-ahead "
        "from the daily event-bias calculation, but the parameter search itself "
        "still chooses the best configuration from the same historical window. "
        "Treat the best grid result as a candidate live rule, not a guarantee."
    )

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# CONSOLE
# ============================================================

def print_top_grid(grid: pd.DataFrame) -> None:
    print()
    print("=" * 118)
    print("TOP GRID RESULTS")
    print("=" * 118)

    top = grid.copy()
    top = top[top["trades"] > 0]

    pf_score = top["profit_factor"].replace(float("inf"), 10.0).clip(upper=10.0)

    top["display_score"] = (
        top["avg_trade_return"]
        * top["win_rate"]
        * pf_score
        * top["trades"].clip(upper=250) ** 0.5
    )

    top = top.sort_values(
        ["display_score", "portfolio_return"],
        ascending=False,
    ).head(20)

    for _, r in top.iterrows():
        pf = r["profit_factor"]
        pf_text = "INF" if math.isinf(pf) else f"{pf:.2f}"

        print(
            f"bias={r['bias_threshold'] * 100:4.2f}% | "
            f"rev={r['reversal_threshold'] * 100:4.2f}% | "
            f"{r['entry_mode']:20} | "
            f"trades={int(r['trades']):3} | "
            f"L={int(r['long_trades']):3} S={int(r['short_trades']):3} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(float(r['avg_trade_return'])):>8} | "
            f"PF={pf_text:>5} | "
            f"portfolio={pct(float(r['portfolio_return'])):>8} | "
            f"DD={pct(float(r['max_drawdown'])):>8}"
        )


def print_best(best: pd.Series, trades: pd.DataFrame, symbols: pd.DataFrame, daily: pd.DataFrame) -> None:
    print()
    print("=" * 118)
    print("BEST CONFIGURATION")
    print("=" * 118)

    pf = float(best["profit_factor"])
    pf_text = "INF" if math.isinf(pf) else f"{pf:.2f}"

    print(f"Bias threshold:          {best['bias_threshold'] * 100:.2f}%")
    print(f"10m reversal threshold:  {best['reversal_threshold'] * 100:.2f}%")
    print(f"Entry mode:              {best['entry_mode']}")
    print(f"Trades:                  {int(best['trades'])}")
    print(f"LONG / SHORT:            {int(best['long_trades'])} / {int(best['short_trades'])}")
    print(f"Win rate:                {best['win_rate'] * 100:.1f}%")
    print(f"Average trade:           {pct(float(best['avg_trade_return']))}")
    print(f"Median trade:            {pct(float(best['median_trade_return']))}")
    print(f"Profit factor:           {pf_text}")
    print(f"Portfolio return:        {pct(float(best['portfolio_return']))}")
    print(f"Max drawdown:            {pct(float(best['max_drawdown']))}")

    print()
    print("DAILY")
    print("-" * 118)

    for _, r in daily.iterrows():
        print(
            f"{r['trading_date']} | "
            f"trades={int(r['trades']):3} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"daily={pct(float(r['daily_return'])):>8}"
        )

    print()
    print("TOP SYMBOLS")
    print("-" * 118)

    for _, r in symbols.head(25).iterrows():
        print(
            f"{r['symbol']:6} {r['trade_direction']:5} | "
            f"trades={int(r['trades']):2} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(float(r['avg_return'])):>8} | "
            f"best={pct(float(r['best_trade'])):>8} | "
            f"worst={pct(float(r['worst_trade'])):>8}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    print("=" * 118)
    print("IRAN EVENT 10-MINUTE FULL GRID BACKTEST")
    print("CAUSAL EVENT BIAS + 10-MIN REVERSAL")
    print("=" * 118)

    print()
    print(
        "Bias thresholds: "
        + ", ".join(f"{x * 100:.2f}%" for x in BIAS_THRESHOLDS)
    )
    print(
        "10m reversal thresholds: "
        + ", ".join(f"{x * 100:.2f}%" for x in REVERSAL_THRESHOLDS)
    )
    print(
        "Entry modes: "
        + ", ".join(ENTRY_MODES)
    )

    df = load_data()

    print()
    print("Building daily returns...")
    daily_returns = build_daily_returns(df)

    grid_rows = []
    all_trade_frames = []

    total_tests = (
        len(BIAS_THRESHOLDS)
        * len(REVERSAL_THRESHOLDS)
        * len(ENTRY_MODES)
    )

    test_no = 0

    # Cache bias tables by threshold.
    bias_cache = {}

    for bias_threshold in BIAS_THRESHOLDS:
        print()
        print(f"Building causal bias for {bias_threshold * 100:.2f}%...")
        bias_cache[bias_threshold] = build_event_bias(
            daily_returns,
            bias_threshold=bias_threshold,
        )

    print()
    print(f"Running {total_tests} strategy combinations...")

    for bias_threshold in BIAS_THRESHOLDS:
        bias_df = bias_cache[bias_threshold]

        for reversal_threshold in REVERSAL_THRESHOLDS:
            for entry_mode in ENTRY_MODES:
                test_no += 1

                trades = run_strategy(
                    df=df,
                    bias_df=bias_df,
                    bias_threshold=bias_threshold,
                    reversal_threshold=reversal_threshold,
                    entry_mode=entry_mode,
                )

                summary = summarize_run(
                    trades=trades,
                    bias_threshold=bias_threshold,
                    reversal_threshold=reversal_threshold,
                    entry_mode=entry_mode,
                )

                grid_rows.append(summary)

                if not trades.empty:
                    all_trade_frames.append(trades)

                print(
                    f"[{test_no:02}/{total_tests}] "
                    f"bias={bias_threshold * 100:.2f}% "
                    f"rev={reversal_threshold * 100:.2f}% "
                    f"mode={entry_mode} "
                    f"trades={summary['trades']} "
                    f"win={summary['win_rate'] * 100:.1f}% "
                    f"avg={pct(summary['avg_trade_return'])}"
                )

    grid = pd.DataFrame(grid_rows)

    if all_trade_frames:
        all_trades = pd.concat(all_trade_frames, ignore_index=True)
    else:
        all_trades = pd.DataFrame()

    grid.to_csv(GRID_FILE, index=False)

    if not all_trades.empty:
        all_trades.to_csv(TRADES_FILE, index=False)

    print_top_grid(grid)

    best = choose_best_run(grid)

    best_trades = all_trades[
        (all_trades["bias_threshold"] == float(best["bias_threshold"]))
        & (all_trades["reversal_threshold"] == float(best["reversal_threshold"]))
        & (all_trades["entry_mode"] == best["entry_mode"])
    ].copy()

    best_symbols = build_symbol_summary(best_trades)
    best_daily = build_daily_portfolio(best_trades)

    best_trades.to_csv(BEST_TRADES_FILE, index=False)
    best_symbols.to_csv(BEST_SYMBOLS_FILE, index=False)
    best_daily.to_csv(BEST_DAILY_FILE, index=False)

    generate_report(
        grid=grid,
        best=best,
        best_trades=best_trades,
        symbols=best_symbols,
        daily=best_daily,
    )

    print_best(
        best=best,
        trades=best_trades,
        symbols=best_symbols,
        daily=best_daily,
    )

    print()
    print("=" * 118)
    print("FILES CREATED")
    print("=" * 118)
    print(f"Grid:         {GRID_FILE}")
    print(f"All trades:   {TRADES_FILE}")
    print(f"Best trades:  {BEST_TRADES_FILE}")
    print(f"Best symbols: {BEST_SYMBOLS_FILE}")
    print(f"Best daily:   {BEST_DAILY_FILE}")
    print(f"Report:       {REPORT_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
