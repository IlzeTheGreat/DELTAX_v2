# File: helpers/test_long_candidate_scanner.py
# Purpose: Scans the full stock universe for Core, Active, and Intraday long candidates using the configured VWAP, market-regime, and ATR rules.

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

MARKET_TIMEZONE = ZoneInfo("America/New_York")
BATCH_SIZE = 25


def load_database_state() -> tuple[dict, list[str]]:
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT config
                FROM strategy_configs
                WHERE version = 'deltax_v2_strategy_v1'
                  AND is_active = true;
                """
            )
            row = cursor.fetchone()

            if row is None:
                raise RuntimeError("Active DELTAX strategy config not found")

            config = row[0]

            cursor.execute(
                """
                SELECT i.alpaca_symbol
                FROM universe_memberships um
                JOIN universes u
                    ON u.id = um.universe_id
                JOIN instruments i
                    ON i.symbol = um.symbol
                WHERE u.code = 'alyrise_base'
                  AND u.is_active = true
                  AND um.is_enabled = true
                  AND i.stock_enabled = true
                  AND (
                      um.eligible_until IS NULL
                      OR um.eligible_until > now()
                  )
                ORDER BY i.symbol;
                """
            )

            symbols = [row[0] for row in cursor.fetchall()]

    return config, symbols


def split_batches(items: list[str]) -> list[list[str]]:
    return [
        items[index:index + BATCH_SIZE]
        for index in range(0, len(items), BATCH_SIZE)
    ]


def load_bars(
    client: StockHistoricalDataClient,
    symbols: list[str],
    timeframe: TimeFrame,
    start: datetime,
) -> pd.DataFrame:
    frames = []

    for batch_number, batch in enumerate(
        split_batches(symbols),
        start=1,
    ):
        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=timeframe,
            start=start,
            feed=DataFeed.IEX,
        )

        frame = client.get_stock_bars(request).df

        if not frame.empty:
            frames.append(frame)

        print(
            f"Batch {batch_number}: "
            f"{len(batch)} symbols, {len(frame)} bars"
        )

    if not frames:
        raise RuntimeError("No bars returned from Alpaca")

    return pd.concat(frames).sort_index()


def prepare_daily_bars(frame: pd.DataFrame) -> pd.DataFrame:
    daily = frame.reset_index().copy()
    daily["timestamp"] = pd.to_datetime(
        daily["timestamp"],
        utc=True,
    )

    local_timestamp = daily["timestamp"].dt.tz_convert(
        MARKET_TIMEZONE
    )
    daily["session_date"] = local_timestamp.dt.date

    if "vwap" not in daily.columns:
        daily["vwap"] = (
            daily["high"]
            + daily["low"]
            + daily["close"]
        ) / 3

    daily["vwap"] = daily["vwap"].fillna(
        (
            daily["high"]
            + daily["low"]
            + daily["close"]
        ) / 3
    )

    return daily


def prepare_intraday_bars(frame: pd.DataFrame) -> pd.DataFrame:
    intraday = frame.reset_index().copy()
    intraday["timestamp"] = pd.to_datetime(
        intraday["timestamp"],
        utc=True,
    )

    local_timestamp = intraday["timestamp"].dt.tz_convert(
        MARKET_TIMEZONE
    )

    intraday["session_date"] = local_timestamp.dt.date
    intraday["market_minutes"] = (
        local_timestamp.dt.hour * 60
        + local_timestamp.dt.minute
    )

    # Only regular US market hours: 09:30–16:00 New York time.
    intraday = intraday[
        (intraday["market_minutes"] >= 570)
        & (intraday["market_minutes"] < 960)
    ].copy()

    if "vwap" not in intraday.columns:
        intraday["vwap"] = (
            intraday["high"]
            + intraday["low"]
            + intraday["close"]
        ) / 3

    intraday["vwap"] = intraday["vwap"].fillna(
        (
            intraday["high"]
            + intraday["low"]
            + intraday["close"]
        ) / 3
    )

    return intraday


def calculate_atr(
    symbol_daily: pd.DataFrame,
    period: int,
) -> float | None:
    ordered = symbol_daily.sort_values("session_date").copy()

    if len(ordered) < period + 1:
        return None

    previous_close = ordered["close"].shift(1)

    true_range = pd.concat(
        [
            ordered["high"] - ordered["low"],
            (ordered["high"] - previous_close).abs(),
            (ordered["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = true_range.tail(period).mean()

    if pd.isna(atr) or atr <= 0:
        return None

    return float(atr)


def calculate_session_metrics(
    intraday: pd.DataFrame,
    symbol: str,
    session_date,
) -> dict | None:
    session = intraday[
        (intraday["symbol"] == symbol)
        & (intraday["session_date"] == session_date)
    ].sort_values("timestamp")

    if session.empty:
        return None

    total_volume = float(session["volume"].sum())

    if total_volume <= 0:
        return None

    session_vwap = float(
        (session["vwap"] * session["volume"]).sum()
        / total_volume
    )

    latest = session.iloc[-1]

    return {
        "latest_price": float(latest["close"]),
        "intraday_vwap": session_vwap,
        "latest_timestamp": latest["timestamp"],
    }


def calculate_market_regime(
    intraday: pd.DataFrame,
    proxies: list[str],
    session_date,
) -> tuple[int | None, list[dict]]:
    results = []

    for symbol in proxies:
        metrics = calculate_session_metrics(
            intraday,
            symbol,
            session_date,
        )

        if metrics is None:
            return None, results

        is_weak = (
            metrics["latest_price"]
            < metrics["intraday_vwap"]
        )

        results.append(
            {
                "symbol": symbol,
                "price": metrics["latest_price"],
                "vwap": metrics["intraday_vwap"],
                "weak": is_weak,
            }
        )

    weak_count = sum(item["weak"] for item in results)

    return weak_count, results


def calculate_stock_metrics(
    symbol: str,
    daily: pd.DataFrame,
    intraday: pd.DataFrame,
    session_date,
    config: dict,
) -> dict | None:
    symbol_daily = daily[
        daily["symbol"] == symbol
    ].sort_values("session_date")

    session_metrics = calculate_session_metrics(
        intraday,
        symbol,
        session_date,
    )

    if symbol_daily.empty or session_metrics is None:
        return None

    previous_sessions = symbol_daily[
        symbol_daily["session_date"] < session_date
    ]

    if previous_sessions.empty:
        return None

    previous_close = float(
        previous_sessions.iloc[-1]["close"]
    )

    core_days = config["stock_strategies"]["core"][
        "reference_lookback_days"
    ]
    active_days = config["stock_strategies"]["active"][
        "reference_lookback_days"
    ]

    if len(symbol_daily) < active_days:
        return None

    core_vwap = float(
        symbol_daily.tail(core_days)["vwap"].mean()
    )
    active_vwap = float(
        symbol_daily.tail(active_days)["vwap"].mean()
    )

    atr_period = config["stock_position_management"][
        "atr_period"
    ]
    atr = calculate_atr(symbol_daily, atr_period)

    if atr is None:
        return None

    return {
        "symbol": symbol,
        "latest_price": session_metrics["latest_price"],
        "previous_close": previous_close,
        "intraday_vwap": session_metrics["intraday_vwap"],
        "core_vwap": core_vwap,
        "active_vwap": active_vwap,
        "atr": atr,
        "atr_pct": atr / session_metrics["latest_price"],
    }


def build_candidate(
    metrics: dict,
    strategy: str,
    reference_price: float,
    threshold: float,
    config: dict,
) -> dict:
    price = metrics["latest_price"]
    drop_pct = (price / reference_price) - 1

    stop_multiple = config["stock_position_management"][
        "stop_loss_atr_multiple"
    ]
    target_multiple = config["stock_position_management"][
        "take_profit_atr_multiple"
    ]
    max_loss = config["portfolio_risk"][
        "max_stock_loss_per_trade_usd"
    ]
    capital_limit = config["capital"]["stocks_max_usd"]

    stop_distance = metrics["atr"] * stop_multiple
    stop_price = price - stop_distance
    target_price = price + metrics["atr"] * target_multiple

    shares_by_risk = int(max_loss / stop_distance)
    shares_by_capital = int(capital_limit / price)
    shares = max(0, min(shares_by_risk, shares_by_capital))

    return {
        "symbol": metrics["symbol"],
        "strategy": strategy,
        "price": price,
        "reference": reference_price,
        "drop_pct": drop_pct,
        "threshold": threshold,
        "atr": metrics["atr"],
        "stop_price": stop_price,
        "target_price": target_price,
        "shares": shares,
    }


def scan_long_candidates(
    metrics_by_symbol: list[dict],
    weak_count: int | None,
    config: dict,
) -> tuple[dict, dict]:
    market_key = (
        "missing"
        if weak_count is None
        else str(weak_count)
    )

    candidates = {}
    nearest = {}

    for strategy in ["core", "active", "intraday"]:
        strategy_config = config["stock_strategies"][strategy]

        threshold = config["market_regime"][
            "long_entry_drop_pct_by_weak_proxy_count"
        ][strategy][market_key]

        strategy_rows = []

        for metrics in metrics_by_symbol:
            reference_key = {
                "core": "core_vwap",
                "active": "active_vwap",
                "intraday": "intraday_vwap",
            }[strategy]

            reference_price = metrics[reference_key]

            candidate = build_candidate(
                metrics=metrics,
                strategy=strategy,
                reference_price=reference_price,
                threshold=threshold,
                config=config,
            )

            if (
                strategy == "intraday"
                and metrics["latest_price"]
                >= metrics["previous_close"]
            ):
                candidate["previous_close_pass"] = False
            else:
                candidate["previous_close_pass"] = True

            strategy_rows.append(candidate)

        strategy_rows.sort(key=lambda row: row["drop_pct"])

        eligible = [
            row
            for row in strategy_rows
            if row["drop_pct"] <= -row["threshold"]
            and row["previous_close_pass"]
            and row["shares"] > 0
        ]

        max_candidates = strategy_config[
            "max_candidates_per_scan"
        ]

        candidates[strategy] = eligible[:max_candidates]
        nearest[strategy] = strategy_rows[:5]

    return candidates, nearest


def print_rows(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")

    if not rows:
        print("No candidates")
        return

    for row in rows:
        print(
            f"{row['symbol']}: "
            f"price={row['price']:.2f}, "
            f"reference={row['reference']:.2f}, "
            f"move={row['drop_pct']:.2%}, "
            f"required=-{row['threshold']:.2%}, "
            f"ATR={row['atr']:.2f}, "
            f"shares={row['shares']}, "
            f"stop={row['stop_price']:.2f}, "
            f"target={row['target_price']:.2f}"
        )


if __name__ == "__main__":
    config, stock_symbols = load_database_state()

    if len(stock_symbols) != 119:
        raise RuntimeError(
            f"Expected 119 stocks, received {len(stock_symbols)}"
        )

    proxies = config["market_regime"]["proxy_symbols"]
    all_symbols = list(dict.fromkeys(stock_symbols + proxies))

    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY_PAPER"],
        os.environ["ALPACA_API_SECRET_PAPER"],
    )

    print("Loading daily bars...")

    daily_raw = load_bars(
        client=client,
        symbols=all_symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=45),
    )

    print("\nLoading 5-minute bars...")

    intraday_raw = load_bars(
        client=client,
        symbols=all_symbols,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=7),
    )

    daily = prepare_daily_bars(daily_raw)
    intraday = prepare_intraday_bars(intraday_raw)

    session_date = intraday["session_date"].max()

    print(f"\nEvaluation session: {session_date}")

    weak_count, proxy_results = calculate_market_regime(
        intraday=intraday,
        proxies=proxies,
        session_date=session_date,
    )

    print("\nMARKET REGIME")

    for proxy in proxy_results:
        print(
            f"{proxy['symbol']}: "
            f"price={proxy['price']:.2f}, "
            f"VWAP={proxy['vwap']:.2f}, "
            f"weak={proxy['weak']}"
        )

    print(f"Weak proxies: {weak_count}")

    metrics_by_symbol = []

    for symbol in stock_symbols:
        metrics = calculate_stock_metrics(
            symbol=symbol,
            daily=daily,
            intraday=intraday,
            session_date=session_date,
            config=config,
        )

        if metrics is not None:
            metrics_by_symbol.append(metrics)

    print(
        f"Stocks with complete indicators: "
        f"{len(metrics_by_symbol)}"
    )

    candidates, nearest = scan_long_candidates(
        metrics_by_symbol=metrics_by_symbol,
        weak_count=weak_count,
        config=config,
    )

    for strategy in ["core", "active", "intraday"]:
        print_rows(
            f"{strategy.upper()} LONG CANDIDATES",
            candidates[strategy],
        )

        if not candidates[strategy]:
            print_rows(
                f"{strategy.upper()} CLOSEST TO TRIGGER",
                nearest[strategy],
            )

    print("\nLONG CANDIDATE SCANNER TEST: OK")