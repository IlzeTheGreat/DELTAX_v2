from __future__ import annotations

import json
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


# ============================================================
# DELTAX ETF TREND SCANNER
# ============================================================
# Purpose:
#   Compare a fixed ETF universe across:
#     1) Friday regular-session close -> Monday regular-session close
#     2) Monday regular-session VWAP -> latest available price
#
# Classification:
#   - CONTINUOUS_DOWN: both changes are negative
#   - CONTINUOUS_UP:   both changes are positive
#   - MIXED:           direction changed
#
# Uses Alpaca market data only. No orders are submitted.

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

DATA_BASE = "https://data.alpaca.markets"
REQUEST_TIMEOUT = 20
NY = ZoneInfo("America/New_York")

ETF_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLE", "XLK", "XLF", "XLI", "XLV", "XLP", "XLY", "XLU", "XLB", "XLC",
    "XBI", "SMH", "SOXX", "TLT", "GLD", "SLV", "USO",
]


def load_environment() -> dict[str, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    # Prefer EVENT credentials because this helper is meant to accompany
    # the EVENT/manual-trading workflow. Fall back to normal PAPER data keys.
    key = (
        os.getenv("ALPACA_API_KEY_EVENT")
        or os.getenv("ALPACA_API_KEY_PAPER")
        or ""
    ).strip()
    secret = (
        os.getenv("ALPACA_API_SECRET_EVENT")
        or os.getenv("ALPACA_API_SECRET_PAPER")
        or ""
    ).strip()
    feed = (
        os.getenv("ALPACA_DATA_FEED_EVENT")
        or os.getenv("ALPACA_DATA_FEED")
        or "iex"
    ).strip()

    if not key:
        raise RuntimeError(
            "Missing ALPACA_API_KEY_EVENT (or ALPACA_API_KEY_PAPER) in .env"
        )
    if not secret:
        raise RuntimeError(
            "Missing ALPACA_API_SECRET_EVENT (or ALPACA_API_SECRET_PAPER) in .env"
        )

    return {"key": key, "secret": secret, "feed": feed}


class AlpacaData:
    def __init__(self, cfg: dict[str, str]):
        self.cfg = cfg
        self.headers = {
            "APCA-API-KEY-ID": cfg["key"],
            "APCA-API-SECRET-KEY": cfg["secret"],
        }

    def _get(self, url: str, params: dict | None = None) -> Any:
        r = requests.get(
            url,
            headers=self.headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(
                f"GET {url} failed: {r.status_code} {r.text[:500]}"
            )
        return r.json()

    def stock_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        page_token = None

        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": start.astimezone(timezone.utc).isoformat(),
                "end": end.astimezone(timezone.utc).isoformat(),
                "adjustment": "raw",
                "feed": self.cfg["feed"],
                "limit": 10000,
            }
            if page_token:
                params["page_token"] = page_token

            data = self._get(
                f"{DATA_BASE}/v2/stocks/bars",
                params=params,
            )

            for symbol, bars in (data.get("bars") or {}).items():
                out.setdefault(symbol, []).extend(bars)

            page_token = data.get("next_page_token")
            if not page_token:
                break

        return out

    def latest_trades(self, symbols: list[str]) -> dict[str, float]:
        data = self._get(
            f"{DATA_BASE}/v2/stocks/trades/latest",
            params={
                "symbols": ",".join(symbols),
                "feed": self.cfg["feed"],
            },
        )
        result = {}
        for symbol, trade in (data.get("trades") or {}).items():
            try:
                result[symbol] = float(trade["p"])
            except (KeyError, TypeError, ValueError):
                pass
        return result


def previous_weekday(day: date, weekday: int) -> date:
    """Return the most recent requested weekday strictly before `day`."""
    d = day - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def resolve_last_friday_and_monday(now_ny: datetime) -> tuple[date, date]:
    # The scanner is designed for Tue-Fri use: compare the most recent
    # completed Friday and Monday sessions.
    today = now_ny.date()

    monday = previous_weekday(today, 0) if today.weekday() != 0 else today
    if monday >= today:
        monday -= timedelta(days=7)

    friday = monday - timedelta(days=3)
    return friday, monday


def regular_session_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, dt_time(9, 30), tzinfo=NY),
        datetime.combine(day, dt_time(16, 0), tzinfo=NY),
    )


def session_close(bars: list[dict]) -> float | None:
    if not bars:
        return None
    bars = sorted(bars, key=lambda x: x["t"])
    try:
        return float(bars[-1]["c"])
    except (KeyError, TypeError, ValueError):
        return None


def session_vwap(bars: list[dict]) -> float | None:
    if not bars:
        return None

    weighted = 0.0
    volume = 0.0

    for bar in bars:
        try:
            # Use bar VWAP when Alpaca supplies it, otherwise use close.
            px = float(bar.get("vw", bar["c"]))
            vol = float(bar.get("v", 0))
        except (KeyError, TypeError, ValueError):
            continue

        if vol <= 0:
            continue

        weighted += px * vol
        volume += vol

    if volume <= 0:
        return None

    return weighted / volume


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def classify(friday_to_monday: float, monday_to_now: float) -> str:
    if friday_to_monday < 0 and monday_to_now < 0:
        return "CONTINUOUS_DOWN"
    if friday_to_monday > 0 and monday_to_now > 0:
        return "CONTINUOUS_UP"
    return "MIXED"


def trend_score(friday_to_monday: float, monday_to_now: float) -> float:
    # Simple directional strength score for sorting.
    # Same-direction moves accumulate; mixed moves partially cancel.
    return friday_to_monday + monday_to_now


def fmt_num(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{decimals}f}"


def print_table(rows: list[dict]) -> None:
    headers = [
        ("Ticker", 7),
        ("Fri close", 11),
        ("Mon close", 11),
        ("Fri->Mon", 10),
        ("Mon VWAP", 11),
        ("Now", 11),
        ("MonAvg->Now", 12),
        ("Trend", 17),
        ("Score", 9),
    ]

    header_line = " ".join(name.ljust(width) for name, width in headers)
    print(header_line)
    print("-" * len(header_line))

    for row in rows:
        values = [
            row["symbol"].ljust(7),
            fmt_num(row["friday_close"]).rjust(11),
            fmt_num(row["monday_close"]).rjust(11),
            (f"{row['fri_to_mon_pct']:+.2f}%").rjust(10),
            fmt_num(row["monday_vwap"]).rjust(11),
            fmt_num(row["latest_price"]).rjust(11),
            (f"{row['mon_vwap_to_now_pct']:+.2f}%").rjust(12),
            row["trend"].ljust(17),
            (f"{row['trend_score']:+.2f}").rjust(9),
        ]
        print(" ".join(values))


def main() -> int:
    cfg = load_environment()
    alpaca = AlpacaData(cfg)

    now_ny = datetime.now(timezone.utc).astimezone(NY)
    friday, monday = resolve_last_friday_and_monday(now_ny)

    fri_start, fri_end = regular_session_bounds(friday)
    mon_start, mon_end = regular_session_bounds(monday)

    print("=" * 104)
    print("DELTAX ETF TREND SCANNER")
    print("=" * 104)
    print(f"Current NY time: {now_ny.isoformat()}")
    print(f"Friday session:  {friday.isoformat()}")
    print(f"Monday session:  {monday.isoformat()}")
    print(f"Data feed:       {cfg['feed']}")
    print("Monday average:  regular-session volume-weighted average price (VWAP)")
    print()

    friday_bars = alpaca.stock_bars(
        ETF_UNIVERSE,
        "5Min",
        fri_start,
        fri_end,
    )
    monday_bars = alpaca.stock_bars(
        ETF_UNIVERSE,
        "5Min",
        mon_start,
        mon_end,
    )
    latest = alpaca.latest_trades(ETF_UNIVERSE)

    rows = []
    errors = []

    for symbol in ETF_UNIVERSE:
        fri_close = session_close(friday_bars.get(symbol, []))
        mon_close = session_close(monday_bars.get(symbol, []))
        mon_vwap = session_vwap(monday_bars.get(symbol, []))
        latest_price = latest.get(symbol)

        if None in (fri_close, mon_close, mon_vwap, latest_price):
            errors.append(
                {
                    "symbol": symbol,
                    "friday_close": fri_close,
                    "monday_close": mon_close,
                    "monday_vwap": mon_vwap,
                    "latest_price": latest_price,
                }
            )
            continue

        a = pct_change(mon_close, fri_close)
        b = pct_change(latest_price, mon_vwap)

        rows.append(
            {
                "symbol": symbol,
                "friday_close": fri_close,
                "monday_close": mon_close,
                "fri_to_mon_pct": a,
                "monday_vwap": mon_vwap,
                "latest_price": latest_price,
                "mon_vwap_to_now_pct": b,
                "trend": classify(a, b),
                "trend_score": trend_score(a, b),
            }
        )

    # Strongest continuous decliners first, then strongest continuous risers,
    # then mixed names.
    rank = {
        "CONTINUOUS_DOWN": 0,
        "CONTINUOUS_UP": 1,
        "MIXED": 2,
    }

    rows.sort(
        key=lambda r: (
            rank[r["trend"]],
            r["trend_score"] if r["trend"] == "CONTINUOUS_DOWN"
            else -r["trend_score"],
        )
    )

    print_table(rows)

    downs = [r for r in rows if r["trend"] == "CONTINUOUS_DOWN"]
    ups = [r for r in rows if r["trend"] == "CONTINUOUS_UP"]

    print()
    print("=" * 104)
    print("SHORT CANDIDATES - fell in both periods")
    print("=" * 104)
    if downs:
        for r in downs:
            print(
                f"{r['symbol']}: Fri->Mon {r['fri_to_mon_pct']:+.2f}% | "
                f"Mon VWAP->Now {r['mon_vwap_to_now_pct']:+.2f}% | "
                f"score {r['trend_score']:+.2f}"
            )
    else:
        print("None")

    print()
    print("=" * 104)
    print("LONG CANDIDATES - rose in both periods")
    print("=" * 104)
    if ups:
        for r in ups:
            print(
                f"{r['symbol']}: Fri->Mon {r['fri_to_mon_pct']:+.2f}% | "
                f"Mon VWAP->Now {r['mon_vwap_to_now_pct']:+.2f}% | "
                f"score {r['trend_score']:+.2f}"
            )
    else:
        print("None")

    if errors:
        print()
        print("=" * 104)
        print("MISSING / INCOMPLETE DATA")
        print("=" * 104)
        print(json.dumps(errors, indent=2, default=str))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
