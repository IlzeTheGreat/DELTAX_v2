from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

INPUT_FILE = SCRIPT_DIR / "market_5min_sp500_2026-02-28_5d.csv"
PLAYBOOK_FILE = ROOT_DIR / "deltax_event_iran_playbook_v1.json"

GRID_FILE = SCRIPT_DIR / "iran_confirmation_vs_reversal_grid.csv"
TRADES_FILE = SCRIPT_DIR / "iran_confirmation_vs_reversal_trades.csv"
BEST_FILE = SCRIPT_DIR / "iran_confirmation_vs_reversal_best.csv"
DAILY_FILE = SCRIPT_DIR / "iran_confirmation_vs_reversal_daily.csv"
REPORT_FILE = SCRIPT_DIR / "iran_confirmation_vs_reversal_report.md"


# ============================================================
# CONFIG
# ============================================================

# The fixed Iran watchlist we agreed on.
LONG_SYMBOLS = {
    "WFC",
    "BX",
    "BAC",
    "APP",
    "XYZ",
    "WDAY",
    "APO",
    "FFIV",
    "LYB",
}

SHORT_SYMBOLS = {
    "LRCX",
    "F",
    "LITE",
    "COHR",
    "TEL",
    "MAS",
}

# First 10-minute move thresholds to test.
THRESHOLDS = [
    0.0015,  # 0.15%
    0.0025,  # 0.25%
    0.0035,  # 0.35%
    0.0050,  # 0.50%
    0.0075,  # 0.75%
    0.0100,  # 1.00%
]

# Two competing entry concepts:
#
# confirmation:
#   LONG expected -> first 10m up >= threshold -> LONG
#   SHORT expected -> first 10m down <= -threshold -> SHORT
#
# reversal:
#   LONG expected -> first 10m down <= -threshold -> LONG
#   SHORT expected -> first 10m up >= threshold -> SHORT
MODES = [
    "confirmation",
    "reversal",
]

# Exit stays same-day close because that is how the stock tests were run.
MIN_OPENING_BARS = 2


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# HELPERS
# ============================================================

def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def profit_factor(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0

    gp = float(returns[returns > 0].sum())
    gl = abs(float(returns[returns < 0].sum()))

    if gl == 0:
        return float("inf") if gp > 0 else 0.0

    return gp / gl


def max_drawdown_from_daily_returns(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0

    equity = 1.0
    values = [equity]

    for r in returns.fillna(0.0):
        equity *= 1.0 + float(r)
        values.append(equity)

    curve = pd.Series(values, dtype=float)
    peak = curve.cummax()
    dd = curve / peak - 1.0

    return float(dd.min())


# ============================================================
# LOAD DATA
# ============================================================

def load_data() -> pd.DataFrame:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input file not found:\n{INPUT_FILE}")

    df = pd.read_csv(INPUT_FILE)

    required = {
        "timestamp_et",
        "trading_date",
        "symbol",
        "open",
        "close",
    }

    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing required columns: {sorted(missing)}")

    df["timestamp_et"] = pd.to_datetime(
        df["timestamp_et"],
        utc=True,
    ).dt.tz_convert("America/New_York")

    df["trading_date"] = pd.to_datetime(
        df["trading_date"]
    ).dt.date

    for col in ["open", "close"]:
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
    ).copy()

    allowed = LONG_SYMBOLS | SHORT_SYMBOLS

    df = df[
        df["symbol"].isin(allowed)
    ].copy()

    df = df.sort_values(
        [
            "trading_date",
            "symbol",
            "timestamp_et",
        ]
    ).reset_index(drop=True)

    print(f"Rows: {len(df):,}")
    print(f"Symbols: {df['symbol'].nunique()}")
    print(
        "Long watchlist: "
        + ", ".join(sorted(LONG_SYMBOLS))
    )
    print(
        "Short watchlist: "
        + ", ".join(sorted(SHORT_SYMBOLS))
    )

    return df


# ============================================================
# STRATEGY
# ============================================================

def run_strategy(
    df: pd.DataFrame,
    mode: str,
    threshold: float,
) -> pd.DataFrame:

    rows = []

    for (trading_date, symbol), g in df.groupby(
        ["trading_date", "symbol"],
        sort=True,
    ):
        g = g.sort_values(
            "timestamp_et"
        ).reset_index(drop=True)

        if len(g) < MIN_OPENING_BARS:
            continue

        opening = g.iloc[:2]

        day_open = float(
            opening.iloc[0]["open"]
        )
        entry_price = float(
            opening.iloc[-1]["close"]
        )
        exit_price = float(
            g.iloc[-1]["close"]
        )

        if day_open <= 0 or entry_price <= 0:
            continue

        opening_return = (
            entry_price / day_open - 1.0
        )

        direction = (
            "LONG"
            if symbol in LONG_SYMBOLS
            else "SHORT"
        )

        take_trade = False

        if mode == "confirmation":
            if (
                direction == "LONG"
                and opening_return >= threshold
            ):
                take_trade = True

            elif (
                direction == "SHORT"
                and opening_return <= -threshold
            ):
                take_trade = True

        elif mode == "reversal":
            if (
                direction == "LONG"
                and opening_return <= -threshold
            ):
                take_trade = True

            elif (
                direction == "SHORT"
                and opening_return >= threshold
            ):
                take_trade = True

        else:
            raise ValueError(mode)

        if not take_trade:
            continue

        if direction == "LONG":
            trade_return = (
                exit_price / entry_price - 1.0
            )
        else:
            trade_return = (
                entry_price - exit_price
            ) / entry_price

        rows.append(
            {
                "mode": mode,
                "threshold": threshold,
                "trading_date": trading_date,
                "symbol": symbol,
                "direction": direction,
                "opening_return_10m": opening_return,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "trade_return": trade_return,
                "result": (
                    "WIN"
                    if trade_return > 0
                    else "LOSS"
                ),
            }
        )

    return pd.DataFrame(rows)


# ============================================================
# SUMMARY
# ============================================================

def build_daily(
    trades: pd.DataFrame,
) -> pd.DataFrame:

    if trades.empty:
        return pd.DataFrame()

    daily = (
        trades
        .groupby(
            [
                "mode",
                "threshold",
                "trading_date",
            ]
        )
        .agg(
            trades=("symbol", "count"),
            wins=(
                "trade_return",
                lambda x: int((x > 0).sum()),
            ),
            daily_return=("trade_return", "mean"),
        )
        .reset_index()
    )

    daily["win_rate"] = (
        daily["wins"] / daily["trades"]
    )

    return daily


def summarize(
    trades: pd.DataFrame,
    mode: str,
    threshold: float,
) -> dict:

    if trades.empty:
        return {
            "mode": mode,
            "threshold": threshold,
            "trades": 0,
            "long_trades": 0,
            "short_trades": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "profit_factor": 0.0,
            "portfolio_return": 0.0,
            "max_drawdown": 0.0,
            "positive_days": 0,
            "trading_days": 0,
        }

    returns = trades["trade_return"]

    daily = (
        trades
        .groupby("trading_date")["trade_return"]
        .mean()
        .sort_index()
    )

    portfolio_return = float(
        (1.0 + daily).prod() - 1.0
    )

    return {
        "mode": mode,
        "threshold": threshold,
        "trades": len(trades),
        "long_trades": int(
            (trades["direction"] == "LONG").sum()
        ),
        "short_trades": int(
            (trades["direction"] == "SHORT").sum()
        ),
        "win_rate": float(
            (returns > 0).mean()
        ),
        "avg_return": float(
            returns.mean()
        ),
        "median_return": float(
            returns.median()
        ),
        "profit_factor": profit_factor(
            returns
        ),
        "portfolio_return": portfolio_return,
        "max_drawdown": max_drawdown_from_daily_returns(
            daily
        ),
        "positive_days": int(
            (daily > 0).sum()
        ),
        "trading_days": len(daily),
    }


def rank_grid(grid: pd.DataFrame) -> pd.DataFrame:
    out = grid.copy()

    pf_score = (
        out["profit_factor"]
        .replace(float("inf"), 10.0)
        .clip(upper=10.0)
    )

    out["rank_score"] = (
        out["avg_return"]
        * out["win_rate"]
        * pf_score
        * out["trades"].clip(upper=100) ** 0.5
        * (1.0 + out["portfolio_return"].clip(lower=-0.99))
        / (1.0 + out["max_drawdown"].abs())
    )

    return out.sort_values(
        [
            "rank_score",
            "portfolio_return",
            "avg_return",
        ],
        ascending=False,
    ).reset_index(drop=True)


# ============================================================
# REPORT
# ============================================================

def write_report(
    ranked: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:

    lines = []

    lines.append(
        "# Iran watchlist: confirmation vs reversal"
    )
    lines.append("")
    lines.append(
        "Fixed watchlist directions were used. "
        "Only the first-10-minute entry logic changes."
    )
    lines.append("")
    lines.append(
        "## Results"
    )
    lines.append("")
    lines.append(
        "| Mode | Threshold | Trades | Win rate | Avg | Median | PF | Portfolio | DD |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    )

    for _, r in ranked.iterrows():
        pf = float(r["profit_factor"])
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        lines.append(
            f"| {r['mode']} | "
            f"{r['threshold'] * 100:.2f}% | "
            f"{int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['avg_return']))} | "
            f"{pct(float(r['median_return']))} | "
            f"{pf_text} | "
            f"{pct(float(r['portfolio_return']))} | "
            f"{pct(float(r['max_drawdown']))} |"
        )

    lines.append("")
    lines.append(
        "## Daily"
    )
    lines.append("")
    lines.append(
        "| Mode | Threshold | Date | Trades | Win rate | Daily return |"
    )
    lines.append(
        "|---|---:|---|---:|---:|---:|"
    )

    for _, r in daily.iterrows():
        lines.append(
            f"| {r['mode']} | "
            f"{r['threshold'] * 100:.2f}% | "
            f"{r['trading_date']} | "
            f"{int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['daily_return']))} |"
        )

    REPORT_FILE.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    print("=" * 110)
    print("IRAN WATCHLIST: CONFIRMATION VS REVERSAL")
    print("=" * 110)

    df = load_data()

    all_trades = []
    grid_rows = []

    for mode in MODES:
        for threshold in THRESHOLDS:
            trades = run_strategy(
                df=df,
                mode=mode,
                threshold=threshold,
            )

            grid_rows.append(
                summarize(
                    trades=trades,
                    mode=mode,
                    threshold=threshold,
                )
            )

            if not trades.empty:
                all_trades.append(trades)

            print(
                f"{mode:12} | "
                f"threshold={threshold * 100:.2f}% | "
                f"trades={len(trades):3} | "
                f"win="
                f"{((trades['trade_return'] > 0).mean() * 100 if not trades.empty else 0):5.1f}% | "
                f"avg="
                f"{pct(float(trades['trade_return'].mean()) if not trades.empty else 0.0)}"
            )

    grid = pd.DataFrame(grid_rows)
    ranked = rank_grid(grid)

    trades_df = (
        pd.concat(
            all_trades,
            ignore_index=True,
        )
        if all_trades
        else pd.DataFrame()
    )

    daily = build_daily(
        trades_df
    )

    ranked.to_csv(
        GRID_FILE,
        index=False,
    )

    trades_df.to_csv(
        TRADES_FILE,
        index=False,
    )

    daily.to_csv(
        DAILY_FILE,
        index=False,
    )

    best = ranked.iloc[0:1].copy()

    best.to_csv(
        BEST_FILE,
        index=False,
    )

    write_report(
        ranked=ranked,
        daily=daily,
    )

    print()
    print("=" * 110)
    print("RANKED RESULTS")
    print("=" * 110)

    for _, r in ranked.iterrows():
        pf = float(r["profit_factor"])
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        print(
            f"{r['mode']:12} | "
            f"thr={r['threshold'] * 100:4.2f}% | "
            f"trades={int(r['trades']):3} | "
            f"L={int(r['long_trades']):2} "
            f"S={int(r['short_trades']):2} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(float(r['avg_return'])):>8} | "
            f"median={pct(float(r['median_return'])):>8} | "
            f"PF={pf_text:>5} | "
            f"portfolio={pct(float(r['portfolio_return'])):>8} | "
            f"DD={pct(float(r['max_drawdown'])):>8}"
        )

    print()
    print("=" * 110)
    print("BEST")
    print("=" * 110)

    b = ranked.iloc[0]

    pf = float(b["profit_factor"])
    pf_text = (
        "INF"
        if math.isinf(pf)
        else f"{pf:.2f}"
    )

    print(f"Mode:              {b['mode']}")
    print(f"Threshold:         {b['threshold'] * 100:.2f}%")
    print(f"Trades:            {int(b['trades'])}")
    print(f"Win rate:          {b['win_rate'] * 100:.1f}%")
    print(f"Average return:    {pct(float(b['avg_return']))}")
    print(f"Median return:     {pct(float(b['median_return']))}")
    print(f"Profit factor:     {pf_text}")
    print(f"Portfolio return:  {pct(float(b['portfolio_return']))}")
    print(f"Max drawdown:      {pct(float(b['max_drawdown']))}")

    print()
    print("Files created:")
    print(f"  {GRID_FILE}")
    print(f"  {TRADES_FILE}")
    print(f"  {BEST_FILE}")
    print(f"  {DAILY_FILE}")
    print(f"  {REPORT_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
