from __future__ import annotations

import json
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
DATA_BASE = "https://data.alpaca.markets"
REQUEST_TIMEOUT = 20
NY = ZoneInfo("America/New_York")

CORE_SECTORS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
}
SUBSECTORS = {
    "SMH": "Semiconductors",
    "IGV": "Software",
    "CIBR": "Cybersecurity",
    "XBI": "Biotech",
    "IHI": "Medical Devices",
    "KRE": "Regional Banks",
    "IAI": "Broker-Dealers",
    "IYT": "Transportation",
    "ITA": "Aerospace & Defense",
    "XOP": "Oil & Gas Exploration",
}
SAFE_HAVENS = {
    "GLD": "Gold",
    "TLT": "Long Treasuries",
    "BIL": "1-3 Month T-Bills",
}
MARKET_REFERENCES = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IWM": "Russell 2000",
    "DIA": "Dow Jones",
    "USO": "Crude Oil",
}
UNIVERSE = {**CORE_SECTORS, **SUBSECTORS, **SAFE_HAVENS, **MARKET_REFERENCES}


def load_environment() -> dict[str, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    key = (os.getenv("ALPACA_API_KEY_EVENT") or os.getenv("ALPACA_API_KEY_PAPER") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET_EVENT") or os.getenv("ALPACA_API_SECRET_PAPER") or "").strip()
    feed = (os.getenv("ALPACA_DATA_FEED_EVENT") or os.getenv("ALPACA_DATA_FEED") or "iex").strip()
    if not key:
        raise RuntimeError("Missing ALPACA_API_KEY_EVENT in .env")
    if not secret:
        raise RuntimeError("Missing ALPACA_API_SECRET_EVENT in .env")
    return {"key": key, "secret": secret, "feed": feed}


class AlpacaData:
    def __init__(self, cfg: dict[str, str]):
        self.cfg = cfg
        self.headers = {
            "APCA-API-KEY-ID": cfg["key"],
            "APCA-API-SECRET-KEY": cfg["secret"],
        }

    def _get(self, url: str, params: dict | None = None) -> Any:
        r = requests.get(url, headers=self.headers, params=params, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"GET {url} failed: {r.status_code} {r.text[:800]}")
        return r.json()

    def bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime) -> dict[str, list[dict]]:
        output = {s: [] for s in symbols}
        token = None
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
            if token:
                params["page_token"] = token
            data = self._get(f"{DATA_BASE}/v2/stocks/bars", params=params)
            for symbol, rows in (data.get("bars") or {}).items():
                output.setdefault(symbol, []).extend(rows)
            token = data.get("next_page_token")
            if not token:
                break
        return output

    def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = self._get(
                f"{DATA_BASE}/v2/stocks/snapshots",
                params={"symbols": ",".join(chunk), "feed": self.cfg["feed"]},
            )
            result.update(data)
        return result


def previous_trading_day(day: date) -> date:
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def session_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, dt_time(9, 30), tzinfo=NY),
        datetime.combine(day, dt_time(16, 0), tzinfo=NY),
    )


def pct(new: float, old: float) -> float:
    return 0.0 if old == 0 else (new / old - 1.0) * 100.0


def latest_price(snapshot: dict) -> float | None:
    trade = snapshot.get("latestTrade") or {}
    minute = snapshot.get("minuteBar") or {}
    daily = snapshot.get("dailyBar") or {}
    for value in (trade.get("p"), minute.get("c"), daily.get("c")):
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def daily_open(snapshot: dict) -> float | None:
    try:
        return float((snapshot.get("dailyBar") or {})["o"])
    except (KeyError, TypeError, ValueError):
        return None


def previous_close(snapshot: dict) -> float | None:
    try:
        return float((snapshot.get("prevDailyBar") or {})["c"])
    except (KeyError, TypeError, ValueError):
        return None


def session_close(bars: list[dict]) -> float | None:
    if not bars:
        return None
    rows = sorted(bars, key=lambda x: x["t"])
    try:
        return float(rows[-1]["c"])
    except (KeyError, TypeError, ValueError):
        return None


def bar_vwap(bars: list[dict]) -> float | None:
    weighted = 0.0
    volume = 0.0
    for bar in bars:
        try:
            price = float(bar.get("vw", bar["c"]))
            vol = float(bar.get("v", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if vol > 0:
            weighted += price * vol
            volume += vol
    return None if volume <= 0 else weighted / volume


def group_name(symbol: str) -> str:
    if symbol in CORE_SECTORS:
        return "CORE"
    if symbol in SUBSECTORS:
        return "SUB"
    if symbol in SAFE_HAVENS:
        return "SAFE"
    return "MARKET"


def score_signs(values: list[float | None]) -> tuple[int, int]:
    l = s = 0
    for value in values:
        if value is None:
            continue
        if value > 0:
            l += 1
        elif value < 0:
            s += 1
    return l, s


def main() -> int:
    cfg = load_environment()
    api = AlpacaData(cfg)

    now_ny = datetime.now(timezone.utc).astimezone(NY)
    today = now_ny.date()
    prev_day = previous_trading_day(today)
    prior_day = previous_trading_day(prev_day)
    symbols = list(UNIVERSE)

    snapshots = api.snapshots(symbols)

    # Fetch completed regular sessions using 5-minute bars.
    prior_start, prior_end = session_bounds(prior_day)
    prev_start, prev_end = session_bounds(prev_day)
    prior_bars = api.bars(symbols, "5Min", prior_start, prior_end)
    prev_bars = api.bars(symbols, "5Min", prev_start, prev_end)

    # Current regular-session bars only after 09:30 NY.
    today_start, today_end = session_bounds(today)
    market_open = now_ny >= today_start
    enough_for_live_confirmation = now_ny >= (today_start + timedelta(minutes=10))

    today_bars = {s: [] for s in symbols}
    if market_open:
        today_bars = api.bars(symbols, "5Min", today_start, min(now_ny, today_end))

    rows = []

    for symbol in symbols:
        snap = snapshots.get(symbol) or {}
        last = latest_price(snap)
        prev_close = session_close(prev_bars.get(symbol, [])) or previous_close(snap)
        prior_close = session_close(prior_bars.get(symbol, []))
        current_vwap = bar_vwap(today_bars.get(symbol, []))

        if last is None or prev_close is None:
            continue

        prior_momentum = pct(prev_close, prior_close) if prior_close else None
        prev_to_now = pct(last, prev_close)

        open_to_now = None
        vwap_to_now = None
        if market_open:
            open_px = daily_open(snap)
            if open_px:
                open_to_now = pct(last, open_px)
            if current_vwap:
                vwap_to_now = pct(last, current_vwap)

        rows.append({
            "symbol": symbol,
            "name": UNIVERSE[symbol],
            "group": group_name(symbol),
            "price": last,
            "prev_to_now": prev_to_now,
            "open_to_now": open_to_now,
            "vwap_to_now": vwap_to_now,
            "prior_momentum": prior_momentum,
        })

    spy = next((r for r in rows if r["symbol"] == "SPY"), None)
    spy_move = spy["prev_to_now"] if spy else 0.0

    for row in rows:
        row["relative_spy"] = row["prev_to_now"] - spy_move

        if enough_for_live_confirmation:
            values = [
                row["prev_to_now"],
                row["open_to_now"],
                row["vwap_to_now"],
                row["relative_spy"],
                row["prior_momentum"],
            ]
            long_score, short_score = score_signs(values)
            if long_score >= 4 and long_score > short_score:
                signal = "LONG"
            elif short_score >= 4 and short_score > long_score:
                signal = "SHORT"
            elif long_score == 3 and long_score > short_score:
                signal = "WATCH_LONG"
            elif short_score == 3 and short_score > long_score:
                signal = "WATCH_SHORT"
            else:
                signal = "NEUTRAL"
        else:
            # Premarket/first 10 minutes: no trading signal. Use only completed
            # session momentum + current move + relative strength for watchlist.
            long_score, short_score = score_signs([
                row["prev_to_now"],
                row["relative_spy"],
                row["prior_momentum"],
            ])
            if long_score >= 2 and long_score > short_score:
                signal = "PREMARKET_WATCH_LONG"
            elif short_score >= 2 and short_score > long_score:
                signal = "PREMARKET_WATCH_SHORT"
            else:
                signal = "PREMARKET_NEUTRAL"

        row["long_score"] = long_score
        row["short_score"] = short_score
        row["signal"] = signal

    rows.sort(
        key=lambda r: (
            0 if "LONG" in r["signal"] else 1 if "SHORT" in r["signal"] else 2,
            -(max(r["long_score"], r["short_score"])),
            -abs(r["prev_to_now"]),
        )
    )

    print("=" * 136)
    print("DELTAX ETF ROTATION SCANNER v2")
    print("=" * 136)
    print(f"NY time:           {now_ny.isoformat()}")
    print(f"Feed:              {cfg['feed']}")
    print(f"Regular open:      {today_start.isoformat()}")
    print(f"Market opened:     {market_open}")
    print(f"Live confirmation: {enough_for_live_confirmation} (starts 10 min after open)")
    print("Existing EVENT-account seed positions are not modified.")
    print()

    header = (
        f"{'Ticker':<7} {'Group':<7} {'Name':<27} {'Price':>9} "
        f"{'Prev->Now':>10} {'Open->Now':>10} {'VWAP->Now':>10} "
        f"{'Rel SPY':>9} {'Prior':>9} {'L':>3} {'S':>3} {'Signal':<22}"
    )
    print(header)
    print("-" * len(header))

    def fmt(value):
        return "n/a" if value is None else f"{value:+.2f}%"

    for r in rows:
        print(
            f"{r['symbol']:<7} {r['group']:<7} {r['name'][:27]:<27} {r['price']:>9.2f} "
            f"{fmt(r['prev_to_now']):>10} {fmt(r['open_to_now']):>10} "
            f"{fmt(r['vwap_to_now']):>10} {fmt(r['relative_spy']):>9} "
            f"{fmt(r['prior_momentum']):>9} {r['long_score']:>3} "
            f"{r['short_score']:>3} {r['signal']:<22}"
        )

    print("\nTOP LONG WATCH")
    longs = [r for r in rows if "LONG" in r["signal"]]
    for r in longs[:10]:
        print(
            f"{r['symbol']}: {r['signal']} | prev->now {r['prev_to_now']:+.2f}% | "
            f"prior {fmt(r['prior_momentum'])} | rel SPY {r['relative_spy']:+.2f}%"
        )
    if not longs:
        print("None")

    print("\nTOP SHORT WATCH")
    shorts = [r for r in rows if "SHORT" in r["signal"]]
    for r in shorts[:10]:
        print(
            f"{r['symbol']}: {r['signal']} | prev->now {r['prev_to_now']:+.2f}% | "
            f"prior {fmt(r['prior_momentum'])} | rel SPY {r['relative_spy']:+.2f}%"
        )
    if not shorts:
        print("None")

    print("\nJSON_RESULT")
    print(json.dumps({
        "market_open": market_open,
        "live_confirmation": enough_for_live_confirmation,
        "ny_time": now_ny.isoformat(),
        "rows": rows,
    }, indent=2, default=str))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
