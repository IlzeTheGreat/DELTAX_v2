from __future__ import annotations

import argparse
import json
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

NY = ZoneInfo("America/New_York")
DATA_BASE = "https://data.alpaca.markets"
TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
REQUEST_TIMEOUT = 20

ENTRY_PREFIX = "dxe-etf-"
EXIT_PREFIX = "dxe-etfx-"

STOP_LOSS_PCT = 0.015
TAKE_PROFIT_PCT = 0.030
TECHNICAL_EXIT_MAX_SCORE = 1
EOD_EXIT_TIME = dt_time(15, 55)
MIN_AI_CONFIDENCE = 0.65
MANAGED_ETFS = set(regime.ETF_NAMES) | {"SPY", "QQQ", "IWM", "DIA"}


def load_env() -> dict[str, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    key = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
    trading = (os.getenv("ALPACA_TRADING_URL_EVENT") or TRADING_BASE_DEFAULT).strip().rstrip("/")
    feed = (os.getenv("ALPACA_DATA_FEED_EVENT") or os.getenv("ALPACA_DATA_FEED") or "iex").strip()
    database_url = (os.getenv("DATABASE_URL") or "").strip()

    if not key:
        raise RuntimeError("Missing ALPACA_API_KEY_EVENT")
    if not secret:
        raise RuntimeError("Missing ALPACA_API_SECRET_EVENT")
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL")
    if not trading.endswith("/v2"):
        trading += "/v2"

    return {
        "key": key,
        "secret": secret,
        "trading": trading,
        "feed": feed,
        "database_url": database_url,
    }


class Alpaca:
    def __init__(self, cfg: dict[str, str]):
        self.cfg = cfg
        self.headers = {
            "APCA-API-KEY-ID": cfg["key"],
            "APCA-API-SECRET-KEY": cfg["secret"],
        }

    def _get(self, url: str, params: dict | None = None) -> Any:
        r = requests.get(url, headers=self.headers, params=params, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"GET {url} failed: {r.status_code} {r.text[:1000]}")
        return r.json()

    def _post(self, url: str, payload: dict) -> Any:
        r = requests.post(
            url,
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(f"POST {url} failed: {r.status_code} {r.text[:1200]}")
        return r.json()

    def clock(self) -> dict:
        return self._get(f"{self.cfg['trading']}/clock")

    def orders(self, status: str = "all", limit: int = 500) -> list[dict]:
        return self._get(
            f"{self.cfg['trading']}/orders",
            params={"status": status, "limit": limit, "direction": "desc"},
        )

    def positions(self) -> list[dict]:
        return self._get(f"{self.cfg['trading']}/positions")

    def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        result = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = self._get(
                f"{DATA_BASE}/v2/stocks/snapshots",
                params={"symbols": ",".join(chunk), "feed": self.cfg["feed"]},
            )
            result.update(data)
        return result

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

            data = self._get(f"{DATA_BASE}/v2/stocks/bars", params)

            for symbol, rows in (data.get("bars") or {}).items():
                output.setdefault(symbol, []).extend(rows)

            token = data.get("next_page_token")
            if not token:
                break

        return output

    def submit_exit(self, symbol: str, qty: int, direction: str, reason: str) -> dict:
        side = "sell" if direction == "long" else "buy"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        client_order_id = f"{EXIT_PREFIX}{stamp}-{symbol.lower()}-{reason.lower()}"[:48]

        return self._post(
            f"{self.cfg['trading']}/orders",
            {
                "symbol": symbol,
                "qty": str(qty),
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            },
        )


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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


def latest_price(snapshot: dict) -> float | None:
    for container, key in (
        ("latestTrade", "p"),
        ("minuteBar", "c"),
        ("dailyBar", "c"),
    ):
        try:
            value = (snapshot.get(container) or {}).get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def first_open(rows: list[dict]) -> float | None:
    if not rows:
        return None
    try:
        return float(sorted(rows, key=lambda x: x["t"])[0]["o"])
    except (KeyError, TypeError, ValueError):
        return None


def last_close(rows: list[dict]) -> float | None:
    if not rows:
        return None
    try:
        return float(sorted(rows, key=lambda x: x["t"])[-1]["c"])
    except (KeyError, TypeError, ValueError):
        return None


def vwap(rows: list[dict]) -> float | None:
    weighted = 0.0
    volume = 0.0
    for row in rows:
        try:
            px = float(row.get("vw", row["c"]))
            vol = float(row.get("v", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if vol > 0:
            weighted += px * vol
            volume += vol
    return None if volume <= 0 else weighted / volume


def pct(new: float, old: float) -> float:
    return 0.0 if old == 0 else (new / old - 1.0) * 100.0


def filled_qty(order: dict) -> int:
    try:
        return int(float(order.get("filled_qty") or 0))
    except (TypeError, ValueError):
        return 0


def filled_price(order: dict) -> float | None:
    try:
        value = order.get("filled_avg_price")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def todays_bot_positions(orders: list[dict], now_ny: datetime) -> dict[str, dict[str, Any]]:
    """
    Reconstruct only DELTAX ETF-owned quantity from today's filled bot orders.
    Manual seed positions are deliberately excluded.
    """
    day = now_ny.date()
    entries: dict[str, list[dict]] = {}
    exited_qty: dict[str, int] = {}

    for order in orders:
        filled_at = parse_dt(order.get("filled_at"))
        if not filled_at or filled_at.astimezone(NY).date() != day:
            continue
        if str(order.get("status") or "").lower() != "filled":
            continue

        cid = str(order.get("client_order_id") or "")
        symbol = str(order.get("symbol") or "").upper()
        qty = filled_qty(order)
        px = filled_price(order)

        if not symbol or qty <= 0:
            continue

        if cid.startswith(ENTRY_PREFIX) and px is not None:
            side = str(order.get("side") or "").lower()
            direction = "long" if side == "buy" else "short"
            entries.setdefault(symbol, []).append({
                "qty": qty,
                "price": px,
                "direction": direction,
                "filled_at": filled_at,
            })
        elif cid.startswith(EXIT_PREFIX):
            exited_qty[symbol] = exited_qty.get(symbol, 0) + qty

    result = {}

    for symbol, rows in entries.items():
        rows.sort(key=lambda x: x["filled_at"])
        direction = rows[0]["direction"]

        if any(r["direction"] != direction for r in rows):
            continue

        total_entry_qty = sum(r["qty"] for r in rows)
        remaining_qty = max(0, total_entry_qty - exited_qty.get(symbol, 0))
        if remaining_qty <= 0:
            continue

        avg_entry = sum(r["price"] * r["qty"] for r in rows) / total_entry_qty

        result[symbol] = {
            "symbol": symbol,
            "direction": direction,
            "qty": remaining_qty,
            "avg_entry": avg_entry,
            "entry_count": len(rows),
        }

    return result


def adopt_current_etf_positions(broker_positions: list[dict]) -> dict[str, dict[str, Any]]:
    """
    Treat every currently open ETF share position in the DELTAX ETF universe
    as if DELTAX had opened it.

    Options and non-ETF stock positions are ignored.
    Alpaca's broker average entry price becomes the DELTAX average entry.
    Existing positions count as one initial entry for pyramiding purposes.
    """
    result: dict[str, dict[str, Any]] = {}

    for pos in broker_positions:
        symbol = str(pos.get("symbol") or "").upper()
        if symbol not in MANAGED_ETFS:
            continue

        try:
            qty_raw = float(pos.get("qty") or 0)
            avg_entry = float(pos.get("avg_entry_price") or 0)
        except (TypeError, ValueError):
            continue

        if qty_raw == 0 or avg_entry <= 0:
            continue

        result[symbol] = {
            "symbol": symbol,
            "direction": "long" if qty_raw > 0 else "short",
            "qty": int(abs(qty_raw)),
            "avg_entry": avg_entry,
            "entry_count": 1,
            "adopted": True,
        }

    return result


def merge_managed_positions(
    adopted: dict[str, dict[str, Any]],
    bot_positions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Broker positions are the source of truth for qty/average price because Alpaca
    aggregates manual + bot fills by symbol. If a symbol already exists at broker,
    manage the whole ETF position. Bot history only adds attribution metadata.
    """
    result = dict(adopted)

    for symbol, bot in bot_positions.items():
        if symbol not in result:
            result[symbol] = dict(bot)
            result[symbol]["adopted"] = False
        else:
            result[symbol]["entry_count"] = max(
                int(result[symbol].get("entry_count", 1)),
                int(bot.get("entry_count", 0)) + 1,
            )
            result[symbol]["has_bot_fills"] = True

    return result


def has_open_exit_order(orders: list[dict], symbol: str) -> bool:
    for order in orders:
        cid = str(order.get("client_order_id") or "")
        status = str(order.get("status") or "").lower()
        if (
            cid.startswith(EXIT_PREFIX)
            and str(order.get("symbol") or "").upper() == symbol
            and status in {"new", "accepted", "pending_new", "partially_filled", "held"}
        ):
            return True
    return False


def ai_direction_for_symbol(regime_result: dict[str, Any], symbol: str) -> str | None:
    for item in regime_result.get("etf_biases", []):
        if item.get("symbol") == symbol and float(item.get("confidence", 0)) >= MIN_AI_CONFIDENCE:
            return item.get("direction")
        if symbol in item.get("subsector_candidates", []) and float(item.get("confidence", 0)) >= MIN_AI_CONFIDENCE:
            return item.get("direction")

    for item in regime_result.get("index_biases", []):
        if item.get("symbol") == symbol and float(item.get("confidence", 0)) >= MIN_AI_CONFIDENCE:
            return item.get("direction")

    return None


def technical_scores(api: Alpaca, symbols: list[str], now_ny: datetime) -> dict[str, dict[str, Any]]:
    """
    Return live intraday technical scores only once the regular session has started.

    Premarket:
    - do NOT request today's regular-session bars because 09:30 ET is still in the future;
    - return latest available prices so SL/TP and AI checks can still be evaluated;
    - technical exit is disabled until regular-session data exists.
    """
    today = now_ny.date()
    prev_day = previous_trading_day(today)
    prior_day = previous_trading_day(prev_day)

    prior_start, prior_end = session_bounds(prior_day)
    prev_start, prev_end = session_bounds(prev_day)
    today_start, today_end = session_bounds(today)

    snapshots = api.snapshots(symbols)

    # Before 09:30 ET there is no current regular-session VWAP/open to score.
    if now_ny < today_start:
        result: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            last = latest_price(snapshots.get(symbol) or {})
            if last is None:
                continue
            result[symbol] = {
                "price": last,
                "technical_available": False,
                "long_score": None,
                "short_score": None,
            }
        return result

    bars_prior = api.bars(symbols, prior_start, prior_end)
    bars_prev = api.bars(symbols, prev_start, prev_end)
    bars_today = api.bars(symbols, today_start, min(now_ny, today_end))

    raw: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        prior_close = last_close(bars_prior.get(symbol, []))
        prev_close = last_close(bars_prev.get(symbol, []))
        rows_today = bars_today.get(symbol, [])
        open_px = first_open(rows_today)
        current_vwap = vwap(rows_today)
        last = latest_price(snapshots.get(symbol) or {})

        if None in (prior_close, prev_close, open_px, current_vwap, last):
            continue

        raw[symbol] = {
            "price": last,
            "technical_available": True,
            "prev_to_now": pct(last, prev_close),
            "open_to_now": pct(last, open_px),
            "vwap_to_now": pct(last, current_vwap),
            "prior_momentum": pct(prev_close, prior_close),
        }

    spy_move = raw.get("SPY", {}).get("prev_to_now", 0.0)

    for symbol, item in raw.items():
        item["relative_spy"] = item["prev_to_now"] - spy_move
        values = [
            item["prev_to_now"],
            item["open_to_now"],
            item["vwap_to_now"],
            item["relative_spy"],
            item["prior_momentum"],
        ]
        item["long_score"] = sum(1 for x in values if x > 0)
        item["short_score"] = sum(1 for x in values if x < 0)

    return raw

def exit_reason(
    position: dict[str, Any],
    current_price: float,
    technical: dict[str, Any] | None,
    ai_direction: str | None,
    now_ny: datetime,
) -> str | None:
    direction = position["direction"]
    avg_entry = float(position["avg_entry"])

    pnl_pct = (
        (current_price / avg_entry - 1.0)
        if direction == "long"
        else (avg_entry / current_price - 1.0)
    )

    # Hard exits first.
    if pnl_pct <= -STOP_LOSS_PCT:
        return "SL"
    if pnl_pct >= TAKE_PROFIT_PCT:
        return "TP"

    # Opposite AI direction = immediate exit.
    if ai_direction is not None and ai_direction != direction:
        return "AI"

    # Neutral / absent AI = HOLD.
    if technical is not None and technical.get("technical_available"):
        score = technical["long_score"] if direction == "long" else technical["short_score"]
        if score is not None and score <= TECHNICAL_EXIT_MAX_SCORE:
            return "TECH"

    if now_ny.time() >= EOD_EXIT_TIME:
        return "EOD"

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="DELTAX ETF intraday exit manager.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit EVENT paper exit orders. Without this flag: check only.",
    )
    args = parser.parse_args()

    cfg = load_env()
    api = Alpaca(cfg)
    clock = api.clock()
    now_ny = datetime.now(timezone.utc).astimezone(NY)

    orders = api.orders("all", 500)
    broker_positions = api.positions()
    bot_positions = todays_bot_positions(orders, now_ny)
    adopted_positions = adopt_current_etf_positions(broker_positions)
    positions = merge_managed_positions(adopted_positions, bot_positions)

    print("=" * 110)
    print("DELTAX ETF EXIT MANAGER v2.1")
    print("=" * 110)
    print(f"Mode:      {'EXECUTE' if args.execute else 'CHECK ONLY'}")
    print(f"NY time:   {now_ny.isoformat()}")
    print(f"Open:      {bool(clock.get('is_open'))}")
    print(f"Managed ETFs: {len(positions)}")
    print(f"Adopted now:  {sum(1 for p in positions.values() if p.get('adopted'))}")
    print()

    if not positions:
        print("No open ETF share positions from the DELTAX ETF universe found in the EVENT account.")
        return 0

    symbols = sorted(set(positions) | {"SPY"})
    technical = technical_scores(api, symbols, now_ny)

    events = regime.load_completed_market_ai(cfg["database_url"], regime.DEFAULT_SINCE_HOURS)
    regime_result = regime.aggregate(events)

    actions = []

    for symbol, position in sorted(positions.items()):
        tech = technical.get(symbol)
        if not tech:
            print(f"{symbol}: SKIP - technical data unavailable.")
            continue

        current_price = float(tech["price"])
        ai_dir = ai_direction_for_symbol(regime_result, symbol)
        reason = exit_reason(position, current_price, tech, ai_dir, now_ny)

        pnl_pct = (
            (current_price / position["avg_entry"] - 1.0) * 100.0
            if position["direction"] == "long"
            else (position["avg_entry"] / current_price - 1.0) * 100.0
        )
        if tech.get("technical_available"):
            score = tech["long_score"] if position["direction"] == "long" else tech["short_score"]
            score_text = f"{score}/5"
        else:
            score = None
            score_text = "N/A premarket"

        print(
            f"{symbol:<5} {position['direction'].upper():<5} "
            f"qty={position['qty']:<4} avg={position['avg_entry']:.2f} now={current_price:.2f} "
            f"P/L={pnl_pct:+.2f}% AI={ai_dir or 'neutral'} TECH={score_text} "
            f"=> {reason or 'HOLD'}"
        )

        if not reason:
            continue

        if has_open_exit_order(orders, symbol):
            print(f"      SKIP - exit order already open.")
            continue

        action = {
            "symbol": symbol,
            "qty": position["qty"],
            "direction": position["direction"],
            "reason": reason,
            "pnl_pct": round(pnl_pct, 4),
        }

        if args.execute:
            if not clock.get("is_open"):
                action["status"] = "NOT_SUBMITTED_MARKET_CLOSED"
            else:
                order = api.submit_exit(
                    symbol=symbol,
                    qty=position["qty"],
                    direction=position["direction"],
                    reason=reason,
                )
                action["status"] = order.get("status")
                action["order_id"] = order.get("id")
                action["client_order_id"] = order.get("client_order_id")
        else:
            action["status"] = "CHECK_ONLY"

        actions.append(action)

    print()
    print("ACTIONS")
    print(json.dumps(actions, indent=2, default=str))
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
