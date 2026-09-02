from __future__ import annotations

import argparse
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import etf_ai_regime as regime


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
DATA_BASE = "https://data.alpaca.markets"
NY = ZoneInfo("America/New_York")
REQUEST_TIMEOUT = 20

MIN_AI_CONFIDENCE = 0.65
MIN_PRICE_SCORE = 4

UNIVERSE = sorted(set(regime.ETF_NAMES) | {"SPY", "QQQ", "IWM", "DIA"})


def load_env() -> dict[str, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    key = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
    feed = (os.getenv("ALPACA_DATA_FEED_EVENT") or os.getenv("ALPACA_DATA_FEED") or "iex").strip()
    database_url = (os.getenv("DATABASE_URL") or "").strip()

    if not key:
        raise RuntimeError("Missing ALPACA_API_KEY_EVENT")
    if not secret:
        raise RuntimeError("Missing ALPACA_API_SECRET_EVENT")
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")

    return {"key": key, "secret": secret, "feed": feed, "database_url": database_url}


class AlpacaData:
    def __init__(self, cfg: dict[str, str]):
        self.cfg = cfg
        self.headers = {
            "APCA-API-KEY-ID": cfg["key"],
            "APCA-API-SECRET-KEY": cfg["secret"],
        }

    def get(self, url: str, params: dict[str, Any]) -> dict:
        r = requests.get(url, headers=self.headers, params=params, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"GET failed {r.status_code}: {r.text[:1000]}")
        return r.json()

    def bars(self, symbols: list[str], start: datetime, end: datetime) -> dict[str, list[dict]]:
        output = {s: [] for s in symbols}
        token = None

        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "5Min",
                "start": start.astimezone(timezone.utc).isoformat(),
                "end": end.astimezone(timezone.utc).isoformat(),
                "adjustment": "raw",
                "feed": self.cfg["feed"],
                "limit": 10000,
            }
            if token:
                params["page_token"] = token

            data = self.get(f"{DATA_BASE}/v2/stocks/bars", params)

            for symbol, rows in (data.get("bars") or {}).items():
                output.setdefault(symbol, []).extend(rows)

            token = data.get("next_page_token")
            if not token:
                break

        return output


def session_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, dt_time(9, 30), tzinfo=NY),
        datetime.combine(day, dt_time(16, 0), tzinfo=NY),
    )


def previous_trading_day(day: date) -> date:
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def parse_bar_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(NY)


def rows_until(rows: list[dict], checkpoint: datetime) -> list[dict]:
    return [r for r in rows if parse_bar_time(r["t"]) <= checkpoint]


def first_open(rows: list[dict]) -> float | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda x: x["t"])
    try:
        return float(rows[0]["o"])
    except (KeyError, TypeError, ValueError):
        return None


def last_close(rows: list[dict]) -> float | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda x: x["t"])
    try:
        return float(rows[-1]["c"])
    except (KeyError, TypeError, ValueError):
        return None


def vwap(rows: list[dict]) -> float | None:
    total = 0.0
    vol = 0.0
    for row in rows:
        try:
            px = float(row.get("vw", row["c"]))
            v = float(row.get("v", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if v > 0:
            total += px * v
            vol += v
    return None if vol <= 0 else total / vol


def pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100.0 if old else 0.0


def ai_candidates(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}

    for item in result.get("etf_biases", []):
        direction = item.get("direction")
        confidence = float(item.get("confidence", 0))
        if direction not in {"long", "short"} or confidence < MIN_AI_CONFIDENCE:
            continue

        out[item["symbol"]] = {
            "direction": direction,
            "confidence": confidence,
            "source": "core",
        }

        for symbol in item.get("subsector_candidates", []):
            out[symbol] = {
                "direction": direction,
                "confidence": confidence,
                "source": f"focused_from_{item['symbol']}",
            }

    for item in result.get("index_biases", []):
        direction = item.get("direction")
        confidence = float(item.get("confidence", 0))
        if direction not in {"long", "short"} or confidence < MIN_AI_CONFIDENCE:
            continue
        out[item["symbol"]] = {
            "direction": direction,
            "confidence": confidence,
            "source": "index",
        }

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Historical replay for DELTAX ETF AI x PRICE logic.")
    parser.add_argument("--date", default="2026-09-01", help="Replay date YYYY-MM-DD")
    args = parser.parse_args()

    replay_day = date.fromisoformat(args.date)
    prev_day = previous_trading_day(replay_day)
    prior_day = previous_trading_day(prev_day)

    cfg = load_env()
    api = AlpacaData(cfg)

    prior_start, prior_end = session_bounds(prior_day)
    prev_start, prev_end = session_bounds(prev_day)
    day_start, day_end = session_bounds(replay_day)

    all_start = prior_start
    all_end = day_end

    bars = api.bars(UNIVERSE, all_start, all_end)

    # AI analyses from DB; filter each checkpoint to avoid future news leakage.
    all_events = regime.load_completed_market_ai(cfg["database_url"], 48)

    checkpoints = [
        datetime.combine(replay_day, dt_time(9, 40), tzinfo=NY),
        datetime.combine(replay_day, dt_time(10, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(11, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(12, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(13, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(14, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(15, 30), tzinfo=NY),
    ]

    print("=" * 118)
    print("DELTAX ETF HISTORICAL REPLAY")
    print("=" * 118)
    print(f"Replay date: {replay_day}")
    print("No orders are submitted. AI events are only used after their publication time.")
    print()

    for checkpoint in checkpoints:
        usable_events = [
            e for e in all_events
            if e.get("last_published_at")
            and e["last_published_at"].astimezone(NY) <= checkpoint
        ]

        regime_result = regime.aggregate(usable_events)
        candidates = ai_candidates(regime_result)

        # Need SPY for relative strength.
        symbols = sorted(set(candidates) | {"SPY"})
        raw = {}

        for symbol in symbols:
            rows = bars.get(symbol, [])
            prior_rows = [
                r for r in rows
                if prior_start <= parse_bar_time(r["t"]) <= prior_end
            ]
            prev_rows = [
                r for r in rows
                if prev_start <= parse_bar_time(r["t"]) <= prev_end
            ]
            today_rows = [
                r for r in rows
                if day_start <= parse_bar_time(r["t"]) <= checkpoint
            ]

            prior_close = last_close(prior_rows)
            prev_close = last_close(prev_rows)
            current = last_close(today_rows)
            open_px = first_open(today_rows)
            current_vwap = vwap(today_rows)

            if None in (prior_close, prev_close, current, open_px, current_vwap):
                continue

            raw[symbol] = {
                "price": current,
                "prev_to_now": pct(current, prev_close),
                "open_to_now": pct(current, open_px),
                "vwap_to_now": pct(current, current_vwap),
                "prior_momentum": pct(prev_close, prior_close),
            }

        spy_move = raw.get("SPY", {}).get("prev_to_now", 0.0)

        qualifying = []

        for symbol, ai in candidates.items():
            item = raw.get(symbol)
            if not item:
                continue

            item["relative_spy"] = item["prev_to_now"] - spy_move
            values = [
                item["prev_to_now"],
                item["open_to_now"],
                item["vwap_to_now"],
                item["relative_spy"],
                item["prior_momentum"],
            ]

            long_score = sum(1 for x in values if x > 0)
            short_score = sum(1 for x in values if x < 0)
            score = long_score if ai["direction"] == "long" else short_score

            if score < MIN_PRICE_SCORE:
                continue

            # "What happened after entry?" For inspection only.
            day_rows_all = [
                r for r in bars.get(symbol, [])
                if day_start <= parse_bar_time(r["t"]) <= day_end
            ]
            close_px = last_close(day_rows_all)

            forward_to_close = None
            if close_px:
                if ai["direction"] == "long":
                    forward_to_close = pct(close_px, item["price"])
                else:
                    forward_to_close = -pct(close_px, item["price"])

            qualifying.append({
                "symbol": symbol,
                "direction": ai["direction"],
                "ai_confidence": ai["confidence"],
                "price_score": score,
                "entry_price": item["price"],
                "forward_to_close_pct": forward_to_close,
                "source": ai["source"],
            })

        qualifying.sort(
            key=lambda x: (-x["ai_confidence"], -x["price_score"])
        )

        print("-" * 118)
        print(
            f"{checkpoint.strftime('%H:%M ET')} | "
            f"regime={regime_result['regime']} | "
            f"AI events available={len(usable_events)} | "
            f"qualifying={len(qualifying)}"
        )

        if not qualifying:
            print("  NO TRADE")
        else:
            for q in qualifying:
                fwd = q["forward_to_close_pct"]
                fwd_text = "n/a" if fwd is None else f"{fwd:+.2f}%"
                print(
                    f"  {q['symbol']:<5} {q['direction'].upper():<5} "
                    f"AI={q['ai_confidence']:.2f} "
                    f"PRICE={q['price_score']}/5 "
                    f"entry~{q['entry_price']:.2f} "
                    f"to-close={fwd_text} "
                    f"[{q['source']}]"
                )

    print()
    print("=" * 118)
    print("Replay complete. This is logic validation, not a statistically valid backtest.")
    print("=" * 118)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
