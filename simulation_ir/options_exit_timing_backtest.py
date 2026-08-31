from __future__ import annotations

import math
import os
import random
import sys
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import urllib3
from dotenv import load_dotenv

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest


# ============================================================
# PURPOSE
# ============================================================
#
# Compare exit timing for the SAME option entries already selected
# by the best options configuration:
#
#   - same day 15:50 ET
#   - next trading day open
#   - next trading day close
#   - second trading day close
#
# It does NOT change entry logic, strike, DTE, or trade direction.
#
# Best tested option setup from the previous backtest:
#   target DTE      = 7
#   moneyness       = OTM_1
#   slippage/side   = 2.5%
#
# This script reads options_backtest_best_trades.csv and refetches
# enough historical option bars to cover overnight exits.
# ============================================================


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

ENV_PATH = ROOT_DIR / ".env"

INPUT_TRADES = SCRIPT_DIR / "options_backtest_best_trades.csv"

EXTENDED_BARS_FILE = SCRIPT_DIR / "options_exit_test_extended_bars.csv"
EXIT_TRADES_FILE = SCRIPT_DIR / "options_exit_test_trades.csv"
EXIT_SUMMARY_FILE = SCRIPT_DIR / "options_exit_test_summary.csv"
DAILY_FILE = SCRIPT_DIR / "options_exit_test_daily.csv"
SYMBOL_FILE = SCRIPT_DIR / "options_exit_test_symbols.csv"
REPORT_FILE = SCRIPT_DIR / "options_exit_test_report.md"


# ============================================================
# CONFIG
# ============================================================

MARKET_TZ = ZoneInfo("America/New_York")

EXPECTED_TARGET_DTE = 7
EXPECTED_MONEYNESS = "OTM_1"
EXPECTED_SLIPPAGE = 0.025

ENTRY_TIME_ET = dt_time(9, 40)
SAME_DAY_EXIT_TIME_ET = dt_time(15, 50)

OPTION_BATCH_SIZE = 75

MAX_ATTEMPTS = 6
RETRY_BASE_SLEEP = 0.8
RETRY_MAX_SLEEP = 12.0

# Need enough calendar room to cover two trading sessions after Friday.
CALENDAR_LOOKAHEAD_DAYS = 10


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# ENV / HELPERS
# ============================================================

def load_env() -> None:
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env not found: {ENV_PATH}")

    load_dotenv(ENV_PATH)
    print(f"Loaded .env: {ENV_PATH}")


def get_required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()

    if not value:
        raise RuntimeError(f"Missing required env variable: {name}")

    return value


def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def chunked(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def retry_call(label: str, fn):
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()

        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            urllib3.exceptions.ProtocolError,
            urllib3.exceptions.MaxRetryError,
        ) as exc:
            last_error = exc

            sleep_seconds = min(
                RETRY_MAX_SLEEP,
                RETRY_BASE_SLEEP * (2 ** (attempt - 1)),
            )
            sleep_seconds += random.uniform(0.0, 0.4)

            print(
                f"Retry {label}: {attempt}/{MAX_ATTEMPTS} | "
                f"{repr(exc)} | sleep {sleep_seconds:.2f}s"
            )

            time.sleep(sleep_seconds)

    if last_error is not None:
        raise last_error

    raise RuntimeError(f"Unknown failure in {label}")


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
# LOAD BEST OPTION TRADES
# ============================================================

def load_best_trades() -> pd.DataFrame:
    if not INPUT_TRADES.exists():
        raise FileNotFoundError(
            f"Input file not found:\n{INPUT_TRADES}"
        )

    df = pd.read_csv(INPUT_TRADES)

    required = {
        "signal_id",
        "trading_date",
        "symbol",
        "trade_direction",
        "option_symbol",
        "target_dte",
        "moneyness_mode",
        "slippage_per_side",
        "entry_fill_premium",
        "raw_entry_premium",
    }

    missing = required - set(df.columns)

    if missing:
        raise RuntimeError(
            f"Missing required columns: {sorted(missing)}"
        )

    df["trading_date"] = pd.to_datetime(
        df["trading_date"]
    ).dt.date

    for col in [
        "target_dte",
        "slippage_per_side",
        "entry_fill_premium",
        "raw_entry_premium",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(
        subset=[
            "signal_id",
            "trading_date",
            "symbol",
            "option_symbol",
            "entry_fill_premium",
        ]
    ).copy()

    # Safety: ensure this really is the expected best configuration.
    subset = df[
        (df["target_dte"] == EXPECTED_TARGET_DTE)
        & (df["moneyness_mode"] == EXPECTED_MONEYNESS)
        & (
            (df["slippage_per_side"] - EXPECTED_SLIPPAGE).abs()
            < 1e-9
        )
    ].copy()

    if subset.empty:
        raise RuntimeError(
            "No rows found matching expected best configuration: "
            f"DTE={EXPECTED_TARGET_DTE}, "
            f"moneyness={EXPECTED_MONEYNESS}, "
            f"slippage={EXPECTED_SLIPPAGE}"
        )

    # One row per signal.
    subset = subset.drop_duplicates(
        subset=["signal_id"],
        keep="first",
    ).reset_index(drop=True)

    print(f"Best option trades loaded: {len(subset)}")
    print(
        "Date range: "
        f"{subset['trading_date'].min()} -> "
        f"{subset['trading_date'].max()}"
    )
    print(
        f"Unique option contracts: "
        f"{subset['option_symbol'].nunique()}"
    )

    return subset


# ============================================================
# TRADING CALENDAR
# ============================================================

def build_calendar(
    trading_client: TradingClient,
    first_date: date,
    last_signal_date: date,
) -> list[date]:

    end_date = (
        last_signal_date
        + timedelta(days=CALENDAR_LOOKAHEAD_DAYS)
    )

    calendar = trading_client.get_calendar(
        GetCalendarRequest(
            start=first_date,
            end=end_date,
        )
    )

    dates = [session.date for session in calendar]

    if not dates:
        raise RuntimeError("Trading calendar returned no sessions.")

    return dates


def nth_next_trading_day(
    calendar_dates: list[date],
    current_date: date,
    n: int,
) -> date | None:

    later = [
        d for d in calendar_dates
        if d > current_date
    ]

    if len(later) < n:
        return None

    return later[n - 1]


# ============================================================
# FETCH EXTENDED OPTION BARS
# ============================================================

def fetch_extended_option_bars(
    data_client: OptionHistoricalDataClient,
    trades: pd.DataFrame,
    calendar_dates: list[date],
) -> pd.DataFrame:

    option_symbols = sorted(
        trades["option_symbol"]
        .astype(str)
        .unique()
        .tolist()
    )

    first_date = min(trades["trading_date"])

    last_signal_date = max(trades["trading_date"])
    day2 = nth_next_trading_day(
        calendar_dates,
        last_signal_date,
        2,
    )

    if day2 is None:
        raise RuntimeError(
            "Could not determine second trading day "
            "after last signal date."
        )

    start_et = datetime.combine(
        first_date,
        dt_time(9, 30),
        tzinfo=MARKET_TZ,
    )

    end_et = datetime.combine(
        day2 + timedelta(days=1),
        dt_time(0, 0),
        tzinfo=MARKET_TZ,
    )

    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)

    timeframe = TimeFrame(
        5,
        TimeFrameUnit.Minute,
    )

    all_rows = []

    batches = list(
        chunked(
            option_symbols,
            OPTION_BATCH_SIZE,
        )
    )

    print()
    print(
        f"Fetching extended bars for "
        f"{len(option_symbols)} option contracts..."
    )
    print(
        f"Window: {start_et.isoformat()} -> "
        f"{end_et.isoformat()}"
    )

    for batch_no, batch in enumerate(
        batches,
        start=1,
    ):
        print(
            f"[Batch {batch_no}/{len(batches)}] "
            f"{len(batch)} contracts"
        )

        request = OptionBarsRequest(
            symbol_or_symbols=batch,
            timeframe=timeframe,
            start=start_utc,
            end=end_utc,
        )

        try:
            response = retry_call(
                f"option bars batch {batch_no}",
                lambda: data_client.get_option_bars(
                    request
                ),
            )
        except Exception as exc:
            print(f"  ERROR: {repr(exc)}")
            continue

        data = (
            response.data
            if hasattr(response, "data")
            else {}
        )

        batch_rows = 0

        for option_symbol in batch:
            bars = data.get(
                option_symbol,
                [],
            )

            for bar in bars:
                ts = bar.timestamp

                if ts.tzinfo is None:
                    ts = ts.replace(
                        tzinfo=timezone.utc
                    )

                ts_et = ts.astimezone(
                    MARKET_TZ
                )

                if not (
                    dt_time(9, 30)
                    <= ts_et.time()
                    < dt_time(16, 0)
                ):
                    continue

                all_rows.append(
                    {
                        "option_symbol":
                            option_symbol,
                        "timestamp_et":
                            ts_et.isoformat(),
                        "trading_date":
                            ts_et.date(),
                        "open":
                            float(bar.open),
                        "high":
                            float(bar.high),
                        "low":
                            float(bar.low),
                        "close":
                            float(bar.close),
                        "volume":
                            int(bar.volume or 0),
                    }
                )

                batch_rows += 1

        print(
            f"  Rows: {batch_rows:,}"
        )

        time.sleep(0.15)

    bars_df = pd.DataFrame(
        all_rows
    )

    if bars_df.empty:
        raise RuntimeError(
            "No extended historical option bars returned."
        )

    bars_df["timestamp_et"] = pd.to_datetime(
        bars_df["timestamp_et"],
        utc=True,
    ).dt.tz_convert(MARKET_TZ)

    bars_df["trading_date"] = pd.to_datetime(
        bars_df["trading_date"]
    ).dt.date

    bars_df = bars_df.sort_values(
        [
            "option_symbol",
            "timestamp_et",
        ]
    ).reset_index(drop=True)

    bars_df.to_csv(
        EXTENDED_BARS_FILE,
        index=False,
    )

    print()
    print(
        f"Extended option bar rows: "
        f"{len(bars_df):,}"
    )
    print(
        f"Contracts with data: "
        f"{bars_df['option_symbol'].nunique()}"
    )
    print(
        f"Saved: {EXTENDED_BARS_FILE}"
    )

    return bars_df


# ============================================================
# EXIT PRICE HELPERS
# ============================================================

def find_bar_at_or_after(
    day_bars: pd.DataFrame,
    target_time: dt_time,
):
    candidates = day_bars[
        day_bars["timestamp_et"].dt.time
        >= target_time
    ]

    if candidates.empty:
        return None

    return candidates.iloc[0]


def find_last_bar(
    day_bars: pd.DataFrame,
):
    if day_bars.empty:
        return None

    return day_bars.sort_values(
        "timestamp_et"
    ).iloc[-1]


def exit_price_for_rule(
    contract_bars: pd.DataFrame,
    signal_date: date,
    calendar_dates: list[date],
    exit_rule: str,
):

    if exit_rule == "SAME_DAY_1550":
        target_date = signal_date

        day = contract_bars[
            contract_bars["trading_date"]
            == target_date
        ].sort_values("timestamp_et")

        row = find_bar_at_or_after(
            day,
            SAME_DAY_EXIT_TIME_ET,
        )

        if row is None:
            row = find_last_bar(day)

    elif exit_rule == "NEXT_OPEN":
        target_date = nth_next_trading_day(
            calendar_dates,
            signal_date,
            1,
        )

        if target_date is None:
            return None

        day = contract_bars[
            contract_bars["trading_date"]
            == target_date
        ].sort_values("timestamp_et")

        row = find_bar_at_or_after(
            day,
            dt_time(9, 30),
        )

    elif exit_rule == "NEXT_CLOSE":
        target_date = nth_next_trading_day(
            calendar_dates,
            signal_date,
            1,
        )

        if target_date is None:
            return None

        day = contract_bars[
            contract_bars["trading_date"]
            == target_date
        ].sort_values("timestamp_et")

        row = find_last_bar(day)

    elif exit_rule == "DAY2_CLOSE":
        target_date = nth_next_trading_day(
            calendar_dates,
            signal_date,
            2,
        )

        if target_date is None:
            return None

        day = contract_bars[
            contract_bars["trading_date"]
            == target_date
        ].sort_values("timestamp_et")

        row = find_last_bar(day)

    else:
        raise ValueError(
            f"Unknown exit rule: {exit_rule}"
        )

    if row is None:
        return None

    return {
        "exit_date":
            target_date,
        "exit_timestamp_et":
            row["timestamp_et"],
        "raw_exit_premium":
            float(row["close"]),
    }


# ============================================================
# BUILD EXIT TEST
# ============================================================

EXIT_RULES = [
    "SAME_DAY_1550",
    "NEXT_OPEN",
    "NEXT_CLOSE",
    "DAY2_CLOSE",
]


def build_exit_trades(
    trades: pd.DataFrame,
    bars_df: pd.DataFrame,
    calendar_dates: list[date],
) -> pd.DataFrame:

    bars_lookup = {
        symbol: g.copy()
        for symbol, g
        in bars_df.groupby("option_symbol")
    }

    rows = []

    for _, trade in trades.iterrows():
        option_symbol = trade["option_symbol"]

        contract_bars = bars_lookup.get(
            option_symbol
        )

        if contract_bars is None:
            continue

        entry_fill = float(
            trade["entry_fill_premium"]
        )

        for exit_rule in EXIT_RULES:
            exit_data = exit_price_for_rule(
                contract_bars=contract_bars,
                signal_date=trade["trading_date"],
                calendar_dates=calendar_dates,
                exit_rule=exit_rule,
            )

            if exit_data is None:
                continue

            raw_exit = float(
                exit_data["raw_exit_premium"]
            )

            # Same slippage assumption as best options backtest:
            # selling gets a worse fill.
            exit_fill = raw_exit * (
                1.0 - EXPECTED_SLIPPAGE
            )

            option_return = (
                exit_fill / entry_fill - 1.0
            )

            rows.append(
                {
                    "signal_id":
                        trade["signal_id"],
                    "trading_date":
                        trade["trading_date"],
                    "symbol":
                        trade["symbol"],
                    "trade_direction":
                        trade["trade_direction"],
                    "option_symbol":
                        option_symbol,
                    "exit_rule":
                        exit_rule,
                    "entry_fill_premium":
                        entry_fill,
                    "exit_date":
                        exit_data["exit_date"],
                    "exit_timestamp_et":
                        exit_data[
                            "exit_timestamp_et"
                        ],
                    "raw_exit_premium":
                        raw_exit,
                    "exit_fill_premium":
                        exit_fill,
                    "option_return":
                        option_return,
                    "pnl_per_contract":
                        (
                            exit_fill
                            - entry_fill
                        )
                        * 100.0,
                    "result":
                        (
                            "WIN"
                            if option_return > 0
                            else "LOSS"
                        ),
                }
            )

    out = pd.DataFrame(rows)

    if out.empty:
        raise RuntimeError(
            "No exit-test trades created."
        )

    out.to_csv(
        EXIT_TRADES_FILE,
        index=False,
    )

    return out


# ============================================================
# SUMMARIES
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
                "exit_rule",
                "trading_date",
            ]
        )
        .agg(
            trades=(
                "signal_id",
                "count",
            ),
            wins=(
                "option_return",
                lambda x:
                    int(
                        (x > 0).sum()
                    ),
            ),
            daily_return=(
                "option_return",
                "mean",
            ),
        )
        .reset_index()
    )

    daily["win_rate"] = (
        daily["wins"]
        / daily["trades"]
    )

    return daily


def summarize_exit_rule(
    subset: pd.DataFrame,
    total_signals: int,
) -> dict:

    returns = subset[
        "option_return"
    ]

    daily = (
        subset
        .groupby("trading_date")
        ["option_return"]
        .mean()
        .sort_index()
    )

    portfolio_return = float(
        (1.0 + daily).prod() - 1.0
    )

    return {
        "exit_rule":
            subset[
                "exit_rule"
            ].iloc[0],
        "executed_trades":
            len(subset),
        "coverage":
            (
                len(subset)
                / total_signals
                if total_signals
                else 0.0
            ),
        "win_rate":
            float(
                (returns > 0).mean()
            ),
        "avg_return":
            float(
                returns.mean()
            ),
        "median_return":
            float(
                returns.median()
            ),
        "profit_factor":
            profit_factor(
                returns
            ),
        "portfolio_return":
            portfolio_return,
        "max_drawdown":
            max_drawdown_from_daily_returns(
                daily
            ),
        "avg_pnl_per_contract":
            float(
                subset[
                    "pnl_per_contract"
                ].mean()
            ),
        "positive_days":
            int(
                (daily > 0).sum()
            ),
        "trading_days":
            len(daily),
    }


def build_summary(
    exit_trades: pd.DataFrame,
    total_signals: int,
) -> pd.DataFrame:

    rows = []

    for exit_rule in EXIT_RULES:
        subset = exit_trades[
            exit_trades["exit_rule"]
            == exit_rule
        ].copy()

        if subset.empty:
            continue

        rows.append(
            summarize_exit_rule(
                subset,
                total_signals,
            )
        )

    summary = pd.DataFrame(
        rows
    )

    if summary.empty:
        raise RuntimeError(
            "Exit summary is empty."
        )

    summary["rank_score"] = (
        summary["avg_return"]
        * summary["win_rate"]
        * (
            summary[
                "profit_factor"
            ]
            .replace(
                float("inf"),
                10.0,
            )
            .clip(
                upper=10.0
            )
        )
        * summary["coverage"]
        / (
            1.0
            + summary[
                "max_drawdown"
            ].abs()
        )
    )

    summary = summary.sort_values(
        [
            "rank_score",
            "portfolio_return",
        ],
        ascending=False,
    ).reset_index(drop=True)

    summary.to_csv(
        EXIT_SUMMARY_FILE,
        index=False,
    )

    return summary


def build_symbol_summary(
    exit_trades: pd.DataFrame,
) -> pd.DataFrame:

    rows = []

    for exit_rule in EXIT_RULES:
        subset = exit_trades[
            exit_trades["exit_rule"]
            == exit_rule
        ]

        if subset.empty:
            continue

        grouped = (
            subset
            .groupby(
                [
                    "symbol",
                    "trade_direction",
                ]
            )
            .agg(
                trades=(
                    "signal_id",
                    "count",
                ),
                win_rate=(
                    "option_return",
                    lambda x:
                        float(
                            (x > 0).mean()
                        ),
                ),
                avg_return=(
                    "option_return",
                    "mean",
                ),
                median_return=(
                    "option_return",
                    "median",
                ),
                best_trade=(
                    "option_return",
                    "max",
                ),
                worst_trade=(
                    "option_return",
                    "min",
                ),
            )
            .reset_index()
        )

        grouped["exit_rule"] = (
            exit_rule
        )

        rows.append(
            grouped
        )

    out = pd.concat(
        rows,
        ignore_index=True,
    )

    out.to_csv(
        SYMBOL_FILE,
        index=False,
    )

    return out


# ============================================================
# REPORT / PRINT
# ============================================================

def write_report(
    summary: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:

    lines = []

    lines.append(
        "# DeltaX option exit timing test"
    )
    lines.append("")
    lines.append(
        "Entry configuration held constant:"
    )
    lines.append("")
    lines.append(
        f"- DTE: {EXPECTED_TARGET_DTE}"
    )
    lines.append(
        f"- Moneyness: {EXPECTED_MONEYNESS}"
    )
    lines.append(
        f"- Slippage: "
        f"{EXPECTED_SLIPPAGE * 100:.1f}% per side"
    )
    lines.append("")
    lines.append(
        "## Exit comparison"
    )
    lines.append("")
    lines.append(
        "| Exit | Trades | Win rate | Avg return | "
        "Median | PF | Portfolio | DD | Avg P&L/contract |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    )

    for _, r in summary.iterrows():
        pf = float(
            r["profit_factor"]
        )
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        lines.append(
            f"| {r['exit_rule']} | "
            f"{int(r['executed_trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['avg_return']))} | "
            f"{pct(float(r['median_return']))} | "
            f"{pf_text} | "
            f"{pct(float(r['portfolio_return']))} | "
            f"{pct(float(r['max_drawdown']))} | "
            f"${r['avg_pnl_per_contract']:.2f} |"
        )

    lines.append("")
    lines.append(
        "## Daily results"
    )
    lines.append("")
    lines.append(
        "| Exit | Signal date | Trades | Win rate | Daily return |"
    )
    lines.append(
        "|---|---|---:|---:|---:|"
    )

    for _, r in daily.iterrows():
        lines.append(
            f"| {r['exit_rule']} | "
            f"{r['trading_date']} | "
            f"{int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['daily_return']))} |"
        )

    lines.append("")
    lines.append(
        "## Caveat"
    )
    lines.append("")
    lines.append(
        "This compares exit timing on the same historical "
        "entry signals and option contracts. Overnight holding "
        "introduces gap, theta, IV and liquidity risk that did "
        "not exist in the same-day baseline."
    )

    REPORT_FILE.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def print_results(
    summary: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:

    print()
    print(
        "=" * 118
    )
    print(
        "EXIT TIMING COMPARISON"
    )
    print(
        "=" * 118
    )

    for _, r in summary.iterrows():
        pf = float(
            r["profit_factor"]
        )
        pf_text = (
            "INF"
            if math.isinf(pf)
            else f"{pf:.2f}"
        )

        print(
            f"{r['exit_rule']:16} | "
            f"trades={int(r['executed_trades']):3} | "
            f"coverage={r['coverage'] * 100:5.1f}% | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(float(r['avg_return'])):>9} | "
            f"median={pct(float(r['median_return'])):>9} | "
            f"PF={pf_text:>5} | "
            f"portfolio={pct(float(r['portfolio_return'])):>9} | "
            f"DD={pct(float(r['max_drawdown'])):>9} | "
            f"avg P&L=${r['avg_pnl_per_contract']:.2f}"
        )

    print()
    print(
        "DAILY BY EXIT RULE"
    )
    print(
        "-" * 118
    )

    for exit_rule in EXIT_RULES:
        subset = daily[
            daily["exit_rule"]
            == exit_rule
        ]

        if subset.empty:
            continue

        print()
        print(
            exit_rule
        )

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

    print(
        "=" * 118
    )
    print(
        "DELTAX OPTION EXIT TIMING BACKTEST"
    )
    print(
        "=" * 118
    )

    print()
    print(
        "Testing:"
    )
    print(
        "  SAME_DAY_1550"
    )
    print(
        "  NEXT_OPEN"
    )
    print(
        "  NEXT_CLOSE"
    )
    print(
        "  DAY2_CLOSE"
    )
    print()

    load_env()

    api_key = get_required_env(
        "ALPACA_API_KEY_PAPER"
    )
    api_secret = get_required_env(
        "ALPACA_API_SECRET_PAPER"
    )
    trading_url = get_required_env(
        "ALPACA_TRADING_URL_PAPER"
    )

    trading_url = trading_url.rstrip(
        "/"
    )

    if trading_url.endswith(
        "/v2"
    ):
        trading_url = trading_url[:-3]

    trades = load_best_trades()

    trading_client = TradingClient(
        api_key=api_key,
        secret_key=api_secret,
        paper=False,
        url_override=trading_url,
    )

    data_client = (
        OptionHistoricalDataClient(
            api_key=api_key,
            secret_key=api_secret,
        )
    )

    calendar_dates = build_calendar(
        trading_client=trading_client,
        first_date=min(
            trades["trading_date"]
        ),
        last_signal_date=max(
            trades["trading_date"]
        ),
    )

    bars_df = fetch_extended_option_bars(
        data_client=data_client,
        trades=trades,
        calendar_dates=calendar_dates,
    )

    exit_trades = build_exit_trades(
        trades=trades,
        bars_df=bars_df,
        calendar_dates=calendar_dates,
    )

    exit_trades.to_csv(
        EXIT_TRADES_FILE,
        index=False,
    )

    summary = build_summary(
        exit_trades=exit_trades,
        total_signals=len(trades),
    )

    daily = build_daily(
        exit_trades
    )

    daily.to_csv(
        DAILY_FILE,
        index=False,
    )

    build_symbol_summary(
        exit_trades
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
    print(
        "=" * 118
    )
    print(
        "FILES CREATED"
    )
    print(
        "=" * 118
    )
    print(
        f"Extended bars: {EXTENDED_BARS_FILE}"
    )
    print(
        f"Trades:        {EXIT_TRADES_FILE}"
    )
    print(
        f"Summary:       {EXIT_SUMMARY_FILE}"
    )
    print(
        f"Daily:         {DAILY_FILE}"
    )
    print(
        f"Symbols:       {SYMBOL_FILE}"
    )
    print(
        f"Report:        {REPORT_FILE}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
