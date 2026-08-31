from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

INPUT_FILE = SCRIPT_DIR / "market_5min_2026-02-28_5d.csv"

TRADES_FILE = SCRIPT_DIR / "iran_event_strategy_trades.csv"
SUMMARY_FILE = SCRIPT_DIR / "iran_event_strategy_summary.csv"


# ============================================================
# STRATEGY CONFIG
# ============================================================

OPENING_WINDOW_MINUTES = 30

# Reversal threshold
REVERSAL_THRESHOLD = 0.0075   # 0.75%

# Optional minimum number of bars expected during first 30 min
MIN_OPENING_BARS = 6


# ============================================================
# EVENT BIAS
# ============================================================

LONG_EVENT_LIST = {
    "NOW",
    "PANW",
    "IBM",
    "RKLB",
    "ORCL",
    "SNPS",
    "BKNG",
    "ADBE",
    "AMZN",
    "SPGI",
    "AMD",
    "NVDA",
    "MSFT",
}

SHORT_EVENT_LIST = {
    "SLB",
    "XOM",
    "VRTX",
    "HAL",
    "LRCX",
    "ELV",
    "PM",
    "PG",
    "OXY",
    "COP",
    "DVN",
    "JCI",
}


# ============================================================
# CONSOLE
# ============================================================

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
    gross_profit = returns[returns > 0].sum()
    gross_loss = abs(returns[returns < 0].sum())

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0

    return gross_profit / gross_loss


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0

    equity = (1 + returns).cumprod()
    peak = equity.cummax()

    drawdown = equity / peak - 1

    return float(drawdown.min())


# ============================================================
# LOAD DATA
# ============================================================

def load_data() -> pd.DataFrame:

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file not found:\n{INPUT_FILE}"
        )

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
        raise RuntimeError(
            f"Missing columns: {sorted(missing)}"
        )

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

    return df.sort_values(
        [
            "trading_date",
            "symbol",
            "timestamp_et",
        ]
    ).reset_index(drop=True)


# ============================================================
# STRATEGY
# ============================================================

def run_strategy(
    df: pd.DataFrame,
) -> pd.DataFrame:

    trades = []

    relevant_symbols = (
        LONG_EVENT_LIST
        | SHORT_EVENT_LIST
    )

    df = df[
        df["symbol"].isin(relevant_symbols)
    ]

    grouped = df.groupby(
        [
            "trading_date",
            "symbol",
        ]
    )

    for (
        trading_date,
        symbol,
    ), g in grouped:

        g = g.sort_values(
            "timestamp_et"
        ).reset_index(drop=True)

        if len(g) < MIN_OPENING_BARS:
            continue

        # --------------------------------------------
        # First 30 minutes = first 6 x 5min bars
        # --------------------------------------------

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

        day_close = float(
            g.iloc[-1]["close"]
        )

        opening_return = (
            entry_price / day_open - 1
        )

        # --------------------------------------------
        # LONG EVENT BIAS
        # Buy only if stock sells off first
        # --------------------------------------------

        if symbol in LONG_EVENT_LIST:

            if opening_return <= -REVERSAL_THRESHOLD:

                trade_return = (
                    day_close / entry_price - 1
                )

                trades.append(
                    {
                        "trading_date": trading_date,
                        "symbol": symbol,
                        "event_bias": "LONG",
                        "trade_direction": "LONG",
                        "day_open": day_open,
                        "entry_price": entry_price,
                        "exit_price": day_close,
                        "opening_return": opening_return,
                        "trade_return": trade_return,
                        "trigger": (
                            f"opening <= "
                            f"-{REVERSAL_THRESHOLD * 100:.2f}%"
                        ),
                    }
                )

        # --------------------------------------------
        # SHORT EVENT BIAS
        # Short only if stock rallies first
        # --------------------------------------------

        elif symbol in SHORT_EVENT_LIST:

            if opening_return >= REVERSAL_THRESHOLD:

                trade_return = (
                    entry_price / day_close - 1
                )

                # Equivalent simple short return:
                # (entry - exit) / entry

                trade_return = (
                    entry_price - day_close
                ) / entry_price

                trades.append(
                    {
                        "trading_date": trading_date,
                        "symbol": symbol,
                        "event_bias": "SHORT",
                        "trade_direction": "SHORT",
                        "day_open": day_open,
                        "entry_price": entry_price,
                        "exit_price": day_close,
                        "opening_return": opening_return,
                        "trade_return": trade_return,
                        "trigger": (
                            f"opening >= "
                            f"+{REVERSAL_THRESHOLD * 100:.2f}%"
                        ),
                    }
                )

    return pd.DataFrame(trades)


# ============================================================
# SUMMARY
# ============================================================

def build_summary(
    trades: pd.DataFrame,
) -> pd.DataFrame:

    if trades.empty:
        return pd.DataFrame()

    rows = []

    # --------------------------------------------
    # Overall
    # --------------------------------------------

    returns = trades["trade_return"]

    rows.append(
        {
            "group": "ALL",
            "trades": len(trades),
            "wins": int(
                (returns > 0).sum()
            ),
            "losses": int(
                (returns <= 0).sum()
            ),
            "win_rate": (
                returns > 0
            ).mean(),
            "avg_return": returns.mean(),
            "median_return": returns.median(),
            "profit_factor": profit_factor(
                returns
            ),
            "compounded_return": (
                (1 + returns).prod() - 1
            ),
            "max_drawdown": max_drawdown(
                returns
            ),
        }
    )

    # --------------------------------------------
    # LONG / SHORT
    # --------------------------------------------

    for direction, g in trades.groupby(
        "trade_direction"
    ):

        returns = g["trade_return"]

        rows.append(
            {
                "group": direction,
                "trades": len(g),
                "wins": int(
                    (returns > 0).sum()
                ),
                "losses": int(
                    (returns <= 0).sum()
                ),
                "win_rate": (
                    returns > 0
                ).mean(),
                "avg_return": returns.mean(),
                "median_return": returns.median(),
                "profit_factor": profit_factor(
                    returns
                ),
                "compounded_return": (
                    (1 + returns).prod() - 1
                ),
                "max_drawdown": max_drawdown(
                    returns
                ),
            }
        )

    return pd.DataFrame(rows)


# ============================================================
# SYMBOL SUMMARY
# ============================================================

def print_symbol_summary(
    trades: pd.DataFrame,
) -> None:

    if trades.empty:
        return

    print()
    print("BY SYMBOL")
    print("-" * 78)

    grouped = (
        trades
        .groupby(
            [
                "symbol",
                "trade_direction",
            ]
        )
        ["trade_return"]
        .agg(
            [
                "count",
                "mean",
                "sum",
            ]
        )
        .reset_index()
        .sort_values(
            "mean",
            ascending=False,
        )
    )

    for _, r in grouped.iterrows():

        print(
            f"{r['symbol']:6} "
            f"{r['trade_direction']:5} | "
            f"trades={int(r['count']):2} | "
            f"avg={pct(r['mean']):>8} | "
            f"sum={pct(r['sum']):>8}"
        )


# ============================================================
# DAILY SUMMARY
# ============================================================

def print_daily_summary(
    trades: pd.DataFrame,
) -> None:

    if trades.empty:
        return

    print()
    print("BY DAY")
    print("-" * 78)

    for trading_date, g in trades.groupby(
        "trading_date"
    ):

        returns = g["trade_return"]

        print(
            f"{trading_date} | "
            f"trades={len(g):2} | "
            f"win={(returns > 0).mean() * 100:5.1f}% | "
            f"avg={pct(returns.mean()):>8} | "
            f"sum={pct(returns.sum()):>8}"
        )


# ============================================================
# PRINT TRADES
# ============================================================

def print_trades(
    trades: pd.DataFrame,
) -> None:

    if trades.empty:
        return

    print()
    print("TRADES")
    print("-" * 78)

    trades_sorted = trades.sort_values(
        [
            "trading_date",
            "symbol",
        ]
    )

    for _, r in trades_sorted.iterrows():

        result = (
            "WIN"
            if r["trade_return"] > 0
            else "LOSS"
        )

        print(
            f"{r['trading_date']} | "
            f"{r['symbol']:6} | "
            f"{r['trade_direction']:5} | "
            f"open move={pct(r['opening_return']):>8} | "
            f"entry={r['entry_price']:.2f} | "
            f"exit={r['exit_price']:.2f} | "
            f"P&L={pct(r['trade_return']):>8} | "
            f"{result}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    print("=" * 78)
    print("IRAN EVENT + OPENING REVERSAL BACKTEST")
    print("=" * 78)

    print()
    print(
        f"Opening window: "
        f"{OPENING_WINDOW_MINUTES} minutes"
    )

    print(
        f"Reversal threshold: "
        f"{REVERSAL_THRESHOLD * 100:.2f}%"
    )

    print(
        f"LONG event symbols: "
        f"{len(LONG_EVENT_LIST)}"
    )

    print(
        f"SHORT event symbols: "
        f"{len(SHORT_EVENT_LIST)}"
    )

    print()

    df = load_data()

    trades = run_strategy(df)

    if trades.empty:

        print()
        print("No trades triggered.")

        return 0

    summary = build_summary(
        trades
    )

    trades.to_csv(
        TRADES_FILE,
        index=False,
    )

    summary.to_csv(
        SUMMARY_FILE,
        index=False,
    )

    # --------------------------------------------
    # Overall summary
    # --------------------------------------------

    print()
    print("=" * 78)
    print("RESULT")
    print("=" * 78)

    for _, r in summary.iterrows():

        pf = r["profit_factor"]

        pf_text = (
            "INF"
            if pf == float("inf")
            else f"{pf:.2f}"
        )

        print()
        print(
            f"{r['group']}"
        )

        print(
            f"  Trades:            "
            f"{int(r['trades'])}"
        )

        print(
            f"  Win rate:          "
            f"{r['win_rate'] * 100:.1f}%"
        )

        print(
            f"  Average return:    "
            f"{pct(r['avg_return'])}"
        )

        print(
            f"  Median return:     "
            f"{pct(r['median_return'])}"
        )

        print(
            f"  Profit factor:     "
            f"{pf_text}"
        )

        print(
            f"  Compounded return: "
            f"{pct(r['compounded_return'])}"
        )

        print(
            f"  Max drawdown:      "
            f"{pct(r['max_drawdown'])}"
        )

    print_daily_summary(
        trades
    )

    print_symbol_summary(
        trades
    )

    print_trades(
        trades
    )

    print()
    print("=" * 78)
    print("FILES CREATED")
    print("=" * 78)

    print(
        f"Trades:  {TRADES_FILE}"
    )

    print(
        f"Summary: {SUMMARY_FILE}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())