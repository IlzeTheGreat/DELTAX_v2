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

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


# ============================================================
# PURPOSE
# ============================================================
#
# Uses the BEST STOCK SIGNALS already produced by:
#   iran_event_strategy_10min_full.py
#
# Baseline stock rule is therefore NOT re-optimized here:
#   bias threshold = 2.50%
#   first 10m reversal threshold = 0.25%
#   entry_mode = threshold_only
#
# For every stock signal, this script:
#   1) finds historical option contracts that existed for that underlying
#   2) tests CALL for LONG and PUT for SHORT
#   3) tests DTE targets: 7, 14, 21, 30 days
#   4) tests moneyness: ATM, 1/2% ITM, 1/2% OTM
#   5) uses historical 5-minute option bars
#   6) enters around 09:40 ET and exits around 15:50 ET
#   7) tests several execution-cost/slippage assumptions
#   8) ranks configurations by coverage, win rate, return, PF and drawdown
#
# NOTE:
# Historical Greeks at the historical entry timestamp are not available
# through the same historical-bars workflow, so this backtest uses strike
# moneyness instead of pretending to know historical delta.
# ============================================================


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

ENV_PATH = ROOT_DIR / ".env"

STOCK_TRADES_FILE = SCRIPT_DIR / "iran_event_10min_best_trades.csv"

CONTRACT_SELECTION_FILE = SCRIPT_DIR / "options_backtest_contract_selection.csv"
OPTION_BARS_FILE = SCRIPT_DIR / "options_backtest_5min_bars.csv"
OPTION_TRADES_FILE = SCRIPT_DIR / "options_backtest_all_trades.csv"
GRID_FILE = SCRIPT_DIR / "options_backtest_grid.csv"
BEST_TRADES_FILE = SCRIPT_DIR / "options_backtest_best_trades.csv"
BEST_SYMBOLS_FILE = SCRIPT_DIR / "options_backtest_best_symbols.csv"
BEST_DAILY_FILE = SCRIPT_DIR / "options_backtest_best_daily.csv"
REPORT_FILE = SCRIPT_DIR / "options_backtest_report.md"


# ============================================================
# CONFIG
# ============================================================

MARKET_TZ = ZoneInfo("America/New_York")

# Historical option entry and exit times.
ENTRY_TIME_ET = dt_time(9, 40)
EXIT_TIME_ET = dt_time(15, 50)

# Target DTEs to compare.
TARGET_DTES = [7, 14, 21, 30]

# How far from the target DTE we allow the selected expiration.
MAX_DTE_DISTANCE = 4

# Strike definitions relative to underlying price at stock entry.
# Positive ITM means deeper intrinsic value.
MONEYNESS_MODES = [
    "ATM",
    "ITM_1",
    "ITM_2",
    "OTM_1",
    "OTM_2",
]

# Execution cost sensitivity.
# Applied to BOTH entry and exit:
# entry_fill = bar_close * (1 + slippage)
# exit_fill  = bar_close * (1 - slippage)
SLIPPAGE_PER_SIDE = [
    0.000,   # idealized
    0.010,   # 1.0% each side
    0.025,   # 2.5% each side
    0.050,   # 5.0% each side
]

# Ignore near-zero option premiums.
MIN_ENTRY_PREMIUM = 0.10

# Option bar fetch batch size; API supports up to 100 symbols/request.
OPTION_BATCH_SIZE = 75

MAX_ATTEMPTS = 6
RETRY_BASE_SLEEP = 0.8
RETRY_MAX_SLEEP = 12.0

# Minimum number of executed option trades required for "best config".
MIN_EXECUTED_TRADES_FOR_BEST = 20

# At least this fraction of stock signals should have usable option data.
MIN_COVERAGE_FOR_BEST = 0.40


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# ENV
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


# ============================================================
# HELPERS
# ============================================================

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
# LOAD STOCK SIGNALS
# ============================================================

def load_stock_signals() -> pd.DataFrame:
    if not STOCK_TRADES_FILE.exists():
        raise FileNotFoundError(
            "Stock signal file not found:\n"
            f"{STOCK_TRADES_FILE}\n\n"
            "Run iran_event_strategy_10min_full.py first."
        )

    df = pd.read_csv(STOCK_TRADES_FILE)

    required = {
        "trading_date",
        "symbol",
        "trade_direction",
        "entry_price",
    }

    missing = required - set(df.columns)

    if missing:
        raise RuntimeError(
            f"Stock trades file is missing columns: {sorted(missing)}"
        )

    df["trading_date"] = pd.to_datetime(df["trading_date"]).dt.date
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df["entry_price"] = pd.to_numeric(df["entry_price"], errors="coerce")

    df = df.dropna(
        subset=[
            "trading_date",
            "symbol",
            "trade_direction",
            "entry_price",
        ]
    ).copy()

    df["option_type"] = df["trade_direction"].map(
        {
            "LONG": "call",
            "SHORT": "put",
        }
    )

    df = df[df["option_type"].notna()].copy()

    df["signal_id"] = (
        df["trading_date"].astype(str)
        + ":"
        + df["symbol"]
        + ":"
        + df["trade_direction"]
    )

    print(f"Stock signals loaded: {len(df)}")
    print(f"Unique symbols: {df['symbol'].nunique()}")
    print(
        "Date range: "
        f"{df['trading_date'].min()} -> {df['trading_date'].max()}"
    )

    return df


# ============================================================
# CONTRACT SELECTION
# ============================================================

def target_strike(
    underlying_price: float,
    option_type: str,
    moneyness_mode: str,
) -> float:

    if moneyness_mode == "ATM":
        return underlying_price

    percent = 0.01 if moneyness_mode.endswith("_1") else 0.02

    if option_type == "call":
        if moneyness_mode.startswith("ITM"):
            return underlying_price * (1.0 - percent)
        return underlying_price * (1.0 + percent)

    if option_type == "put":
        if moneyness_mode.startswith("ITM"):
            return underlying_price * (1.0 + percent)
        return underlying_price * (1.0 - percent)

    raise ValueError(option_type)


def fetch_contracts_for_signal(
    trading_client: TradingClient,
    symbol: str,
    trading_date: date,
    option_type: str,
) -> list:

    expiration_gte = trading_date + timedelta(
        days=min(TARGET_DTES) - MAX_DTE_DISTANCE
    )
    expiration_lte = trading_date + timedelta(
        days=max(TARGET_DTES) + MAX_DTE_DISTANCE
    )

    ctype = (
        ContractType.CALL
        if option_type == "call"
        else ContractType.PUT
    )

    all_contracts = []
    page_token = None

    while True:
        request = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.INACTIVE,
            expiration_date_gte=expiration_gte,
            expiration_date_lte=expiration_lte,
            type=ctype,
            limit=10000,
            page_token=page_token,
        )

        response = retry_call(
            f"contracts {symbol} {trading_date}",
            lambda: trading_client.get_option_contracts(request),
        )

        contracts = getattr(response, "option_contracts", None) or []
        all_contracts.extend(contracts)

        page_token = getattr(response, "next_page_token", None)

        if not page_token:
            break

    return all_contracts


def select_contract(
    contracts: list,
    trading_date: date,
    underlying_price: float,
    option_type: str,
    target_dte: int,
    moneyness_mode: str,
):
    if not contracts:
        return None

    target = target_strike(
        underlying_price=underlying_price,
        option_type=option_type,
        moneyness_mode=moneyness_mode,
    )

    eligible = []

    for contract in contracts:
        expiration = contract.expiration_date
        dte = (expiration - trading_date).days

        if abs(dte - target_dte) > MAX_DTE_DISTANCE:
            continue

        strike = float(contract.strike_price)

        eligible.append(
            (
                abs(dte - target_dte),
                abs(strike - target),
                dte,
                strike,
                contract,
            )
        )

    if not eligible:
        return None

    eligible.sort(
        key=lambda x: (
            x[0],  # closest DTE
            x[1],  # closest strike
        )
    )

    return eligible[0][4]


def build_contract_selection(
    trading_client: TradingClient,
    signals: pd.DataFrame,
) -> pd.DataFrame:

    rows = []

    total = len(signals)

    for i, (_, signal) in enumerate(signals.iterrows(), start=1):
        symbol = signal["symbol"]
        trading_date = signal["trading_date"]
        option_type = signal["option_type"]
        underlying_price = float(signal["entry_price"])

        print(
            f"[Contract {i}/{total}] "
            f"{trading_date} {symbol} "
            f"{signal['trade_direction']} "
            f"underlying={underlying_price:.2f}"
        )

        try:
            contracts = fetch_contracts_for_signal(
                trading_client=trading_client,
                symbol=symbol,
                trading_date=trading_date,
                option_type=option_type,
            )
        except Exception as exc:
            print(f"  ERROR contracts: {repr(exc)}")
            contracts = []

        if not contracts:
            print("  No inactive historical contracts found.")

        for target_dte in TARGET_DTES:
            for moneyness_mode in MONEYNESS_MODES:
                selected = select_contract(
                    contracts=contracts,
                    trading_date=trading_date,
                    underlying_price=underlying_price,
                    option_type=option_type,
                    target_dte=target_dte,
                    moneyness_mode=moneyness_mode,
                )

                base = {
                    "signal_id": signal["signal_id"],
                    "trading_date": trading_date,
                    "symbol": symbol,
                    "trade_direction": signal["trade_direction"],
                    "option_type": option_type,
                    "underlying_entry_price": underlying_price,
                    "target_dte": target_dte,
                    "moneyness_mode": moneyness_mode,
                    "target_strike": target_strike(
                        underlying_price,
                        option_type,
                        moneyness_mode,
                    ),
                }

                if selected is None:
                    rows.append(
                        {
                            **base,
                            "option_symbol": "",
                            "expiration_date": "",
                            "actual_dte": "",
                            "strike_price": "",
                            "contract_size": "",
                            "contract_found": False,
                        }
                    )
                    continue

                rows.append(
                    {
                        **base,
                        "option_symbol": selected.symbol,
                        "expiration_date": selected.expiration_date,
                        "actual_dte": (
                            selected.expiration_date - trading_date
                        ).days,
                        "strike_price": float(selected.strike_price),
                        "contract_size": getattr(selected, "size", ""),
                        "contract_found": True,
                    }
                )

        time.sleep(0.05)

    selection = pd.DataFrame(rows)
    selection.to_csv(CONTRACT_SELECTION_FILE, index=False)

    found = int(selection["contract_found"].sum())
    print()
    print(
        f"Contract selections found: {found}/{len(selection)} "
        f"({found / len(selection) * 100:.1f}%)"
    )
    print(f"Saved: {CONTRACT_SELECTION_FILE}")

    return selection


# ============================================================
# OPTION BARS
# ============================================================

def fetch_option_bars(
    data_client: OptionHistoricalDataClient,
    selection: pd.DataFrame,
) -> pd.DataFrame:

    usable = selection[
        selection["contract_found"] == True
    ].copy()

    symbols = sorted(
        usable["option_symbol"]
        .dropna()
        .astype(str)
        .loc[lambda s: s.str.len() > 0]
        .unique()
        .tolist()
    )

    if not symbols:
        raise RuntimeError("No option symbols available to fetch.")

    first_date = min(usable["trading_date"])
    last_date = max(usable["trading_date"])

    start_et = datetime.combine(
        first_date,
        dt_time(9, 30),
        tzinfo=MARKET_TZ,
    )

    end_et = datetime.combine(
        last_date,
        dt_time(16, 0),
        tzinfo=MARKET_TZ,
    ) + timedelta(days=1)

    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)

    timeframe = TimeFrame(
        5,
        TimeFrameUnit.Minute,
    )

    all_rows = []

    batches = list(chunked(symbols, OPTION_BATCH_SIZE))

    print()
    print(
        f"Fetching historical option bars for "
        f"{len(symbols)} unique contracts in {len(batches)} batches..."
    )

    for batch_no, batch in enumerate(batches, start=1):
        print(
            f"[Option bars {batch_no}/{len(batches)}] "
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
                lambda: data_client.get_option_bars(request),
            )
        except Exception as exc:
            print(f"  ERROR: {repr(exc)}")
            continue

        data = response.data if hasattr(response, "data") else {}

        batch_rows = 0

        for option_symbol in batch:
            bars = data.get(option_symbol, [])

            for bar in bars:
                ts = bar.timestamp

                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                ts_et = ts.astimezone(MARKET_TZ)

                if not (
                    dt_time(9, 30)
                    <= ts_et.time()
                    < dt_time(16, 0)
                ):
                    continue

                all_rows.append(
                    {
                        "option_symbol": option_symbol,
                        "timestamp_utc": ts.astimezone(
                            timezone.utc
                        ).isoformat(),
                        "timestamp_et": ts_et.isoformat(),
                        "trading_date": ts_et.date(),
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": int(bar.volume or 0),
                        "trade_count": (
                            int(bar.trade_count)
                            if getattr(
                                bar,
                                "trade_count",
                                None,
                            ) is not None
                            else ""
                        ),
                        "vwap": (
                            float(bar.vwap)
                            if getattr(
                                bar,
                                "vwap",
                                None,
                            ) is not None
                            else ""
                        ),
                    }
                )

                batch_rows += 1

        print(f"  Rows: {batch_rows:,}")
        time.sleep(0.15)

    bars_df = pd.DataFrame(all_rows)

    if bars_df.empty:
        raise RuntimeError(
            "No historical option bars returned. "
            "Check your Alpaca market-data entitlement and contract selection."
        )

    bars_df["timestamp_et"] = pd.to_datetime(
        bars_df["timestamp_et"],
        utc=True,
    ).dt.tz_convert(MARKET_TZ)

    bars_df["trading_date"] = pd.to_datetime(
        bars_df["trading_date"]
    ).dt.date

    bars_df = bars_df.sort_values(
        ["option_symbol", "timestamp_et"]
    ).reset_index(drop=True)

    bars_df.to_csv(OPTION_BARS_FILE, index=False)

    print()
    print(f"Option bar rows: {len(bars_df):,}")
    print(f"Option contracts with bars: {bars_df['option_symbol'].nunique()}")
    print(f"Saved: {OPTION_BARS_FILE}")

    return bars_df


# ============================================================
# HISTORICAL ENTRY/EXIT
# ============================================================

def find_entry_exit(
    bars: pd.DataFrame,
    trading_date: date,
):
    day = bars[
        bars["trading_date"] == trading_date
    ].sort_values("timestamp_et")

    if day.empty:
        return None

    entry_candidates = day[
        day["timestamp_et"].dt.time >= ENTRY_TIME_ET
    ]

    if entry_candidates.empty:
        return None

    entry_row = entry_candidates.iloc[0]

    # 15:50 bar is preferred. If absent, use the latest bar <= 15:55.
    exit_candidates = day[
        (day["timestamp_et"].dt.time >= EXIT_TIME_ET)
        & (day["timestamp_et"].dt.time < dt_time(16, 0))
    ]

    if exit_candidates.empty:
        exit_candidates = day[
            day["timestamp_et"].dt.time < dt_time(16, 0)
        ]

    if exit_candidates.empty:
        return None

    exit_row = exit_candidates.iloc[-1]

    if exit_row["timestamp_et"] <= entry_row["timestamp_et"]:
        return None

    return {
        "entry_timestamp_et": entry_row["timestamp_et"],
        "entry_bar_close": float(entry_row["close"]),
        "entry_bar_volume": int(entry_row["volume"]),
        "exit_timestamp_et": exit_row["timestamp_et"],
        "exit_bar_close": float(exit_row["close"]),
        "exit_bar_volume": int(exit_row["volume"]),
    }


def build_option_trades(
    selection: pd.DataFrame,
    bars_df: pd.DataFrame,
) -> pd.DataFrame:

    bars_lookup = {
        symbol: g.copy()
        for symbol, g in bars_df.groupby("option_symbol")
    }

    rows = []

    usable = selection[
        selection["contract_found"] == True
    ].copy()

    total = len(usable)

    print()
    print(f"Building option trades from {total} contract selections...")

    for i, (_, sel) in enumerate(usable.iterrows(), start=1):
        option_symbol = str(sel["option_symbol"])
        trading_date = sel["trading_date"]

        contract_bars = bars_lookup.get(option_symbol)

        if contract_bars is None:
            continue

        fills = find_entry_exit(
            contract_bars,
            trading_date,
        )

        if fills is None:
            continue

        raw_entry = fills["entry_bar_close"]
        raw_exit = fills["exit_bar_close"]

        if raw_entry < MIN_ENTRY_PREMIUM:
            continue

        for slip in SLIPPAGE_PER_SIDE:
            entry_fill = raw_entry * (1.0 + slip)
            exit_fill = raw_exit * (1.0 - slip)

            option_return = exit_fill / entry_fill - 1.0

            rows.append(
                {
                    "signal_id": sel["signal_id"],
                    "trading_date": trading_date,
                    "symbol": sel["symbol"],
                    "trade_direction": sel["trade_direction"],
                    "option_type": sel["option_type"],
                    "option_symbol": option_symbol,
                    "target_dte": int(sel["target_dte"]),
                    "actual_dte": int(sel["actual_dte"]),
                    "moneyness_mode": sel["moneyness_mode"],
                    "underlying_entry_price": float(
                        sel["underlying_entry_price"]
                    ),
                    "target_strike": float(sel["target_strike"]),
                    "strike_price": float(sel["strike_price"]),
                    "slippage_per_side": slip,
                    "entry_timestamp_et": fills["entry_timestamp_et"],
                    "exit_timestamp_et": fills["exit_timestamp_et"],
                    "raw_entry_premium": raw_entry,
                    "raw_exit_premium": raw_exit,
                    "entry_fill_premium": entry_fill,
                    "exit_fill_premium": exit_fill,
                    "entry_contract_cost": entry_fill * 100.0,
                    "exit_contract_value": exit_fill * 100.0,
                    "pnl_per_contract": (
                        exit_fill - entry_fill
                    ) * 100.0,
                    "option_return": option_return,
                    "entry_bar_volume": fills["entry_bar_volume"],
                    "exit_bar_volume": fills["exit_bar_volume"],
                    "result": "WIN" if option_return > 0 else "LOSS",
                }
            )

    out = pd.DataFrame(rows)

    if out.empty:
        raise RuntimeError(
            "No executable option trades were built from the historical bars."
        )

    out.to_csv(OPTION_TRADES_FILE, index=False)

    print(f"Executable scenario rows: {len(out):,}")
    print(f"Saved: {OPTION_TRADES_FILE}")

    return out


# ============================================================
# GRID
# ============================================================

def build_daily_portfolio(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    # Equal premium allocation across same-day trades.
    daily = (
        trades
        .groupby("trading_date")
        .agg(
            trades=("signal_id", "count"),
            wins=("option_return", lambda x: int((x > 0).sum())),
            daily_return=("option_return", "mean"),
        )
        .reset_index()
        .sort_values("trading_date")
    )

    daily["win_rate"] = daily["wins"] / daily["trades"]

    return daily


def summarize_configuration(
    trades: pd.DataFrame,
    total_stock_signals: int,
    target_dte: int,
    moneyness_mode: str,
    slippage_per_side: float,
) -> dict:

    executed = len(trades)
    coverage = (
        executed / total_stock_signals
        if total_stock_signals
        else 0.0
    )

    if trades.empty:
        return {
            "target_dte": target_dte,
            "moneyness_mode": moneyness_mode,
            "slippage_per_side": slippage_per_side,
            "executed_trades": 0,
            "coverage": 0.0,
            "win_rate": 0.0,
            "avg_option_return": 0.0,
            "median_option_return": 0.0,
            "profit_factor": 0.0,
            "portfolio_return": 0.0,
            "max_drawdown": 0.0,
            "avg_entry_premium": 0.0,
            "avg_contract_cost": 0.0,
            "positive_days": 0,
            "trading_days": 0,
        }

    returns = trades["option_return"]
    daily = build_daily_portfolio(trades)

    portfolio_return = float(
        (1.0 + daily["daily_return"]).prod() - 1.0
    )

    return {
        "target_dte": target_dte,
        "moneyness_mode": moneyness_mode,
        "slippage_per_side": slippage_per_side,
        "executed_trades": executed,
        "coverage": coverage,
        "win_rate": float((returns > 0).mean()),
        "avg_option_return": float(returns.mean()),
        "median_option_return": float(returns.median()),
        "profit_factor": profit_factor(returns),
        "portfolio_return": portfolio_return,
        "max_drawdown": max_drawdown_from_daily_returns(
            daily["daily_return"]
        ),
        "avg_entry_premium": float(
            trades["entry_fill_premium"].mean()
        ),
        "avg_contract_cost": float(
            trades["entry_contract_cost"].mean()
        ),
        "positive_days": int(
            (daily["daily_return"] > 0).sum()
        ),
        "trading_days": len(daily),
    }


def build_grid(
    option_trades: pd.DataFrame,
    total_stock_signals: int,
) -> pd.DataFrame:

    rows = []

    for target_dte in TARGET_DTES:
        for moneyness_mode in MONEYNESS_MODES:
            for slip in SLIPPAGE_PER_SIDE:
                subset = option_trades[
                    (option_trades["target_dte"] == target_dte)
                    & (
                        option_trades["moneyness_mode"]
                        == moneyness_mode
                    )
                    & (
                        option_trades["slippage_per_side"]
                        == slip
                    )
                ].copy()

                rows.append(
                    summarize_configuration(
                        trades=subset,
                        total_stock_signals=total_stock_signals,
                        target_dte=target_dte,
                        moneyness_mode=moneyness_mode,
                        slippage_per_side=slip,
                    )
                )

    grid = pd.DataFrame(rows)

    # Ranking score rewards expectancy, PF, coverage and sample size,
    # with a drawdown penalty.
    pf_score = (
        grid["profit_factor"]
        .replace(float("inf"), 10.0)
        .clip(upper=10.0)
    )

    grid["rank_score"] = (
        grid["avg_option_return"]
        * grid["win_rate"]
        * pf_score
        * grid["coverage"]
        * grid["executed_trades"].clip(upper=250) ** 0.5
        / (1.0 + grid["max_drawdown"].abs())
    )

    grid = grid.sort_values(
        [
            "rank_score",
            "portfolio_return",
            "avg_option_return",
        ],
        ascending=False,
    ).reset_index(drop=True)

    grid.to_csv(GRID_FILE, index=False)

    return grid


def choose_best(grid: pd.DataFrame) -> pd.Series:
    # Prefer a realistic 2.5% per-side execution-cost case for the
    # live recommendation. Idealized 0% results remain in the grid.
    realistic = grid[
        (grid["slippage_per_side"] == 0.025)
        & (
            grid["executed_trades"]
            >= MIN_EXECUTED_TRADES_FOR_BEST
        )
        & (
            grid["coverage"]
            >= MIN_COVERAGE_FOR_BEST
        )
        & (grid["avg_option_return"] > 0)
        & (grid["profit_factor"] > 1)
    ].copy()

    if realistic.empty:
        realistic = grid[
            (
                grid["executed_trades"]
                >= MIN_EXECUTED_TRADES_FOR_BEST
            )
            & (grid["avg_option_return"] > 0)
            & (grid["profit_factor"] > 1)
        ].copy()

    if realistic.empty:
        realistic = grid[
            grid["executed_trades"] > 0
        ].copy()

    return realistic.sort_values(
        [
            "rank_score",
            "portfolio_return",
            "avg_option_return",
        ],
        ascending=False,
    ).iloc[0]


# ============================================================
# BEST CONFIG DETAIL
# ============================================================

def build_symbol_summary(trades: pd.DataFrame) -> pd.DataFrame:
    out = (
        trades
        .groupby(
            [
                "symbol",
                "trade_direction",
                "option_type",
            ]
        )
        .agg(
            trades=("signal_id", "count"),
            wins=(
                "option_return",
                lambda x: int((x > 0).sum()),
            ),
            win_rate=(
                "option_return",
                lambda x: float((x > 0).mean()),
            ),
            avg_option_return=("option_return", "mean"),
            median_option_return=("option_return", "median"),
            total_option_return=("option_return", "sum"),
            best_trade=("option_return", "max"),
            worst_trade=("option_return", "min"),
            avg_contract_cost=("entry_contract_cost", "mean"),
        )
        .reset_index()
    )

    out["score"] = (
        out["avg_option_return"]
        * out["win_rate"]
        * out["trades"].clip(upper=5)
    )

    return out.sort_values(
        ["score", "avg_option_return"],
        ascending=False,
    ).reset_index(drop=True)


# ============================================================
# REPORT
# ============================================================

def generate_report(
    grid: pd.DataFrame,
    best: pd.Series,
    best_trades: pd.DataFrame,
    symbols: pd.DataFrame,
    daily: pd.DataFrame,
    total_stock_signals: int,
) -> None:

    lines = []

    lines.append("# DeltaX historical options backtest")
    lines.append("")
    lines.append(
        f"Underlying stock signals tested: **{total_stock_signals}**"
    )
    lines.append("")
    lines.append("## Best realistic tested configuration")
    lines.append("")
    lines.append(f"- Target DTE: **{int(best['target_dte'])}**")
    lines.append(
        f"- Moneyness: **{best['moneyness_mode']}**"
    )
    lines.append(
        f"- Assumed slippage per side: "
        f"**{best['slippage_per_side'] * 100:.1f}%**"
    )
    lines.append(
        f"- Executed trades: **{int(best['executed_trades'])}**"
    )
    lines.append(
        f"- Historical-data coverage: "
        f"**{best['coverage'] * 100:.1f}%**"
    )
    lines.append(
        f"- Win rate: **{best['win_rate'] * 100:.1f}%**"
    )
    lines.append(
        f"- Average option return: "
        f"**{pct(float(best['avg_option_return']))}**"
    )
    lines.append(
        f"- Median option return: "
        f"**{pct(float(best['median_option_return']))}**"
    )

    pf = float(best["profit_factor"])
    pf_text = "INF" if math.isinf(pf) else f"{pf:.2f}"

    lines.append(f"- Profit factor: **{pf_text}**")
    lines.append(
        f"- Equal-premium portfolio return: "
        f"**{pct(float(best['portfolio_return']))}**"
    )
    lines.append(
        f"- Max drawdown: "
        f"**{pct(float(best['max_drawdown']))}**"
    )
    lines.append(
        f"- Average contract cost: "
        f"**${best['avg_contract_cost']:.2f}**"
    )

    lines.append("")
    lines.append("## Daily")
    lines.append("")
    lines.append("| Date | Trades | Win rate | Daily return |")
    lines.append("|---|---:|---:|---:|")

    for _, r in daily.iterrows():
        lines.append(
            f"| {r['trading_date']} | "
            f"{int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['daily_return']))} |"
        )

    lines.append("")
    lines.append("## Top symbols")
    lines.append("")
    lines.append(
        "| Symbol | Side | Trades | Win rate | Avg option return | "
        "Best | Worst | Avg contract cost |"
    )
    lines.append(
        "|---|---|---:|---:|---:|---:|---:|---:|"
    )

    for _, r in symbols.head(25).iterrows():
        lines.append(
            f"| {r['symbol']} | {r['trade_direction']} | "
            f"{int(r['trades'])} | "
            f"{r['win_rate'] * 100:.1f}% | "
            f"{pct(float(r['avg_option_return']))} | "
            f"{pct(float(r['best_trade']))} | "
            f"{pct(float(r['worst_trade']))} | "
            f"${r['avg_contract_cost']:.2f} |"
        )

    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- Historical option bars are used for premium returns. "
        "This does not reconstruct historical bid/ask quotes."
    )
    lines.append(
        "- The script therefore runs several explicit slippage assumptions."
    )
    lines.append(
        "- Historical Greeks are not reconstructed; strike moneyness is "
        "used instead of historical delta."
    )
    lines.append(
        "- This is still the same five-session geopolitical event sample, "
        "so parameter selection is in-sample and should not be treated as "
        "a guarantee of live results."
    )

    REPORT_FILE.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


# ============================================================
# PRINT
# ============================================================

def print_grid(grid: pd.DataFrame) -> None:
    print()
    print("=" * 122)
    print("TOP OPTION CONFIGURATIONS")
    print("=" * 122)

    for _, r in grid.head(30).iterrows():
        pf = float(r["profit_factor"])
        pf_text = "INF" if math.isinf(pf) else f"{pf:.2f}"

        print(
            f"DTE={int(r['target_dte']):2} | "
            f"{r['moneyness_mode']:6} | "
            f"slip={r['slippage_per_side'] * 100:4.1f}%/side | "
            f"trades={int(r['executed_trades']):3} | "
            f"coverage={r['coverage'] * 100:5.1f}% | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(float(r['avg_option_return'])):>9} | "
            f"PF={pf_text:>5} | "
            f"portfolio={pct(float(r['portfolio_return'])):>9} | "
            f"DD={pct(float(r['max_drawdown'])):>9} | "
            f"avg cost=${r['avg_contract_cost']:.0f}"
        )


def print_best(
    best: pd.Series,
    symbols: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:

    print()
    print("=" * 122)
    print("BEST REALISTIC CONFIGURATION")
    print("=" * 122)

    pf = float(best["profit_factor"])
    pf_text = "INF" if math.isinf(pf) else f"{pf:.2f}"

    print(f"Target DTE:             {int(best['target_dte'])}")
    print(f"Moneyness:              {best['moneyness_mode']}")
    print(
        f"Slippage per side:      "
        f"{best['slippage_per_side'] * 100:.1f}%"
    )
    print(
        f"Executed trades:        "
        f"{int(best['executed_trades'])}"
    )
    print(
        f"Coverage:               "
        f"{best['coverage'] * 100:.1f}%"
    )
    print(
        f"Win rate:               "
        f"{best['win_rate'] * 100:.1f}%"
    )
    print(
        f"Average option return:  "
        f"{pct(float(best['avg_option_return']))}"
    )
    print(
        f"Median option return:   "
        f"{pct(float(best['median_option_return']))}"
    )
    print(f"Profit factor:          {pf_text}")
    print(
        f"Portfolio return:       "
        f"{pct(float(best['portfolio_return']))}"
    )
    print(
        f"Max drawdown:           "
        f"{pct(float(best['max_drawdown']))}"
    )
    print(
        f"Average contract cost:  "
        f"${best['avg_contract_cost']:.2f}"
    )

    print()
    print("DAILY")
    print("-" * 122)

    for _, r in daily.iterrows():
        print(
            f"{r['trading_date']} | "
            f"trades={int(r['trades']):3} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"daily={pct(float(r['daily_return'])):>9}"
        )

    print()
    print("TOP OPTION SYMBOL SETUPS")
    print("-" * 122)

    for _, r in symbols.head(30).iterrows():
        print(
            f"{r['symbol']:6} {r['trade_direction']:5} "
            f"{r['option_type']:4} | "
            f"trades={int(r['trades']):2} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(float(r['avg_option_return'])):>9} | "
            f"best={pct(float(r['best_trade'])):>9} | "
            f"worst={pct(float(r['worst_trade'])):>9} | "
            f"cost=${r['avg_contract_cost']:.0f}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    print("=" * 122)
    print("DELTAX OPTIONS BACKTEST")
    print("10-MIN STOCK SIGNAL -> HISTORICAL CALL/PUT EXECUTION")
    print("=" * 122)

    print()
    print(
        "DTE targets: "
        + ", ".join(str(x) for x in TARGET_DTES)
    )
    print(
        "Moneyness: "
        + ", ".join(MONEYNESS_MODES)
    )
    print(
        "Slippage per side: "
        + ", ".join(
            f"{x * 100:.1f}%"
            for x in SLIPPAGE_PER_SIDE
        )
    )
    print(
        f"Entry: {ENTRY_TIME_ET.strftime('%H:%M')} ET | "
        f"Exit: {EXIT_TIME_ET.strftime('%H:%M')} ET"
    )
    print()

    load_env()

    api_key = get_required_env("ALPACA_API_KEY_PAPER")
    api_secret = get_required_env("ALPACA_API_SECRET_PAPER")
    trading_url = get_required_env("ALPACA_TRADING_URL_PAPER")

    trading_url = trading_url.rstrip("/")
    if trading_url.endswith("/v2"):
        trading_url = trading_url[:-3]

    signals = load_stock_signals()

    trading_client = TradingClient(
        api_key=api_key,
        secret_key=api_secret,
        paper=False,
        url_override=trading_url,
    )

    option_data_client = OptionHistoricalDataClient(
        api_key=api_key,
        secret_key=api_secret,
    )

    # --------------------------------------------------------
    # STEP 1: contracts
    # --------------------------------------------------------

    selection = build_contract_selection(
        trading_client=trading_client,
        signals=signals,
    )

    # --------------------------------------------------------
    # STEP 2: historical option bars
    # --------------------------------------------------------

    bars_df = fetch_option_bars(
        data_client=option_data_client,
        selection=selection,
    )

    # --------------------------------------------------------
    # STEP 3: option trade scenarios
    # --------------------------------------------------------

    option_trades = build_option_trades(
        selection=selection,
        bars_df=bars_df,
    )

    # --------------------------------------------------------
    # STEP 4: rank DTE x moneyness x execution cost
    # --------------------------------------------------------

    grid = build_grid(
        option_trades=option_trades,
        total_stock_signals=len(signals),
    )

    print_grid(grid)

    best = choose_best(grid)

    best_trades = option_trades[
        (option_trades["target_dte"] == int(best["target_dte"]))
        & (
            option_trades["moneyness_mode"]
            == best["moneyness_mode"]
        )
        & (
            option_trades["slippage_per_side"]
            == float(best["slippage_per_side"])
        )
    ].copy()

    best_symbols = build_symbol_summary(best_trades)
    best_daily = build_daily_portfolio(best_trades)

    best_trades.to_csv(
        BEST_TRADES_FILE,
        index=False,
    )
    best_symbols.to_csv(
        BEST_SYMBOLS_FILE,
        index=False,
    )
    best_daily.to_csv(
        BEST_DAILY_FILE,
        index=False,
    )

    generate_report(
        grid=grid,
        best=best,
        best_trades=best_trades,
        symbols=best_symbols,
        daily=best_daily,
        total_stock_signals=len(signals),
    )

    print_best(
        best=best,
        symbols=best_symbols,
        daily=best_daily,
    )

    print()
    print("=" * 122)
    print("FILES CREATED")
    print("=" * 122)
    print(f"Contract selection: {CONTRACT_SELECTION_FILE}")
    print(f"Option bars:        {OPTION_BARS_FILE}")
    print(f"All option trades:  {OPTION_TRADES_FILE}")
    print(f"Grid:               {GRID_FILE}")
    print(f"Best trades:        {BEST_TRADES_FILE}")
    print(f"Best symbols:       {BEST_SYMBOLS_FILE}")
    print(f"Best daily:         {BEST_DAILY_FILE}")
    print(f"Report:             {REPORT_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
