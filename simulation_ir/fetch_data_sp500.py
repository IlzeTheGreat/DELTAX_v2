from __future__ import annotations

import csv
import os
import random
import sys
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import urllib3
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


# ============================================================
# CONFIG
# ============================================================

EVENT_DATE = date(2026, 2, 28)

TRADING_DAYS_TO_FETCH = 5

# False = only regular US market session 09:30-16:00 ET
# True  = also keep pre-market / after-hours bars returned by Alpaca
INCLUDE_EXTENDED_HOURS = False

BATCH_SIZE = 30

MAX_ATTEMPTS = 6
RETRY_BASE_SLEEP = 0.8
RETRY_MAX_SLEEP = 12.0

MARKET_TZ = ZoneInfo("America/New_York")


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

ENV_PATH = ROOT_DIR / ".env"
STOCKS_PATH = ROOT_DIR / "sp500.txt"

OUTPUT_PATH = (
    SCRIPT_DIR
    / f"market_5min_sp500_{EVENT_DATE.isoformat()}_{TRADING_DAYS_TO_FETCH}d.csv"
)


# ============================================================
# CONSOLE
# ============================================================

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# ENV
# ============================================================

def load_env() -> None:
    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f".env not found: {ENV_PATH}"
        )

    load_dotenv(ENV_PATH)

    print(f"Loaded .env: {ENV_PATH}")


def get_required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()

    if not value:
        raise RuntimeError(f"Missing required env variable: {name}")

    return value


# ============================================================
# SYMBOLS
# ============================================================

def load_symbols() -> list[str]:
    if not STOCKS_PATH.exists():
        raise FileNotFoundError(
            f"sp500.txt not found: {STOCKS_PATH}"
        )

    symbols: list[str] = []

    with open(STOCKS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            symbol = line.strip().upper()

            if not symbol:
                continue

            if symbol.startswith("#"):
                continue

            symbols.append(symbol)

    # Remove duplicates but preserve order
    symbols = list(dict.fromkeys(symbols))

    if not symbols:
        raise RuntimeError("sp500.txt contains no symbols")

    return symbols


# ============================================================
# HELPERS
# ============================================================

def chunked(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_market_days(
    trading_client: TradingClient,
) -> list:
    """
    Find first N US trading sessions starting from EVENT_DATE.
    EVENT_DATE itself is included if it is a trading day.
    """

    # Large enough safety window to cover weekends / holidays.
    search_end = EVENT_DATE + timedelta(days=20)

    calendar = trading_client.get_calendar(
        GetCalendarRequest(
            start=EVENT_DATE,
            end=search_end,
        )
    )

    sessions = [
        session
        for session in calendar
        if session.date >= EVENT_DATE
    ]

    if len(sessions) < TRADING_DAYS_TO_FETCH:
        raise RuntimeError(
            f"Only found {len(sessions)} trading days after "
            f"{EVENT_DATE}; expected {TRADING_DAYS_TO_FETCH}"
        )

    return sessions[:TRADING_DAYS_TO_FETCH]


def request_bars_with_retry(
    data_client: StockHistoricalDataClient,
    request: StockBarsRequest,
    batch_label: str,
):
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return data_client.get_stock_bars(request)

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
                f"Retry {batch_label}: "
                f"{attempt}/{MAX_ATTEMPTS} | "
                f"{repr(exc)} | "
                f"sleep {sleep_seconds:.2f}s"
            )

            time.sleep(sleep_seconds)

    raise last_error


def is_regular_market_bar(timestamp: datetime) -> bool:
    """
    Keep bars between 09:30 and 16:00 America/New_York.
    Alpaca bar timestamp represents beginning of 5-minute bar.
    """

    local_ts = timestamp.astimezone(MARKET_TZ)
    local_time = local_ts.time()

    return (
        dt_time(9, 30)
        <= local_time
        < dt_time(16, 0)
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    print("=" * 70)
    print("IRAN EVENT S&P 500 MARKET DATA FETCH")
    print("=" * 70)

    load_env()

    api_key = get_required_env("ALPACA_API_KEY_PAPER")
    api_secret = get_required_env("ALPACA_API_SECRET_PAPER")
    trading_url = get_required_env("ALPACA_TRADING_URL_PAPER")
    trading_url = trading_url.rstrip("/")

    if trading_url.endswith("/v2"):
        trading_url = trading_url[:-3]

    data_feed = get_required_env("ALPACA_DATA_FEED_PAPER")

    symbols = load_symbols()

    print(f"Symbols: {len(symbols)}")
    print(f"Event date: {EVENT_DATE}")
    print(f"Data feed: {data_feed}")
    print(f"Extended hours: {INCLUDE_EXTENDED_HOURS}")
    print()

    # --------------------------------------------------------
    # Clients
    # --------------------------------------------------------

    trading_client = TradingClient(
        api_key=api_key,
        secret_key=api_secret,
        paper=False,
        url_override=trading_url,
    )

    data_client = StockHistoricalDataClient(
        api_key,
        api_secret,
    )

    # --------------------------------------------------------
    # Find exact 5 trading days
    # --------------------------------------------------------

    sessions = get_market_days(trading_client)

    session_dates = [session.date for session in sessions]

    print("Trading sessions:")

    for session in sessions:
        print(
            f"  {session.date} | "
            f"open={session.open} | "
            f"close={session.close}"
        )

    print()

    first_day = sessions[0].date
    last_day = sessions[-1].date

    # Request a wide UTC window.
    # Filtering to exact trading sessions happens afterwards.
    start_et = datetime.combine(
        first_day,
        dt_time(0, 0),
        tzinfo=MARKET_TZ,
    )

    end_et = datetime.combine(
        last_day + timedelta(days=1),
        dt_time(0, 0),
        tzinfo=MARKET_TZ,
    )

    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)

    print(f"Request start UTC: {start_utc.isoformat()}")
    print(f"Request end UTC:   {end_utc.isoformat()}")
    print()

    # --------------------------------------------------------
    # Output CSV
    # --------------------------------------------------------

    fieldnames = [
        "timestamp_utc",
        "timestamp_et",
        "trading_date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    ]

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    total_rows = 0
    symbols_with_data: set[str] = set()
    symbols_without_data: set[str] = set()

    batches = list(chunked(symbols, BATCH_SIZE))

    timeframe = TimeFrame(
        5,
        TimeFrameUnit.Minute,
    )

    with open(
        OUTPUT_PATH,
        "w",
        newline="",
        encoding="utf-8",
    ) as csv_file:

        writer = csv.DictWriter(
            csv_file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        # ----------------------------------------------------
        # Pull symbols in batches
        # ----------------------------------------------------

        for batch_number, batch in enumerate(
            batches,
            start=1,
        ):

            print(
                f"[Batch {batch_number}/{len(batches)}] "
                f"Fetching {len(batch)} symbols..."
            )

            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=timeframe,
                start=start_utc,
                end=end_utc,
                feed=data_feed,
                adjustment="raw",
            )

            try:
                bars_response = request_bars_with_retry(
                    data_client,
                    request,
                    batch_label=f"batch {batch_number}",
                )

            except Exception as exc:
                print(
                    f"ERROR batch {batch_number}: "
                    f"{repr(exc)}"
                )

                symbols_without_data.update(batch)
                continue

            bars_data = (
                bars_response.data
                if hasattr(bars_response, "data")
                else {}
            )

            batch_rows = 0

            for symbol in batch:

                bars = bars_data.get(symbol, [])

                if not bars:
                    print(f"  {symbol}: no data")
                    symbols_without_data.add(symbol)
                    continue

                symbol_rows = 0

                for bar in bars:
                    timestamp = bar.timestamp

                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(
                            tzinfo=timezone.utc
                        )

                    timestamp_utc = timestamp.astimezone(
                        timezone.utc
                    )

                    timestamp_et = timestamp.astimezone(
                        MARKET_TZ
                    )

                    trading_date = timestamp_et.date()

                    # Only keep the five selected trading dates.
                    if trading_date not in session_dates:
                        continue

                    # By default keep only 09:30-16:00 ET.
                    if (
                        not INCLUDE_EXTENDED_HOURS
                        and not is_regular_market_bar(timestamp)
                    ):
                        continue

                    writer.writerow(
                        {
                            "timestamp_utc":
                                timestamp_utc.isoformat(),
                            "timestamp_et":
                                timestamp_et.isoformat(),
                            "trading_date":
                                trading_date.isoformat(),
                            "symbol":
                                symbol,
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
                            "trade_count":
                                (
                                    int(bar.trade_count)
                                    if getattr(
                                        bar,
                                        "trade_count",
                                        None,
                                    ) is not None
                                    else ""
                                ),
                            "vwap":
                                (
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

                    symbol_rows += 1
                    batch_rows += 1
                    total_rows += 1

                if symbol_rows > 0:
                    symbols_with_data.add(symbol)
                    symbols_without_data.discard(symbol)

                    print(
                        f"  {symbol}: "
                        f"{symbol_rows} bars"
                    )

                else:
                    symbols_without_data.add(symbol)

            csv_file.flush()

            print(
                f"Batch rows written: {batch_rows}"
            )
            print()

            time.sleep(0.3)

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print("=" * 70)
    print("DONE")
    print("=" * 70)

    print(
        "Trading dates: "
        + ", ".join(
            d.isoformat()
            for d in session_dates
        )
    )

    print(f"Symbols requested: {len(symbols)}")
    print(f"Symbols with data: {len(symbols_with_data)}")
    print(f"Total CSV rows: {total_rows}")
    print(f"Output: {OUTPUT_PATH}")

    if symbols_without_data:
        print()
        print(
            f"Symbols without data "
            f"({len(symbols_without_data)}):"
        )

        print(
            ", ".join(
                sorted(symbols_without_data)
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())