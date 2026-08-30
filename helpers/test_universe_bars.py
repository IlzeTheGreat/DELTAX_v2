# File: helpers/test_universe_bars.py
# Purpose: Loads the full stock universe from Neon and verifies seven days of 5-minute IEX bars required for market-state and signal calculations.

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

MARKET_PROXIES = ["SPY", "QQQ", "IWM"]
BATCH_SIZE = 25


def load_symbols() -> list[str]:
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
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

            return [row[0] for row in cursor.fetchall()]


def split_batches(items: list[str], size: int) -> list[list[str]]:
    return [
        items[index:index + size]
        for index in range(0, len(items), size)
    ]


def load_bars(symbols: list[str]):
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY_PAPER"],
        os.environ["ALPACA_API_SECRET_PAPER"],
    )

    start = datetime.now(timezone.utc) - timedelta(days=7)
    frames = []

    for batch_number, batch in enumerate(
        split_batches(symbols, BATCH_SIZE),
        start=1,
    ):
        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            feed=DataFeed.IEX,
        )

        bars = client.get_stock_bars(request)
        frame = bars.df

        if not frame.empty:
            frames.append(frame)

        print(
            f"Batch {batch_number}: "
            f"requested {len(batch)}, received {len(frame)} bars"
        )

    if not frames:
        raise RuntimeError("Alpaca returned no historical bars")

    import pandas as pd

    return pd.concat(frames).sort_index()


if __name__ == "__main__":
    stock_symbols = load_symbols()

    if len(stock_symbols) != 119:
        raise RuntimeError(
            f"Expected 119 stocks, received {len(stock_symbols)}"
        )

    requested_symbols = stock_symbols + MARKET_PROXIES
    requested_symbols = list(dict.fromkeys(requested_symbols))

    print(f"Stocks: {len(stock_symbols)}")
    print(f"Market proxies: {', '.join(MARKET_PROXIES)}")
    print(f"Total requested symbols: {len(requested_symbols)}")

    bars = load_bars(requested_symbols)

    available_symbols = set(
        bars.index.get_level_values("symbol").unique()
    )
    missing_symbols = sorted(
        set(requested_symbols) - available_symbols
    )

    counts = (
        bars.reset_index()
        .groupby("symbol")
        .size()
        .sort_values()
    )

    print(f"\nTotal bars: {len(bars)}")
    print(f"Symbols with bars: {len(available_symbols)}")
    print(f"Missing symbols: {len(missing_symbols)}")
    print(f"Minimum bars per symbol: {counts.min()}")
    print(f"Maximum bars per symbol: {counts.max()}")

    if missing_symbols:
        print("Missing:")
        print(", ".join(missing_symbols))

    print("\nLATEST MARKET PROXIES")

    for symbol in MARKET_PROXIES:
        symbol_frame = bars.xs(symbol, level="symbol")
        latest = symbol_frame.iloc[-1]

        print(
            f"{symbol}: "
            f"close={latest['close']}, "
            f"volume={latest['volume']}, "
            f"time={symbol_frame.index[-1]}"
        )

    print("\nUNIVERSE BAR TEST: OK")