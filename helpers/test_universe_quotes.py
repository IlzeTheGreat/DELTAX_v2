# File: helpers/test_universe_quotes.py
# Purpose: Loads the 119-symbol Alyrise universe from Neon and retrieves current IEX quotes from Alpaca in batches.

import os
from pathlib import Path

import psycopg
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

BATCH_SIZE = 50


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
                  AND (
                      um.eligible_until IS NULL
                      OR um.eligible_until > now()
                  )
                  AND i.stock_enabled = true
                ORDER BY um.rank NULLS LAST, i.symbol;
                """
            )

            return [row[0] for row in cursor.fetchall()]


def split_batches(items: list[str], size: int) -> list[list[str]]:
    return [
        items[index:index + size]
        for index in range(0, len(items), size)
    ]


def load_quotes(symbols: list[str]) -> dict:
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY_PAPER"],
        os.environ["ALPACA_API_SECRET_PAPER"],
    )

    quotes = {}

    for batch_number, batch in enumerate(
        split_batches(symbols, BATCH_SIZE),
        start=1,
    ):
        request = StockLatestQuoteRequest(
            symbol_or_symbols=batch,
            feed=DataFeed.IEX,
        )

        batch_quotes = client.get_stock_latest_quote(request)
        quotes.update(batch_quotes)

        print(
            f"Batch {batch_number}: "
            f"requested {len(batch)}, received {len(batch_quotes)}"
        )

    return quotes


def print_sample(quotes: dict, limit: int = 10) -> None:
    print("\nQUOTE SAMPLE")

    for symbol in sorted(quotes)[:limit]:
        quote = quotes[symbol]

        print(
            f"{symbol}: "
            f"bid={quote.bid_price}, "
            f"ask={quote.ask_price}, "
            f"time={quote.timestamp}"
        )


if __name__ == "__main__":
    symbols = load_symbols()

    print(f"Neon universe symbols: {len(symbols)}")

    if len(symbols) != 119:
        raise RuntimeError(
            f"Expected 119 enabled symbols, received {len(symbols)}"
        )

    quotes = load_quotes(symbols)

    missing_symbols = sorted(set(symbols) - set(quotes))

    print(f"\nRequested symbols: {len(symbols)}")
    print(f"Received quotes: {len(quotes)}")
    print(f"Missing quotes: {len(missing_symbols)}")

    if missing_symbols:
        print("Missing symbols:")
        print(", ".join(missing_symbols))

    print_sample(quotes)

    print("\nUNIVERSE QUOTE TEST: OK")