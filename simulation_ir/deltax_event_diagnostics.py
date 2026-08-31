from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
ENV_PATH = ROOT_DIR / ".env"

NY = ZoneInfo("America/New_York")

TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets"

LONG_SYMBOLS = [
    "APO", "APP", "BAC", "BX", "FFIV", "LYB", "WDAY", "WFC", "XYZ"
]
SHORT_SYMBOLS = [
    "COHR", "F", "LITE", "LRCX", "MAS", "TEL"
]
WATCHLIST = sorted(LONG_SYMBOLS + SHORT_SYMBOLS)

MIN_EVENT_GAP = 0.0050
MIN_REVERSAL_10M = 0.0025


load_dotenv(ENV_PATH)

KEY = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
SECRET = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
TRADING_URL = (
    os.getenv("ALPACA_TRADING_URL_EVENT")
    or TRADING_BASE_DEFAULT
).strip().rstrip("/")
FEED = (os.getenv("ALPACA_DATA_FEED_EVENT") or "iex").strip()

if not TRADING_URL.endswith("/v2"):
    TRADING_URL += "/v2"

HEADERS = {
    "APCA-API-KEY-ID": KEY,
    "APCA-API-SECRET-KEY": SECRET,
}


def get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    if not r.ok:
        raise RuntimeError(f"{r.status_code} {r.text[:500]}")
    return r.json()


def parse_ts(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(NY)


clock = get(f"{TRADING_URL}/clock")
now = datetime.fromisoformat(
    clock["timestamp"].replace("Z", "+00:00")
).astimezone(NY)
trading_day = now.date()

print("=" * 132)
print("DELTAX EVENT DIAGNOSTICS")
print("=" * 132)
print(f"Alpaca clock: {now.isoformat()}")
print(f"Market open: {clock.get('is_open')}")
print(f"Feed: {FEED}")
print()
print(
    f"{'SYMBOL':<6} {'DIR':<5} {'PREV CLOSE':>11} {'OPEN':>11} "
    f"{'09:40':>11} {'GAP':>9} {'10M':>9} {'GAP?':>6} {'REV?':>6}  DECISION"
)
print("-" * 132)

for symbol in WATCHLIST:
    direction = "LONG" if symbol in LONG_SYMBOLS else "SHORT"

    # Previous close
    start = datetime.combine(
        trading_day - timedelta(days=10),
        dt_time(0, 0),
        tzinfo=NY,
    )
    end = datetime.combine(
        trading_day,
        dt_time(0, 0),
        tzinfo=NY,
    )

    daily = get(
        f"{DATA_BASE}/v2/stocks/bars",
        params={
            "symbols": symbol,
            "timeframe": "1Day",
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
            "adjustment": "raw",
            "feed": FEED,
            "limit": 1000,
        },
    ).get("bars", {}).get(symbol, [])

    prev_close = None
    valid_daily = []
    for bar in daily:
        ts = parse_ts(bar["t"])
        if ts.date() < trading_day:
            valid_daily.append(bar)

    if valid_daily:
        prev_close = float(valid_daily[-1]["c"])

    # 09:30 -> 09:40 1m
    start_i = datetime.combine(
        trading_day,
        dt_time(9, 30),
        tzinfo=NY,
    )
    end_i = datetime.combine(
        trading_day,
        dt_time(9, 41),
        tzinfo=NY,
    )

    intraday = get(
        f"{DATA_BASE}/v2/stocks/bars",
        params={
            "symbols": symbol,
            "timeframe": "1Min",
            "start": start_i.astimezone(timezone.utc).isoformat(),
            "end": end_i.astimezone(timezone.utc).isoformat(),
            "adjustment": "raw",
            "feed": FEED,
            "limit": 1000,
        },
    ).get("bars", {}).get(symbol, [])

    opening_bar = None
    close_0940_bar = None

    for bar in intraday:
        ts = parse_ts(bar["t"])
        if ts.hour == 9 and ts.minute == 30:
            opening_bar = bar
        if ts.hour == 9 and ts.minute == 39:
            close_0940_bar = bar

    if prev_close is None or opening_bar is None or close_0940_bar is None:
        missing = []
        if prev_close is None:
            missing.append("prev_close")
        if opening_bar is None:
            missing.append("09:30")
        if close_0940_bar is None:
            missing.append("09:39")
        print(
            f"{symbol:<6} {direction:<5} {'—':>11} {'—':>11} {'—':>11} "
            f"{'—':>9} {'—':>9} {'—':>6} {'—':>6}  MISSING {','.join(missing)}"
        )
        continue

    today_open = float(opening_bar["o"])
    price_0940 = float(close_0940_bar["c"])

    gap = today_open / prev_close - 1.0
    rev = price_0940 / today_open - 1.0

    if direction == "LONG":
        gap_ok = gap >= MIN_EVENT_GAP
        rev_ok = rev <= -MIN_REVERSAL_10M
    else:
        gap_ok = gap <= -MIN_EVENT_GAP
        rev_ok = rev >= MIN_REVERSAL_10M

    if gap_ok and rev_ok:
        decision = "TRADE"
    elif not gap_ok and not rev_ok:
        decision = "NO: gap + reversal"
    elif not gap_ok:
        decision = "NO: gap"
    else:
        decision = "NO: reversal"

    print(
        f"{symbol:<6} {direction:<5} "
        f"{prev_close:>11.2f} {today_open:>11.2f} {price_0940:>11.2f} "
        f"{gap*100:>+8.2f}% {rev*100:>+8.2f}% "
        f"{('YES' if gap_ok else 'NO'):>6} "
        f"{('YES' if rev_ok else 'NO'):>6}  {decision}"
    )

print()
print("Rules:")
print("  LONG  -> gap >= +0.50% AND 09:30→09:40 <= -0.25%")
print("  SHORT -> gap <= -0.50% AND 09:30→09:40 >= +0.25%")
