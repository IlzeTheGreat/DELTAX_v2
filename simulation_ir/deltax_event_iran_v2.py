from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


# ============================================================
# DELTAX EVENT - IRAN PLAYBOOK V2
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

ENV_PATH = ROOT_DIR / ".env"
STATE_FILE = SCRIPT_DIR / "deltax_event_iran_v2_state.json"

NY = ZoneInfo("America/New_York")

TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets"

# Fixed historical Iran watchlist
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

OPTION_FIRST = {
    "WFC",
    "BX",
    "BAC",
    "APP",
    "XYZ",
    "WDAY",
    "LRCX",
    "F",
    "LITE",
    "COHR",
}

STOCK_FIRST = {
    "APO",
    "FFIV",
    "LYB",
    "TEL",
    "MAS",
}

WATCHLIST = sorted(LONG_SYMBOLS | SHORT_SYMBOLS)

# Backtested entry rule
MIN_EVENT_GAP = 0.0050          # 0.50%
MIN_REVERSAL_10M = 0.0025       # 0.25%

ENTRY_DECISION_TIME = dt_time(9, 40)
ENTRY_CUTOFF_TIME = dt_time(10, 00)
EXIT_TIME = dt_time(15, 50)

# Position sizing
RISK_PER_TRADE = 0.01            # 1% of equity
MAX_TOTAL_NEW_OPTION_PREMIUM = 0.05
MAX_CONCURRENT_NEW_TRADES = 999

# Options
TARGET_DTE = 7
MIN_DTE = 5
MAX_DTE = 10
TARGET_OTM_PCT = 0.01            # ~1% OTM
MAX_OPTION_SPREAD_PCT = 0.20     # skip very ugly quotes
MIN_OPTION_ASK = 0.05
MAX_OPTION_ASK = 50.0

REQUEST_TIMEOUT = 20


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class Signal:
    symbol: str
    direction: str
    previous_close: float
    today_open: float
    price_0940: float
    event_gap: float
    reversal_10m: float


# ============================================================
# ENV / HTTP
# ============================================================

def load_environment() -> dict[str, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    key = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
    trading_url = (
        os.getenv("ALPACA_TRADING_URL_EVENT")
        or TRADING_BASE_DEFAULT
    ).strip().rstrip("/")
    feed = (os.getenv("ALPACA_DATA_FEED_EVENT") or "iex").strip()

    if not key:
        raise RuntimeError("Missing ALPACA_API_KEY_EVENT in .env")
    if not secret:
        raise RuntimeError("Missing ALPACA_API_SECRET_EVENT in .env")

    # We use raw REST, so /v2 is correct here.
    if not trading_url.endswith("/v2"):
        trading_url = trading_url + "/v2"

    return {
        "key": key,
        "secret": secret,
        "trading_url": trading_url,
        "feed": feed,
    }


class Alpaca:
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

    def _post(self, url: str, payload: dict) -> Any:
        r = requests.post(
            url,
            headers={
                **self.headers,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(
                f"POST {url} failed: {r.status_code} {r.text[:500]}"
            )
        return r.json()

    def _delete(self, url: str) -> Any:
        r = requests.delete(
            url,
            headers=self.headers,
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code not in (200, 204):
            raise RuntimeError(
                f"DELETE {url} failed: {r.status_code} {r.text[:500]}"
            )
        return None if not r.text else r.json()

    # ---------- Trading ----------

    def account(self) -> dict:
        return self._get(f"{self.cfg['trading_url']}/account")

    def clock(self) -> dict:
        return self._get(f"{self.cfg['trading_url']}/clock")

    def positions(self) -> list[dict]:
        return self._get(f"{self.cfg['trading_url']}/positions")

    def orders(self, status: str = "open") -> list[dict]:
        return self._get(
            f"{self.cfg['trading_url']}/orders",
            params={
                "status": status,
                "limit": 500,
                "direction": "desc",
            },
        )

    def submit_market_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        client_order_id: str,
    ) -> dict:
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        return self._post(
            f"{self.cfg['trading_url']}/orders",
            payload,
        )

    def close_position(self, symbol: str) -> Any:
        return self._delete(
            f"{self.cfg['trading_url']}/positions/{symbol}"
        )

    # ---------- Stock market data ----------

    def stock_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> dict:
        data = self._get(
            f"{DATA_BASE}/v2/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": start.astimezone(timezone.utc).isoformat(),
                "end": end.astimezone(timezone.utc).isoformat(),
                "adjustment": "raw",
                "feed": self.cfg["feed"],
                "limit": 10000,
            },
        )
        return data.get("bars", {})

    # ---------- Options ----------

    def option_contracts(
        self,
        underlying: str,
        option_type: str,
        expiration_gte: date,
        expiration_lte: date,
        strike_low: float,
        strike_high: float,
    ) -> list[dict]:
        out: list[dict] = []
        token = None

        while True:
            params = {
                "underlying_symbols": underlying,
                "status": "active",
                "type": option_type,
                "expiration_date_gte": expiration_gte.isoformat(),
                "expiration_date_lte": expiration_lte.isoformat(),
                "strike_price_gte": f"{strike_low:.2f}",
                "strike_price_lte": f"{strike_high:.2f}",
                "limit": 1000,
            }
            if token:
                params["page_token"] = token

            data = self._get(
                f"{self.cfg['trading_url']}/options/contracts",
                params=params,
            )
            out.extend(data.get("option_contracts", []))
            token = data.get("next_page_token")
            if not token:
                break

        return out

    def option_snapshots(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}

        result: dict[str, Any] = {}

        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = self._get(
                f"{DATA_BASE}/v1beta1/options/snapshots",
                params={
                    "symbols": ",".join(chunk),
                    # Don't force opra. Alpaca will use the account's available feed.
                    "limit": 100,
                },
            )
            result.update(data.get("snapshots", {}))

        return result


# ============================================================
# STATE
# ============================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "version": 2,
            "days": {},
        }

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {
            "version": 2,
            "days": {},
        }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )


def day_state(state: dict, trading_day: date) -> dict:
    key = trading_day.isoformat()
    return state.setdefault("days", {}).setdefault(
        key,
        {
            "signals": {},
            "orders": {},
            "exits_done": False,
        },
    )


# ============================================================
# MARKET DATA LOGIC
# ============================================================

def parse_bar_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(NY)


def get_previous_close(
    alpaca: Alpaca,
    symbol: str,
    trading_day: date,
) -> float | None:
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

    bars = alpaca.stock_bars(
        [symbol],
        "1Day",
        start,
        end,
    ).get(symbol, [])

    valid = []

    for bar in bars:
        ts = parse_bar_time(bar["t"])
        if ts.date() < trading_day:
            valid.append(bar)

    if not valid:
        return None

    return float(valid[-1]["c"])


def get_open_and_0940(
    alpaca: Alpaca,
    symbol: str,
    trading_day: date,
) -> tuple[float, float] | None:
    start = datetime.combine(
        trading_day,
        dt_time(9, 30),
        tzinfo=NY,
    )
    end = datetime.combine(
        trading_day,
        dt_time(9, 41),
        tzinfo=NY,
    )

    bars = alpaca.stock_bars(
        [symbol],
        "1Min",
        start,
        end,
    ).get(symbol, [])

    if not bars:
        return None

    bars = sorted(bars, key=lambda b: b["t"])

    opening_bar = None
    close_0940 = None

    for bar in bars:
        ts = parse_bar_time(bar["t"])
        if ts.hour == 9 and ts.minute == 30:
            opening_bar = bar
        # 09:39 bar closes at 09:40.
        if ts.hour == 9 and ts.minute == 39:
            close_0940 = bar

    if opening_bar is None or close_0940 is None:
        return None

    return (
        float(opening_bar["o"]),
        float(close_0940["c"]),
    )


def build_signal(
    alpaca: Alpaca,
    symbol: str,
    trading_day: date,
) -> Signal | None:
    previous_close = get_previous_close(
        alpaca,
        symbol,
        trading_day,
    )
    if previous_close is None or previous_close <= 0:
        return None

    pair = get_open_and_0940(
        alpaca,
        symbol,
        trading_day,
    )
    if pair is None:
        return None

    today_open, price_0940 = pair
    if today_open <= 0:
        return None

    event_gap = today_open / previous_close - 1.0
    reversal_10m = price_0940 / today_open - 1.0

    direction = "LONG" if symbol in LONG_SYMBOLS else "SHORT"

    if direction == "LONG":
        gap_ok = event_gap >= MIN_EVENT_GAP
        reversal_ok = reversal_10m <= -MIN_REVERSAL_10M
    else:
        gap_ok = event_gap <= -MIN_EVENT_GAP
        reversal_ok = reversal_10m >= MIN_REVERSAL_10M

    if not (gap_ok and reversal_ok):
        return None

    return Signal(
        symbol=symbol,
        direction=direction,
        previous_close=previous_close,
        today_open=today_open,
        price_0940=price_0940,
        event_gap=event_gap,
        reversal_10m=reversal_10m,
    )


# ============================================================
# OPTION SELECTION
# ============================================================

def quote_mid(snapshot: dict) -> tuple[float, float, float] | None:
    q = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    bid = q.get("bp", q.get("bid_price"))
    ask = q.get("ap", q.get("ask_price"))

    try:
        bid = float(bid)
        ask = float(ask)
    except (TypeError, ValueError):
        return None

    if bid < 0 or ask <= 0 or ask < bid:
        return None

    mid = (bid + ask) / 2.0
    return bid, ask, mid


def choose_option(
    alpaca: Alpaca,
    signal: Signal,
    trading_day: date,
) -> dict | None:
    option_type = "call" if signal.direction == "LONG" else "put"

    min_exp = trading_day + timedelta(days=MIN_DTE)
    max_exp = trading_day + timedelta(days=MAX_DTE)

    spot = signal.price_0940

    # Broad enough to catch nearby strikes.
    strike_low = max(0.01, spot * 0.90)
    strike_high = spot * 1.10

    contracts = alpaca.option_contracts(
        signal.symbol,
        option_type,
        min_exp,
        max_exp,
        strike_low,
        strike_high,
    )

    if not contracts:
        return None

    target_strike = (
        spot * (1.0 + TARGET_OTM_PCT)
        if signal.direction == "LONG"
        else spot * (1.0 - TARGET_OTM_PCT)
    )

    def contract_key(c: dict):
        exp = date.fromisoformat(c["expiration_date"])
        dte_distance = abs((exp - trading_day).days - TARGET_DTE)
        strike = float(c["strike_price"])
        strike_distance = abs(strike - target_strike) / spot
        return (dte_distance, strike_distance)

    contracts = sorted(contracts, key=contract_key)[:40]

    symbols = [
        c.get("symbol")
        for c in contracts
        if c.get("symbol")
    ]

    snapshots = alpaca.option_snapshots(symbols)

    candidates = []

    for c in contracts:
        sym = c.get("symbol")
        if not sym:
            continue

        snap = snapshots.get(sym)
        if not snap:
            continue

        qm = quote_mid(snap)
        if qm is None:
            continue

        bid, ask, mid = qm

        if ask < MIN_OPTION_ASK or ask > MAX_OPTION_ASK:
            continue

        spread_pct = (
            (ask - bid) / mid
            if mid > 0
            else float("inf")
        )

        if spread_pct > MAX_OPTION_SPREAD_PCT:
            continue

        exp = date.fromisoformat(c["expiration_date"])
        strike = float(c["strike_price"])

        score = (
            abs((exp - trading_day).days - TARGET_DTE) * 10.0
            + abs(strike - target_strike) / spot * 100.0
            + spread_pct * 5.0
        )

        candidates.append(
            {
                "symbol": sym,
                "underlying": signal.symbol,
                "type": option_type,
                "expiration_date": exp.isoformat(),
                "strike": strike,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_pct": spread_pct,
                "estimated_contract_cost": ask * 100.0,
                "score": score,
            }
        )

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score"])
    return candidates[0]


# ============================================================
# POSITION SIZING / EXECUTION
# ============================================================

def equity_and_buying_power(account: dict) -> tuple[float, float]:
    equity = float(account.get("equity") or 0)
    buying_power = float(account.get("buying_power") or 0)
    return equity, buying_power


def current_option_premium_exposure(
    positions: list[dict],
) -> float:
    total = 0.0

    for p in positions:
        asset_class = (p.get("asset_class") or "").lower()
        if "option" not in asset_class:
            continue

        try:
            market_value = abs(float(p.get("market_value") or 0))
        except (TypeError, ValueError):
            market_value = 0.0

        total += market_value

    return total


def qty_for_stock(
    equity: float,
    buying_power: float,
    price: float,
) -> int:
    budget = min(
        equity * RISK_PER_TRADE,
        buying_power * 0.10,
    )
    return max(0, int(budget // price))


def qty_for_option(
    equity: float,
    buying_power: float,
    contract_cost: float,
    current_option_exposure: float,
) -> int:
    per_trade_budget = equity * RISK_PER_TRADE
    total_cap = equity * MAX_TOTAL_NEW_OPTION_PREMIUM
    remaining_option_budget = max(
        0.0,
        total_cap - current_option_exposure,
    )

    budget = min(
        per_trade_budget,
        remaining_option_budget,
        buying_power * 0.10,
    )

    if contract_cost <= 0:
        return 0

    return max(
        0,
        int(budget // contract_cost),
    )


def client_order_id(
    trading_day: date,
    underlying: str,
    instrument_kind: str,
) -> str:
    day = trading_day.strftime("%Y%m%d")
    return f"dxir2-{day}-{underlying}-{instrument_kind}"[:48]


def execute_signal(
    alpaca: Alpaca,
    signal: Signal,
    state_day: dict,
    execute: bool,
) -> dict:
    if signal.symbol in state_day["orders"]:
        return {
            "status": "SKIP_DUPLICATE",
            "symbol": signal.symbol,
        }

    account = alpaca.account()
    positions = alpaca.positions()

    equity, buying_power = equity_and_buying_power(account)

    result: dict[str, Any] = {
        "symbol": signal.symbol,
        "direction": signal.direction,
        "event_gap": signal.event_gap,
        "reversal_10m": signal.reversal_10m,
        "paper_execute": execute,
    }

    # -----------------------------------------
    # OPTION-FIRST
    # -----------------------------------------
    if signal.symbol in OPTION_FIRST:
        option = choose_option(
            alpaca,
            signal,
            datetime.now(NY).date(),
        )

        if option:
            current_exposure = current_option_premium_exposure(
                positions
            )

            qty = qty_for_option(
                equity,
                buying_power,
                option["estimated_contract_cost"],
                current_exposure,
            )

            if qty >= 1:
                result["instrument"] = "OPTION"
                result["option"] = option
                result["qty"] = qty
                result["side"] = "buy"

                if execute:
                    oid = client_order_id(
                        datetime.now(NY).date(),
                        signal.symbol,
                        "opt",
                    )
                    order = alpaca.submit_market_order(
                        option["symbol"],
                        qty,
                        "buy",
                        oid,
                    )
                    result["order_id"] = order.get("id")
                    result["client_order_id"] = order.get(
                        "client_order_id"
                    )
                else:
                    result["status"] = "DRY_RUN"

                return result

            result["option_skip_reason"] = (
                "Selected option exceeds sizing budget"
            )
        else:
            result["option_skip_reason"] = (
                "No suitable liquid option found"
            )

    # -----------------------------------------
    # STOCK-FIRST or OPTION FALLBACK
    # -----------------------------------------
    qty = qty_for_stock(
        equity,
        buying_power,
        signal.price_0940,
    )

    if qty < 1:
        result["status"] = "SKIP_NO_BUDGET"
        return result

    side = "buy" if signal.direction == "LONG" else "sell"

    result["instrument"] = "STOCK"
    result["qty"] = qty
    result["side"] = side

    if execute:
        oid = client_order_id(
            datetime.now(NY).date(),
            signal.symbol,
            "stk",
        )
        order = alpaca.submit_market_order(
            signal.symbol,
            qty,
            side,
            oid,
        )
        result["order_id"] = order.get("id")
        result["client_order_id"] = order.get(
            "client_order_id"
        )
    else:
        result["status"] = "DRY_RUN"

    return result


# ============================================================
# EXIT MANAGEMENT
# ============================================================

def close_event_positions(
    alpaca: Alpaca,
    state_day: dict,
    execute: bool,
) -> list[dict]:
    results = []

    if state_day.get("exits_done"):
        return results

    positions = {
        p["symbol"]: p
        for p in alpaca.positions()
    }

    for underlying, order_info in list(
        state_day.get("orders", {}).items()
    ):
        instrument_symbol = (
            order_info.get("option", {}).get("symbol")
            if order_info.get("instrument") == "OPTION"
            else underlying
        )

        if not instrument_symbol:
            continue

        if instrument_symbol not in positions:
            results.append(
                {
                    "underlying": underlying,
                    "instrument_symbol": instrument_symbol,
                    "status": "ALREADY_CLOSED_OR_NOT_FILLED",
                }
            )
            continue

        if execute:
            try:
                alpaca.close_position(
                    instrument_symbol
                )
                status = "CLOSE_SUBMITTED"
            except Exception as exc:
                status = f"CLOSE_ERROR: {exc}"
        else:
            status = "DRY_RUN_CLOSE"

        results.append(
            {
                "underlying": underlying,
                "instrument_symbol": instrument_symbol,
                "status": status,
            }
        )

    if execute:
        state_day["exits_done"] = True

    return results


# ============================================================
# RUNNER
# ============================================================

def show_config(account: dict, clock: dict) -> None:
    print("=" * 92)
    print("DELTAX EVENT - IRAN PLAYBOOK V2")
    print("=" * 92)
    print(f"Account:              {account.get('account_number', 'n/a')}")
    print(f"Equity:               ${float(account.get('equity') or 0):,.2f}")
    print(f"Buying power:         ${float(account.get('buying_power') or 0):,.2f}")
    print(f"Market open:          {clock.get('is_open')}")
    print(f"Clock timestamp:      {clock.get('timestamp')}")
    print(f"Watchlist size:       {len(WATCHLIST)}")
    print(f"LONG:                 {', '.join(sorted(LONG_SYMBOLS))}")
    print(f"SHORT:                {', '.join(sorted(SHORT_SYMBOLS))}")
    print(f"Gap threshold:        {MIN_EVENT_GAP * 100:.2f}%")
    print(f"10m reversal:         {MIN_REVERSAL_10M * 100:.2f}%")
    print(f"Decision time:        {ENTRY_DECISION_TIME.strftime('%H:%M')} ET")
    print(f"Exit time:            {EXIT_TIME.strftime('%H:%M')} ET")
    print(f"Risk/trade:           {RISK_PER_TRADE * 100:.1f}% equity")
    print(f"Max option exposure:  {MAX_TOTAL_NEW_OPTION_PREMIUM * 100:.1f}% equity")
    print(f"Max new trades/day:   {MAX_CONCURRENT_NEW_TRADES}")
    print()


def run_cycle(
    alpaca: Alpaca,
    execute: bool,
) -> int:
    clock = alpaca.clock()
    account = alpaca.account()

    show_config(account, clock)

    now = datetime.fromisoformat(
        clock["timestamp"].replace("Z", "+00:00")
    ).astimezone(NY)

    trading_day = now.date()

    state = load_state()
    sday = day_state(state, trading_day)

    # Exit management first.
    if now.time() >= EXIT_TIME:
        print("EXIT WINDOW")
        results = close_event_positions(
            alpaca,
            sday,
            execute,
        )
        for item in results:
            print(json.dumps(item, indent=2))
        save_state(state)
        return 0

    if not clock.get("is_open"):
        print("Market is closed. No entry action.")
        return 0

    if now.time() < ENTRY_DECISION_TIME:
        print(
            f"Observation window. Waiting until "
            f"{ENTRY_DECISION_TIME.strftime('%H:%M')} ET."
        )
        return 0

    if now.time() >= ENTRY_CUTOFF_TIME:
        print(
            f"Entry cutoff passed ({ENTRY_CUTOFF_TIME.strftime('%H:%M')} ET). "
            f"No new Iran-event trades."
        )
        return 0

    already = len(sday.get("orders", {}))
    remaining_slots = max(
        0,
        MAX_CONCURRENT_NEW_TRADES - already,
    )

    if remaining_slots <= 0:
        print("Daily new-trade cap already reached.")
        return 0

    print("Evaluating Iran playbook signals...")
    print()

    signals: list[Signal] = []

    for symbol in WATCHLIST:
        try:
            sig = build_signal(
                alpaca,
                symbol,
                trading_day,
            )
        except Exception as exc:
            print(f"{symbol}: DATA ERROR: {exc}")
            continue

        if sig is None:
            print(f"{symbol}: NO TRADE")
            continue

        signals.append(sig)

        print(
            f"{symbol}: SIGNAL {sig.direction} | "
            f"gap={sig.event_gap * 100:+.2f}% | "
            f"10m={sig.reversal_10m * 100:+.2f}%"
        )

    # Rank stronger event gaps first.
    signals.sort(
        key=lambda s: abs(s.event_gap),
        reverse=True,
    )

    selected = signals[:remaining_slots]

    if not selected:
        print()
        print("No qualifying signals.")
        return 0

    print()
    print(f"Selected {len(selected)} signal(s).")
    print()

    for sig in selected:
        if sig.symbol in sday["orders"]:
            print(f"{sig.symbol}: already processed today")
            continue

        try:
            result = execute_signal(
                alpaca,
                sig,
                sday,
                execute,
            )
        except Exception as exc:
            result = {
                "symbol": sig.symbol,
                "status": "EXECUTION_ERROR",
                "error": str(exc),
            }

        print(json.dumps(result, indent=2))

        sday["signals"][sig.symbol] = {
            "direction": sig.direction,
            "previous_close": sig.previous_close,
            "today_open": sig.today_open,
            "price_0940": sig.price_0940,
            "event_gap": sig.event_gap,
            "reversal_10m": sig.reversal_10m,
        }

        # Record only if actionable instrument was selected.
        if result.get("instrument") in {"OPTION", "STOCK"}:
            sday["orders"][sig.symbol] = result

        save_state(state)

    return 0


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="DeltaX Iran event paper-trading runner v2"
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate EVENT credentials and show strategy config.",
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit paper orders. Without this flag the runner is dry-run.",
    )

    parser.add_argument(
        "--manage-exits",
        action="store_true",
        help="Run exit management immediately if exit window is reached.",
    )

    args = parser.parse_args()

    cfg = load_environment()
    alpaca = Alpaca(cfg)

    if args.check:
        show_config(
            alpaca.account(),
            alpaca.clock(),
        )
        return 0

    # Normal scheduler call:
    # - before 09:40: observe only
    # - 09:40-09:44: evaluate/enter
    # - >=15:50: close tracked positions
    return run_cycle(
        alpaca=alpaca,
        execute=args.execute,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1)
