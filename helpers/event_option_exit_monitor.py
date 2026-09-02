from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


# ============================================================
# DELTAX EVENT OPTION EXIT MONITOR
# ============================================================
# Automatically manages 2-leg ETF credit spreads in the EVENT PAPER account.
#
# DELTAX exit rules:
#   TAKE PROFIT -> conservative close debit <= 50% of initial credit
#   STOP LOSS   -> conservative close debit >= 2.0 x initial credit
#   TIME EXIT   -> <= 3 DTE
#   KILL SWITCH -> account daily P/L <= -5%
#
# Conservative close debit:
#   price to BUY BACK short leg at ASK
#   minus proceeds from SELLING long protection at BID
#
# Safety:
# - EVENT PAPER credentials only.
# - Only manages two-leg, 1:1 ETF credit spreads found from Alpaca MLeg history.
# - Only closes contracts that are still open in Alpaca positions.
# - Skips a spread if an open close-order already exists for either leg.
# - Uses one MLeg LIMIT order to close both legs together.
# - No manual confirmation.
#
# Usage:
#   python helpers/event_option_exit_monitor.py
#       -> checks and EXECUTES exits when a rule is hit
#
#   python helpers/event_option_exit_monitor.py --check
#       -> read-only check; no orders submitted


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets"
REQUEST_TIMEOUT = 20

TAKE_PROFIT_FRACTION = 0.50
STOP_LOSS_MULTIPLE = 2.00
TIME_EXIT_DTE = 3
KILL_SWITCH_PCT = -0.05

ETF_UNIVERSE = {
    "SPY", "QQQ", "IWM", "DIA",
    "XLE", "XLK", "XLF", "XLI", "XLV", "XLP", "XLY", "XLU", "XLB", "XLC",
    "XBI", "SMH", "SOXX", "TLT", "GLD", "SLV", "USO",
}

OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def load_environment() -> dict[str, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    key = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
    trading_url = (
        os.getenv("ALPACA_TRADING_URL_EVENT")
        or TRADING_BASE_DEFAULT
    ).strip().rstrip("/")

    if not key:
        raise RuntimeError("Missing ALPACA_API_KEY_EVENT in .env")
    if not secret:
        raise RuntimeError("Missing ALPACA_API_SECRET_EVENT in .env")

    if not trading_url.endswith("/v2"):
        trading_url += "/v2"

    return {
        "key": key,
        "secret": secret,
        "trading_url": trading_url,
    }


class AlpacaEventClient:
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
                f"GET {url} failed: {r.status_code} {r.text[:1000]}"
            )
        return r.json()

    def _post(self, url: str, payload: dict) -> Any:
        r = requests.post(
            url,
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(
                f"POST {url} failed: {r.status_code} {r.text[:1200]}"
            )
        return r.json()

    def account(self) -> dict:
        return self._get(f"{self.cfg['trading_url']}/account")

    def clock(self) -> dict:
        return self._get(f"{self.cfg['trading_url']}/clock")

    def positions(self) -> list[dict]:
        return self._get(f"{self.cfg['trading_url']}/positions")

    def orders(self, status: str, limit: int = 500) -> list[dict]:
        return self._get(
            f"{self.cfg['trading_url']}/orders",
            params={
                "status": status,
                "limit": limit,
                "direction": "desc",
                "nested": "true",
            },
        )

    def option_latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}

        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = self._get(
                f"{DATA_BASE}/v1beta1/options/quotes/latest",
                params={"symbols": ",".join(chunk)},
            )
            result.update(data.get("quotes", {}))

        return result

    def submit_mleg(self, payload: dict) -> dict:
        return self._post(
            f"{self.cfg['trading_url']}/orders",
            payload,
        )


def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def occ_info(symbol: str) -> dict | None:
    match = OCC_RE.match(symbol or "")
    if not match:
        return None

    underlying, yymmdd, cp, strike_raw = match.groups()

    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None

    return {
        "underlying": underlying,
        "expiry": expiry,
        "type": "call" if cp == "C" else "put",
        "strike": int(strike_raw) / 1000.0,
    }


def order_legs(order: dict) -> list[dict]:
    legs = order.get("legs") or []
    return [leg for leg in legs if isinstance(leg, dict)]


def leg_intent(leg: dict) -> str:
    return str(leg.get("position_intent") or "").lower()


def leg_side(leg: dict) -> str:
    return str(leg.get("side") or "").lower()


def filled_leg_price(leg: dict) -> float | None:
    value = leg.get("filled_avg_price")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_filled(order: dict) -> bool:
    return str(order.get("status") or "").lower() == "filled"


def derive_open_credit_spread(order: dict) -> dict | None:
    """
    Accept only a clean DELTAX-style 2-leg vertical credit spread:
      one sell_to_open + one buy_to_open,
      same underlying / expiry / option type,
      ratio 1:1,
      and ETF underlying.
    """
    if str(order.get("order_class") or "").lower() != "mleg":
        return None

    if not is_filled(order):
        return None

    legs = order_legs(order)
    if len(legs) != 2:
        return None

    short_legs = [
        x for x in legs
        if leg_side(x) == "sell" and leg_intent(x) == "sell_to_open"
    ]
    long_legs = [
        x for x in legs
        if leg_side(x) == "buy" and leg_intent(x) == "buy_to_open"
    ]

    if len(short_legs) != 1 or len(long_legs) != 1:
        return None

    short_leg = short_legs[0]
    long_leg = long_legs[0]

    short_symbol = short_leg.get("symbol")
    long_symbol = long_leg.get("symbol")

    si = occ_info(short_symbol)
    li = occ_info(long_symbol)

    if not si or not li:
        return None

    if si["underlying"] != li["underlying"]:
        return None
    if si["expiry"] != li["expiry"]:
        return None
    if si["type"] != li["type"]:
        return None
    if si["underlying"] not in ETF_UNIVERSE:
        return None

    short_ratio = f(short_leg.get("ratio_qty"), 1.0)
    long_ratio = f(long_leg.get("ratio_qty"), 1.0)
    if abs(short_ratio - 1.0) > 1e-9 or abs(long_ratio - 1.0) > 1e-9:
        return None

    # Vertical credit spread geometry.
    if si["type"] == "put":
        # Bull put: short strike > long strike.
        if not si["strike"] > li["strike"]:
            return None
        strategy = "BULL_PUT_CREDIT_SPREAD"
    else:
        # Bear call: short strike < long strike.
        if not si["strike"] < li["strike"]:
            return None
        strategy = "BEAR_CALL_CREDIT_SPREAD"

    short_fill = filled_leg_price(short_leg)
    long_fill = filled_leg_price(long_leg)

    if short_fill is None or long_fill is None:
        return None

    initial_credit = short_fill - long_fill
    if initial_credit <= 0:
        return None

    filled_qty = f(order.get("filled_qty"), f(order.get("qty"), 0.0))
    if filled_qty <= 0:
        return None

    return {
        "order_id": order.get("id"),
        "created_at": order.get("created_at"),
        "filled_at": order.get("filled_at"),
        "underlying": si["underlying"],
        "strategy": strategy,
        "expiry": si["expiry"],
        "short_symbol": short_symbol,
        "long_symbol": long_symbol,
        "short_strike": si["strike"],
        "long_strike": li["strike"],
        "initial_credit": initial_credit,
        "opening_qty": filled_qty,
    }


def current_option_position_qty(positions: list[dict]) -> dict[str, float]:
    result: dict[str, float] = {}
    for p in positions:
        symbol = p.get("symbol")
        if not symbol or not occ_info(symbol):
            continue
        result[symbol] = abs(f(p.get("qty")))
    return result


def active_close_symbols(open_orders: list[dict]) -> set[str]:
    symbols: set[str] = set()

    for order in open_orders:
        for leg in order_legs(order):
            intent = leg_intent(leg)
            if intent in {"buy_to_close", "sell_to_close"}:
                symbol = leg.get("symbol")
                if symbol:
                    symbols.add(symbol)

    return symbols


def quote_bid_ask(quote: dict) -> tuple[float, float] | None:
    bid = quote.get("bp", quote.get("bid_price"))
    ask = quote.get("ap", quote.get("ask_price"))

    try:
        bid = float(bid)
        ask = float(ask)
    except (TypeError, ValueError):
        return None

    if bid < 0 or ask <= 0 or ask < bid:
        return None

    return bid, ask


def daily_pnl_pct(account: dict) -> float:
    equity = f(account.get("equity"))
    last_equity = f(account.get("last_equity"))

    if last_equity <= 0:
        return 0.0

    return (equity - last_equity) / last_equity


def client_order_id(spread: dict, reason: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    underlying = spread["underlying"].lower()
    reason_short = {
        "TAKE_PROFIT": "tp",
        "STOP_LOSS": "sl",
        "TIME_EXIT": "time",
        "KILL_SWITCH": "kill",
    }.get(reason, "exit")
    return f"dxe-exit-{stamp}-{underlying}-{reason_short}"[:48]


def make_close_payload(
    spread: dict,
    qty: int,
    close_debit: float,
    reason: str,
) -> dict:
    # Alpaca MLeg sign convention:
    #   positive = debit paid
    #   negative = credit received
    limit_price = round(close_debit, 2)

    # Avoid "-0.00".
    if abs(limit_price) < 0.005:
        limit_price = 0.01

    return {
        "order_class": "mleg",
        "qty": str(qty),
        "type": "limit",
        "limit_price": f"{limit_price:.2f}",
        "time_in_force": "day",
        "client_order_id": client_order_id(spread, reason),
        "legs": [
            {
                "symbol": spread["short_symbol"],
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_close",
            },
            {
                "symbol": spread["long_symbol"],
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_close",
            },
        ],
    }


def choose_exit_reason(
    *,
    close_debit: float,
    initial_credit: float,
    dte: int,
    kill_switch: bool,
) -> str | None:
    if kill_switch:
        return "KILL_SWITCH"

    if dte <= TIME_EXIT_DTE:
        return "TIME_EXIT"

    if close_debit <= initial_credit * TAKE_PROFIT_FRACTION:
        return "TAKE_PROFIT"

    if close_debit >= initial_credit * STOP_LOSS_MULTIPLE:
        return "STOP_LOSS"

    return None


def merge_same_pair(spreads: list[dict]) -> list[dict]:
    """
    If the same exact spread was opened more than once, Alpaca positions are
    aggregated by contract. Merge those opening fills and keep a weighted
    initial credit, so we don't accidentally try to close the same legs twice.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for s in spreads:
        key = (s["short_symbol"], s["long_symbol"])
        groups[key].append(s)

    merged: list[dict] = []

    for items in groups.values():
        total_qty = sum(x["opening_qty"] for x in items)
        if total_qty <= 0:
            continue

        weighted_credit = (
            sum(x["initial_credit"] * x["opening_qty"] for x in items)
            / total_qty
        )

        base = dict(items[0])
        base["initial_credit"] = weighted_credit
        base["opening_qty"] = total_qty
        base["source_order_ids"] = [x["order_id"] for x in items]
        merged.append(base)

    return merged


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automatically close DELTAX EVENT ETF option spreads."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only. Evaluate exits but submit no orders.",
    )
    args = parser.parse_args()

    cfg = load_environment()
    client = AlpacaEventClient(cfg)

    account = client.account()
    clock = client.clock()
    positions = client.positions()
    closed_orders = client.orders("closed", 500)
    open_orders = client.orders("open", 500)

    pnl_pct = daily_pnl_pct(account)
    kill_switch = pnl_pct <= KILL_SWITCH_PCT
    market_open = bool(clock.get("is_open"))

    print("=" * 92)
    print("DELTAX EVENT OPTION EXIT MONITOR")
    print("=" * 92)
    print(f"Mode:             {'CHECK ONLY' if args.check else 'AUTO EXECUTE'}")
    print(f"Market open:      {market_open}")
    print(f"Account equity:   ${f(account.get('equity')):,.2f}")
    print(f"Daily P/L:        {pnl_pct * 100:+.2f}%")
    print(
        f"Kill switch:      {'ACTIVE' if kill_switch else 'inactive'} "
        f"(threshold {KILL_SWITCH_PCT * 100:.0f}%)"
    )
    print()

    raw_spreads = []
    for order in closed_orders:
        spread = derive_open_credit_spread(order)
        if spread:
            raw_spreads.append(spread)

    spreads = merge_same_pair(raw_spreads)

    position_qty = current_option_position_qty(positions)
    closing_symbols = active_close_symbols(open_orders)

    # Keep only spreads whose two legs still exist as open Alpaca positions.
    live_spreads = []
    for s in spreads:
        short_qty = position_qty.get(s["short_symbol"], 0.0)
        long_qty = position_qty.get(s["long_symbol"], 0.0)

        live_qty = int(min(short_qty, long_qty, s["opening_qty"]))
        if live_qty <= 0:
            continue

        s = dict(s)
        s["live_qty"] = live_qty
        live_spreads.append(s)

    if not live_spreads:
        print("No live DELTAX-style ETF credit spreads found.")
        return 0

    quote_symbols = sorted({
        symbol
        for s in live_spreads
        for symbol in (s["short_symbol"], s["long_symbol"])
    })
    quotes = client.option_latest_quotes(quote_symbols)

    exit_count = 0
    hold_count = 0
    skip_count = 0

    today = datetime.now(timezone.utc).date()

    for spread in live_spreads:
        short_symbol = spread["short_symbol"]
        long_symbol = spread["long_symbol"]

        if short_symbol in closing_symbols or long_symbol in closing_symbols:
            print(
                f"{spread['underlying']} {spread['strategy']}: "
                "SKIP - close order already open."
            )
            skip_count += 1
            continue

        short_q = quote_bid_ask(quotes.get(short_symbol, {}))
        long_q = quote_bid_ask(quotes.get(long_symbol, {}))

        if short_q is None or long_q is None:
            print(
                f"{spread['underlying']} {spread['strategy']}: "
                "SKIP - valid quotes unavailable."
            )
            skip_count += 1
            continue

        short_bid, short_ask = short_q
        long_bid, long_ask = long_q

        # Conservative net debit to close:
        # buy short at ask, sell long at bid.
        close_debit = short_ask - long_bid
        initial_credit = spread["initial_credit"]
        dte = (spread["expiry"] - today).days

        tp_level = initial_credit * TAKE_PROFIT_FRACTION
        sl_level = initial_credit * STOP_LOSS_MULTIPLE

        reason = choose_exit_reason(
            close_debit=close_debit,
            initial_credit=initial_credit,
            dte=dte,
            kill_switch=kill_switch,
        )

        print("-" * 92)
        print(
            f"{spread['underlying']} | {spread['strategy']} | "
            f"expiry {spread['expiry']} | {dte} DTE | qty {spread['live_qty']}"
        )
        print(
            f"Initial credit: ${initial_credit:.2f} | "
            f"Close debit now: ${close_debit:.2f}"
        )
        print(
            f"TP trigger <= ${tp_level:.2f} | "
            f"SL trigger >= ${sl_level:.2f} | "
            f"Time exit <= {TIME_EXIT_DTE} DTE"
        )

        if reason is None:
            print("Decision: HOLD")
            hold_count += 1
            continue

        print(f"Decision: EXIT -> {reason}")

        if args.check:
            print("CHECK ONLY: no order submitted.")
            exit_count += 1
            continue

        if not market_open:
            print("NOT SUBMITTED: options market is closed.")
            skip_count += 1
            continue

        payload = make_close_payload(
            spread=spread,
            qty=spread["live_qty"],
            close_debit=close_debit,
            reason=reason,
        )

        print("Submitting MLeg close order...")
        order = client.submit_mleg(payload)

        print(
            json.dumps(
                {
                    "id": order.get("id"),
                    "client_order_id": order.get("client_order_id"),
                    "status": order.get("status"),
                    "qty": order.get("qty"),
                    "limit_price": order.get("limit_price"),
                    "reason": reason,
                    "underlying": spread["underlying"],
                },
                indent=2,
                default=str,
            )
        )
        exit_count += 1

    print()
    print("=" * 92)
    print("SUMMARY")
    print("=" * 92)
    print(f"Live spreads: {len(live_spreads)}")
    print(f"Exit signals: {exit_count}")
    print(f"Holds:        {hold_count}")
    print(f"Skipped:      {skip_count}")

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
