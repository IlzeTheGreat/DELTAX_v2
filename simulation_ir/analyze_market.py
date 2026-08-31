from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

INPUT_FILE = SCRIPT_DIR / "market_5min_2026-02-28_5d.csv"

DAILY_SUMMARY_FILE = SCRIPT_DIR / "daily_summary.csv"
STOCK_IMPACT_FILE = SCRIPT_DIR / "stock_impact.csv"
DAILY_STOCK_FILE = SCRIPT_DIR / "daily_stock_returns.csv"
THEME_SUMMARY_FILE = SCRIPT_DIR / "theme_summary.csv"
STRATEGY_FILE = SCRIPT_DIR / "strategy_backtest.csv"
STRATEGY_TRADES_FILE = SCRIPT_DIR / "strategy_trades.csv"
REPORT_FILE = SCRIPT_DIR / "report.md"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# EVENT / SECTOR THEMES
# ============================================================

# Šie nav domāti kā perfekta GICS klasifikācija.
# Tie ir analīzei noderīgi "market themes", īpaši ģeopolitiskam eventam.

THEMES = {
    # Energy
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "EOG": "Energy",
    "OXY": "Energy",
    "DVN": "Energy",
    "FANG": "Energy",
    "EQT": "Energy",
    "SLB": "Energy",
    "HAL": "Energy",
    "MPC": "Energy",
    "VLO": "Energy",
    "PSX": "Energy",
    "KMI": "Energy",
    "WMB": "Energy",
    "OKE": "Energy",

    # Defense / aerospace
    "LMT": "Defense",
    "NOC": "Defense",
    "GD": "Defense",
    "LHX": "Defense",
    "RTX": "Defense",
    "HWM": "Defense",
    "TDG": "Defense",
    "CW": "Defense",
    "BA": "Aerospace",

    # Utilities / power
    "NEE": "Utilities",
    "CEG": "Utilities",
    "DUK": "Utilities",
    "SO": "Utilities",
    "D": "Utilities",
    "AEP": "Utilities",
    "VST": "Utilities",
    "EXC": "Utilities",
    "PEG": "Utilities",
    "XEL": "Utilities",
    "PCG": "Utilities",

    # Industrial / infrastructure
    "CAT": "Industrials",
    "DE": "Industrials",
    "GE": "Industrials",
    "HON": "Industrials",
    "ETN": "Industrials",
    "VRT": "Industrials",
    "PWR": "Industrials",
    "EMR": "Industrials",
    "ROK": "Industrials",
    "JCI": "Industrials",
    "HUBB": "Industrials",
    "CARR": "Industrials",

    # Semiconductors / hardware
    "NVDA": "Semiconductors",
    "AMD": "Semiconductors",
    "AVGO": "Semiconductors",
    "ADI": "Semiconductors",
    "AMAT": "Semiconductors",
    "INTC": "Semiconductors",
    "LRCX": "Semiconductors",
    "MU": "Semiconductors",
    "QCOM": "Semiconductors",
    "TXN": "Semiconductors",

    # Software / tech
    "MSFT": "Technology",
    "GOOG": "Technology",
    "GOOGL": "Technology",
    "META": "Technology",
    "AMZN": "Technology",
    "ADBE": "Technology",
    "CRM": "Technology",
    "CSCO": "Technology",
    "IBM": "Technology",
    "NOW": "Technology",
    "ORCL": "Technology",
    "PANW": "Technology",
    "CDNS": "Technology",
    "SNPS": "Technology",

    # Financials
    "BAC": "Financials",
    "BLK": "Financials",
    "GS": "Financials",
    "JPM": "Financials",
    "MA": "Financials",
    "MS": "Financials",
    "SCHW": "Financials",
    "SPGI": "Financials",
    "V": "Financials",
    "BRK.B": "Financials",

    # Healthcare
    "ABT": "Healthcare",
    "ABBV": "Healthcare",
    "AMGN": "Healthcare",
    "CI": "Healthcare",
    "DHR": "Healthcare",
    "ELV": "Healthcare",
    "ISRG": "Healthcare",
    "JNJ": "Healthcare",
    "LLY": "Healthcare",
    "MDT": "Healthcare",
    "MRK": "Healthcare",
    "REGN": "Healthcare",
    "TMO": "Healthcare",
    "UNH": "Healthcare",
    "VRTX": "Healthcare",

    # Consumer
    "BKNG": "Consumer",
    "COST": "Consumer",
    "DIS": "Consumer",
    "HD": "Consumer",
    "KO": "Consumer",
    "LOW": "Consumer",
    "MCD": "Consumer",
    "NFLX": "Consumer",
    "PEP": "Consumer",
    "PG": "Consumer",
    "PM": "Consumer",
    "TGT": "Consumer",
    "TSLA": "Consumer",
    "UBER": "Consumer",
    "WMT": "Consumer",

    # Communications
    "VZ": "Communications",

    # Real estate
    "PLD": "Real Estate",

    # Other / growth
    "IREN": "High Beta Growth",
    "RKLB": "High Beta Growth",
    "ENPH": "High Beta Growth",

    # Materials / industrial chemistry
    "LIN": "Materials",
}


# ============================================================
# SETTINGS
# ============================================================

OPENING_WINDOW_MINUTES = 30

# Signal thresholds
OPENING_MOVE_THRESHOLD = 0.0075      # 0.75%
GAP_THRESHOLD = 0.0100               # 1%
VWAP_THRESHOLD = 0.0050              # 0.50%

# Classification
MODERATE_IMPACT = 0.02               # 2 percentage points
STRONG_IMPACT = 0.05                 # 5 percentage points


# ============================================================
# HELPERS
# ============================================================

def pct(x: float | None, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "n/a"

    return f"{x * 100:+.{digits}f}%"


def num(x: float | None, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "n/a"

    return f"{x:.{digits}f}"


def profit_factor(returns: pd.Series) -> float:
    winners = returns[returns > 0].sum()
    losers = abs(returns[returns < 0].sum())

    if losers == 0:
        return float("inf") if winners > 0 else 0.0

    return winners / losers


def max_drawdown_from_returns(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0

    equity = (1 + returns.fillna(0)).cumprod()
    peak = equity.cummax()

    drawdown = equity / peak - 1

    return float(drawdown.min())


def impact_label(relative_return: float, total_return: float) -> str:
    """
    Classification uses both:
    - relative performance versus the user's full universe
    - absolute five-day return
    """

    if relative_return >= STRONG_IMPACT or total_return >= 0.08:
        return "STRONG POSITIVE"

    if relative_return >= MODERATE_IMPACT or total_return >= 0.04:
        return "POSITIVE"

    if relative_return <= -STRONG_IMPACT or total_return <= -0.08:
        return "STRONG NEGATIVE"

    if relative_return <= -MODERATE_IMPACT or total_return <= -0.04:
        return "NEGATIVE"

    return "LIMITED / MARKET-LIKE"


# ============================================================
# LOAD DATA
# ============================================================

def load_data() -> pd.DataFrame:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input CSV not found:\n{INPUT_FILE}"
        )

    print(f"Loading: {INPUT_FILE}")

    df = pd.read_csv(INPUT_FILE)

    required = {
        "timestamp_utc",
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
            f"Missing required columns: {sorted(missing)}"
        )

    df["timestamp_et"] = pd.to_datetime(
        df["timestamp_et"],
        utc=True,
    ).dt.tz_convert("America/New_York")

    df["trading_date"] = pd.to_datetime(
        df["trading_date"]
    ).dt.date

    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    if "vwap" in df.columns:
        numeric_columns.append("vwap")

    for col in numeric_columns:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "symbol",
            "open",
            "high",
            "low",
            "close",
        ]
    )

    df = df.sort_values(
        ["symbol", "trading_date", "timestamp_et"]
    ).reset_index(drop=True)

    print(f"Rows loaded: {len(df):,}")
    print(f"Symbols: {df['symbol'].nunique()}")
    print(f"Days: {df['trading_date'].nunique()}")

    return df


# ============================================================
# DAILY STOCK DATA
# ============================================================

def build_daily_stock_data(df: pd.DataFrame) -> pd.DataFrame:
    rows = []

    grouped = df.groupby(
        ["trading_date", "symbol"],
        sort=True,
    )

    for (trading_date, symbol), g in grouped:
        g = g.sort_values("timestamp_et")

        first = g.iloc[0]
        last = g.iloc[-1]

        day_open = float(first["open"])
        day_close = float(last["close"])

        day_high = float(g["high"].max())
        day_low = float(g["low"].min())

        open_close_return = day_close / day_open - 1

        intraday_high_return = day_high / day_open - 1
        intraday_low_return = day_low / day_open - 1

        intraday_range = day_high / day_low - 1

        volume = float(g["volume"].sum())

        rows.append(
            {
                "trading_date": trading_date,
                "symbol": symbol,
                "theme": THEMES.get(symbol, "Other"),
                "open": day_open,
                "high": day_high,
                "low": day_low,
                "close": day_close,
                "return_open_close": open_close_return,
                "intraday_high_from_open": intraday_high_return,
                "intraday_low_from_open": intraday_low_return,
                "intraday_range": intraday_range,
                "volume": volume,
                "bars": len(g),
            }
        )

    daily = pd.DataFrame(rows)

    daily = daily.sort_values(
        ["trading_date", "symbol"]
    ).reset_index(drop=True)

    return daily


# ============================================================
# MARKET DAILY SUMMARY
# ============================================================

def build_daily_summary(
    daily: pd.DataFrame,
) -> pd.DataFrame:

    rows = []

    for trading_date, g in daily.groupby("trading_date"):
        returns = g["return_open_close"]

        rows.append(
            {
                "trading_date": trading_date,
                "stocks": len(g),
                "equal_weight_return": returns.mean(),
                "median_return": returns.median(),
                "advancers_pct": (returns > 0).mean(),
                "decliners_pct": (returns < 0).mean(),
                "average_absolute_move": returns.abs().mean(),
                "average_intraday_range": g[
                    "intraday_range"
                ].mean(),
                "best_symbol": g.loc[
                    returns.idxmax(),
                    "symbol",
                ],
                "best_return": returns.max(),
                "worst_symbol": g.loc[
                    returns.idxmin(),
                    "symbol",
                ],
                "worst_return": returns.min(),
            }
        )

    return pd.DataFrame(rows).sort_values(
        "trading_date"
    )


# ============================================================
# STOCK EVENT IMPACT
# ============================================================

def build_stock_impact(
    daily: pd.DataFrame,
) -> tuple[pd.DataFrame, float]:

    market_daily = (
        daily.groupby("trading_date")["return_open_close"]
        .mean()
        .sort_index()
    )

    market_event_return = (
        (1 + market_daily).prod() - 1
    )

    rows = []

    for symbol, g in daily.groupby("symbol"):
        g = g.sort_values("trading_date")

        stock_event_return = (
            (1 + g["return_open_close"]).prod() - 1
        )

        relative_return = (
            stock_event_return - market_event_return
        )

        best_idx = g["return_open_close"].idxmax()
        worst_idx = g["return_open_close"].idxmin()

        positive_days = int(
            (g["return_open_close"] > 0).sum()
        )

        negative_days = int(
            (g["return_open_close"] < 0).sum()
        )

        rows.append(
            {
                "symbol": symbol,
                "theme": THEMES.get(symbol, "Other"),
                "event_return_5d": stock_event_return,
                "universe_return_5d": market_event_return,
                "relative_return_5d": relative_return,
                "impact": impact_label(
                    relative_return,
                    stock_event_return,
                ),
                "positive_days": positive_days,
                "negative_days": negative_days,
                "best_day": g.loc[
                    best_idx,
                    "trading_date",
                ],
                "best_day_return": g.loc[
                    best_idx,
                    "return_open_close",
                ],
                "worst_day": g.loc[
                    worst_idx,
                    "trading_date",
                ],
                "worst_day_return": g.loc[
                    worst_idx,
                    "return_open_close",
                ],
                "max_intraday_up": g[
                    "intraday_high_from_open"
                ].max(),
                "max_intraday_down": g[
                    "intraday_low_from_open"
                ].min(),
                "avg_intraday_range": g[
                    "intraday_range"
                ].mean(),
            }
        )

    impact = pd.DataFrame(rows)

    impact = impact.sort_values(
        "relative_return_5d",
        ascending=False,
    ).reset_index(drop=True)

    return impact, market_event_return


# ============================================================
# THEME / SECTOR ANALYSIS
# ============================================================

def build_theme_summary(
    daily: pd.DataFrame,
) -> pd.DataFrame:

    symbol_returns = []

    for (theme, symbol), g in daily.groupby(
        ["theme", "symbol"]
    ):
        total_return = (
            (1 + g["return_open_close"]).prod() - 1
        )

        symbol_returns.append(
            {
                "theme": theme,
                "symbol": symbol,
                "return_5d": total_return,
            }
        )

    sr = pd.DataFrame(symbol_returns)

    theme = (
        sr.groupby("theme")
        .agg(
            stocks=("symbol", "count"),
            average_return_5d=("return_5d", "mean"),
            median_return_5d=("return_5d", "median"),
            positive_stocks_pct=(
                "return_5d",
                lambda x: (x > 0).mean(),
            ),
        )
        .reset_index()
        .sort_values(
            "average_return_5d",
            ascending=False,
        )
    )

    return theme


# ============================================================
# STRATEGY BACKTEST
# ============================================================

def create_trade(
    strategy: str,
    trading_date,
    symbol: str,
    direction: int,
    entry_price: float,
    exit_price: float,
    signal_value: float,
) -> dict:

    raw_return = exit_price / entry_price - 1
    strategy_return = raw_return * direction

    return {
        "strategy": strategy,
        "trading_date": trading_date,
        "symbol": symbol,
        "direction": "LONG" if direction == 1 else "SHORT",
        "entry_price": entry_price,
        "exit_price": exit_price,
        "signal_value": signal_value,
        "return": strategy_return,
    }


def build_strategy_trades(
    df: pd.DataFrame,
) -> pd.DataFrame:

    trades = []

    previous_close = {}

    all_dates = sorted(df["trading_date"].unique())

    for trading_date in all_dates:

        day_df = df[
            df["trading_date"] == trading_date
        ]

        for symbol, g in day_df.groupby("symbol"):
            g = g.sort_values("timestamp_et").copy()

            if len(g) < 7:
                continue

            day_open = float(g.iloc[0]["open"])
            day_close = float(g.iloc[-1]["close"])

            # ------------------------------------------------
            # First 30 minutes
            # 09:30 -> roughly 10:00
            # ------------------------------------------------

            opening = g.iloc[
                : int(OPENING_WINDOW_MINUTES / 5)
            ]

            if opening.empty:
                continue

            opening_end_price = float(
                opening.iloc[-1]["close"]
            )

            opening_return = (
                opening_end_price / day_open - 1
            )

            # =================================================
            # STRATEGY 1:
            # OPENING MOMENTUM
            # =================================================

            if abs(opening_return) >= OPENING_MOVE_THRESHOLD:

                direction = (
                    1 if opening_return > 0 else -1
                )

                trades.append(
                    create_trade(
                        strategy="OPENING_MOMENTUM",
                        trading_date=trading_date,
                        symbol=symbol,
                        direction=direction,
                        entry_price=opening_end_price,
                        exit_price=day_close,
                        signal_value=opening_return,
                    )
                )

                # =============================================
                # STRATEGY 2:
                # OPENING REVERSAL
                # =============================================

                trades.append(
                    create_trade(
                        strategy="OPENING_REVERSAL",
                        trading_date=trading_date,
                        symbol=symbol,
                        direction=-direction,
                        entry_price=opening_end_price,
                        exit_price=day_close,
                        signal_value=opening_return,
                    )
                )

            # =================================================
            # STRATEGY 3 + 4:
            # GAP FOLLOW / GAP FADE
            # =================================================

            if symbol in previous_close:

                prev_close = previous_close[symbol]

                gap = day_open / prev_close - 1

                if abs(gap) >= GAP_THRESHOLD:

                    direction = 1 if gap > 0 else -1

                    trades.append(
                        create_trade(
                            strategy="GAP_FOLLOW",
                            trading_date=trading_date,
                            symbol=symbol,
                            direction=direction,
                            entry_price=day_open,
                            exit_price=day_close,
                            signal_value=gap,
                        )
                    )

                    trades.append(
                        create_trade(
                            strategy="GAP_FADE",
                            trading_date=trading_date,
                            symbol=symbol,
                            direction=-direction,
                            entry_price=day_open,
                            exit_price=day_close,
                            signal_value=gap,
                        )
                    )

            # =================================================
            # STRATEGY 5:
            # 30-MIN VWAP MOMENTUM
            # =================================================

            opening_volume = opening["volume"].fillna(0)

            if opening_volume.sum() > 0:

                if (
                    "vwap" in opening.columns
                    and opening["vwap"].notna().any()
                ):

                    px = opening["vwap"].fillna(
                        opening["close"]
                    )

                else:
                    px = opening["close"]

                opening_vwap = float(
                    (px * opening_volume).sum()
                    / opening_volume.sum()
                )

                distance_from_vwap = (
                    opening_end_price / opening_vwap - 1
                )

                if abs(distance_from_vwap) >= VWAP_THRESHOLD:

                    direction = (
                        1
                        if distance_from_vwap > 0
                        else -1
                    )

                    trades.append(
                        create_trade(
                            strategy="VWAP_MOMENTUM",
                            trading_date=trading_date,
                            symbol=symbol,
                            direction=direction,
                            entry_price=opening_end_price,
                            exit_price=day_close,
                            signal_value=distance_from_vwap,
                        )
                    )

            previous_close[symbol] = day_close

    return pd.DataFrame(trades)


def summarize_strategies(
    trades: pd.DataFrame,
) -> pd.DataFrame:

    if trades.empty:
        return pd.DataFrame()

    rows = []

    for strategy, g in trades.groupby("strategy"):

        returns = g["return"]

        rows.append(
            {
                "strategy": strategy,
                "trades": len(g),
                "win_rate": (returns > 0).mean(),
                "average_return_per_trade": returns.mean(),
                "median_return_per_trade": returns.median(),
                "total_compounded_return": (
                    (1 + returns).prod() - 1
                ),
                "profit_factor": profit_factor(returns),
                "max_drawdown": max_drawdown_from_returns(
                    returns
                ),
                "best_trade": returns.max(),
                "worst_trade": returns.min(),
            }
        )

    result = pd.DataFrame(rows)

    # Ranking is deliberately based mainly on average return,
    # then profit factor, rather than compounded return across
    # hundreds of overlapping positions.
    result = result.sort_values(
        [
            "average_return_per_trade",
            "win_rate",
        ],
        ascending=False,
    ).reset_index(drop=True)

    return result


# ============================================================
# MARKDOWN REPORT
# ============================================================

def markdown_table(
    df: pd.DataFrame,
    columns: list[str],
    headers: list[str],
    formatters: dict | None = None,
) -> str:

    formatters = formatters or {}

    lines = []

    lines.append(
        "| " + " | ".join(headers) + " |"
    )

    lines.append(
        "| " + " | ".join(["---"] * len(headers)) + " |"
    )

    for _, row in df.iterrows():

        values = []

        for col in columns:
            value = row[col]

            if col in formatters:
                value = formatters[col](value)

            values.append(str(value))

        lines.append(
            "| " + " | ".join(values) + " |"
        )

    return "\n".join(lines)


def generate_report(
    daily: pd.DataFrame,
    daily_summary: pd.DataFrame,
    impact: pd.DataFrame,
    market_event_return: float,
    theme_summary: pd.DataFrame,
    strategies: pd.DataFrame,
    trades: pd.DataFrame,
):

    lines = []

    lines.append("# Iran event: 5 trading day market analysis")
    lines.append("")

    dates = sorted(daily["trading_date"].unique())

    lines.append(
        f"Period: **{dates[0]} to {dates[-1]}**"
    )

    lines.append(
        f"Stocks analysed: **{daily['symbol'].nunique()}**"
    )

    lines.append(
        f"Equal-weight universe 5-day return: "
        f"**{pct(market_event_return)}**"
    )

    lines.append("")

    # ========================================================
    # DAILY
    # ========================================================

    lines.append("## 1. What happened each day")
    lines.append("")

    for _, row in daily_summary.iterrows():

        date_value = row["trading_date"]

        direction = (
            "POSITIVE"
            if row["equal_weight_return"] > 0
            else "NEGATIVE"
        )

        lines.append(
            f"### {date_value} — {direction}"
        )

        lines.append("")

        lines.append(
            f"- Equal-weight universe: "
            f"**{pct(row['equal_weight_return'])}**"
        )

        lines.append(
            f"- Median stock: "
            f"**{pct(row['median_return'])}**"
        )

        lines.append(
            f"- Stocks rising: "
            f"**{row['advancers_pct'] * 100:.1f}%**"
        )

        lines.append(
            f"- Stocks falling: "
            f"**{row['decliners_pct'] * 100:.1f}%**"
        )

        lines.append(
            f"- Average intraday range: "
            f"**{pct(row['average_intraday_range'])}**"
        )

        lines.append(
            f"- Best: **{row['best_symbol']} "
            f"{pct(row['best_return'])}**"
        )

        lines.append(
            f"- Worst: **{row['worst_symbol']} "
            f"{pct(row['worst_return'])}**"
        )

        day = daily[
            daily["trading_date"] == date_value
        ]

        winners = day.nlargest(
            5,
            "return_open_close",
        )

        losers = day.nsmallest(
            5,
            "return_open_close",
        )

        lines.append("")
        lines.append("Top 5 winners:")
        lines.append("")

        for _, x in winners.iterrows():
            lines.append(
                f"- {x['symbol']} ({x['theme']}): "
                f"{pct(x['return_open_close'])}"
            )

        lines.append("")
        lines.append("Top 5 losers:")
        lines.append("")

        for _, x in losers.iterrows():
            lines.append(
                f"- {x['symbol']} ({x['theme']}): "
                f"{pct(x['return_open_close'])}"
            )

        lines.append("")

    # ========================================================
    # IMPACT
    # ========================================================

    lines.append("## 2. Most positively affected stocks")
    lines.append("")

    top_positive = impact.head(15)

    lines.append(
        markdown_table(
            top_positive,
            columns=[
                "symbol",
                "theme",
                "event_return_5d",
                "relative_return_5d",
                "impact",
            ],
            headers=[
                "Symbol",
                "Theme",
                "5d return",
                "vs universe",
                "Impact",
            ],
            formatters={
                "event_return_5d": pct,
                "relative_return_5d": pct,
            },
        )
    )

    lines.append("")

    lines.append("## 3. Most negatively affected stocks")
    lines.append("")

    top_negative = impact.tail(15).sort_values(
        "relative_return_5d"
    )

    lines.append(
        markdown_table(
            top_negative,
            columns=[
                "symbol",
                "theme",
                "event_return_5d",
                "relative_return_5d",
                "impact",
            ],
            headers=[
                "Symbol",
                "Theme",
                "5d return",
                "vs universe",
                "Impact",
            ],
            formatters={
                "event_return_5d": pct,
                "relative_return_5d": pct,
            },
        )
    )

    lines.append("")

    # ========================================================
    # THEMES
    # ========================================================

    lines.append("## 4. Sector / theme behaviour")
    lines.append("")

    lines.append(
        markdown_table(
            theme_summary,
            columns=[
                "theme",
                "stocks",
                "average_return_5d",
                "median_return_5d",
                "positive_stocks_pct",
            ],
            headers=[
                "Theme",
                "Stocks",
                "Avg 5d",
                "Median 5d",
                "Positive",
            ],
            formatters={
                "average_return_5d": pct,
                "median_return_5d": pct,
                "positive_stocks_pct": lambda x:
                    f"{x * 100:.1f}%",
            },
        )
    )

    lines.append("")

    # ========================================================
    # STRATEGY BACKTEST
    # ========================================================

    lines.append("## 5. Strategy backtest")
    lines.append("")

    if strategies.empty:

        lines.append("No strategy trades generated.")

    else:

        lines.append(
            markdown_table(
                strategies,
                columns=[
                    "strategy",
                    "trades",
                    "win_rate",
                    "average_return_per_trade",
                    "median_return_per_trade",
                    "profit_factor",
                    "max_drawdown",
                ],
                headers=[
                    "Strategy",
                    "Trades",
                    "Win rate",
                    "Avg trade",
                    "Median",
                    "Profit factor",
                    "Max DD",
                ],
                formatters={
                    "win_rate": lambda x:
                        f"{x * 100:.1f}%",
                    "average_return_per_trade": pct,
                    "median_return_per_trade": pct,
                    "profit_factor": lambda x:
                        (
                            "inf"
                            if math.isinf(x)
                            else f"{x:.2f}"
                        ),
                    "max_drawdown": pct,
                },
            )
        )

        lines.append("")

        best = strategies.iloc[0]

        lines.append("### Best strategy in this event window")
        lines.append("")

        lines.append(
            f"**{best['strategy']}**"
        )

        lines.append("")

        lines.append(
            f"- Trades: {int(best['trades'])}"
        )

        lines.append(
            f"- Win rate: {best['win_rate'] * 100:.1f}%"
        )

        lines.append(
            f"- Average return/trade: "
            f"{pct(best['average_return_per_trade'])}"
        )

        lines.append(
            f"- Profit factor: "
            f"{num(best['profit_factor'])}"
        )

        best_strategy_trades = trades[
            trades["strategy"] == best["strategy"]
        ]

        best_symbols = (
            best_strategy_trades.groupby("symbol")["return"]
            .agg(["mean", "count"])
            .sort_values("mean", ascending=False)
            .head(10)
            .reset_index()
        )

        lines.append("")
        lines.append(
            "Stocks where this strategy worked best:"
        )
        lines.append("")

        for _, x in best_symbols.iterrows():

            lines.append(
                f"- {x['symbol']}: "
                f"avg {pct(x['mean'])}, "
                f"{int(x['count'])} trades"
            )

    lines.append("")

    # ========================================================
    # INTERPRETATION
    # ========================================================

    lines.append("## 6. How to interpret this for trading")
    lines.append("")

    lines.append(
        "This is an event study, not proof that a strategy "
        "will remain profitable in normal market conditions."
    )

    lines.append("")

    lines.append(
        "The most useful signals are:"
    )

    lines.append("")

    lines.append(
        "1. **Relative strength** — stocks that strongly "
        "outperformed the full universe are the clearest "
        "event winners."
    )

    lines.append(
        "2. **Relative weakness** — stocks that strongly "
        "underperformed are candidates for avoiding longs "
        "or for short setups during continued escalation."
    )

    lines.append(
        "3. **Theme persistence** — if several stocks in the "
        "same sector move together, the move is more likely "
        "to be macro/event-driven rather than company-specific."
    )

    lines.append(
        "4. **Opening behaviour** — the strategy backtest "
        "shows whether large overnight / early-session moves "
        "tended to continue or reverse."
    )

    lines.append(
        "5. **Do not generalise from five sessions alone.** "
        "The winning strategy here should be treated as the "
        "best strategy for this specific geopolitical shock "
        "window, not as a universal trading rule."
    )

    REPORT_FILE.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


# ============================================================
# CONSOLE OUTPUT
# ============================================================

def print_console_summary(
    daily_summary,
    impact,
    theme_summary,
    strategies,
    market_event_return,
):

    print()
    print("=" * 78)
    print("IRAN EVENT MARKET ANALYSIS")
    print("=" * 78)

    print()
    print(
        f"Universe 5-day return: "
        f"{pct(market_event_return)}"
    )

    print()
    print("DAILY MARKET BEHAVIOUR")
    print("-" * 78)

    for _, r in daily_summary.iterrows():

        print(
            f"{r['trading_date']} | "
            f"market {pct(r['equal_weight_return'])} | "
            f"up {r['advancers_pct'] * 100:.0f}% | "
            f"BEST {r['best_symbol']} "
            f"{pct(r['best_return'])} | "
            f"WORST {r['worst_symbol']} "
            f"{pct(r['worst_return'])}"
        )

    print()
    print("TOP POSITIVE EVENT IMPACT")
    print("-" * 78)

    for _, r in impact.head(10).iterrows():

        print(
            f"{r['symbol']:6} "
            f"{r['theme']:18} "
            f"5d={pct(r['event_return_5d']):>8} "
            f"relative={pct(r['relative_return_5d']):>8} "
            f"{r['impact']}"
        )

    print()
    print("TOP NEGATIVE EVENT IMPACT")
    print("-" * 78)

    for _, r in (
        impact.tail(10)
        .sort_values("relative_return_5d")
        .iterrows()
    ):

        print(
            f"{r['symbol']:6} "
            f"{r['theme']:18} "
            f"5d={pct(r['event_return_5d']):>8} "
            f"relative={pct(r['relative_return_5d']):>8} "
            f"{r['impact']}"
        )

    print()
    print("BEST THEMES")
    print("-" * 78)

    for _, r in theme_summary.head(8).iterrows():

        print(
            f"{r['theme']:20} "
            f"{pct(r['average_return_5d'])} "
            f"| positive stocks "
            f"{r['positive_stocks_pct'] * 100:.0f}%"
        )

    print()

    if not strategies.empty:

        print("STRATEGIES")
        print("-" * 78)

        for _, r in strategies.iterrows():

            print(
                f"{r['strategy']:20} "
                f"trades={int(r['trades']):4} | "
                f"win={r['win_rate'] * 100:5.1f}% | "
                f"avg={pct(r['average_return_per_trade']):>8} | "
                f"PF={num(r['profit_factor'])}"
            )

        print()

        best = strategies.iloc[0]

        print(
            f"BEST STRATEGY: {best['strategy']}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    df = load_data()

    print()
    print("Building daily stock returns...")

    daily = build_daily_stock_data(df)

    print("Building daily market summary...")

    daily_summary = build_daily_summary(daily)

    print("Calculating event impact...")

    impact, market_event_return = build_stock_impact(
        daily
    )

    print("Building sector/theme analysis...")

    theme_summary = build_theme_summary(daily)

    print("Backtesting strategies...")

    trades = build_strategy_trades(df)

    strategies = summarize_strategies(trades)

    # --------------------------------------------------------
    # SAVE CSV
    # --------------------------------------------------------

    daily.to_csv(
        DAILY_STOCK_FILE,
        index=False,
    )

    daily_summary.to_csv(
        DAILY_SUMMARY_FILE,
        index=False,
    )

    impact.to_csv(
        STOCK_IMPACT_FILE,
        index=False,
    )

    theme_summary.to_csv(
        THEME_SUMMARY_FILE,
        index=False,
    )

    trades.to_csv(
        STRATEGY_TRADES_FILE,
        index=False,
    )

    strategies.to_csv(
        STRATEGY_FILE,
        index=False,
    )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    generate_report(
        daily=daily,
        daily_summary=daily_summary,
        impact=impact,
        market_event_return=market_event_return,
        theme_summary=theme_summary,
        strategies=strategies,
        trades=trades,
    )

    print_console_summary(
        daily_summary=daily_summary,
        impact=impact,
        theme_summary=theme_summary,
        strategies=strategies,
        market_event_return=market_event_return,
    )

    print()
    print("=" * 78)
    print("FILES CREATED")
    print("=" * 78)

    print(f"Report:          {REPORT_FILE}")
    print(f"Stock impact:    {STOCK_IMPACT_FILE}")
    print(f"Daily returns:   {DAILY_STOCK_FILE}")
    print(f"Daily summary:   {DAILY_SUMMARY_FILE}")
    print(f"Themes:          {THEME_SUMMARY_FILE}")
    print(f"Strategies:      {STRATEGY_FILE}")
    print(f"Strategy trades: {STRATEGY_TRADES_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())