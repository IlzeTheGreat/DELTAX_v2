# File: helpers/test_news_price_confirmation.py
# Purpose: Anchors every stored news event to its first tradable price and measures the following 10-minute price move using Alpaca 1-minute bars.

import argparse
import os
from datetime import (
    date,
    datetime,
    time,
    timedelta,
    timezone,
)
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
import requests
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

MARKET_TIMEZONE = ZoneInfo("America/New_York")
DEFAULT_SYMBOLS = ["IREN", "PCG", "RKLB", "CW"]

CONFIRMATION_MINUTES = 10
ENTRY_CUTOFF_MINUTES = 30
BAR_TOLERANCE_MINUTES = 3


def load_news_events(
    symbols: list[str],
) -> list[dict]:
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    se.id,
                    ses.symbol,
                    se.external_id,
                    se.headline,
                    se.published_at
                FROM source_events se
                JOIN source_event_symbols ses
                    ON ses.source_event_id = se.id
                WHERE se.source = 'alpaca_news'
                  AND ses.symbol = ANY(%s)
                ORDER BY se.published_at;
                """,
                (symbols,),
            )

            return [
                {
                    "source_event_id": str(row[0]),
                    "symbol": row[1],
                    "external_id": row[2],
                    "headline": row[3],
                    "published_at": row[4],
                }
                for row in cursor.fetchall()
            ]


def fetch_market_calendar(
    start_date: date,
    end_date: date,
) -> list[dict]:
    trading_url = os.environ[
        "ALPACA_TRADING_URL_PAPER"
    ].strip().rstrip("/")

    response = requests.get(
        f"{trading_url}/calendar",
        headers={
            "APCA-API-KEY-ID": os.environ[
                "ALPACA_API_KEY_PAPER"
            ],
            "APCA-API-SECRET-KEY": os.environ[
                "ALPACA_API_SECRET_PAPER"
            ],
        },
        params={
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        timeout=30,
    )

    response.raise_for_status()
    sessions = []

    for item in response.json():
        session_date = date.fromisoformat(item["date"])
        open_time = time.fromisoformat(item["open"])
        close_time = time.fromisoformat(item["close"])

        sessions.append(
            {
                "date": session_date,
                "open": datetime.combine(
                    session_date,
                    open_time,
                    tzinfo=MARKET_TIMEZONE,
                ),
                "close": datetime.combine(
                    session_date,
                    close_time,
                    tzinfo=MARKET_TIMEZONE,
                ),
            }
        )

    return sessions


def ceil_to_next_minute(
    value: datetime,
) -> datetime:
    rounded = value.replace(
        second=0,
        microsecond=0,
    )

    if value.second or value.microsecond:
        rounded += timedelta(minutes=1)

    return rounded


def resolve_price_anchor(
    published_at: datetime,
    sessions: list[dict],
) -> dict:
    published_local = published_at.astimezone(
        MARKET_TIMEZONE
    )

    for session in sessions:
        market_open = session["open"]
        market_close = session["close"]
        entry_cutoff = market_close - timedelta(
            minutes=ENTRY_CUTOFF_MINUTES
        )

        if published_local < market_open:
            return {
                "anchor_at": market_open,
                "anchor_type": "next_market_open",
                "session_close": market_close,
            }

        if market_open <= published_local < market_close:
            candidate_anchor = ceil_to_next_minute(
                published_local
            )
            confirmation_due = (
                candidate_anchor
                + timedelta(
                    minutes=CONFIRMATION_MINUTES
                )
            )

            # Entry is allowed only if confirmation finishes
            # before the 30-minute new-entry cutoff.
            if confirmation_due <= entry_cutoff:
                return {
                    "anchor_at": candidate_anchor,
                    "anchor_type": "news_during_market",
                    "session_close": market_close,
                }

            continue

    raise RuntimeError(
        f"No future market session for {published_at}"
    )


def load_minute_bars(
    symbols: list[str],
    start: datetime,
) -> pd.DataFrame:
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY_PAPER"],
        os.environ["ALPACA_API_SECRET_PAPER"],
    )

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=start.astimezone(timezone.utc),
        feed=DataFeed.IEX,
    )

    frame = client.get_stock_bars(request).df.reset_index()

    if frame.empty:
        return frame

    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"],
        utc=True,
    )

    return frame.sort_values(
        ["symbol", "timestamp"]
    )


def find_price_at(
    symbol_bars: pd.DataFrame,
    target_at: datetime,
) -> tuple[float, datetime] | None:
    target_utc = pd.Timestamp(
        target_at.astimezone(timezone.utc)
    )

    candidates = symbol_bars[
        symbol_bars["timestamp"] >= target_utc
    ]

    if candidates.empty:
        return None

    row = candidates.iloc[0]
    actual_at = row["timestamp"].to_pydatetime()

    if actual_at > (
        target_utc.to_pydatetime()
        + timedelta(minutes=BAR_TOLERANCE_MINUTES)
    ):
        return None

    return float(row["open"]), actual_at


def evaluate_event(
    event: dict,
    anchor: dict,
    bars: pd.DataFrame,
    now: datetime,
) -> dict:
    anchor_at = anchor["anchor_at"]
    confirmation_due_at = (
        anchor_at
        + timedelta(minutes=CONFIRMATION_MINUTES)
    )

    result = {
        **event,
        **anchor,
        "confirmation_due_at": confirmation_due_at,
        "status": "pending",
        "signal_price": None,
        "confirmation_price": None,
        "price_change_pct": None,
        "bullish_pass": None,
        "bearish_pass": None,
    }

    if confirmation_due_at > now:
        return result

    symbol_bars = bars[
        bars["symbol"] == event["symbol"]
    ]

    signal_result = find_price_at(
        symbol_bars,
        anchor_at,
    )
    confirmation_result = find_price_at(
        symbol_bars,
        confirmation_due_at,
    )

    if (
        signal_result is None
        or confirmation_result is None
    ):
        result["status"] = "missing_bars"
        return result

    signal_price, actual_signal_at = signal_result
    confirmation_price, actual_confirmation_at = (
        confirmation_result
    )

    price_change_pct = (
        confirmation_price / signal_price
    ) - 1

    result.update(
        {
            "status": "completed",
            "actual_signal_at": actual_signal_at,
            "actual_confirmation_at": (
                actual_confirmation_at
            ),
            "signal_price": signal_price,
            "confirmation_price": confirmation_price,
            "price_change_pct": price_change_pct,
            "bullish_pass": price_change_pct > 0,
            "bearish_pass": price_change_pct < 0,
        }
    )

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "symbols",
        nargs="*",
        default=DEFAULT_SYMBOLS,
    )

    args = parser.parse_args()

    symbols = sorted(
        {
            symbol.upper()
            for symbol in args.symbols
        }
    )

    events = load_news_events(symbols)

    if not events:
        raise RuntimeError(
            "No stored news events found"
        )

    print(f"Stored news events loaded: {len(events)}")

    earliest_publication = min(
        event["published_at"]
        for event in events
    )
    latest_publication = max(
        event["published_at"]
        for event in events
    )

    calendar = fetch_market_calendar(
        start_date=(
            earliest_publication.astimezone(
                MARKET_TIMEZONE
            ).date()
            - timedelta(days=2)
        ),
        end_date=(
            latest_publication.astimezone(
                MARKET_TIMEZONE
            ).date()
            + timedelta(days=10)
        ),
    )

    anchored_events = []

    for event in events:
        anchor = resolve_price_anchor(
            published_at=event["published_at"],
            sessions=calendar,
        )

        anchored_events.append(
            (event, anchor)
        )

    earliest_anchor = min(
        anchor["anchor_at"]
        for _, anchor in anchored_events
    )

    bars = load_minute_bars(
        symbols=symbols,
        start=earliest_anchor - timedelta(minutes=5),
    )

    now = datetime.now(timezone.utc)

    results = [
        evaluate_event(
            event=event,
            anchor=anchor,
            bars=bars,
            now=now,
        )
        for event, anchor in anchored_events
    ]

    completed = [
        result
        for result in results
        if result["status"] == "completed"
    ]
    pending = [
        result
        for result in results
        if result["status"] == "pending"
    ]
    missing = [
        result
        for result in results
        if result["status"] == "missing_bars"
    ]

    print("\nCONFIRMATION SUMMARY")
    print(f"Completed: {len(completed)}")
    print(f"Pending next session: {len(pending)}")
    print(f"Missing bars: {len(missing)}")

    print("\nLATEST NEWS CONFIRMATIONS")

    for result in sorted(
        results,
        key=lambda item: item["published_at"],
        reverse=True,
    )[:20]:
        print(
            f"\n{result['symbol']} | "
            f"published={result['published_at']} | "
            f"anchor={result['anchor_at']} | "
            f"type={result['anchor_type']} | "
            f"status={result['status']}"
        )

        print(result["headline"])

        if result["status"] == "completed":
            print(
                f"price {result['signal_price']:.2f}"
                f" -> "
                f"{result['confirmation_price']:.2f} | "
                f"move={result['price_change_pct']:.3%} | "
                f"bullish_pass={result['bullish_pass']} | "
                f"bearish_pass={result['bearish_pass']}"
            )
        else:
            print(
                f"confirmation_due="
                f"{result['confirmation_due_at']}"
            )

    print("\nNEWS PRICE CONFIRMATION TEST: OK")