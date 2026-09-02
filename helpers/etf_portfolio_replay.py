from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass, field
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
MAX_ENTRIES_PER_ETF = 3
MAX_NEW_TRADES_PER_CYCLE = 5
DEFAULT_NOTIONAL_PER_TRADE = 4000.0

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
            amount = float(row.get("v", 0))
        except (KeyError, TypeError, ValueError):
            continue

        if amount > 0:
            total += px * amount
            vol += amount

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


@dataclass
class Entry:
    time: datetime
    price: float
    qty: int
    direction: str
    ai_confidence: float
    price_score: int


@dataclass
class SimPosition:
    symbol: str
    direction: str
    entries: list[Entry] = field(default_factory=list)

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def total_qty(self) -> int:
        return sum(e.qty for e in self.entries)

    @property
    def avg_entry(self) -> float:
        qty = self.total_qty
        if qty <= 0:
            return 0.0
        return sum(e.price * e.qty for e in self.entries) / qty


def main() -> int:
    parser = argparse.ArgumentParser(description="Portfolio-level historical simulation for DELTAX ETF logic.")
    parser.add_argument("--date", default="2026-09-01", help="Replay date YYYY-MM-DD")
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL_PER_TRADE)
    args = parser.parse_args()

    replay_day = date.fromisoformat(args.date)
    prev_day = previous_trading_day(replay_day)
    prior_day = previous_trading_day(prev_day)

    cfg = load_env()
    api = AlpacaData(cfg)

    prior_start, prior_end = session_bounds(prior_day)
    prev_start, prev_end = session_bounds(prev_day)
    day_start, day_end = session_bounds(replay_day)

    bars = api.bars(UNIVERSE, prior_start, day_end)

    all_events = regime.load_completed_market_ai(cfg["database_url"], 48)

    checkpoints = [
        datetime.combine(replay_day, dt_time(9, 40), tzinfo=NY),
        datetime.combine(replay_day, dt_time(10, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(11, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(12, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(13, 30), tzinfo=NY),
        datetime.combine(replay_day, dt_time(14, 30), tzinfo=NY),
    ]

    portfolio: dict[str, SimPosition] = {}
    trades = []

    print("=" * 124)
    print("DELTAX ETF PORTFOLIO REPLAY")
    print("=" * 124)
    print(f"Replay date:          {replay_day}")
    print(f"Notional per entry:   ${args.notional:,.2f}")
    print(f"Max entries per ETF:  {MAX_ENTRIES_PER_ETF}")
    print(f"Max trades per cycle: {MAX_NEW_TRADES_PER_CYCLE}")
    print("No orders are submitted.")
    print()

    for checkpoint in checkpoints:
        usable_events = [
            e for e in all_events
            if e.get("last_published_at")
            and e["last_published_at"].astimezone(NY) <= checkpoint
        ]

        regime_result = regime.aggregate(usable_events)
        candidates = ai_candidates(regime_result)
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

            existing = portfolio.get(symbol)
            if existing and existing.entry_count >= MAX_ENTRIES_PER_ETF:
                continue

            # Do not reverse direction inside this simple intraday replay.
            if existing and existing.direction != ai["direction"]:
                continue

            composite = 0.60 * ai["confidence"] + 0.40 * (score / 5.0)

            qualifying.append({
                "symbol": symbol,
                "direction": ai["direction"],
                "ai_confidence": ai["confidence"],
                "price_score": score,
                "price": item["price"],
                "source": ai["source"],
                "composite": composite,
            })

        qualifying.sort(
            key=lambda x: (-x["composite"], -x["price_score"], -x["ai_confidence"])
        )

        chosen = qualifying[:MAX_NEW_TRADES_PER_CYCLE]

        print("-" * 124)
        print(
            f"{checkpoint.strftime('%H:%M ET')} | "
            f"regime={regime_result['regime']} | "
            f"qualifying={len(qualifying)} | chosen={len(chosen)}"
        )

        if not chosen:
            print("  NO NEW TRADES")
            continue

        for q in chosen:
            qty = int(math.floor(args.notional / q["price"]))
            if qty < 1:
                continue

            pos = portfolio.get(q["symbol"])
            if pos is None:
                pos = SimPosition(symbol=q["symbol"], direction=q["direction"])
                portfolio[q["symbol"]] = pos

            entry = Entry(
                time=checkpoint,
                price=q["price"],
                qty=qty,
                direction=q["direction"],
                ai_confidence=q["ai_confidence"],
                price_score=q["price_score"],
            )
            pos.entries.append(entry)

            trades.append({
                "time": checkpoint,
                "symbol": q["symbol"],
                "direction": q["direction"],
                "qty": qty,
                "price": q["price"],
                "notional": qty * q["price"],
                "entry_no": pos.entry_count,
                "composite": q["composite"],
            })

            print(
                f"  BUY#{pos.entry_count} {q['symbol']:<5} {q['direction'].upper():<5} "
                f"qty={qty:<4} px={q['price']:.2f} "
                f"AI={q['ai_confidence']:.2f} PRICE={q['price_score']}/5 "
                f"COMPOSITE={q['composite']:.3f}"
            )

    print()
    print("=" * 124)
    print("END-OF-DAY PORTFOLIO")
    print("=" * 124)

    total_pnl = 0.0
    total_capital = 0.0

    for symbol in sorted(portfolio):
        pos = portfolio[symbol]
        rows = [
            r for r in bars.get(symbol, [])
            if day_start <= parse_bar_time(r["t"]) <= day_end
        ]
        close_px = last_close(rows)
        if close_px is None:
            continue

        qty = pos.total_qty
        avg = pos.avg_entry

        if pos.direction == "long":
            pnl = (close_px - avg) * qty
        else:
            pnl = (avg - close_px) * qty

        capital = sum(e.price * e.qty for e in pos.entries)
        pnl_pct = pnl / capital * 100 if capital else 0.0

        total_pnl += pnl
        total_capital += capital

        print(
            f"{symbol:<5} {pos.direction.upper():<5} "
            f"entries={pos.entry_count} qty={qty:<4} avg={avg:.2f} close={close_px:.2f} "
            f"P/L=${pnl:+.2f} ({pnl_pct:+.2f}%)"
        )

    total_pct = total_pnl / total_capital * 100 if total_capital else 0.0

    print()
    print("=" * 124)
    print("SUMMARY")
    print("=" * 124)
    print(f"Total entries:       {len(trades)}")
    print(f"Unique ETFs:         {len(portfolio)}")
    print(f"Capital deployed:    ${total_capital:,.2f}")
    print(f"End-of-day P/L:      ${total_pnl:+,.2f}")
    print(f"Return on deployed:  {total_pct:+.2f}%")
    print()
    print("NOTE: This replay closes everything at the regular-session close.")
    print("It does not yet simulate stop-loss, take-profit, thesis exits, slippage or commissions.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
