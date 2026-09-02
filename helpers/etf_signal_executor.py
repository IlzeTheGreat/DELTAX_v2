from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row

# Reuse ETF regime logic from helper.
import etf_ai_regime as regime


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

NY = ZoneInfo("America/New_York")
DATA_BASE = "https://data.alpaca.markets"
TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
REQUEST_TIMEOUT = 20

BOT_PREFIX = "dxe-etf-"
EXIT_PREFIX = "dxe-etfx-"
DEFAULT_NOTIONAL_PER_TRADE = 4000.0
MAX_NEW_TRADES_PER_RUN = 5
MIN_AI_CONFIDENCE = 0.65
MIN_PRICE_SCORE = 4
ENTRY_DELAY_MINUTES = 10
NO_NEW_ENTRY_BEFORE_CLOSE_MINUTES = 30

# Sector and ETF universe.
ETF_NAMES = regime.ETF_NAMES
CORE = set(regime.SECTOR_TO_ETF.values())
FOCUSED = {"SMH", "IGV", "CIBR", "XBI", "IHI", "KRE", "IAI", "ITA", "XOP", "USO"}
INDEX = {"SPY", "QQQ", "IWM", "DIA"}
UNIVERSE = sorted(set(ETF_NAMES) | INDEX)

# Directional candidate expansion from regime output.
def ai_candidates(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    # Core ETF signals
    for item in result.get("etf_biases", []):
        if item.get("direction") not in {"long", "short"}:
            continue
        conf = float(item.get("confidence", 0))
        if conf < MIN_AI_CONFIDENCE:
            continue
        out[item["symbol"]] = {
            "direction": item["direction"],
            "confidence": conf,
            "source": "core",
            "parent": item["symbol"],
        }

        # Focused candidates inherit parent direction/confidence, but still
        # require independent price confirmation.
        for symbol in item.get("subsector_candidates", []):
            out[symbol] = {
                "direction": item["direction"],
                "confidence": conf,
                "source": "focused",
                "parent": item["symbol"],
            }

    # Index candidates
    for item in result.get("index_biases", []):
        if item.get("direction") not in {"long", "short"}:
            continue
        conf = float(item.get("confidence", 0))
        if conf < MIN_AI_CONFIDENCE:
            continue
        out[item["symbol"]] = {
            "direction": item["direction"],
            "confidence": conf,
            "source": "index",
            "parent": item["symbol"],
        }

    return out


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

    def account(self) -> dict:
        return self._get(f"{self.cfg['trading']}/account")

    def asset(self, symbol: str) -> dict:
        return self._get(f"{self.cfg['trading']}/assets/{symbol}")

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
            chunk = symbols[i:i+100]
            data = self._get(
                f"{DATA_BASE}/v2/stocks/snapshots",
                params={"symbols": ",".join(chunk), "feed": self.cfg["feed"]},
            )
            result.update(data)
        return result

    def bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
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
            data = self._get(f"{DATA_BASE}/v2/stocks/bars", params)
            for symbol, rows in (data.get("bars") or {}).items():
                output.setdefault(symbol, []).extend(rows)
            token = data.get("next_page_token")
            if not token:
                break
        return output

    def submit_market_order(self, symbol: str, qty: int, direction: str, client_order_id: str) -> dict:
        side = "buy" if direction == "long" else "sell"
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        return self._post(f"{self.cfg['trading']}/orders", payload)


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


def price_confirmations(api: Alpaca, symbols: list[str]) -> dict[str, dict[str, Any]]:
    now_ny = datetime.now(timezone.utc).astimezone(NY)
    today = now_ny.date()
    prev_day = previous_trading_day(today)
    prior_day = previous_trading_day(prev_day)

    today_start, today_end = session_bounds(today)
    if now_ny < today_start + timedelta(minutes=ENTRY_DELAY_MINUTES):
        raise RuntimeError(
            f"Live confirmation starts at {(today_start + timedelta(minutes=ENTRY_DELAY_MINUTES)).isoformat()}"
        )
    if now_ny >= today_end - timedelta(minutes=NO_NEW_ENTRY_BEFORE_CLOSE_MINUTES):
        raise RuntimeError("New entries are blocked during the last 30 minutes before close.")

    snapshots = api.snapshots(symbols)

    prior_start, prior_end = session_bounds(prior_day)
    prev_start, prev_end = session_bounds(prev_day)

    prior_bars = api.bars(symbols, "5Min", prior_start, prior_end)
    prev_bars = api.bars(symbols, "5Min", prev_start, prev_end)
    today_bars = api.bars(symbols, "5Min", today_start, min(now_ny, today_end))

    raw = {}
    for symbol in symbols:
        snap = snapshots.get(symbol) or {}
        last = latest_price(snap)
        open_px = daily_open(snap)
        prev_close = session_close(prev_bars.get(symbol, []))
        prior_close = session_close(prior_bars.get(symbol, []))
        vwap = bar_vwap(today_bars.get(symbol, []))

        if not all(x is not None for x in (last, open_px, prev_close, prior_close, vwap)):
            continue

        raw[symbol] = {
            "price": last,
            "prev_to_now": pct(last, prev_close),
            "open_to_now": pct(last, open_px),
            "vwap_to_now": pct(last, vwap),
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


def current_managed_etf_symbols(api: Alpaca) -> set[str]:
    managed = set(regime.ETF_NAMES) | {"SPY", "QQQ", "IWM", "DIA"}
    symbols: set[str] = set()
    try:
        positions = api.positions()
    except Exception:
        return symbols

    for pos in positions:
        symbol = str(pos.get("symbol") or "").upper()
        try:
            qty = float(pos.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if symbol in managed and qty != 0:
            symbols.add(symbol)
    return symbols


def today_bot_state(api: Alpaca) -> dict[str, dict[str, Any]]:
    """
    Track only DELTAX ETF orders for the current NY trading day.

    Re-entry rules:
    - max 3 entry orders per ETF/day
    - after SL: locked for rest of day
    - after TP: one re-entry episode allowed
    - after AI/TECH/EOD exit: locked for rest of day
    - manual seed positions are ignored
    """
    now_ny = datetime.now(timezone.utc).astimezone(NY)
    today = now_ny.date()
    state: dict[str, dict[str, Any]] = {}

    def get_state(symbol: str) -> dict[str, Any]:
        return state.setdefault(symbol, {
            "entries": 0,
            "tp_exits": 0,
            "locked": False,
            "lock_reason": None,
        })

    for order in api.orders("all", 500):
        cid = str(order.get("client_order_id") or "")
        symbol = str(order.get("symbol") or "").upper()
        status = str(order.get("status") or "").lower()
        created_at_raw = order.get("created_at")
        if not symbol or not created_at_raw:
            continue

        try:
            created_at = datetime.fromisoformat(
                str(created_at_raw).replace("Z", "+00:00")
            ).astimezone(NY)
        except ValueError:
            continue

        if created_at.date() != today:
            continue

        s = get_state(symbol)

        if cid.startswith(BOT_PREFIX):
            if status in {
                "new", "accepted", "pending_new", "partially_filled",
                "filled", "held", "pending_replace", "replaced"
            }:
                s["entries"] += 1
            continue

        if not cid.startswith(EXIT_PREFIX):
            continue

        cid_lower = cid.lower()

        if "-sl" in cid_lower:
            s["locked"] = True
            s["lock_reason"] = "STOP_LOSS"
        elif "-tp" in cid_lower:
            s["tp_exits"] += 1
            # One re-entry is allowed after the first TP exit.
            if s["tp_exits"] >= 2:
                s["locked"] = True
                s["lock_reason"] = "SECOND_TAKE_PROFIT"
        elif any(token in cid_lower for token in ("-ai", "-tech", "-eod")):
            s["locked"] = True
            s["lock_reason"] = "THESIS_OR_EOD_EXIT"

    return state

def make_client_order_id(symbol: str, direction: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    d = "l" if direction == "long" else "s"
    return f"{BOT_PREFIX}{stamp}-{symbol.lower()}-{d}"[:48]


def main() -> int:
    parser = argparse.ArgumentParser(description="DELTAX ETF AI x PRICE executor")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit EVENT paper orders. Without this flag, read-only.",
    )
    parser.add_argument(
        "--notional",
        type=float,
        default=DEFAULT_NOTIONAL_PER_TRADE,
        help=f"USD notional per new ETF trade. Default {DEFAULT_NOTIONAL_PER_TRADE:.0f}.",
    )
    parser.add_argument(
        "--max-trades",
        type=int,
        default=MAX_NEW_TRADES_PER_RUN,
        help=f"Maximum new orders per cycle. Default {MAX_NEW_TRADES_PER_RUN}.",
    )
    args = parser.parse_args()

    if args.notional <= 0:
        raise RuntimeError("--notional must be > 0")
    if not 1 <= args.max_trades <= 10:
        raise RuntimeError("--max-trades must be between 1 and 10")

    cfg = load_env()
    api = Alpaca(cfg)

    account = api.account()
    clock = api.clock()

    if not clock.get("is_open"):
        raise RuntimeError("US regular market is not open.")

    # 1) Existing DELTAX AI regime.
    events = regime.load_completed_market_ai(cfg["database_url"], regime.DEFAULT_SINCE_HOURS)
    regime_result = regime.aggregate(events)
    candidates = ai_candidates(regime_result)

    # SPY is required for relative-strength comparison even if AI did not select it.
    symbols_for_price = sorted(set(candidates) | {"SPY"})
    price = price_confirmations(api, symbols_for_price)

    bot_state = today_bot_state(api)
    adopted_symbols = current_managed_etf_symbols(api)

    intersections = []

    for symbol, ai in candidates.items():
        p = price.get(symbol)
        if not p:
            continue

        direction = ai["direction"]
        score = p["long_score"] if direction == "long" else p["short_score"]

        if score < MIN_PRICE_SCORE:
            continue

        # Defensive asset check before shorting.
        asset = api.asset(symbol)
        if not asset.get("tradable"):
            continue
        if direction == "short" and not asset.get("shortable"):
            continue

        intersections.append({
            "symbol": symbol,
            "direction": direction,
            "ai_confidence": ai["confidence"],
            "ai_source": ai["source"],
            "parent": ai["parent"],
            "price_score": score,
            **p,
        })

    # Prefer strongest AI + strongest price confirmation + larger move.
    intersections.sort(
        key=lambda x: (
            -x["ai_confidence"],
            -x["price_score"],
            -abs(x["relative_spy"]),
        )
    )

    print("=" * 112)
    print("DELTAX ETF AI x PRICE EXECUTOR v1")
    print("=" * 112)
    print(f"Mode:              {'EXECUTE' if args.execute else 'CHECK ONLY'}")
    print(f"Regime:            {regime_result['regime']}")
    print(f"Regime confidence: {regime_result['regime_confidence']:.2f}")
    print(f"EVENT equity:      ${float(account.get('equity', 0)):,.2f}")
    print(f"Notional/trade:    ${args.notional:,.2f}")
    print(f"Max new trades:    {args.max_trades}\nMax entries/ETF:   3")
    print()

    print("QUALIFYING INTERSECTIONS")
    if not intersections:
        print("None")
    for x in intersections:
        print(
            f"{x['symbol']:<5} {x['direction'].upper():<5} | "
            f"AI {x['ai_confidence']:.2f} | price {x['price_score']}/5 | "
            f"prev {x['prev_to_now']:+.2f}% | open {x['open_to_now']:+.2f}% | "
            f"VWAP {x['vwap_to_now']:+.2f}% | relSPY {x['relative_spy']:+.2f}%"
        )

    submitted = []
    skipped = []

    for item in intersections:
        if len(submitted) >= args.max_trades:
            break

        symbol = item["symbol"]

        # DELTAX may enter the same ETF up to 3 times per NY trading day.
        # Manual seed positions do not count.
        state = bot_state.get(symbol, {
            "entries": 0,
            "tp_exits": 0,
            "locked": False,
            "lock_reason": None,
        }).copy()

        # Any currently open ETF position in the EVENT account is adopted as
        # DELTAX entry #1. Bot entries then build on top of it, up to 3 total.
        if symbol in adopted_symbols:
            state["entries"] = max(1, state["entries"] + 1)

        if state["locked"]:
            skipped.append({
                "symbol": symbol,
                "reason": f"locked for day after {state['lock_reason']}"
            })
            continue

        if state["entries"] >= 3:
            skipped.append({
                "symbol": symbol,
                "reason": f"DELTAX ETF max 3 entries reached ({state['entries']}/3)"
            })
            continue

        price_now = float(item["price"])
        qty = int(math.floor(args.notional / price_now))
        if qty < 1:
            skipped.append({"symbol": symbol, "reason": "notional too small"})
            continue

        cid = make_client_order_id(symbol, item["direction"])

        preview = {
            "symbol": symbol,
            "direction": item["direction"],
            "qty": qty,
            "approx_notional": round(qty * price_now, 2),
            "client_order_id": cid,
        }

        if not args.execute:
            preview["status"] = "CHECK_ONLY"
            submitted.append(preview)
            continue

        order = api.submit_market_order(
            symbol=symbol,
            qty=qty,
            direction=item["direction"],
            client_order_id=cid,
        )
        preview["status"] = order.get("status")
        preview["order_id"] = order.get("id")
        submitted.append(preview)

    print()
    print("ORDERS / PREVIEW")
    print(json.dumps(submitted, indent=2, default=str))

    if skipped:
        print()
        print("SKIPPED")
        print(json.dumps(skipped, indent=2, default=str))

    print()
    print("JSON_RESULT")
    print(json.dumps({
        "regime": regime_result["regime"],
        "regime_confidence": regime_result["regime_confidence"],
        "qualifying_intersections": intersections,
        "orders": submitted,
        "skipped": skipped,
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
