from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

INPUT_FILE = SCRIPT_DIR / "market_5min_sp500_2026-02-27_plus_5d.csv"

GRID_FILE = SCRIPT_DIR / "iran_gap_5m_vs_10m_grid.csv"
TRADES_FILE = SCRIPT_DIR / "iran_gap_5m_vs_10m_trades.csv"
BEST_FILE = SCRIPT_DIR / "iran_gap_5m_vs_10m_best.csv"
DAILY_FILE = SCRIPT_DIR / "iran_gap_5m_vs_10m_daily.csv"
REPORT_FILE = SCRIPT_DIR / "iran_gap_5m_vs_10m_report.md"


# ============================================================
# FIXED IRAN WATCHLIST
# ============================================================

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


# ============================================================
# TEST MATRIX
# ============================================================

# Minimum gap from previous close to today's open,
# in the EXPECTED Iran direction.
#
# LONG:
#   gap = today_open / previous_close - 1
#   require gap >= +threshold
#
# SHORT:
#   require gap <= -threshold
GAP_THRESHOLDS = [
    0.0000,  # any correct-direction gap
    0.0050,  # 0.50%
    0.0100,  # 1.00%
    0.0200,  # 2.00%
    0.0300,  # 3.00%
]

# Pullback/bounce magnitude after open.
REVERSAL_THRESHOLDS = [
    0.0015,  # 0.15%
    0.0025,  # 0.25%
    0.0035,  # 0.35%
    0.0050,  # 0.50%
]

ENTRY_WINDOWS_MINUTES = [
    5,
    10,
]


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
        raise RuntimeError(
            f"Missing required columns: {sorted(missing)}"
        )

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
            "symbol",
            "trading_date",
            "timestamp_et",
        ]
    ).reset_index(drop=True)

    print(f"Rows: {len(df):,}")
    print(f"Symbols: {df['symbol'].nunique()}")

    return df


# ============================================================
# BUILD PREVIOUS CLOSE MAP
# ============================================================

def build_previous_close_map(
    df: pd.DataFrame,
) -> dict[tuple[str, object], float]:

    out = {}

    for symbol, gsym in df.groupby(
        "symbol",
        sort=False,
    ):
        dates = sorted(
            gsym["trading_date"].unique()
        )

        prior_close = None

        for d in dates:
            day = gsym[
                gsym["trading_date"] == d
            ].sort_values(
                "timestamp_et"
            )

            if day.empty:
                continue

            day_close = float(
                day.iloc[-1]["close"]
            )

            if prior_close is not None:
                out[(symbol, d)] = prior_close

            prior_close = day_close

    return out


# ============================================================
# STRATEGY
# ============================================================

def run_strategy(
    df: pd.DataFrame,
    prev_close_map: dict,
    gap_threshold: float,
    reversal_threshold: float,
    entry_window_minutes: int,
) -> pd.DataFrame:

    rows = []

    bars_needed = (
        1
        if entry_window_minutes == 5
        else 2
    )

    for (trading_date, symbol), g in df.groupby(
        [
            "trading_date",
            "symbol",
        ],
        sort=True,
    ):
        prev_close = prev_close_map.get(
            (symbol, trading_date)
        )

        if prev_close is None or prev_close <= 0:
            continue

        g = g.sort_values(
            "timestamp_et"
        ).reset_index(drop=True)

        if len(g) < bars_needed:
            continue

        today_open = float(
            g.iloc[0]["open"]
        )

        entry_price = float(
            g.iloc[bars_needed - 1]["close"]
        )

        day_close = float(
            g.iloc[-1]["close"]
        )

        if today_open <= 0 or entry_price <= 0:
            continue

        event_gap = (
            today_open / prev_close - 1.0
        )

        post_open_move = (
            entry_price / today_open - 1.0
        )

        direction = (
            "LONG"
            if symbol in LONG_SYMBOLS
            else "SHORT"
        )

        # Step 1: gap must confirm the historical Iran direction.
        gap_ok = False

        if direction == "LONG":
            gap_ok = (
                event_gap >= gap_threshold
            )
        else:
            gap_ok = (
                event_gap <= -gap_threshold
            )

        if not gap_ok:
            continue

        # Step 2: after a correct-direction gap,
        # wait for a pullback/bounce.
        reversal_ok = False

        if direction == "LONG":
            reversal_ok = (
                post_open_move
                <= -reversal_threshold
            )
        else:
            reversal_ok = (
                post_open_move
                >= reversal_threshold
            )

        if not reversal_ok:
            continue

        if direction == "LONG":
            trade_return = (
                day_close / entry_price - 1.0
            )
        else:
            trade_return = (
                entry_price - day_close
            ) / entry_price

        rows.append(
            {
                "gap_threshold": gap_threshold,
                "reversal_threshold": reversal_threshold,
                "entry_window_minutes": entry_window_minutes,
                "trading_date": trading_date,
                "symbol": symbol,
                "direction": direction,
                "previous_close": prev_close,
                "today_open": today_open,
                "event_gap": event_gap,
                "post_open_move": post_open_move,
                "entry_price": entry_price,
                "exit_price": day_close,
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
# SUMMARIES
# ============================================================

def summarize(
    trades: pd.DataFrame,
    gap_threshold: float,
    reversal_threshold: float,
    entry_window_minutes: int,
) -> dict:

    if trades.empty:
        return {
            "gap_threshold": gap_threshold,
            "reversal_threshold": reversal_threshold,
            "entry_window_minutes": entry_window_minutes,
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
        "gap_threshold": gap_threshold,
        "reversal_threshold": reversal_threshold,
        "entry_window_minutes": entry_window_minutes,
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


def build_daily(
    trades: pd.DataFrame,
) -> pd.DataFrame:

    if trades.empty:
        return pd.DataFrame()

    daily = (
        trades
        .groupby(
            [
                "gap_threshold",
                "reversal_threshold",
                "entry_window_minutes",
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


def rank_grid(
    grid: pd.DataFrame,
) -> pd.DataFrame:

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
        "# Iran event gap + 5m vs 10m reversal backtest"
    )
    lines.append("")
    lines.append(
        "Fixed Iran watchlist directions are used."
    )
    lines.append("")
    lines.append(
        "Entry requires:"
    )
    lines.append("")
    lines.append(
        "1. Previous-close -> today's-open gap in expected Iran direction."
    )
    lines.append(
        "2. Then a pullback/bounce against that direction after 5 or 10 minutes."
    )
    lines.append("")
    lines.append(
        "## Ranked results"
    )
    lines.append("")
    lines.append(
        "| Window | Gap | Reversal | Trades | Win rate | Avg | Median | PF | Portfolio | DD |"
    )
    lines.append(
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )

    for _, r in ranked.iterrows():
        pf = float(r["profit_factor"])
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        lines.append(
            f"| {int(r['entry_window_minutes'])}m | "
            f"{r['gap_threshold'] * 100:.2f}% | "
            f"{r['reversal_threshold'] * 100:.2f}% | "
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
        "| Window | Gap | Reversal | Date | Trades | Win rate | Daily return |"
    )
    lines.append(
        "|---:|---:|---:|---|---:|---:|---:|"
    )

    for _, r in daily.iterrows():
        lines.append(
            f"| {int(r['entry_window_minutes'])}m | "
            f"{r['gap_threshold'] * 100:.2f}% | "
            f"{r['reversal_threshold'] * 100:.2f}% | "
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

    print("=" * 114)
    print("IRAN EVENT GAP + 5M VS 10M REVERSAL BACKTEST")
    print("=" * 114)

    df = load_data()

    prev_close_map = build_previous_close_map(
        df
    )

    all_trade_frames = []
    grid_rows = []

    total = (
        len(GAP_THRESHOLDS)
        * len(REVERSAL_THRESHOLDS)
        * len(ENTRY_WINDOWS_MINUTES)
    )

    counter = 0

    for window in ENTRY_WINDOWS_MINUTES:
        for gap_thr in GAP_THRESHOLDS:
            for rev_thr in REVERSAL_THRESHOLDS:
                counter += 1

                trades = run_strategy(
                    df=df,
                    prev_close_map=prev_close_map,
                    gap_threshold=gap_thr,
                    reversal_threshold=rev_thr,
                    entry_window_minutes=window,
                )

                summary = summarize(
                    trades=trades,
                    gap_threshold=gap_thr,
                    reversal_threshold=rev_thr,
                    entry_window_minutes=window,
                )

                grid_rows.append(
                    summary
                )

                if not trades.empty:
                    all_trade_frames.append(
                        trades
                    )

                print(
                    f"[{counter:02}/{total}] "
                    f"{window:2}m | "
                    f"gap={gap_thr * 100:4.2f}% | "
                    f"rev={rev_thr * 100:4.2f}% | "
                    f"trades={summary['trades']:3} | "
                    f"win={summary['win_rate'] * 100:5.1f}% | "
                    f"avg={pct(summary['avg_return'])}"
                )

    grid = pd.DataFrame(
        grid_rows
    )

    ranked = rank_grid(
        grid
    )

    trades_df = (
        pd.concat(
            all_trade_frames,
            ignore_index=True,
        )
        if all_trade_frames
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

    ranked.iloc[0:1].to_csv(
        BEST_FILE,
        index=False,
    )

    write_report(
        ranked=ranked,
        daily=daily,
    )

    print()
    print("=" * 114)
    print("TOP 20")
    print("=" * 114)

    for _, r in ranked.head(20).iterrows():
        pf = float(r["profit_factor"])
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        print(
            f"{int(r['entry_window_minutes']):2}m | "
            f"gap={r['gap_threshold'] * 100:4.2f}% | "
            f"rev={r['reversal_threshold'] * 100:4.2f}% | "
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

    best = ranked.iloc[0]

    print()
    print("=" * 114)
    print("BEST")
    print("=" * 114)

    pf = float(best["profit_factor"])
    pf_text = (
        "INF"
        if math.isinf(pf)
        else f"{pf:.2f}"
    )

    print(
        f"Entry window:      "
        f"{int(best['entry_window_minutes'])} min"
    )
    print(
        f"Gap threshold:     "
        f"{best['gap_threshold'] * 100:.2f}%"
    )
    print(
        f"Reversal threshold:"
        f" {best['reversal_threshold'] * 100:.2f}%"
    )
    print(
        f"Trades:            "
        f"{int(best['trades'])}"
    )
    print(
        f"Win rate:          "
        f"{best['win_rate'] * 100:.1f}%"
    )
    print(
        f"Average return:    "
        f"{pct(float(best['avg_return']))}"
    )
    print(
        f"Median return:     "
        f"{pct(float(best['median_return']))}"
    )
    print(
        f"Profit factor:     "
        f"{pf_text}"
    )
    print(
        f"Portfolio return:  "
        f"{pct(float(best['portfolio_return']))}"
    )
    print(
        f"Max drawdown:      "
        f"{pct(float(best['max_drawdown']))}"
    )

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
