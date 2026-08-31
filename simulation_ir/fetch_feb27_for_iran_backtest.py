from __future__ import annotations

import os
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

ENV_PATH = ROOT_DIR / ".env"

INPUT_FILE = SCRIPT_DIR / "market_5min_sp500_2026-02-28_5d.csv"
OUTPUT_FILE = SCRIPT_DIR / "market_5min_sp500_2026-02-27_plus_5d.csv"

MARKET_TZ = ZoneInfo("America/New_York")

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

WATCHLIST = sorted(LONG_SYMBOLS | SHORT_SYMBOLS)


def load_env():
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env not found: {ENV_PATH}")
    load_dotenv(ENV_PATH)


def required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing env variable: {name}")
    return value


def fetch_feb27_rows() -> pd.DataFrame:
    load_env()

    api_key = required("ALPACA_API_KEY_PAPER")
    api_secret = required("ALPACA_API_SECRET_PAPER")
    feed = required("ALPACA_DATA_FEED_PAPER")

    client = StockHistoricalDataClient(
        api_key,
        api_secret,
    )

    start_et = datetime(
        2026, 2, 27, 9, 30,
        tzinfo=MARKET_TZ,
    )
    end_et = datetime(
        2026, 2, 27, 16, 0,
        tzinfo=MARKET_TZ,
    )

    request = StockBarsRequest(
        symbol_or_symbols=WATCHLIST,
        timeframe=TimeFrame(
            5,
            TimeFrameUnit.Minute,
        ),
        start=start_et.astimezone(timezone.utc),
        end=end_et.astimezone(timezone.utc),
        feed=feed,
        adjustment="raw",
    )

    response = client.get_stock_bars(request)
    data = response.data if hasattr(response, "data") else {}

    rows = []

    for symbol in WATCHLIST:
        bars = data.get(symbol, [])

        for bar in bars:
            ts = bar.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            ts_et = ts.astimezone(MARKET_TZ)

            if ts_et.date().isoformat() != "2026-02-27":
                continue

            if not (
                dt_time(9, 30)
                <= ts_et.time()
                < dt_time(16, 0)
            ):
                continue

            rows.append(
                {
                    "timestamp_et": ts_et.isoformat(),
                    "trading_date": ts_et.date().isoformat(),
                    "symbol": symbol,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume or 0),
                }
            )

    out = pd.DataFrame(rows)

    if out.empty:
        raise RuntimeError(
            "No Feb 27 rows returned from Alpaca."
        )

    return out


def main() -> int:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Existing S&P file not found:\n{INPUT_FILE}"
        )

    existing = pd.read_csv(INPUT_FILE)

    feb27 = fetch_feb27_rows()

    print("=" * 90)
    print("FEB 27 FETCH")
    print("=" * 90)
    print(f"Watchlist symbols requested: {len(WATCHLIST)}")
    print(f"Symbols with data: {feb27['symbol'].nunique()}")
    print(f"Rows fetched: {len(feb27):,}")

    missing = sorted(
        set(WATCHLIST)
        - set(feb27["symbol"].unique())
    )

    if missing:
        print(
            "Missing symbols: "
            + ", ".join(missing)
        )

    # Keep all existing 501-stock rows for Mar 2-6,
    # add Feb 27 only for our fixed Iran watchlist.
    combined = pd.concat(
        [
            feb27,
            existing,
        ],
        ignore_index=True,
        sort=False,
    )

    combined["timestamp_et"] = pd.to_datetime(
        combined["timestamp_et"],
        utc=True,
    ).dt.tz_convert(MARKET_TZ)

    combined["trading_date"] = pd.to_datetime(
        combined["trading_date"]
    ).dt.date

    combined = combined.sort_values(
        [
            "trading_date",
            "symbol",
            "timestamp_et",
        ]
    ).reset_index(drop=True)

    # Convert back to clean strings for CSV.
    combined["timestamp_et"] = (
        combined["timestamp_et"]
        .apply(lambda x: x.isoformat())
    )
    combined["trading_date"] = (
        combined["trading_date"]
        .astype(str)
    )

    combined.to_csv(
        OUTPUT_FILE,
        index=False,
    )

    print()
    print("=" * 90)
    print("DONE")
    print("=" * 90)
    print(f"Output: {OUTPUT_FILE}")
    print(f"Total rows: {len(combined):,}")
    print(
        "Dates: "
        + ", ".join(
            sorted(
                combined["trading_date"].unique()
            )
        )
    )

    print()
    print(
        "Next: change INPUT_FILE in "
        "iran_gap_5m_vs_10m_backtest.py to:"
    )
    print(
        OUTPUT_FILE.name
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
