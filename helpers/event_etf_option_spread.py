from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


# ============================================================
# DELTAX EVENT ETF OPTION SPREAD TRADER
# ============================================================
# LONG  -> Bull Put Credit Spread
# SHORT -> Bear Call Credit Spread
#
# Selection rules:
# - 7-21 DTE
# - short leg abs(delta) 0.20-0.30, target ~0.25
# - same expiry
# - defined-risk 1:1 vertical spread
# - conservative net credit = short bid - long ask
# - minimum credit >= 30% of spread width
# - max loss constrained by user-entered USD risk
# - MLeg LIMIT order only
# - EVENT PAPER account only
#
# Without final confirmation, no order is submitted.

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets"

REQUEST_TIMEOUT = 20

MIN_DTE = 7
MAX_DTE = 21
TARGET_DTE = 14

MIN_ABS_DELTA = 0.20
MAX_ABS_DELTA = 0.30
TARGET_ABS_DELTA = 0.25

MIN_CREDIT_TO_WIDTH = 0.30
MAX_LEG_SPREAD_PCT = 0.35
MAX_CONTRACTS = 20


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
                f"GET {url} failed: {r.status_code} {r.text[:800]}"
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
                f"POST {url} failed: {r.status_code} {r.text[:1000]}"
            )
        return r.json()

    def account(self) -> dict:
        return self._get(f"{self.cfg['trading_url']}/account")

    def clock(self) -> dict:
        return self._get(f"{self.cfg['trading_url']}/clock")

    def asset(self, symbol: str) -> dict:
        return self._get(f"{self.cfg['trading_url']}/assets/{symbol}")

    def stock_snapshot(self, symbol: str) -> dict:
        return self._get(
            f"{DATA_BASE}/v2/stocks/{symbol}/snapshot",
        )

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
        result: dict[str, Any] = {}

        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = self._get(
                f"{DATA_BASE}/v1beta1/options/snapshots",
                params={
                    "symbols": ",".join(chunk),
                    "limit": 100,
                },
            )
            result.update(data.get("snapshots", {}))

        return result

    def submit_mleg(self, payload: dict) -> dict:
        return self._post(
            f"{self.cfg['trading_url']}/orders",
            payload,
        )


def ask_ticker() -> str:
    while True:
        value = input("Ticker: ").strip().upper()
        if value and value.replace(".", "").replace("-", "").isalnum():
            return value
        print("Invalid ticker.")


def ask_direction() -> str:
    while True:
        value = input("Direction [long/short]: ").strip().lower()
        if value in {"long", "l"}:
            return "long"
        if value in {"short", "s"}:
            return "short"
        print("Enter long or short.")


def ask_max_risk() -> float:
    while True:
        raw = input("Max risk in USD [1000]: $").strip().replace(",", "")
        if not raw:
            return 1000.0
        try:
            value = float(raw)
        except ValueError:
            print("Enter a number.")
            continue
        if value > 0:
            return value
        print("Risk must be greater than 0.")


def latest_stock_price(snapshot: dict) -> float:
    latest_trade = snapshot.get("latestTrade") or snapshot.get("latest_trade") or {}
    daily_bar = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}

    for value in (
        latest_trade.get("p"),
        latest_trade.get("price"),
        daily_bar.get("c"),
        daily_bar.get("close"),
    ):
        if value is not None:
            return float(value)

    raise RuntimeError("Could not determine latest underlying price.")


def quote_values(snapshot: dict) -> tuple[float, float, float] | None:
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


def option_delta(snapshot: dict) -> float | None:
    g = snapshot.get("greeks") or {}
    value = g.get("delta")

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def leg_quote_ok(bid: float, ask: float, mid: float) -> bool:
    if mid <= 0:
        return False
    return ((ask - bid) / mid) <= MAX_LEG_SPREAD_PCT


def candidate_score(item: dict) -> tuple:
    return (
        abs(item["short_abs_delta"] - TARGET_ABS_DELTA),
        abs(item["dte"] - TARGET_DTE),
        -item["credit_to_width"],
        item["max_loss_per_spread"],
    )


def select_spread(
    client: AlpacaEventClient,
    ticker: str,
    direction: str,
    spot: float,
    max_risk: float,
) -> dict:
    today = datetime.now(timezone.utc).date()
    min_exp = today + timedelta(days=MIN_DTE)
    max_exp = today + timedelta(days=MAX_DTE)

    option_type = "put" if direction == "long" else "call"

    # Wide enough to include likely 20-30 delta contracts plus protection.
    strike_low = max(0.01, spot * 0.70)
    strike_high = spot * 1.30

    contracts = client.option_contracts(
        underlying=ticker,
        option_type=option_type,
        expiration_gte=min_exp,
        expiration_lte=max_exp,
        strike_low=strike_low,
        strike_high=strike_high,
    )

    if not contracts:
        raise RuntimeError("No active option contracts found in 7-21 DTE window.")

    contract_by_symbol = {
        c["symbol"]: c
        for c in contracts
        if c.get("symbol")
    }

    snapshots = client.option_snapshots(list(contract_by_symbol))

    enriched = []

    for symbol, contract in contract_by_symbol.items():
        snap = snapshots.get(symbol)
        if not snap:
            continue

        delta = option_delta(snap)
        q = quote_values(snap)

        if delta is None or q is None:
            continue

        bid, ask, mid = q

        if not leg_quote_ok(bid, ask, mid):
            continue

        strike = float(contract["strike_price"])
        expiration = date.fromisoformat(contract["expiration_date"])
        dte = (expiration - today).days

        enriched.append(
            {
                "symbol": symbol,
                "strike": strike,
                "expiration": expiration,
                "dte": dte,
                "delta": delta,
                "abs_delta": abs(delta),
                "bid": bid,
                "ask": ask,
                "mid": mid,
            }
        )

    short_candidates = [
        c for c in enriched
        if MIN_ABS_DELTA <= c["abs_delta"] <= MAX_ABS_DELTA
        and (
            (direction == "long" and c["strike"] < spot)
            or (direction == "short" and c["strike"] > spot)
        )
    ]

    if not short_candidates:
        raise RuntimeError(
            "No liquid short-leg contracts found with abs(delta) 0.20-0.30."
        )

    spreads = []

    for short_leg in short_candidates:
        protections = [
            c for c in enriched
            if c["expiration"] == short_leg["expiration"]
            and (
                (
                    direction == "long"
                    and c["strike"] < short_leg["strike"]
                )
                or (
                    direction == "short"
                    and c["strike"] > short_leg["strike"]
                )
            )
        ]

        for long_leg in protections:
            width = abs(short_leg["strike"] - long_leg["strike"])

            if width <= 0:
                continue

            # Conservative executable credit:
            # sell short leg at bid, buy protection at ask.
            credit = short_leg["bid"] - long_leg["ask"]

            if credit <= 0:
                continue

            credit_to_width = credit / width

            if credit_to_width < MIN_CREDIT_TO_WIDTH:
                continue

            max_profit_per_spread = credit * 100.0
            max_loss_per_spread = (width - credit) * 100.0

            if max_loss_per_spread <= 0:
                continue

            qty = min(
                MAX_CONTRACTS,
                math.floor(max_risk / max_loss_per_spread),
            )

            if qty < 1:
                continue

            spreads.append(
                {
                    "ticker": ticker,
                    "direction": direction,
                    "strategy": (
                        "BULL_PUT_CREDIT_SPREAD"
                        if direction == "long"
                        else "BEAR_CALL_CREDIT_SPREAD"
                    ),
                    "spot": spot,
                    "expiration": short_leg["expiration"].isoformat(),
                    "dte": short_leg["dte"],
                    "short_leg": short_leg,
                    "long_leg": long_leg,
                    "width": width,
                    "credit": credit,
                    "credit_to_width": credit_to_width,
                    "max_profit_per_spread": max_profit_per_spread,
                    "max_loss_per_spread": max_loss_per_spread,
                    "qty": qty,
                    "total_max_profit": max_profit_per_spread * qty,
                    "total_max_loss": max_loss_per_spread * qty,
                    "short_abs_delta": short_leg["abs_delta"],
                }
            )

    if not spreads:
        raise RuntimeError(
            "No spread passed all rules: delta, liquidity, "
            "credit >= 30% of width, and max-risk sizing."
        )

    spreads.sort(key=candidate_score)
    return spreads[0]


def mleg_payload(spread: dict) -> dict:
    short_leg = spread["short_leg"]
    long_leg = spread["long_leg"]

    # Alpaca MLeg convention:
    # negative limit_price = net CREDIT received.
    limit_price = -round(spread["credit"], 2)

    return {
        "order_class": "mleg",
        "qty": str(spread["qty"]),
        "type": "limit",
        "limit_price": f"{limit_price:.2f}",
        "time_in_force": "day",
        "legs": [
            {
                "symbol": short_leg["symbol"],
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": long_leg["symbol"],
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
        ],
    }


def print_spread(spread: dict) -> None:
    s = spread["short_leg"]
    l = spread["long_leg"]

    print()
    print("=" * 78)
    print("SELECTED DEFINED-RISK CREDIT SPREAD")
    print("=" * 78)
    print(f"Ticker:              {spread['ticker']}")
    print(f"Direction:           {spread['direction'].upper()}")
    print(f"Strategy:            {spread['strategy']}")
    print(f"Spot:                ${spread['spot']:.2f}")
    print(f"Expiry:              {spread['expiration']} ({spread['dte']} DTE)")
    print()
    print(
        f"SHORT leg:           {s['symbol']} | strike {s['strike']:.2f} | "
        f"delta {s['delta']:+.3f} | bid/ask {s['bid']:.2f}/{s['ask']:.2f}"
    )
    print(
        f"LONG protection:     {l['symbol']} | strike {l['strike']:.2f} | "
        f"delta {l['delta']:+.3f} | bid/ask {l['bid']:.2f}/{l['ask']:.2f}"
    )
    print()
    print(f"Spread width:        ${spread['width']:.2f}")
    print(f"Conservative credit: ${spread['credit']:.2f}")
    print(f"Credit / width:      {spread['credit_to_width'] * 100:.1f}%")
    print(f"Contracts:           {spread['qty']}")
    print(
        f"Max profit/spread:   ${spread['max_profit_per_spread']:.2f}"
    )
    print(
        f"Max loss/spread:     ${spread['max_loss_per_spread']:.2f}"
    )
    print(f"TOTAL max profit:    ${spread['total_max_profit']:.2f}")
    print(f"TOTAL max loss:      ${spread['total_max_loss']:.2f}")
    print("=" * 78)


def main() -> int:
    cfg = load_environment()
    client = AlpacaEventClient(cfg)

    print("=" * 78)
    print("DELTAX EVENT ETF OPTION SPREAD TRADER")
    print("=" * 78)
    print("LONG  = bull put credit spread")
    print("SHORT = bear call credit spread")
    print()

    ticker = ask_ticker()
    direction = ask_direction()
    max_risk = ask_max_risk()

    asset = client.asset(ticker)
    if not asset.get("tradable", False):
        raise RuntimeError(f"{ticker} is not tradable on Alpaca.")

    clock = client.clock()
    if not clock.get("is_open"):
        print("WARNING: Regular market is currently closed.")

    account = client.account()
    stock_snapshot = client.stock_snapshot(ticker)
    spot = latest_stock_price(stock_snapshot)

    print()
    print(f"Account:        {account.get('account_number', 'n/a')}")
    print(f"Equity:         ${float(account.get('equity') or 0):,.2f}")
    print(f"Buying power:   ${float(account.get('buying_power') or 0):,.2f}")
    print(f"Underlying:     {ticker} @ ${spot:.2f}")
    print(f"Max risk input: ${max_risk:,.2f}")
    print()
    print("Searching option chain...")

    spread = select_spread(
        client=client,
        ticker=ticker,
        direction=direction,
        spot=spot,
        max_risk=max_risk,
    )

    print_spread(spread)

    payload = mleg_payload(spread)

    print()
    print("Alpaca MLeg limit payload:")
    print(json.dumps(payload, indent=2))
    print()

    answer = input("Submit this PAPER MLeg order? [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Cancelled. No order submitted.")
        return 0

    order = client.submit_mleg(payload)

    print()
    print("ORDER SUBMITTED")
    print(json.dumps(
        {
            "id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
            "order_class": order.get("order_class"),
            "qty": order.get("qty"),
            "type": order.get("type"),
            "limit_price": order.get("limit_price"),
            "status": order.get("status"),
            "legs": order.get("legs"),
        },
        indent=2,
        default=str,
    ))

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
