from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

EXIT_TRADES_FILE = SCRIPT_DIR / "options_exit_test_trades.csv"

SUMMARY_FILE = SCRIPT_DIR / "options_partial_exit_summary.csv"
DAILY_FILE = SCRIPT_DIR / "options_partial_exit_daily.csv"
SYMBOL_FILE = SCRIPT_DIR / "options_partial_exit_symbols.csv"
REPORT_FILE = SCRIPT_DIR / "options_partial_exit_report.md"


# ============================================================
# CONFIG
# ============================================================

# Tickers where previous exit-timing test suggested NEXT_OPEN
# improved results vs SAME_DAY_1550.
OVERNIGHT_ELIGIBLE = {
    "LRCX",
    "APP",
    "XYZ",
    "WDAY",
}

# Fraction closed SAME DAY.
# Remainder goes to NEXT_OPEN for overnight-eligible symbols.
PARTIAL_EXIT_WEIGHTS = [
    1.00,  # baseline: 100% same-day
    0.75,  # 75% same-day / 25% next open
    0.50,  # 50% / 50%
    0.25,  # 25% same-day / 75% next open
    0.00,  # 100% next open, eligible names only
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
# LOAD
# ============================================================

def load_exit_trades() -> pd.DataFrame:
    if not EXIT_TRADES_FILE.exists():
        raise FileNotFoundError(
            f"Missing input file:\n{EXIT_TRADES_FILE}\n\n"
            "Run options_exit_timing_backtest.py first."
        )

    df = pd.read_csv(EXIT_TRADES_FILE)

    required = {
        "signal_id",
        "trading_date",
        "symbol",
        "trade_direction",
        "exit_rule",
        "option_return",
        "pnl_per_contract",
    }

    missing = required - set(df.columns)

    if missing:
        raise RuntimeError(
            f"Missing required columns: {sorted(missing)}"
        )

    df["trading_date"] = pd.to_datetime(
        df["trading_date"]
    ).dt.date

    df["option_return"] = pd.to_numeric(
        df["option_return"],
        errors="coerce",
    )

    df["pnl_per_contract"] = pd.to_numeric(
        df["pnl_per_contract"],
        errors="coerce",
    )

    df = df.dropna(
        subset=[
            "signal_id",
            "trading_date",
            "symbol",
            "exit_rule",
            "option_return",
        ]
    ).copy()

    return df


# ============================================================
# BUILD PIVOT
# ============================================================

def build_trade_matrix(df: pd.DataFrame) -> pd.DataFrame:
    same_day = df[
        df["exit_rule"] == "SAME_DAY_1550"
    ][
        [
            "signal_id",
            "trading_date",
            "symbol",
            "trade_direction",
            "option_return",
            "pnl_per_contract",
        ]
    ].copy()

    same_day = same_day.rename(
        columns={
            "option_return": "same_day_return",
            "pnl_per_contract": "same_day_pnl",
        }
    )

    next_open = df[
        df["exit_rule"] == "NEXT_OPEN"
    ][
        [
            "signal_id",
            "option_return",
            "pnl_per_contract",
        ]
    ].copy()

    next_open = next_open.rename(
        columns={
            "option_return": "next_open_return",
            "pnl_per_contract": "next_open_pnl",
        }
    )

    merged = same_day.merge(
        next_open,
        on="signal_id",
        how="left",
    )

    return merged


# ============================================================
# SCENARIOS
# ============================================================

def build_scenario_rows(
    matrix: pd.DataFrame,
) -> pd.DataFrame:

    rows = []

    for same_day_weight in PARTIAL_EXIT_WEIGHTS:
        overnight_weight = 1.0 - same_day_weight

        scenario_name = (
            f"{int(same_day_weight * 100)}_SAME_"
            f"{int(overnight_weight * 100)}_NEXT"
        )

        for _, r in matrix.iterrows():
            symbol = r["symbol"]

            same_day_return = float(r["same_day_return"])
            same_day_pnl = float(r["same_day_pnl"])

            overnight_allowed = (
                symbol in OVERNIGHT_ELIGIBLE
                and pd.notna(r["next_open_return"])
            )

            if overnight_allowed:
                next_open_return = float(r["next_open_return"])
                next_open_pnl = float(r["next_open_pnl"])

                blended_return = (
                    same_day_weight * same_day_return
                    + overnight_weight * next_open_return
                )

                blended_pnl = (
                    same_day_weight * same_day_pnl
                    + overnight_weight * next_open_pnl
                )

                used_next_open = overnight_weight > 0

            else:
                # Non-eligible names remain 100% same-day.
                blended_return = same_day_return
                blended_pnl = same_day_pnl
                used_next_open = False

            rows.append(
                {
                    "scenario": scenario_name,
                    "same_day_weight": same_day_weight,
                    "next_open_weight": overnight_weight,
                    "signal_id": r["signal_id"],
                    "trading_date": r["trading_date"],
                    "symbol": symbol,
                    "trade_direction": r["trade_direction"],
                    "same_day_return": same_day_return,
                    "next_open_return": (
                        float(r["next_open_return"])
                        if pd.notna(r["next_open_return"])
                        else None
                    ),
                    "blended_return": blended_return,
                    "blended_pnl_per_contract": blended_pnl,
                    "overnight_eligible": symbol in OVERNIGHT_ELIGIBLE,
                    "used_next_open": used_next_open,
                }
            )

    return pd.DataFrame(rows)


# ============================================================
# SUMMARIES
# ============================================================

def build_daily(scenarios: pd.DataFrame) -> pd.DataFrame:
    daily = (
        scenarios
        .groupby(
            [
                "scenario",
                "trading_date",
            ]
        )
        .agg(
            trades=("signal_id", "count"),
            wins=(
                "blended_return",
                lambda x: int((x > 0).sum()),
            ),
            daily_return=("blended_return", "mean"),
        )
        .reset_index()
    )

    daily["win_rate"] = (
        daily["wins"] / daily["trades"]
    )

    return daily


def build_summary(
    scenarios: pd.DataFrame,
) -> pd.DataFrame:

    rows = []

    for scenario, g in scenarios.groupby("scenario"):
        returns = g["blended_return"]

        daily = (
            g
            .groupby("trading_date")["blended_return"]
            .mean()
            .sort_index()
        )

        portfolio_return = float(
            (1.0 + daily).prod() - 1.0
        )

        rows.append(
            {
                "scenario": scenario,
                "same_day_weight": float(
                    g["same_day_weight"].iloc[0]
                ),
                "next_open_weight": float(
                    g["next_open_weight"].iloc[0]
                ),
                "trades": len(g),
                "overnight_used_trades": int(
                    g["used_next_open"].sum()
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
                "avg_pnl_per_contract": float(
                    g["blended_pnl_per_contract"].mean()
                ),
                "positive_days": int(
                    (daily > 0).sum()
                ),
                "trading_days": len(daily),
            }
        )

    out = pd.DataFrame(rows)

    pf_score = (
        out["profit_factor"]
        .replace(float("inf"), 10.0)
        .clip(upper=10.0)
    )

    out["rank_score"] = (
        out["avg_return"]
        * out["win_rate"]
        * pf_score
        * (1.0 + out["portfolio_return"].clip(lower=-0.99))
        / (1.0 + out["max_drawdown"].abs())
    )

    out = out.sort_values(
        [
            "rank_score",
            "portfolio_return",
            "avg_return",
        ],
        ascending=False,
    ).reset_index(drop=True)

    return out


def build_symbol_summary(
    scenarios: pd.DataFrame,
) -> pd.DataFrame:

    out = (
        scenarios
        .groupby(
            [
                "scenario",
                "symbol",
                "trade_direction",
            ]
        )
        .agg(
            trades=("signal_id", "count"),
            win_rate=(
                "blended_return",
                lambda x: float((x > 0).mean()),
            ),
            avg_return=("blended_return", "mean"),
            median_return=("blended_return", "median"),
            best_trade=("blended_return", "max"),
            worst_trade=("blended_return", "min"),
            avg_pnl_per_contract=(
                "blended_pnl_per_contract",
                "mean",
            ),
        )
        .reset_index()
    )

    return out


# ============================================================
# REPORT
# ============================================================

def write_report(
    summary: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:

    lines = []

    lines.append("# DeltaX partial exit backtest")
    lines.append("")
    lines.append(
        "Overnight-eligible symbols: "
        + ", ".join(sorted(OVERNIGHT_ELIGIBLE))
    )
    lines.append("")
    lines.append(
        "Non-eligible symbols remain 100% same-day in every scenario."
    )
    lines.append("")
    lines.append("## Scenario comparison")
    lines.append("")
    lines.append(
        "| Scenario | Trades | Overnight used | Win rate | "
        "Avg return | Median | PF | Portfolio | DD | Avg P&L/contract |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )

    for _, r in summary.iterrows():
        pf = float(r["profit_factor"])
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        lines.append(
            f"| {r['scenario']} | "
            f"{int(r['trades'])} | "
            f"{int(r['overnight_used_trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['avg_return']))} | "
            f"{pct(float(r['median_return']))} | "
            f"{pf_text} | "
            f"{pct(float(r['portfolio_return']))} | "
            f"{pct(float(r['max_drawdown']))} | "
            f"${r['avg_pnl_per_contract']:.2f} |"
        )

    lines.append("")
    lines.append("## Daily results")
    lines.append("")
    lines.append(
        "| Scenario | Date | Trades | Win rate | Daily return |"
    )
    lines.append(
        "|---|---|---:|---:|---:|"
    )

    for _, r in daily.iterrows():
        lines.append(
            f"| {r['scenario']} | "
            f"{r['trading_date']} | "
            f"{int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['daily_return']))} |"
        )

    lines.append("")
    lines.append("## Caveat")
    lines.append("")
    lines.append(
        "This is still based on the same five-session event sample. "
        "Partial overnight holding may improve historical returns for "
        "the whitelist, but the whitelist itself was selected using "
        "the same event data, so treat it as in-sample evidence."
    )

    REPORT_FILE.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


# ============================================================
# PRINT
# ============================================================

def print_results(
    summary: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:

    print()
    print("=" * 118)
    print("PARTIAL EXIT COMPARISON")
    print("=" * 118)

    for _, r in summary.iterrows():
        pf = float(r["profit_factor"])
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        print(
            f"{r['scenario']:18} | "
            f"trades={int(r['trades']):3} | "
            f"overnight={int(r['overnight_used_trades']):2} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(float(r['avg_return'])):>9} | "
            f"median={pct(float(r['median_return'])):>9} | "
            f"PF={pf_text:>5} | "
            f"portfolio={pct(float(r['portfolio_return'])):>9} | "
            f"DD={pct(float(r['max_drawdown'])):>9} | "
            f"avg P&L=${r['avg_pnl_per_contract']:.2f}"
        )

    print()
    print("DAILY")
    print("-" * 118)

    for scenario in summary["scenario"]:
        subset = daily[
            daily["scenario"] == scenario
        ]

        print()
        print(scenario)

        for _, r in subset.iterrows():
            print(
                f"  {r['trading_date']} | "
                f"trades={int(r['trades']):3} | "
                f"win={r['win_rate'] * 100:5.1f}% | "
                f"daily={pct(float(r['daily_return'])):>9}"
            )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    print("=" * 118)
    print("DELTAX PARTIAL EXIT BACKTEST")
    print("=" * 118)

    print()
    print(
        "Overnight eligible: "
        + ", ".join(sorted(OVERNIGHT_ELIGIBLE))
    )
    print(
        "Testing same-day weights: "
        + ", ".join(
            f"{int(x * 100)}%"
            for x in PARTIAL_EXIT_WEIGHTS
        )
    )

    df = load_exit_trades()
    matrix = build_trade_matrix(df)

    print(f"Baseline signals: {len(matrix)}")
    print(
        "Signals with NEXT_OPEN data: "
        f"{matrix['next_open_return'].notna().sum()}"
    )

    scenarios = build_scenario_rows(
        matrix
    )

    summary = build_summary(
        scenarios
    )

    daily = build_daily(
        scenarios
    )

    symbols = build_symbol_summary(
        scenarios
    )

    summary.to_csv(
        SUMMARY_FILE,
        index=False,
    )

    daily.to_csv(
        DAILY_FILE,
        index=False,
    )

    symbols.to_csv(
        SYMBOL_FILE,
        index=False,
    )

    write_report(
        summary=summary,
        daily=daily,
    )

    print_results(
        summary=summary,
        daily=daily,
    )

    print()
    print("=" * 118)
    print("FILES CREATED")
    print("=" * 118)
    print(f"Summary: {SUMMARY_FILE}")
    print(f"Daily:   {DAILY_FILE}")
    print(f"Symbols: {SYMBOL_FILE}")
    print(f"Report:  {REPORT_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
